"""Alerts: every buy with how far the price moved, the $100 floor, the daily cap, grouped alerts edited in place,
Useful / Not useful, saved interests and big-trade alerts. No network: Telegram and the SDK are stand-ins."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from uselayer import Trader, TraderDetail, WhalePosition, WhaleTrade

from signals import app as engine

FAPI = "clerk.signals.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SAM, ANN, BOB, WHALE = ("0x" + c * 40 for c in "abcd")


def auth(sub: str = "user_1") -> dict[str, str]:
    now = int(time.time())
    t = jwt.encode({"sub": sub, "iss": f"https://{FAPI}", "iat": now, "exp": now + 60, "azp": engine.SITE}, KEY, algorithm="RS256")
    return {"Authorization": f"Bearer {t}"}


class Telegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []

    def __call__(self, method: str, **params: Any) -> Any:
        if method == "sendMessage":
            self.sent.append(params)
            return {"message_id": len(self.sent)}
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
    monkeypatch.setattr(engine, "tg", Telegram())
    monkeypatch.setattr(engine, "_categories", lambda markets: {m: "Politics" for m in markets})
    scores = [{"wallet": w, "name": n, "segment": "too_fast", "copyable": "no", "copy": None}
              for w, n in ((SAM, "Sam"), (ANN, "Ann"), (BOB, "Bob"))]  # fmt: skip
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": scores})
    return TestClient(engine.app)


def user(uid: str = "user_1", chat: str = "555", follows: tuple[str, ...] = (SAM, ANN, BOB), interests: Any = None) -> None:
    d = engine._load_follows()
    d["users"][uid] = {"follows": list(follows), "chat": chat} | ({"interests": interests} if interests is not None else {})
    engine._save_follows(d)


def trade(w: str, at: datetime, price: float = 0.49, usd: float = 1000, market: str = "0xcid:0", i: str = "1",
          name: str | None = None) -> WhaleTrade:  # fmt: skip
    return WhaleTrade("polymarket", w, name, market, "yes", "buy", price, usd / price, usd, at, f"{w}:{market}:{i}",
                      title="Will it <rain>?", outcome="Yes", url="https://polymarket.com/event/rain", token=f"tok-{market}")  # fmt: skip


def new_buy(w: str, at: datetime, price: float = 0.49, now: float = 0.50, usd: float = 1000, market: str = "0xcid:0") -> Any:
    return engine.NewBuy(w, [trade(w, at, price, usd, market)], now)


# ---- every buy alerts, with how far the price moved ----


def test_a_buy_outside_the_range_says_the_price_already_moved_even_for_a_copyable_trader(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "copy_offer", lambda s: (True, None))
    t = trade(SAM, datetime.now(UTC))
    text = engine._alert_text(SAM, t, 0.49, 0.60, 2000, 980)
    assert "Paid 49¢ · now 60¢, price already moved." in text
    assert "get in" not in text and "$10 gets you" not in text
    assert "Buy on Polymarket" in text and text.endswith(engine.DATA_DELAY)
    below = engine._alert_text(SAM, t, 0.49, 0.30, 2000, 980)  # the market turned against them: moved too
    assert "Paid 49¢ · now 30¢, price already moved." in below


def test_a_buy_under_100_dollars_doesnt_alert(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    trades: list[WhaleTrade] = []
    detail = lambda *a, **k: TraderDetail(Trader(venue="polymarket", id=SAM, name="Sam"), "visible", {},  # noqa: E731
                                          [WhalePosition("0xcid:0", None, "Yes", 100, 0.4, 0.40, None, None),
                                           WhalePosition("0xcid:1", None, "Yes", 100, 0.4, 0.40, None, None)], list(trades))  # fmt: skip
    monkeypatch.setattr(engine.reads.whales, "trader", detail)
    assert engine.alert_pass() == 0  # first round: noted only
    fresh = datetime.now(UTC)
    trades[:0] = [trade(SAM, fresh, 0.40, 99, "0xcid:0"), trade(SAM, fresh, 0.40, 150, "0xcid:1")]
    engine._cache.clear()
    assert engine.alert_pass() == 1
    assert "($150)" in engine.tg.sent[-1]["text"]


# ---- the daily cap ----


def test_twenty_alerts_a_day_then_one_limit_message_then_nothing_until_tomorrow(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = {"d": "2026-10-13"}
    monkeypatch.setattr(engine, "_today", lambda now=None: day["d"])
    user()
    sent = [engine._send("user_1", "555", "buy", lambda aid: f"alert {aid}") for _ in range(engine.DAILY_CAP)]
    assert all(sent) and len(engine.tg.sent) == 20
    assert engine._send("user_1", "555", "whale", lambda aid: "one more") is None
    assert engine.tg.sent[-1]["text"] == "You've reached today's alert limit; more tomorrow."
    assert "reply_markup" not in engine.tg.sent[-1]
    assert engine._send("user_1", "555", "group", lambda aid: "and another") is None
    assert len(engine.tg.sent) == 21  # nothing more today, not even the limit message again
    assert engine._send("user_2", "777", "buy", lambda aid: "someone else") is not None  # per person
    day["d"] = "2026-10-14"  # the next UTC day
    assert engine._send("user_1", "555", "buy", lambda aid: "fresh") is not None


# ---- grouped alerts ----


def test_a_second_trader_buying_the_same_outcome_sends_one_grouped_alert_and_later_buys_edit_it(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "_today", lambda now=None: "2026-10-13")
    user()
    t0 = datetime(2026, 10, 13, 14, 2, tzinfo=UTC)
    assert engine._deliver("user_1", "555", new_buy(SAM, t0, 0.49, 0.50), "Politics") == 1
    assert "Sam" in engine.tg.sent[-1]["text"] and "traders you follow" not in engine.tg.sent[-1]["text"]  # alone: as usual
    assert engine._deliver("user_1", "555", new_buy(ANN, t0 + timedelta(minutes=18), 0.51, 0.53), "Politics") == 1
    group = engine.tg.sent[-1]
    lines = group["text"].splitlines()
    assert lines[0] == "<b>2 traders you follow bought Yes</b> on Will it &lt;rain&gt;?"
    assert lines[1:3] == ["· Sam paid 49¢ at 14:02 UTC", "· Ann paid 51¢ at 14:20 UTC"]  # who went first, first
    assert lines[3] == "Now 53¢." and "Buy on Polymarket" in lines[4] and lines[5] == engine.DATA_DELAY
    assert [b["text"] for b in group["reply_markup"]["inline_keyboard"][0]] == ["Useful", "Not useful"]
    # A third trader, over 6 hours after the first but within 6 of the latest (the window slides): no new ping, the
    # grouped message is edited in place
    assert engine._deliver("user_1", "555", new_buy(BOB, t0 + timedelta(hours=6, minutes=10), 0.52, 0.62), "Politics") == 0
    assert len(engine.tg.sent) == 2
    edit = engine.tg.edits[-1]
    assert (edit["chat_id"], edit["message_id"]) == ("555", 2)
    assert edit["text"].startswith("<b>3 traders you follow bought Yes</b>") and "· Bob paid 52¢ at 20:12 UTC" in edit["text"]
    assert "Now 62¢, price already moved." in edit["text"]
    assert engine._sent_today("user_1") == 2  # Sam's alert and the group, counted once


def test_groups_in_sports_and_esports_need_buys_within_an_hour(api: TestClient) -> None:
    user()
    t0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    engine._deliver("user_1", "555", new_buy(SAM, t0, market="0xs:0"), "Sports")
    engine._deliver("user_1", "555", new_buy(ANN, t0 + timedelta(hours=2), market="0xs:0"), "Sports")
    assert "traders you follow" not in engine.tg.sent[-1]["text"]  # two hours apart in Sports: two plain alerts
    engine._deliver("user_1", "555", new_buy(SAM, t0, market="0xp:0"), "Politics")
    engine._deliver("user_1", "555", new_buy(ANN, t0 + timedelta(hours=2), market="0xp:0"), "Politics")
    assert engine.tg.sent[-1]["text"].startswith("<b>2 traders you follow")  # within 6 hours elsewhere: grouped


def test_a_grouped_alert_counts_once_towards_the_daily_cap(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_today", lambda now=None: "2026-10-13")
    user()
    t0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    for i, w in enumerate((SAM, ANN, BOB)):
        engine._deliver("user_1", "555", new_buy(w, t0 + timedelta(minutes=i)), "Politics")
    assert engine._sent_today("user_1") == 2  # Sam's alert, then the group; Bob's buy edited it


# ---- Useful / Not useful ----


def test_useful_votes_are_one_per_alert_and_account_and_can_change(api: TestClient) -> None:
    user()
    user("user_2", chat="777")
    aid = engine._send("user_1", "555", "buy", lambda a: "alert", stop=SAM)
    keys = engine.tg.sent[-1]["reply_markup"]["inline_keyboard"]
    assert [(b["text"], b["callback_data"]) for b in keys[0]] == [("Useful", f"v:{aid}:1"), ("Not useful", f"v:{aid}:0")]
    assert keys[1][0]["text"] == "Stop alerts from Sam"  # beside the existing button
    tap = lambda chat, data: engine._on_update({"callback_query": {"id": "q", "data": data, "message": {"chat": {"id": chat}}}})  # noqa: E731
    tap(555, f"v:{aid}:1")
    assert engine.tg.answers[-1] == {"callback_query_id": "q", "text": "Noted: useful."}  # quietly: no chat message
    assert len(engine.tg.sent) == 1
    tap(555, f"v:{aid}:0")  # changed
    tap(777, f"v:{aid}:1")  # someone else's alert: not theirs to vote on
    with engine._db() as con:
        assert con.execute("SELECT alert_id, uid, useful FROM votes").fetchall() == [(aid, "user_1", 0)]
    assert "text" not in engine.tg.answers[-1]


# ---- interests on the account ----


def test_interests_are_saved_per_account_and_only_from_the_markets_on_offer(api: TestClient) -> None:
    assert api.get("/api/me", headers=auth()).json()["interests"] is None  # never saved
    r = api.put("/api/me/interests", json={"interests": ["Sports", "Politics", "Sports"]}, headers=auth())
    assert r.json()["interests"] == ["Politics", "Sports"] and r.headers["cache-control"] == "private, no-store"
    assert api.get("/api/me", headers=auth("user_2")).json()["interests"] is None  # someone else's
    assert api.put("/api/me/interests", json={"interests": ["<script>"]}, headers=auth()).status_code == 400
    assert api.put("/api/me/interests", json={"interests": []}, headers=auth()).json()["interests"] == []  # all markets
    assert api.put("/api/me/interests", json={"interests": ["Sports"]}).status_code == 401


def test_the_page_saves_interests_to_the_account(api: TestClient) -> None:
    page = api.get("/").text
    assert '"/api/me/interests"' in page and "#/check/" in page


# ---- big trades in your markets ----


def big(w: str, usd: float = 12_400, market: str = "0xw:0", i: str = "1", at: datetime | None = None) -> WhaleTrade:
    return trade(w, at or datetime.now(UTC), 0.49, usd, market, i)


def test_big_trades_alert_people_who_saved_markets_there_whoever_the_trader(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user("politics", "1", follows=(), interests=["Politics"])
    user("sports", "2", follows=(), interests=["Sports"])
    user("never", "3", follows=())  # never saved interests: no big-trade alerts
    user("all", "4", follows=(), interests=[])  # Skip, or all markets: no choice made, no big-trade alerts
    user("follower", "5", follows=(WHALE,), interests=["Politics"])  # follows the whale: their own alert covers it
    rows: list[WhaleTrade] = [big(WHALE, i="0")]
    asked: list[Any] = []
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda usd, **k: asked.append((usd, k)) or list(rows))
    monkeypatch.setattr(engine.reads.whales, "trader", lambda *a, **k: TraderDetail(
        Trader(venue="polymarket", id=WHALE, name=None), "visible", {}, [WhalePosition("0xw:0", None, "Yes", 1, 0.49, 0.51, None, None)], []))  # fmt: skip
    users = {uid: u for uid, u in engine._load_follows()["users"].items()}
    assert engine.whale_pass(users, time.time()) == 0  # the first round only notes what's there
    assert asked[0] == (10_000, {"venues": engine.VENUES})  # the SDK's big trades, as the Feed's
    rows.insert(0, big(WHALE, i="1"))
    rows.insert(0, big(WHALE, usd=9_000, market="0xw:1", i="2"))  # under $10,000: not a big trade
    engine._cache.clear()
    assert engine.whale_pass(users, time.time()) == 1
    assert [m["chat_id"] for m in engine.tg.sent] == ["1"]
    text = engine.tg.sent[-1]["text"]
    lines = text.splitlines()
    assert lines[0] == "<b>Big trade in Politics:</b> Trader 0xdd…dd bought <b>Yes</b> ($12,400) on Will it &lt;rain&gt;?"
    assert lines[1] == "Paid 49¢ · now 51¢."  # neutral: no "get in" for a trader copying isn't offered for
    assert f'href="{engine.SITE}/#/check/{WHALE}/0xw:0">Check your price</a>' in lines[2] and "Buy on Polymarket" in lines[2]
    assert lines[3] == engine.DATA_DELAY
    assert "Stop alerts" not in str(engine.tg.sent[-1]["reply_markup"])  # they don't follow them


def test_a_big_trade_by_a_scored_trader_links_to_their_signals_page(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    text = engine._whale_text(big(SAM), 0.51, "Politics")
    assert f'<a href="{engine.SITE}/#/worth/{SAM}">Sam</a>' in text
    monkeypatch.setattr(engine, "copy_offer", lambda s: (True, None))
    assert "Paid 49¢ · now 51¢: you can still get in." in engine._whale_text(big(SAM), 0.51, None)  # copyable, in range
    assert "price already moved" in engine._whale_text(big(SAM), 0.70, None)


def test_big_trade_alerts_are_capped_within_the_daily_cap(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_today", lambda now=None: "2026-10-13")
    user("u", "1", follows=(), interests=["Politics"])
    rows: list[WhaleTrade] = []
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda usd, **k: list(rows))
    monkeypatch.setattr(engine, "_live_read", lambda w: TraderDetail(
        Trader(venue="polymarket", id=w, name=None), "visible", {}, [WhalePosition(f"0x{w[2:6]}:0", None, "Yes", 1, 0.49, 0.5, None, None)], []))  # fmt: skip
    users = engine._load_follows()["users"]
    engine.whale_pass(users, time.time())
    engine._cache.clear()
    rows[:] = [big("0x" + f"{n:04x}" * 10, market=f"0x{n:04x}:0", i=str(n)) for n in range(1, 9)]
    assert engine.whale_pass(users, time.time()) == engine.WHALE_DAILY
    assert engine._sent_today("u") == engine.WHALE_DAILY  # and they count towards the daily cap


# ---- what the bot says ----


def test_connecting_describes_the_alert_rules(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user("user_1", chat=None, follows=(SAM,))  # type: ignore[arg-type]
    monkeypatch.setattr(engine, "_tg", {"bot": "SignalsAlertsBot", "offset": 0, "error": None})
    link = api.post("/api/me/telegram", headers=auth()).json()["link"]
    engine._on_update({"message": {"chat": {"id": 555}, "text": "/start " + link.rsplit("=", 1)[1]}})
    text = engine.tg.sent[-1]["text"]
    for words in ("Connected.", "buys $100 or more", "what they paid and the price now", "two or more of them buy the same thing",
                  "trades of $10,000 or more", "At most 20 alerts a day", "Following: Sam"):  # fmt: skip
        assert words in text, words
    assert "something you can still get into" not in text


# ---- an alert's "Check your price" ----


def test_check_your_price_opens_that_one_trade(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    at = datetime.now(UTC)
    fills = [trade(WHALE, at, 0.40, 400, "0xw:0", "1"), trade(WHALE, at, 0.50, 500, "0xw:0", "2")]
    monkeypatch.setattr(engine.reads.whales, "trader", lambda *a, **k: TraderDetail(
        Trader(venue="polymarket", id=WHALE, name="whaley"), "visible", {}, [WhalePosition("0xw:0", None, "Yes", 1, 0.45, 0.46, None, None)], fills))  # fmt: skip
    b = api.get(f"/api/bet/{WHALE}/0xw:0").json()
    assert (b["name"], b["copy_ok"], b["status"], b["category"], b["usd"]) == ("whaley", False, "in", "Politics", 900)
    assert b["price"] == pytest.approx(900 / 2000)
    assert api.get(f"/api/bet/{WHALE}/0xnope:0").status_code == 404
    assert api.get("/api/bet/0x12/0xw:0").status_code == 400


# ---- review fixes: settled prices, sliding and late groups, failures, restarts ----


def detail_of(trades: list[WhaleTrade], prices: dict[str, float]) -> Any:
    return lambda *a, **k: TraderDetail(Trader(venue="polymarket", id=a[1] if len(a) > 1 else SAM, name=None), "visible", {},
                                        [WhalePosition(m, None, "Yes", 1, 0.5, p, None, None) for m, p in prices.items()],
                                        [t for t in trades if t.trader == (a[1] if len(a) > 1 else SAM)])  # fmt: skip


@pytest.mark.parametrize("price", [0.98, 0.99, 0.02, 0.005])
def test_no_alert_at_a_settled_price_alone_or_grouped(api: TestClient, monkeypatch: pytest.MonkeyPatch, price: float) -> None:
    user(follows=(SAM, ANN))
    trades: list[WhaleTrade] = []
    prices = {"0xcid:0": 0.50}
    monkeypatch.setattr(engine.reads.whales, "trader", detail_of(trades, prices))
    engine.alert_pass()  # first round: noted only
    fresh = datetime.now(UTC)
    trades.append(trade(SAM, fresh, 0.49, 1000, "0xcid:0", "1"))
    engine._cache.clear()
    assert engine.alert_pass() == 1  # Sam alone, at 50¢
    prices["0xcid:0"] = price  # now settled, or as good as
    trades.append(trade(ANN, fresh, 0.97 if price > 0.5 else 0.03, 1000, "0xcid:0", "2"))
    engine._cache.clear()
    assert engine.alert_pass() == 0 and not engine.tg.edits  # no grouped alert either
    trades.append(trade(SAM, fresh, 0.97, 1000, "0xcid:9", "3"))  # a new market, already at the price
    prices["0xcid:9"] = price
    engine._alerted.clear()
    engine._cache.clear()
    assert engine.alert_pass() == 0


def test_no_big_trade_alert_at_a_settled_price(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user("politics", "1", follows=(), interests=["Politics"])
    rows: list[WhaleTrade] = []
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda usd, **k: list(rows))
    monkeypatch.setattr(engine, "_live_read", lambda w: TraderDetail(Trader(venue="polymarket", id=w, name=None), "visible", {},
                                                                     [WhalePosition("0xw:0", None, "Yes", 1, 0.49, 0.985, None, None)], []))  # fmt: skip
    users = engine._load_follows()["users"]
    engine.whale_pass(users, time.time())
    engine._cache.clear()
    rows.append(big(WHALE))
    assert engine.whale_pass(users, time.time()) == 0 and not engine.tg.sent


def test_a_late_buy_with_an_earlier_time_joins_its_group(api: TestClient) -> None:
    user()
    t0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    engine._deliver("user_1", "555", new_buy(SAM, t0), "Politics")
    engine._deliver("user_1", "555", new_buy(ANN, t0 + timedelta(minutes=30)), "Politics")  # the group
    assert engine._deliver("user_1", "555", new_buy(BOB, t0 - timedelta(minutes=10), 0.45), "Politics") == 0  # seen late
    lines = engine.tg.edits[-1]["text"].splitlines()
    assert lines[0].startswith("<b>3 traders you follow") and lines[1] == "· Bob paid 45¢ at 13:50 UTC"  # first, first


def test_groups_use_six_hours_when_the_category_isnt_known(api: TestClient) -> None:
    user()
    t0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    engine._deliver("user_1", "555", new_buy(SAM, t0), None)
    engine._deliver("user_1", "555", new_buy(ANN, t0 + timedelta(hours=3)), None)
    assert engine.tg.sent[-1]["text"].startswith("<b>2 traders you follow")


def test_a_failed_or_impossible_edit_sends_no_new_ping(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user()
    t0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    engine._deliver("user_1", "555", new_buy(SAM, t0), "Politics")
    engine._deliver("user_1", "555", new_buy(ANN, t0 + timedelta(minutes=5)), "Politics")
    real = engine.tg

    def fails(method: str, **params: Any) -> Any:
        if method == "editMessageText":
            raise RuntimeError("Telegram: message to edit not found")
        return real(method, **params)

    monkeypatch.setattr(engine, "tg", fails)
    assert engine._deliver("user_1", "555", new_buy(BOB, t0 + timedelta(minutes=9)), "Politics") == 0
    assert len(real.sent) == 2
    monkeypatch.setattr(engine, "tg", real)
    with engine._db() as con:
        con.execute("UPDATE alerts SET message_id = NULL WHERE kind = 'group'")
    third = "0x" + "e" * 40
    assert engine._deliver("user_1", "555", new_buy(third, t0 + timedelta(minutes=12)), "Politics") == 0
    assert len(real.sent) == 2 and not real.edits  # nothing to edit, and no second group


def test_the_limit_message_is_tried_again_when_it_fails(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_today", lambda now=None: "2026-10-13")
    user()
    for _ in range(engine.DAILY_CAP):
        engine._send("user_1", "555", "buy", lambda aid: "x")
    real = engine.tg
    monkeypatch.setattr(engine, "tg", lambda method, **p: (_ for _ in ()).throw(RuntimeError("Telegram didn't answer")))
    assert engine._send("user_1", "555", "buy", lambda aid: "y") is None
    monkeypatch.setattr(engine, "tg", real)
    engine._send("user_1", "555", "buy", lambda aid: "z")
    assert real.sent[-1]["text"] == engine.CAP_TEXT and len(real.sent) == 21  # sent on the next try, once


def test_a_big_trade_at_the_daily_cap_sends_the_limit_message_instead(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_today", lambda now=None: "2026-10-13")
    user("u", "1", follows=(), interests=["Politics"])
    for _ in range(engine.DAILY_CAP):
        engine._send("u", "1", "buy", lambda aid: "x")
    rows: list[WhaleTrade] = []
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda usd, **k: list(rows))
    monkeypatch.setattr(engine, "_live_read", lambda w: TraderDetail(Trader(venue="polymarket", id=w, name=None), "visible", {},
                                                                     [WhalePosition("0xw:0", None, "Yes", 1, 0.49, 0.5, None, None)], []))  # fmt: skip
    users = engine._load_follows()["users"]
    engine.whale_pass(users, time.time())
    engine._cache.clear()
    rows.append(big(WHALE))
    assert engine.whale_pass(users, time.time()) == 0  # under the 5 big trades, but over the 20
    assert engine.tg.sent[-1]["text"] == engine.CAP_TEXT


def test_stop_alerts_from_another_chat_changes_nobody_elses_follows(api: TestClient) -> None:
    user("user_1", "555", follows=(SAM,))
    user("user_2", "777", follows=(ANN,))
    engine._on_update({"callback_query": {"id": "q", "data": f"unf:{SAM}", "message": {"chat": {"id": 777}}}})
    users = engine._load_follows()["users"]
    assert users["user_1"]["follows"] == [SAM] and users["user_2"]["follows"] == [ANN]


def test_a_restart_doesnt_alert_the_same_trader_and_market_again(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    trades: list[WhaleTrade] = []
    monkeypatch.setattr(engine.reads.whales, "trader", detail_of(trades, {"0xcid:0": 0.50}))
    engine.alert_pass()
    trades.append(trade(SAM, datetime.now(UTC), 0.49, 1000, "0xcid:0", "1"))
    engine._cache.clear()
    assert engine.alert_pass() == 1
    engine._alerted.clear()  # a restart: what was in memory is gone
    engine._seen.clear()
    engine._cache.clear()
    engine.alert_pass()  # first round after the restart: noted only
    trades.append(trade(SAM, datetime.now(UTC), 0.50, 1000, "0xcid:0", "2"))  # another fill, same market
    engine._cache.clear()
    assert engine.alert_pass() == 0  # alerts.db remembers


def test_the_alert_text_and_its_price_check_are_built_once_per_buy(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(3):
        user(f"user_{i}", str(100 + i), follows=(SAM,))
    built = []
    real = engine._alert_text
    monkeypatch.setattr(engine, "_alert_text", lambda *a, **k: built.append(a) or real(*a, **k))
    trades: list[WhaleTrade] = []
    monkeypatch.setattr(engine.reads.whales, "trader", detail_of(trades, {"0xcid:0": 0.50}))
    engine.alert_pass()
    trades.append(trade(SAM, datetime.now(UTC), 0.49, 1000, "0xcid:0", "1"))
    engine._cache.clear()
    assert engine.alert_pass() == 3 and len(built) == 1


def test_traders_chips_never_change_the_saved_markets(api: TestClient, run_js: Any) -> None:
    deps = ("INTERESTS", "PICKED_KEY", "wasPicked", "saveInterests", "setInterests", "syncInterests")
    got = run_js(deps, """(() => {
      const puts = [];
      globalThis.interests = null;
      globalThis.localStorage = { d: {}, getItem(k) { return this.d[k] ?? null; }, setItem(k, v) { this.d[k] = String(v); } };
      globalThis.signedIn = () => true;
      globalThis.auth = { me: { interests: null } };
      globalThis.api = (path, o) => { puts.push(JSON.parse(o.body).interests); return { then() {} }; };
      setInterests(["Sports"]);                    // a Traders chip: the view only
      const chip = puts.length;
      setInterests([]);                            // the "All" chip
      syncInterests();                             // signing in with nothing picked: nothing saved
      const signin = puts.length;
      setInterests(["Politics"], { picked: true }); // onboarding, or the Feed's Change
      auth.me.interests = ["Politics"];
      setInterests([]);                            // the All chip again: the saved choice stays
      syncInterests();
      return [chip, signin, puts];
    })()""")
    assert got == [0, 0, [["Politics"]]]
