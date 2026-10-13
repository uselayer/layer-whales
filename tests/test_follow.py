"""Following traders: Clerk sign-in, follows, connecting Telegram and alerts (no network: Clerk's keys and
Telegram's API are stand-ins)."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from uselayer import Trader, TraderDetail, Whales, WhalePosition, WhaleTrade

from signals import app as engine

FAPI = "clerk.signals.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SHARP = "0x" + "ab" * 20


def token(sub: str = "user_1", *, key: Any = KEY, ttl: int = 60, **claims: Any) -> str:
    now = int(time.time())
    body = {"sub": sub, "iss": f"https://{FAPI}", "iat": now, "nbf": now, "exp": now + ttl, "azp": engine.SITE} | claims
    return jwt.encode(body, key, algorithm="RS256")


def auth(t: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {t or token()}"}


class Telegram:
    """Telegram's Bot API, as far as the engine uses it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def __call__(self, method: str, **params: Any) -> Any:
        if method == "getMe":
            return {"username": "SignalsAlertsBot"}
        if method == "sendMessage":
            self.sent.append(params)
        return True


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    for d in (engine._codes, engine._seen, engine._alerted):
        d.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "CLERK_KEY", "pk_test_" + base64.b64encode(f"{FAPI}$".encode()).decode().rstrip("="))
    monkeypatch.setattr(engine, "_signing_key", lambda t: KEY.public_key())
    monkeypatch.setattr(engine, "TG_TOKEN", "123:test")
    monkeypatch.setattr(engine, "_tg", {"bot": None, "offset": 0, "error": None})
    monkeypatch.setattr(engine, "tg", Telegram())
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": [
        {"wallet": SHARP, "name": "Sharp <Sam>", "segment": "rising", "copyable": "yes", "copy": None}]})  # fmt: skip
    return TestClient(engine.app)


def test_the_page_learns_whether_signing_in_and_alerts_are_on(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c = api.get("/api/config").json()
    assert c["clerk_key"] == engine.CLERK_KEY and c["alerts"] is True  # the publishable key is public by design
    monkeypatch.setattr(engine, "CLERK_KEY", "")
    assert api.get("/api/config").json()["clerk_key"] is None
    assert api.get("/api/me", headers=auth()).status_code == 404  # signing in is off: nothing to sign in to


def test_following_needs_a_valid_clerk_session(api: TestClient) -> None:
    assert api.get("/api/me").status_code == 401
    assert api.get("/api/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert api.get("/api/me", headers=auth(token(key=OTHER_KEY))).status_code == 401  # not signed by Clerk
    assert api.get("/api/me", headers=auth(token(ttl=-60))).status_code == 401  # expired
    assert api.get("/api/me", headers=auth(token(iss="https://evil.test"))).status_code == 401
    assert api.get("/api/me", headers=auth(token(azp="https://evil.test"))).status_code == 401  # another site's
    r = api.get("/api/me", headers=auth())
    assert r.status_code == 200 and r.json() == {"follows": [], "telegram": False, "alerts": True}
    assert r.headers["cache-control"] == "private, no-store"


def test_follow_and_unfollow_a_trader(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    r = api.post("/api/me/follows", json={"wallet": SHARP.upper().replace("0X", "0x")}, headers=auth())
    assert r.json()["follows"] == [SHARP]
    assert api.post("/api/me/follows", json={"wallet": SHARP}, headers=auth()).json()["follows"] == [SHARP]  # once
    assert api.get("/api/me", headers=auth(token("user_2"))).json()["follows"] == []  # someone else's
    assert api.post("/api/me/follows", json={"wallet": "0x12"}, headers=auth()).status_code == 400
    monkeypatch.setattr(engine, "MAX_FOLLOWS", 1)
    r = api.post("/api/me/follows", json={"wallet": "0x" + "cd" * 20}, headers=auth())
    assert r.status_code == 400 and "up to 1" in r.json()["detail"]
    assert api.delete(f"/api/me/follows/{SHARP}", headers=auth()).json()["follows"] == []


def connect(api: TestClient, chat: int = 555, sub: str = "user_1") -> None:
    link = api.post("/api/me/telegram", headers=auth(token(sub))).json()["link"]
    assert link.startswith("https://t.me/SignalsAlertsBot?start=")
    engine._on_update({"message": {"chat": {"id": chat}, "text": "/start " + link.rsplit("=", 1)[1]}})


def test_connecting_telegram_takes_a_one_time_link(api: TestClient) -> None:
    api.post("/api/me/follows", json={"wallet": SHARP}, headers=auth())
    link = api.post("/api/me/telegram", headers=auth()).json()["link"]
    code = link.rsplit("=", 1)[1]
    engine._on_update({"message": {"chat": {"id": 555}, "text": f"/start {code}"}})
    assert api.get("/api/me", headers=auth()).json()["telegram"] is True
    said = engine.tg.sent[-1]
    assert said["chat_id"] == "555" and "Connected" in said["text"] and "Sharp &lt;Sam&gt;" in said["text"]
    engine._on_update({"message": {"chat": {"id": 777}, "text": f"/start {code}"}})  # used already
    assert "expired" in engine.tg.sent[-1]["text"]
    engine._on_update({"message": {"chat": {"id": 555}, "text": "/stop"}})
    assert api.get("/api/me", headers=auth()).json() == {"follows": [SHARP], "telegram": False, "alerts": True}


def buy(i: int, at: datetime, price: float = 0.40, market: str = "0xcid:0") -> WhaleTrade:
    return WhaleTrade("polymarket", SHARP, "Sharp", market, "yes", "buy", price, 100, 100 * price, at, f"t{i}",
                      title="Will it <rain>?", outcome="Yes", url="https://polymarket.com/event/rain")  # fmt: skip


def test_alerts_go_out_for_new_buys_you_can_still_get_into(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    api.post("/api/me/follows", json={"wallet": SHARP}, headers=auth())
    connect(api)
    now_price = {"0xcid:0": 0.41, "0xcid:1": 0.60}
    trades = [buy(1, datetime(2026, 10, 1, tzinfo=UTC))]
    detail = lambda *a, **k: TraderDetail(  # noqa: E731
        Trader(venue="polymarket", id=SHARP, name="Sharp"), "visible", {},
        [WhalePosition(m, None, "Yes", 100, 0.4, p, None, None) for m, p in now_price.items()], list(trades))  # fmt: skip
    monkeypatch.setattr(engine.reads.whales, "trader", detail)
    monkeypatch.setattr(engine, "copy_offer", lambda s: (False, "Not offered."))  # alerted, but no $10 line
    monkeypatch.setattr(Whales, "preview_copy", lambda self, t, **kw: pytest.fail("priced a trader copying isn't offered for"))
    engine.tg.sent.clear()
    assert engine.alert_pass() == 0  # the first round only notes what they've done already
    fresh = datetime.now(UTC)
    trades[:0] = [buy(2, fresh), buy(3, fresh, 0.42), buy(4, fresh, 0.40, "0xcid:1")]  # 0xcid:1 moved to 60¢
    engine._cache.clear()
    assert engine.alert_pass() == 1  # one message for the two fills on 0xcid:0; none for the moved market
    text = engine.tg.sent[-1]["text"]
    # Copying isn't offered for them: following is keeping an eye on them, so the alert reads neutrally.
    assert "<b>Sharp &lt;Sam&gt;</b> bought <b>Yes</b> at 41¢ ($82) on Will it &lt;rain&gt;? · now 41¢" in text
    assert "you can still get in" not in text and "$10 gets you" not in text
    assert "Buy on Polymarket" in text and SHARP in text  # the link only opens the market
    assert text.endswith("Polymarket's public data runs 3–5 minutes behind.")
    assert engine.tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"unf:{SHARP}"
    trades.insert(0, buy(5, fresh))
    engine._cache.clear()
    assert engine.alert_pass() == 0  # the same market again within ALERT_AGAIN_S: no second message

    # "Stop alerts from …" under the message unfollows them for that chat
    engine._on_update({"callback_query": {"id": "q1", "data": f"unf:{SHARP}", "message": {"chat": {"id": 555}}}})
    assert api.get("/api/me", headers=auth()).json()["follows"] == []


def test_an_alert_prices_ten_dollars_for_a_trader_copying_is_offered_for(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "copy_offer", lambda s: (True, None))

    class Preview:
        ok, price, contracts, fee = True, 0.41, 24.0, 0.15

    monkeypatch.setattr(Whales, "preview_copy", lambda self, t, **kw: Preview())
    line = engine._alert_cost(SHARP, buy(1, datetime.now(UTC)), 0.40, 100, 40)
    assert line == "$10 gets you 24 contracts at 41¢, plus a $0.15 fee."
    monkeypatch.setattr(engine, "copy_offer", lambda s: (False, "No."))
    assert engine._alert_cost(SHARP, buy(1, datetime.now(UTC)), 0.40, 100, 40) is None


def test_an_alert_for_a_trader_copying_is_offered_for_says_you_can_still_get_in(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "copy_offer", lambda s: (True, None))

    class Preview:
        ok, price, contracts, fee = True, 0.41, 24.0, 0.15

    monkeypatch.setattr(Whales, "preview_copy", lambda self, t, **kw: Preview())
    text = engine._alert_text(SHARP, buy(1, datetime.now(UTC)), 0.40, 0.41, 100, 40)
    assert text.splitlines() == [
        "<b>Sharp &lt;Sam&gt;</b> bought <b>Yes</b> at 40¢ ($40)",
        "Will it &lt;rain&gt;?",
        "Now 41¢: you can still get in.",
        "$10 gets you 24 contracts at 41¢, plus a $0.15 fee.",
        f'<a href="{engine.SITE}/#/worth/{SHARP}">Their record</a> · <a href="https://polymarket.com/event/rain">Buy on Polymarket</a>',
        "Polymarket's public data runs 3–5 minutes behind.",
    ]
    monkeypatch.setattr(engine, "copy_offer", lambda s: (False, "No."))
    neutral = engine._alert_text(SHARP, buy(1, datetime.now(UTC)), 0.40, 0.41, 100, 40)
    assert neutral.splitlines() == [
        "<b>Sharp &lt;Sam&gt;</b> bought <b>Yes</b> at 40¢ ($40) on Will it &lt;rain&gt;? · now 41¢",
        f'<a href="{engine.SITE}/#/worth/{SHARP}">Their record on Signals</a> · <a href="https://polymarket.com/event/rain">Buy on Polymarket</a>',
        "Polymarket's public data runs 3–5 minutes behind.",
    ]


def test_clerk_frontend_api_comes_from_the_publishable_key() -> None:
    enc = base64.b64encode(b"clerk.usesignals.app$").decode().rstrip("=")
    assert engine.clerk_frontend_api(f"pk_live_{enc}") == "clerk.usesignals.app"
    assert engine.clerk_frontend_api("sk_live_abc") is None and engine.clerk_frontend_api("pk_live_!!") is None
