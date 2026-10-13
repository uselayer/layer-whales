"""Counting, per alert and per follow, with nothing personal beyond the signed-in account: minutes from trade to
alert, taps through /go, stops and unfollows after an alert, votes, follows per interest pick, Telegram
connected; and /api/metrics for the owner only. No network."""

from __future__ import annotations

import base64
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from uselayer import WhaleTrade

from signals import app as engine

FAPI = "clerk.signals.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SAM, ANN = "0x" + "a" * 40, "0x" + "b" * 40
LOCAL = ("127.0.0.1", 50000)


def auth(sub: str = "user_1") -> dict[str, str]:
    now = int(time.time())
    t = jwt.encode({"sub": sub, "iss": f"https://{FAPI}", "iat": now, "exp": now + 60, "azp": engine.SITE}, KEY, algorithm="RS256")
    return {"Authorization": f"Bearer {t}"}


class Telegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def __call__(self, method: str, **params: Any) -> Any:
        if method == "sendMessage":
            self.sent.append(params)
            return {"message_id": len(self.sent)}
        return True


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "CLERK_KEY", "pk_test_" + base64.b64encode(f"{FAPI}$".encode()).decode().rstrip("="))
    monkeypatch.setattr(engine, "_signing_key", lambda t: KEY.public_key())
    monkeypatch.setattr(engine, "TG_TOKEN", "123:test")
    monkeypatch.setattr(engine, "tg", Telegram())
    monkeypatch.setattr(engine, "METRICS_KEY", "")
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": [
        {"wallet": SAM, "name": "Sam", "segment": "too_fast", "copyable": "no", "copy": None}]})  # fmt: skip
    return TestClient(engine.app, client=LOCAL)


def user(uid: str = "user_1", chat: str | None = "555", follows: tuple[str, ...] = (SAM,), interests: Any = None) -> None:
    d = engine._load_follows()
    d["users"][uid] = {"follows": list(follows), "chat": chat} | ({"interests": interests} if interests is not None else {})
    engine._save_follows(d)


def buy(w: str, minutes_ago: float, market: str = "0xc:0") -> Any:
    at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    t = WhaleTrade("polymarket", w, None, market, "yes", "buy", 0.49, 1000, 490, at, f"{w}{market}", title="T", outcome="Yes",
                   url="https://polymarket.com/event/t", token=f"tok{market}")  # fmt: skip
    return engine.NewBuy(w, [t], 0.50)


def metrics(api: TestClient) -> dict[str, Any]:
    r = api.get("/api/metrics")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    return r.json()  # type: ignore[no-any-return]


def test_minutes_from_their_trade_to_the_alert(api: TestClient) -> None:
    user(follows=(SAM, ANN))
    engine._deliver("user_1", "555", buy(SAM, 6, "0xa:0"), "Politics")
    engine._deliver("user_1", "555", buy(ANN, 4, "0xb:0"), "Politics")
    m = metrics(api)["alerts"]
    assert m["sent"] == 2 and m["by_kind"] == {"buy": 2}
    lag = m["minutes_from_trade_to_alert"]
    assert lag["n"] == 2 and 3.9 <= lag["median"] <= 6.1


def test_alert_links_go_through_signals_and_count_taps(api: TestClient) -> None:
    user()
    engine._deliver("user_1", "555", buy(SAM, 5), "Politics")
    text = engine.tg.sent[-1]["text"]
    aid = engine._alert_of("555", 1)
    market = f"{engine.SITE}/go/{aid}/market?to={urllib.parse.quote('https://polymarket.com/event/t', safe='')}"
    assert f'href="{market}">Buy on Polymarket International</a>' in text and f"/go/{aid}/record?to=" in text
    r = api.get(f"/go/{aid}/market", params={"to": "https://polymarket.com/event/t"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"], r.headers["cache-control"]) == (302, "https://polymarket.com/event/t", "no-store")
    api.get(f"/go/{aid}/record", params={"to": f"{engine.SITE}/#/worth/{SAM}"}, follow_redirects=False)
    r = api.get("/go/madeup12/market", params={"to": "https://polymarket.com/event/t"}, follow_redirects=False)
    assert r.headers["location"] == "https://polymarket.com/event/t"  # redirected, but not one of our alerts: not a tap
    m = metrics(api)["taps"]
    assert m["by_target"] == {"market": 1, "record": 1} and m["alerts_tapped"] == 1 and m["share_of_alerts_tapped"] == 1.0
    with engine._db() as con:  # alert id, target and time: nothing about who tapped
        assert con.execute("SELECT uid, trader FROM events WHERE kind = 'tap'").fetchall() == [(None, None), (None, None)]


def test_go_never_sends_anyone_off_site_and_the_link_works_when_counting_fails(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = api.get("/go/abcd1234/market", params={"to": "https://evil.test/phish"}, follow_redirects=False)
    assert r.headers["location"] == engine.SITE  # not an open redirect
    r = api.get("/go/abcd1234/market", params={"to": "https://polymarket.com.evil.test/x"}, follow_redirects=False)
    assert r.headers["location"] == engine.SITE

    def broken() -> Any:
        raise engine.sqlite3.OperationalError("disk full")

    monkeypatch.setattr(engine, "_db", broken)
    r = api.get("/go/abcd1234/market", params={"to": "https://polymarket.com/event/t"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (302, "https://polymarket.com/event/t")


def test_stops_and_unfollows_within_seven_days_of_an_alert(api: TestClient) -> None:
    user(follows=(SAM, ANN))
    engine._deliver("user_1", "555", buy(SAM, 5), "Politics")
    engine._on_update({"callback_query": {"id": "q", "data": f"unf:{SAM}", "message": {"chat": {"id": 555}, "message_id": 1}}})
    aid = engine._alert_of("555", 1)
    with engine._db() as con:
        assert con.execute("SELECT kind, uid, trader, alert_id FROM events WHERE kind = 'stop'").fetchall() == [
            ("stop", "user_1", SAM, aid)]  # fmt: skip
    api.delete(f"/api/me/follows/{ANN}", headers=auth())  # no alert about Ann: an unfollow, not after an alert
    m = metrics(api)["stops"]
    assert m == {"stop_taps": 1, "stop_taps_within_7_days_of_an_alert": 1, "unfollows": 1, "unfollows_within_7_days_of_an_alert": 0}


def test_votes_follows_per_interest_pick_and_telegram(api: TestClient) -> None:
    aid = engine._send("user_1", "555", "buy", lambda a: "x")
    user("user_1", follows=())
    engine._on_update({"callback_query": {"id": "q", "data": f"v:{aid}:1", "message": {"chat": {"id": 555}}}})
    for _ in range(4):
        api.post("/api/count/picked")  # anonymous
    api.post("/api/me/follows", json={"wallet": SAM}, headers=auth("user_1"))
    api.post("/api/me/follows", json={"wallet": ANN}, headers=auth("user_1"))
    api.post("/api/me/follows", json={"wallet": SAM}, headers=auth("user_2"))
    api.put("/api/me/interests", json={"interests": ["Sports"]}, headers=auth("user_1"))
    m = metrics(api)
    assert m["votes"] == {"useful": 1, "not_useful": 0, "alerts_voted_on": 1}
    f = m["follows"]
    assert (f["interest_picks"], f["follows"], f["follows_per_interest_pick"]) == (4, 3, 0.75)
    assert (f["accounts_with_saved_interests"], f["follows_per_account_with_saved_interests"]) == (1, 2.0)
    assert m["telegram"] == {"followers": 2, "followers_with_telegram": 1, "share": 0.5, "connected_in_period": 0}
    with engine._db() as con:
        assert con.execute("SELECT uid, alert_id, target, trader FROM events WHERE kind = 'picked'").fetchall() == [(None,) * 4] * 4


def test_metrics_are_for_the_owner_only(api: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    assert metrics(api)["days"] == 30  # this machine, running locally
    away = TestClient(engine.app, client=("203.0.113.9", 4000))
    assert away.get("/api/metrics").status_code == 404  # anyone else: nothing here
    assert api.get("/api/metrics", headers={"x-forwarded-for": "203.0.113.9"}).status_code == 404  # through a proxy
    monkeypatch.setattr(engine, "HOSTED", True)
    assert api.get("/api/metrics").status_code == 404  # hosted, even from inside: the key only
    monkeypatch.setattr(engine, "METRICS_KEY", "owner-secret")
    assert away.get("/api/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404
    r = away.get("/api/metrics", headers={"Authorization": "Bearer owner-secret"})
    assert r.status_code == 200 and "owner-secret" not in r.text
    monkeypatch.setattr(engine, "METRICS_KEY", "")
    assert away.get("/api/metrics", headers={"Authorization": "Bearer "}).status_code == 404  # no key set: no way in


def test_the_page_counts_an_interest_pick_anonymously(api: TestClient) -> None:
    assert '"/api/count/picked"' in api.get("/").text


# ---- review fixes ----


def test_a_grouped_alerts_delay_counts_from_the_buy_that_sent_it(api: TestClient) -> None:
    user(follows=(SAM, ANN))
    engine._deliver("user_1", "555", buy(SAM, 60), "Politics")  # an hour ago: alone
    engine._deliver("user_1", "555", buy(ANN, 2), "Politics")  # two minutes ago: sends the group
    with engine._db() as con:
        (lag,) = con.execute("SELECT sent_at - COALESCE(trigger_at, trade_at) FROM alerts WHERE kind = 'group'").fetchone()
    assert 100 <= lag <= 140  # about 2 minutes, not the hour since the first buy
    assert metrics(api)["alerts"]["minutes_from_trade_to_alert"]["n"] == 2


def test_each_followers_links_name_their_own_alert(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from uselayer import Trader, TraderDetail, WhalePosition

    user("user_1", "555")
    user("user_2", "777")
    trades: list[WhaleTrade] = []
    monkeypatch.setattr(engine.reads.whales, "trader", lambda *a, **k: TraderDetail(
        Trader(venue="polymarket", id=SAM, name=None), "visible", {}, [WhalePosition("0xc:0", None, "Yes", 1, 0.5, 0.5, None, None)], list(trades)))  # fmt: skip
    engine.alert_pass()
    trades.append(buy(SAM, 1).t)
    engine._cache.clear()
    assert engine.alert_pass() == 2
    for m in engine.tg.sent:
        aid = engine._alert_of(m["chat_id"], m.get("message_id") or engine.tg.sent.index(m) + 1)
        assert f"/go/{aid}/market?to=" in m["text"] and "\x00" not in m["text"]


def test_go_and_metrics_never_answer_500_to_odd_input(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    r = api.get("/go/abcd1234/market", params={"to": "https://[::1"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (302, engine.SITE)
    monkeypatch.setattr(engine, "METRICS_KEY", "owner-secret")
    away = TestClient(engine.app, client=("203.0.113.9", 4000))
    assert away.get("/api/metrics", headers={"Authorization": "Bearer ünïcode".encode()}).status_code == 404


def test_a_stop_counts_only_when_they_still_followed_the_trader(api: TestClient) -> None:
    user(follows=(SAM,))
    tap = {"callback_query": {"id": "q", "data": f"unf:{SAM}", "message": {"chat": {"id": 555}, "message_id": 1}}}
    engine._on_update(tap)
    engine._on_update(tap)  # tapped again, already unfollowed
    assert metrics(api)["stops"]["stop_taps"] == 1
