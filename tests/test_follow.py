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

from conftest import live_reads
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
        self.edits: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []

    def __call__(self, method: str, **params: Any) -> Any:
        if method == "getMe":
            return {"username": "SignalsAlertsBot"}
        if method == "sendMessage":
            self.sent.append(params)
            return {"message_id": len(self.sent), "chat": {"id": params["chat_id"]}}
        if method == "editMessageText":
            self.edits.append(params)
        if method == "answerCallbackQuery":
            self.answers.append(params)
        return True


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    for d in (engine._codes, engine._seen, engine._alerted, engine._whale_alerted):
        d.clear()
    monkeypatch.setattr(engine, "_whale", {"started": False, "seen": set()})
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
    assert r.status_code == 200 and r.json() == {"follows": [], "telegram": False, "alerts": True, "interests": None, "alert_for": {},
                                                "pace": {}, "daily_cap": 200, "summary_hours": 6}  # fmt: skip
    assert r.headers["cache-control"] == "private, no-store"


def test_the_feed_needs_signing_in_and_traders_need_no_account(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    for path in ("/api/live", "/api/trading-now", f"/api/bet/{SHARP}/0xm:0"):  # Recommended, Big trades, an alert's
        r = api.get(path)
        assert r.status_code == 401 and r.json()["detail"] == "Sign in to see the Feed."
        assert api.get(path, headers=auth(token(key=OTHER_KEY))).status_code == 401  # not signed by Clerk
        r = api.get(path, headers=auth(token(azp="https://evil.test")))  # another site's
        assert r.json()["detail"] == "Sign in to see the Feed on this site."
    engine._worth_data["scores"][0]["label"] = "Rising"
    r = api.get("/api/live", headers=auth())
    assert r.status_code == 200 and [t["wallet"] for t in r.json()["traders"]] == [SHARP]
    assert r.headers["cache-control"] == "private, no-store"  # never kept in front of us for the signed-out
    assert api.get("/api/worth").status_code == 200  # Traders and trader pages: no account needed
    monkeypatch.setattr(engine, "CLERK_KEY", "")
    assert api.get("/api/live").status_code == 200  # signing in isn't switched on (locally): nothing gated


def test_follow_and_unfollow_a_trader(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    r = api.post("/api/me/follows", json={"wallet": SHARP.upper().replace("0X", "0x")}, headers=auth())
    assert r.json()["follows"] == [SHARP]
    assert api.post("/api/me/follows", json={"wallet": SHARP}, headers=auth()).json()["follows"] == [SHARP]  # once
    engine._directory_writer.submit(lambda: None).result()  # the follow is kept on the directory's writer thread
    found = engine.directory().sources_of(SHARP)  # the trader directory keeps the wallet, never who follows it
    assert [(s["source"], s["times"]) for s in found] == [("follow", 1)] and "user_1" not in repr(found)
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
    assert api.get("/api/me", headers=auth()).json() == {"follows": [SHARP], "telegram": False, "alerts": True, "interests": None,
                                                          "alert_for": {SHARP: {"mode": "all", "usd": None}}, "pace": {},
                                                          "daily_cap": 200, "summary_hours": 6}  # fmt: skip


def buy(i: int, at: datetime, price: float = 0.40, market: str = "0xcid:0", size: float = 1000) -> WhaleTrade:
    return WhaleTrade("polymarket", SHARP, "Sharp", market, "yes", "buy", price, size, size * price, at, f"t{i}",
                      title="Will it <rain>?", outcome="Yes", url="https://polymarket.com/event/rain")  # fmt: skip


def test_every_new_buy_alerts_with_how_far_the_price_moved(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    api.post("/api/me/follows", json={"wallet": SHARP}, headers=auth())
    connect(api)
    now_price = {"0xcid:0": 0.41, "0xcid:1": 0.60}
    trades = [buy(1, datetime(2026, 10, 1, tzinfo=UTC))]
    detail = lambda *a, **k: TraderDetail(  # noqa: E731
        Trader(venue="polymarket", id=SHARP, name="Sharp"), "visible", {},
        [WhalePosition(m, None, "Yes", 100, 0.4, p, None, None) for m, p in now_price.items()], list(trades))  # fmt: skip
    live_reads(monkeypatch, detail)
    monkeypatch.setattr(engine, "copy_offer", lambda s: (False, "Not offered."))  # alerted all the same, neutrally
    monkeypatch.setattr(Whales, "preview_copy", lambda self, t, **kw: pytest.fail("an alert never prices a copy"))
    engine.tg.sent.clear()
    assert engine.alert_pass() == 0  # the first round only notes what they've done already
    fresh = datetime.now(UTC)
    trades[:0] = [buy(2, fresh), buy(3, fresh, 0.42), buy(4, fresh, 0.40, "0xcid:1")]  # 0xcid:1 moved to 60¢
    engine._cache.clear()
    assert engine.alert_pass() == 2  # one message for the two fills on 0xcid:0, and one for the moved market
    text, moved = engine.tg.sent[-2]["text"], engine.tg.sent[-1]["text"]
    assert "bought <b>Yes</b> at 40¢ ($400)" in moved and "Now 60¢, price already moved." in moved and "get in" not in moved
    assert "Buy on Polymarket International" in moved and moved.endswith(engine.FOLLOW_DELAY)
    # Copying isn't offered for them: following is research too, so the alert gives the same facts, neutrally.
    assert text.splitlines()[:3] == ["<b>Sharp &lt;Sam&gt;</b> bought <b>Yes</b> at 41¢ ($820)", "Will it &lt;rain&gt;?", "Now 41¢."]
    assert "before this alert." in text and "you can still get in" not in text and engine.COPY_LINE not in text
    assert "Buy on Polymarket International" in text and SHARP in text  # the link only opens the market
    assert text.endswith(engine.FOLLOW_DELAY)
    keys = engine.tg.sent[-1]["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in keys[0]] == ["Useful", "Not useful"] and keys[1][0]["callback_data"] == f"unf:{SHARP}"
    trades.insert(0, buy(5, fresh))
    engine._cache.clear()
    assert engine.alert_pass() == 0  # the same market again within ALERT_AGAIN_S: no second message

    # "Stop alerts from …" under the message unfollows them for that chat
    engine._on_update({"callback_query": {"id": "q1", "data": f"unf:{SHARP}", "message": {"chat": {"id": 555}}}})
    assert api.get("/api/me", headers=auth()).json()["follows"] == []


def test_an_alert_gives_the_same_facts_for_every_trader_and_a_copy_line_only_where_copying_is_offered(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Whales, "preview_copy", lambda self, t, **kw: pytest.fail("an alert never prices a copy"))
    at = datetime(2026, 10, 13, 14, 2, tzinfo=UTC).timestamp()
    links = (f'<a href="{engine.SITE}/#/check/{SHARP}/0xcid:0">Check current price</a> · '
             f'<a href="{engine.SITE}/#/worth/{SHARP}">Their record</a> · <a href="https://polymarket.com/event/rain">Buy on Polymarket International</a>')  # fmt: skip
    facts = ["<b>Sharp &lt;Sam&gt;</b> bought <b>Yes</b> at 40¢ ($40)", "Will it &lt;rain&gt;?", "Now 41¢.",
             "Traded 14:02 UTC, 6 min before this alert.", engine.TRADED_INTL]  # fmt: skip
    monkeypatch.setattr(engine, "copy_offer", lambda s: (True, None))
    text = engine._alert_text(SHARP, buy(1, datetime.now(UTC)), 0.40, 0.41, 40, at, at + 6 * 60 + 10)
    assert text.splitlines() == [*facts, engine.COPY_LINE, links, engine.FOLLOW_DELAY]
    assert "not a promise" in engine.COPY_LINE and "$" not in engine.COPY_LINE  # secondary, no cost: that's the preview's
    monkeypatch.setattr(engine, "copy_offer", lambda s: (False, "No."))
    neutral = engine._alert_text(SHARP, buy(1, datetime.now(UTC)), 0.40, 0.41, 40, at, at + 6 * 60 + 10)
    assert neutral.splitlines() == [*facts, links, engine.FOLLOW_DELAY]


def test_each_alert_says_how_late_it_can_be_for_where_its_trade_came_from(api: TestClient) -> None:
    # a trader you follow is read on their own every round: no "3–5 minutes", and never "instant"
    assert "3–5" not in engine.FOLLOW_DELAY and str(engine.LIVE_POLL_S) in engine.FOLLOW_DELAY
    # big trades come from Polymarket's feed of every trade, which does run 3–5 minutes behind
    assert "typically runs 3–5 minutes behind" in engine.BIG_DELAY and "typically" in engine.FOLLOW_DELAY
    for line in (engine.FOLLOW_DELAY, engine.BIG_DELAY):
        assert not any(w in line.lower() for w in ("instant", "real time", "real-time", "immediately"))
    page = api.get("/").text
    assert "Polymarket's public data runs 3–5 minutes behind" not in page  # no blanket figure on the page either


def test_clerk_frontend_api_comes_from_the_publishable_key() -> None:
    enc = base64.b64encode(b"clerk.usesignals.app$").decode().rstrip("=")
    assert engine.clerk_frontend_api(f"pk_live_{enc}") == "clerk.usesignals.app"
    assert engine.clerk_frontend_api("sk_live_abc") is None and engine.clerk_frontend_api("pk_live_!!") is None
