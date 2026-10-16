"""Alerts: every buy by a trader you follow, whatever the amount, with how far the price moved and when they traded, the daily cap, grouped alerts edited in place,
Useful / Not useful, saved interests and big-trade alerts. No network: Telegram and the SDK are stand-ins."""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from uselayer import Trader, TraderDetail, WhalePosition, WhaleTrade

from conftest import live_reads
from signals import app as engine

FAPI = "clerk.signals.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SAM, ANN, BOB, WHALE = ("0x" + c * 40 for c in "abcd")
CAP = "You've reached today's limit of 200 alerts. Alerts resume at 00:00 UTC, in "


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
    return engine.NewBuy(w, [trade(w, at, price, usd, market)], now, seen=at.timestamp() + 5 * 60)  # seen 5 min later


# ---- every buy alerts, with how far the price moved ----


def test_a_buy_outside_the_range_says_the_price_already_moved_even_for_a_copyable_trader(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "copy_offer", lambda s: (True, None))
    t = trade(SAM, datetime.now(UTC))
    at = datetime(2026, 10, 13, 14, 2, tzinfo=UTC).timestamp()
    lines = engine._alert_text(SAM, t, 0.49, 0.60, 980, at, at + 7 * 60).splitlines()
    assert lines[:4] == ["<b>Sam</b> bought <b>Yes</b> at 49¢ ($980)", "Will it &lt;rain&gt;?", "Now 60¢, price already moved.",
                         "Traded 14:02 UTC, 7 min before this alert."]  # fmt: skip
    assert engine.COPY_LINE not in lines and "get in" not in "\n".join(lines)  # no copy cue once the price moved
    assert lines[4] == engine.TRADED_INTL  # where they traded; no US claim while Layer's matches aren't read
    assert "Check current price" in lines[5] and "Buy on Polymarket International" in lines[5] and lines[6] == engine.FOLLOW_DELAY
    below = engine._alert_text(SAM, t, 0.49, 0.30, 980, at, at)  # the market turned against them: moved too
    assert "Now 30¢, price already moved." in below and "Traded 14:02 UTC, under a minute before this alert." in below


def test_every_buy_by_a_followed_trader_alerts_whatever_the_amount(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    trades: list[WhaleTrade] = []
    detail = lambda *a, **k: TraderDetail(Trader(venue="polymarket", id=SAM, name="Sam"), "visible", {},  # noqa: E731
                                          [WhalePosition("0xcid:0", None, "Yes", 100, 0.4, 0.40, None, None),
                                           WhalePosition("0xcid:1", None, "Yes", 100, 0.4, 0.40, None, None)], list(trades))  # fmt: skip
    live_reads(monkeypatch, detail)
    assert engine.alert_pass() == 0  # first round: noted only
    fresh = datetime.now(UTC)
    trades[:0] = [trade(SAM, fresh, 0.40, 5, "0xcid:0"), trade(SAM, fresh, 0.40, 150, "0xcid:1")]
    engine._cache.clear()
    assert engine.alert_pass() == 2
    assert sorted("($5)" in m["text"] for m in engine.tg.sent) == [False, True]  # a $5 buy is news too


def test_a_followed_trader_nobody_recommends_or_has_checked_alerts_too(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Following, and its alerts, never depend on a tier: a trader Explore lists as "Limited evidence" or "Not checked
    # yet" (in no check, never scored) alerts their followers like any other.
    quiet = "0x" + "e" * 40
    user(follows=(quiet,))
    trades: list[WhaleTrade] = []
    detail = lambda *a, **k: TraderDetail(Trader(venue="polymarket", id=quiet, name="Quiet"), "visible", {},  # noqa: E731
                                          [WhalePosition("0xcid:0", None, "Yes", 100, 0.4, 0.40, None, None)], list(trades))  # fmt: skip
    live_reads(monkeypatch, detail)
    assert engine.alert_pass() == 0  # first round: noted only
    trades[:0] = [trade(quiet, datetime.now(UTC), 0.40, 2500, "0xcid:0")]
    engine._cache.clear()
    assert engine.alert_pass() == 1 and "bought <b>Yes</b> at 40¢ ($2,500)" in engine.tg.sent[0]["text"]


# ---- the daily cap ----


def test_twenty_alerts_a_day_then_one_limit_message_then_nothing_until_tomorrow(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = {"d": "2026-10-13"}
    monkeypatch.setattr(engine, "_today", lambda now=None: day["d"])
    user()
    sent = [engine._send("user_1", "555", "buy", lambda aid: f"alert {aid}") for _ in range(engine.DAILY_CAP)]
    assert all(sent) and len(engine.tg.sent) == engine.DAILY_CAP
    assert engine._send("user_1", "555", "whale", lambda aid: "one more") is None
    assert engine.tg.sent[-1]["text"].startswith(CAP) and "still in your Feed" in engine.tg.sent[-1]["text"]
    assert "reply_markup" not in engine.tg.sent[-1]
    assert engine._send("user_1", "555", "group", lambda aid: "and another") is None
    assert len(engine.tg.sent) == engine.DAILY_CAP + 1  # nothing more today, not even the limit message again
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
    assert lines[3:5] == ["Now 53¢.", "Latest buy 5 min before this alert."]
    check = urllib.parse.quote(f"{engine.SITE}/#/check/{ANN}/0xcid:0", safe="")  # the latest buyer's trade
    assert lines[5] == engine.TRADED_INTL
    assert f"/check?to={check}" in lines[6] and ">Check current price</a>" in lines[6] and "Buy on Polymarket International" in lines[6]
    assert lines[7] == engine.FOLLOW_DELAY
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
    live_reads(monkeypatch, lambda *a, **k: TraderDetail(
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
    assert lines[0] == "<b>Big trade in Politics:</b> Trader 0xdd…dd bought <b>Yes</b> at 49¢ ($12,400)"
    assert lines[1:3] == ["Will it &lt;rain&gt;?", "Now 51¢."]  # neutral: no "get in" for a trader copying isn't offered for
    assert lines[3].startswith("Traded ") and lines[3].endswith(" UTC, under a minute before this alert.")
    # each link goes through /go (counted), on to Check current price and the market; no record for an unscored trader
    check = urllib.parse.quote(f"{engine.SITE}/#/check/{WHALE}/0xw:0", safe="")
    assert lines[4] == engine.TRADED_INTL
    assert f"/check?to={check}" in lines[5] and ">Check current price</a>" in lines[5] and "Buy on Polymarket International" in lines[5]
    assert "Their record" not in lines[5] and lines[6] == engine.BIG_DELAY
    assert "Stop alerts" not in str(engine.tg.sent[-1]["reply_markup"])  # they don't follow them


def test_a_big_trade_by_a_scored_trader_links_to_their_signals_page(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = time.time()
    text = engine._whale_text(big(SAM), 0.51, "Politics", seen)
    assert f'<a href="{engine.SITE}/#/worth/{SAM}">Sam</a>' in text and engine.COPY_LINE not in text
    monkeypatch.setattr(engine, "copy_offer", lambda s: (True, None))
    assert engine.COPY_LINE in engine._whale_text(big(SAM), 0.51, None, seen).splitlines()  # copyable, in range
    moved = engine._whale_text(big(SAM), 0.70, None, seen)
    assert "Now 70¢, price already moved." in moved and engine.COPY_LINE not in moved


def test_big_trade_alerts_are_capped_within_the_daily_cap(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_today", lambda now=None: "2026-10-13")
    user("u", "1", follows=(), interests=["Politics"])
    rows: list[WhaleTrade] = []
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda usd, **k: list(rows))
    monkeypatch.setattr(engine, "_live_read", lambda w, *a, **k: TraderDetail(
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
    for words in ("Connected.", "a few minutes after a trader you follow buys or sells, of any size",
                  "at what price, the price now and when they traded", "alerts are never instant",
                  "Not every trade gets its own message", "buys of one outcome within 30 minutes come as one",
                  "more buys of that outcome (or more sells of it) are left out for 6 hours", "two or more traders you follow buy the same thing",
                  "all but decided (2¢ or less, 98¢ or more)", "trades of $10,000 or more", "At most 200 alerts a day",
                  "unless you choose fewer for them", "only bets over an amount you pick", "one summary every 6 hours",
                  "Fewer alerts under any alert", "A sale of something they alerted you about still comes right away",
                  "Following: Sam"):  # fmt: skip
        assert words in text, words
    assert "$100" not in text and "whatever the amount" not in text and "every trade" not in text.replace("Not every trade", "")
    assert engine.ALERT_AGAIN_S == 6 * 3600 and engine.ALERT_MAX_AGE_S == 30 * 60  # what it says is what alert_pass does
    assert "something you can still get into" not in text


# ---- an alert's "Check your price" ----


def test_check_your_price_opens_that_one_trade(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    at = datetime.now(UTC)
    fills = [trade(WHALE, at, 0.40, 400, "0xw:0", "1"), trade(WHALE, at, 0.50, 500, "0xw:0", "2")]
    live_reads(monkeypatch, lambda *a, **k: TraderDetail(
        Trader(venue="polymarket", id=WHALE, name="whaley"), "visible", {}, [WhalePosition("0xw:0", None, "Yes", 1, 0.45, 0.46, None, None)], fills))  # fmt: skip
    b = api.get(f"/api/bet/{WHALE}/0xw:0", headers=auth()).json()  # the Feed's: signed in
    assert (b["name"], b["copy_ok"], b["status"], b["category"], b["usd"]) == ("whaley", False, "in", "Politics", 900)
    assert b["price"] == pytest.approx(900 / 2000)
    assert api.get(f"/api/bet/{WHALE}/0xnope:0", headers=auth()).status_code == 404
    assert api.get("/api/bet/0x12/0xw:0", headers=auth()).status_code == 400


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
    live_reads(monkeypatch, detail_of(trades, prices))
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
    monkeypatch.setattr(engine, "_live_read", lambda w, *a, **k: TraderDetail(Trader(venue="polymarket", id=w, name=None), "visible", {},
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
    assert real.sent[-1]["text"].startswith(CAP) and len(real.sent) == engine.DAILY_CAP + 1  # sent on the next try, once


def test_a_big_trade_at_the_daily_cap_sends_the_limit_message_instead(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_today", lambda now=None: "2026-10-13")
    user("u", "1", follows=(), interests=["Politics"])
    for _ in range(engine.DAILY_CAP):
        engine._send("u", "1", "buy", lambda aid: "x")
    rows: list[WhaleTrade] = []
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda usd, **k: list(rows))
    monkeypatch.setattr(engine, "_live_read", lambda w, *a, **k: TraderDetail(Trader(venue="polymarket", id=w, name=None), "visible", {},
                                                                     [WhalePosition("0xw:0", None, "Yes", 1, 0.49, 0.5, None, None)], []))  # fmt: skip
    users = engine._load_follows()["users"]
    engine.whale_pass(users, time.time())
    engine._cache.clear()
    rows.append(big(WHALE))
    assert engine.whale_pass(users, time.time()) == 0  # under the 5 big trades, but over the daily cap
    assert engine.tg.sent[-1]["text"].startswith(CAP)


def test_stop_alerts_from_another_chat_changes_nobody_elses_follows(api: TestClient) -> None:
    user("user_1", "555", follows=(SAM,))
    user("user_2", "777", follows=(ANN,))
    engine._on_update({"callback_query": {"id": "q", "data": f"unf:{SAM}", "message": {"chat": {"id": 777}}}})
    users = engine._load_follows()["users"]
    assert users["user_1"]["follows"] == [SAM] and users["user_2"]["follows"] == [ANN]


def test_a_restart_doesnt_alert_the_same_trader_and_market_again(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    trades: list[WhaleTrade] = []
    live_reads(monkeypatch, detail_of(trades, {"0xcid:0": 0.50}))
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
    live_reads(monkeypatch, detail_of(trades, {"0xcid:0": 0.50}))
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


def test_an_alerts_db_from_before_trigger_at_gets_the_column(api: TestClient) -> None:
    import sqlite3

    engine.STORE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(engine.alerts_db())
    con.execute("CREATE TABLE alerts (id TEXT PRIMARY KEY, uid TEXT NOT NULL, chat TEXT, kind TEXT NOT NULL, trader TEXT,"
                " outcome TEXT, message_id INTEGER, sent_at REAL NOT NULL, trade_at REAL, day TEXT NOT NULL)")
    con.commit()
    con.close()
    engine._db_ready.discard(str(engine.alerts_db()))
    assert engine._send("user_1", "555", "buy", lambda aid: "x", trade_at=1.0) is not None


# ---- split buys add up ----


def _split(monkeypatch: pytest.MonkeyPatch, gaps_min: list[float], usd: float = 60) -> tuple[int, list[str]]:
    """A trader's buys of one outcome, each seen in its own round, ``gaps_min`` minutes after the first."""
    user(follows=(SAM,))
    trades: list[WhaleTrade] = []
    live_reads(monkeypatch, detail_of(trades, {"0xcid:0": 0.50}))
    t0 = datetime.now(UTC) - timedelta(hours=1)
    engine.alert_pass(now=t0.timestamp() - 60)  # first round: noted only
    sent = 0
    for i, gap in enumerate(gaps_min):
        at = t0 + timedelta(minutes=gap)
        trades.append(trade(SAM, at, 0.48 + 0.02 * i, usd, "0xcid:0", str(i)))
        engine._cache.clear()
        sent += engine.alert_pass(now=at.timestamp() + 30)
    return sent, [m["text"] for m in engine.tg.sent]


def test_one_60_dollar_buy_alerts_and_a_second_soon_after_doesnt_alert_again(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent, texts = _split(monkeypatch, [0, 0.5, 31])
    assert sent == 1 and "bought <b>Yes</b> at 48¢ ($60)" in texts[-1]  # one alert per trader and market in ALERT_AGAIN_S


def test_fills_seen_before_there_was_a_price_add_into_the_next_alert_across_a_restart(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user(follows=(SAM,))
    trades: list[WhaleTrade] = []
    prices: dict[str, float] = {}  # no open position to read the price now from yet: no alert
    live_reads(monkeypatch, detail_of(trades, prices))
    t0 = datetime.now(UTC) - timedelta(minutes=5)
    engine.alert_pass(now=t0.timestamp() - 60)
    trades.append(trade(SAM, t0, 0.50, 60, "0xcid:0", "a"))
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 10) == 0
    engine._seen.clear()  # a restart
    engine._cache.clear()
    engine.alert_pass(now=t0.timestamp() + 20)  # first round after it: noted only
    prices["0xcid:0"] = 0.50
    trades.append(trade(SAM, t0 + timedelta(minutes=1), 0.50, 60, "0xcid:0", "b"))
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 90) == 1 and "($120)" in engine.tg.sent[-1]["text"]


def test_fills_from_one_transaction_all_add_into_the_alert(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Polymarket's trade id is the transaction's hash, and one transaction can fill several orders: each fill counts
    (it used to keep only the first, so the alert said $60 for a $160 buy), and reading them again adds nothing."""
    user(follows=(SAM,))
    trades: list[WhaleTrade] = []
    live_reads(monkeypatch, detail_of(trades, {"0xcid:0": 0.50}))
    t0 = datetime.now(UTC) - timedelta(minutes=5)
    engine.alert_pass(now=t0.timestamp() - 60)  # first round: noted only
    one, two = trade(SAM, t0, 0.48, 60, "0xcid:0", "tx"), trade(SAM, t0, 0.50, 100, "0xcid:0", "tx")
    assert one.trade_id == two.trade_id  # the same transaction
    trades += [one, two]
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 60) == 1
    assert "bought <b>Yes</b> at 49¢ ($160)" in engine.tg.sent[-1]["text"]  # both fills, at their average price
    again = [trade(ANN, t0, 0.48, 60, "0xcid:1", "tx"), trade(ANN, t0, 0.50, 100, "0xcid:1", "tx")]
    engine._add_up(ANN, again, t0.timestamp() + 90)
    assert engine._add_up(ANN, again, t0.timestamp() + 95)["0xcid:1"][1] == 160  # read twice, kept once


# ---- sells ----


def sold(w: str, at: datetime, price: float = 0.62, usd: float = 800, market: str = "0xcid:0", i: str = "1") -> WhaleTrade:
    return WhaleTrade("polymarket", w, None, market, "yes", "sell", price, usd / price, usd, at, f"{w}:{market}:sell:{i}",
                      title="Will it <rain>?", outcome="Yes", url="https://polymarket.com/event/rain", token=f"tok-{market}")  # fmt: skip


def test_a_sell_alerts_with_the_price_now_while_they_still_hold_some_and_only_once(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "copy_offer", lambda s: (True, None))  # copyable or not: a sell never has a copy line
    user(follows=(SAM,))
    trades: list[WhaleTrade] = []
    live_reads(monkeypatch, detail_of(trades, {"0xcid:0": 0.61}))
    t0 = datetime.now(UTC) - timedelta(minutes=6)
    engine.alert_pass(now=t0.timestamp() - 60)  # first round: noted only
    trades += [sold(SAM, t0, 0.62, 500, i="a"), sold(SAM, t0, 0.62, 300, i="b")]  # two fills, one alert
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 6 * 60) == 1
    m = engine.tg.sent[-1]
    lines = m["text"].splitlines()
    assert lines[:4] == ["<b>Sam</b> sold <b>Yes</b> at 62¢ ($800)", "Will it &lt;rain&gt;?", "Now 61¢. They still hold some.",
                         f"Traded {t0:%H:%M} UTC, 6 min before this alert."]  # fmt: skip
    assert lines[4] == engine.TRADED_INTL
    assert ">Check current price</a>" in lines[5] and ">Their record</a>" in lines[5] and ">Open on Polymarket International</a>" in lines[5]
    assert lines[6] == engine.FOLLOW_DELAY and engine.COPY_LINE not in m["text"] and "get in" not in m["text"]
    assert m["reply_markup"]["inline_keyboard"][1][0]["callback_data"] == f"unf:{SAM}"  # Stop alerts from Sam
    assert engine._sent_today("user_1") == 1  # counts towards the daily cap
    trades.append(sold(SAM, t0 + timedelta(minutes=1), i="c"))  # sold more soon after: no second alert
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 7 * 60) == 0
    engine._seen.clear()  # a restart
    engine._alerted.clear()
    engine._cache.clear()
    engine.alert_pass(now=t0.timestamp() + 8 * 60)  # first round after it: noted only
    trades.append(sold(SAM, t0 + timedelta(minutes=2), i="d"))
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 9 * 60) == 0  # alerts.db remembers the sell


def test_selling_the_rest_soon_after_a_partial_sell_still_alerts_once(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """After "They still hold some.", a sale that leaves them none within ALERT_AGAIN_S still goes out (it isn't
    left out with the other sells), once in that time, and a restart doesn't send it again."""
    user(follows=(SAM,))
    trades: list[WhaleTrade] = []
    prices = {"0xcid:0": 0.61}
    live_reads(monkeypatch, detail_of(trades, prices))
    t0 = datetime.now(UTC) - timedelta(minutes=6)
    engine.alert_pass(now=t0.timestamp() - 60)  # first round: noted only
    trades.append(sold(SAM, t0, 0.62, 500, i="a"))
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 6 * 60) == 1
    assert "They still hold some." in engine.tg.sent[-1]["text"]
    trades.append(sold(SAM, t0 + timedelta(minutes=2), 0.60, 300, i="b"))  # sold the rest
    prices.clear()
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 8 * 60) == 1
    lines = engine.tg.sent[-1]["text"].splitlines()
    assert lines[:3] == ["<b>Sam</b> sold <b>Yes</b> at 60¢ ($300)", "Will it &lt;rain&gt;?", engine.EXITED]
    trades.append(sold(SAM, t0 + timedelta(minutes=3), 0.60, 100, i="c"))  # bought back and sold again: left out
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 9 * 60) == 0
    engine._seen.clear()  # a restart
    engine._alerted.clear()
    engine._cache.clear()
    engine.alert_pass(now=t0.timestamp() + 10 * 60)  # first round after it: noted only
    trades.append(sold(SAM, t0 + timedelta(minutes=4), 0.60, 100, i="d"))
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 11 * 60) == 0  # alerts.db remembers the exit
    assert len(engine.tg.sent) == 2


def test_a_sell_of_everything_says_so_and_never_makes_up_a_price(api: TestClient) -> None:
    t = sold(SAM, datetime(2026, 10, 13, 14, 2, tzinfo=UTC))
    at = t.at.timestamp()
    text = engine._sell_text(SAM, t, 0.62, 800, False, None, at, at + 30)
    lines = text.splitlines()
    assert lines[2:4] == ["They no longer hold it.", "Traded 14:02 UTC, under a minute before this alert."]
    assert lines[4] == engine.TRADED_INTL and "Now" not in text and "Check current price" not in text
    assert "Open on Polymarket International" in lines[5]
    held = engine._sell_text(SAM, t, 0.62, 800, True, None, at, at + 30)  # still hold some, but no price to show
    assert "They still hold some." in held.splitlines() and "Now" not in held and "Check current price" in held


@pytest.mark.parametrize(("paid", "now"), [(0.99, 0.99), (0.50, 0.01), (0.01, None)])
def test_no_alert_for_a_sell_at_a_settled_price(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, paid: float, now: float | None
) -> None:
    user(follows=(SAM,))
    trades: list[WhaleTrade] = []
    live_reads(monkeypatch, detail_of(trades, {} if now is None else {"0xcid:0": now}))
    engine.alert_pass()
    trades.append(sold(SAM, datetime.now(UTC), paid))
    engine._cache.clear()
    assert engine.alert_pass() == 0


# ---- alerts from the Feed's poller: followed traders first, however many, and no trade missed by scheduling ----


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def test_with_hundreds_followed_every_buy_still_alerts_once_within_a_round_and_alerts_add_no_reads(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """300 followed traders (more than one 30 s round holds at 0.25 s a read) and 100 Recommended ones: everyone
    followed is read every round before any Recommended trader, Recommended still gets its floor, each new buy is
    alerted exactly once by the alert pass after the round that read it, and the alert pass reads nothing itself."""
    clock = _Clock()
    monkeypatch.setattr(engine.time, "monotonic", clock)
    followed = [f"0x{i:040x}" for i in range(300)]
    rec = [f"0x{i:040x}" for i in range(10**6, 10**6 + 100)]
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": [
        {"wallet": w, "name": "R", "segment": "proven", "label": "p", "copyable": "no", "copy": None} for w in rec]})  # fmt: skip
    user(follows=tuple(followed))
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: [])
    old = datetime.now(UTC) - timedelta(days=3)
    trades = {w: [trade(w, old, i="old")] for w in followed + rec}
    reads: list[tuple[str, str]] = []

    def read(kind: str, w: str) -> None:
        reads.append((kind, w))
        clock.t += 0.25

    monkeypatch.setattr(engine.reads.whales, "trades", lambda venue, w, **k: read("trades", w) or list(trades[w]))
    monkeypatch.setattr(engine, "_open_positions", lambda w, markets=None: read("positions", w) or [
        WhalePosition(t.market, None, "Yes", 100, 0.49, 0.50, None, None) for t in trades[w]])  # fmt: skip
    buys = {followed[0]: 2, followed[150]: 3, followed[-1]: 3, followed[77]: 5}  # who buys, in which round
    placed: dict[str, float] = {}
    for rnd in range(8):
        for w, at_round in buys.items():
            if rnd == at_round:
                trades[w].insert(0, trade(w, datetime.now(UTC), market=f"0xm{w[-4:]}:0", i=f"new{rnd}"))  # own market: no group
                placed[w] = clock.t
        start, n = clock.t, len(reads)
        engine.live_poll_once()
        got = [w for kind, w in reads[n:] if kind == "trades"]
        first_rec = next((i for i, w in enumerate(got) if w in rec), len(got))
        assert set(got[:first_rec]) == set(followed) and len(got) - first_rec >= engine.LIVE_REC_FLOOR
        n, before = len(reads), set(engine._alerted)
        engine.alert_pass()
        assert len(reads) == n  # the alert pass reads what the poller kept
        assert {w for w, _ in set(engine._alerted) - before} == {w for w, r in buys.items() if r == rnd}  # this round's
        clock.t = max(clock.t, start + engine.LIVE_POLL_S)
    assert len(engine.tg.sent) == len(buys)  # every buy once, none missed, none twice


def test_more_trades_between_reads_than_one_read_holds_reads_further_back_so_none_is_missed(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user(follows=(SAM,))
    old = datetime.now(UTC) - timedelta(days=3)
    trades = [trade(SAM, old, i="old")]
    asked: list[int] = []
    monkeypatch.setattr(engine.reads.whales, "trades", lambda venue, w, limit=50, **k: asked.append(limit) or trades[:limit])
    monkeypatch.setattr(engine, "_open_positions", lambda w, markets=None: [WhalePosition(f"0xm{i}:0", None, "Yes", 1, 0.49, 0.50, None, None)
                                                              for i in range(150)])  # fmt: skip
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: [])
    engine.live_poll_once()
    engine.alert_pass()  # noted
    trades[:0] = [trade(SAM, datetime.now(UTC), market=f"0xm{i}:0", i=str(i)) for i in range(150)]  # a burst of 150 fills
    hit = engine._cache[f"live:{SAM}"]
    engine._cache[f"live:{SAM}"] = (hit[0] - 60, hit[1])
    asked.clear()
    engine.live_poll_once()
    assert asked == [engine.LIVE_TRADES, engine.LIVE_GAP_TRADES]  # none of the last 100 was kept: read further back
    engine.alert_pass()
    assert len({m for (w, m) in engine._alerted if w == SAM}) == 150  # every market of the burst, not only 100


def test_a_rate_limit_ends_the_pollers_round(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM, ANN, BOB))
    asked: list[str] = []

    def trades(venue: str, w: str, **k: Any) -> list[WhaleTrade]:
        asked.append(w)
        if len(asked) == 2:
            raise engine.VenueError("rate_limited", "polymarket said too many requests.", venue="polymarket", status=429)
        return []

    monkeypatch.setattr(engine.reads.whales, "trades", trades)
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: [])
    assert engine.live_poll_once() == 1 and len(asked) == 2  # the third waits for the next round
    assert engine.live_poll_once() == 3 and asked[2:] == [ANN, BOB, SAM]  # the ones not read yet first


def test_a_trader_with_more_open_positions_than_one_read_holds_still_gets_every_buy_and_sell_alerted(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """60 open positions, the 50 biggest read first: a $1 buy in a market outside them still alerts with its price
    now, and a sell there says they still hold some (never "no longer hold it"), from one more read of just the
    markets they traded; a trader whose trades are all in their 50 biggest costs no extra read."""
    user(follows=(SAM,))

    def pos(cid: str, size: float, side: int = 0) -> dict[str, Any]:
        return {"conditionId": cid, "outcomeIndex": side, "size": size, "curPrice": 0.4, "avgPrice": 0.39,
                "currentValue": size * 0.4, "redeemable": False, "outcome": "Yes", "title": cid}  # fmt: skip

    book = [pos(f"0xbig{i}", 1000 - i) for i in range(50)] + [pos("0xsmall", 3), pos("0xsold", 2), pos("0xbig2", 1, 1)]
    asked: list[Any] = []

    def positions(wallet: str, limit: int = 50, market: str | None = None) -> list[dict[str, Any]]:
        if wallet == SAM:  # the others here are Recommended: read too, not what this is about
            asked.append((limit, market))
        rows = book if market is None else [p for p in book if p["conditionId"] in market.split(",")]
        return rows[:limit]

    monkeypatch.setattr(engine.reads.whales._poly, "positions", positions)
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: [])
    trades: list[WhaleTrade] = [trade(SAM, datetime.now(UTC) - timedelta(days=3), market="0xbig0:0", i="old")]
    monkeypatch.setattr(engine.reads.whales, "trades", lambda venue, w, **k: list(trades))
    engine.live_poll_once()
    engine.alert_pass()  # noted
    now = datetime.now(UTC)
    trades[:0] = [trade(SAM, now, 0.40, 1, "0xsmall:0", "s1"), sold(SAM, now, 0.41, 20, "0xsold:0", "x1"),
                  trade(SAM, now, 0.40, 50, "0xbig3:0", "b3"), trade(SAM, now, 0.40, 2, "0xbig2:1", "o1")]  # fmt: skip
    asked.clear()
    engine._cache.clear()
    engine.live_poll_once()
    # one more read, of just what the 50 biggest left out: two markets, and the other side of one they hold
    assert asked == [(50, None), (500, "0xsmall,0xsold,0xbig2")]
    assert engine.alert_pass() == 4
    text = "\n".join(m["text"] for m in engine.tg.sent)
    assert "($1)" in text and "Now 40¢" in text  # the $1 buy, with its price now
    assert "They still hold some." in text and "no longer hold" not in text
    trades.insert(0, trade(SAM, datetime.now(UTC), 0.40, 70, "0xbig7:0", "b7"))
    asked.clear()
    engine._cache.clear()
    engine.live_poll_once()
    assert asked == [(50, None), (500, "0xsmall,0xsold,0xbig2")]  # still traded lately: still read with them
    trades[:] = [trade(SAM, datetime.now(UTC), 0.40, 70, "0xbig8:0", "b8")]
    asked.clear()
    engine._cache.clear()
    engine.live_poll_once()
    assert asked == [(50, None)]  # all in their 50 biggest: no extra read


# ---- a restart (a deploy) neither skips nor repeats an alert ----


def restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a restart forgets: everything in memory. alerts.db (the store's volume) stays."""
    engine._cache.clear()
    for d in (engine._seen, engine._alerted, engine._whale_alerted, engine._live_priced, engine._live_failed):
        d.clear()
    monkeypatch.setattr(engine, "_whale", {"started": False, "seen": set()})
    engine._retry_state["started"] = False


def test_a_trade_made_while_the_engine_restarts_is_alerted_once_after(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    trades = [trade(SAM, datetime.now(UTC) - timedelta(days=3), i="old")]
    live_reads(monkeypatch, detail_of(trades, {"0xcid:0": 0.50, "0xcid:1": 0.50, "0xsold:0": 0.40}))
    assert engine.alert_pass() == 0  # first round: noted only
    restart(monkeypatch)
    trades[:0] = [trade(SAM, datetime.now(UTC), 0.49, 300, "0xcid:1", "during"),
                  sold(SAM, datetime.now(UTC), 0.41, 90, "0xsold:0", "s")]  # fmt: skip
    assert engine.alert_pass() == 2  # the buy and the sell made while it was down: alerted, not just noted
    assert sorted(m["text"].split("</b> ")[1].split()[0] for m in engine.tg.sent) == ["bought", "sold"]
    restart(monkeypatch)
    assert engine.alert_pass() == 0  # and after another restart, never again
    engine._cache.clear()
    assert engine.alert_pass() == 0


def test_an_alert_sent_just_before_a_crash_isnt_sent_again(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    trades = [trade(SAM, datetime.now(UTC) - timedelta(days=3), i="old")]
    live_reads(monkeypatch, detail_of(trades, {"0xcid:0": 0.50}))
    engine.alert_pass()
    keep = engine._seen_keep
    engine._seen_keep = lambda *a, **k: None  # it went down after the alert went out, before keeping the round
    try:
        trades.insert(0, trade(SAM, datetime.now(UTC), i="new"))
        engine._cache.clear()
        assert engine.alert_pass() == 1
    finally:
        engine._seen_keep = keep
    restart(monkeypatch)
    assert engine.alert_pass() == 0 and len(engine.tg.sent) == 1  # alerts.db already has it: one message, ever


def test_after_a_restart_a_new_follow_is_still_only_noted_and_an_unfollow_is_forgotten(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user(follows=(SAM,))
    trades = [trade(SAM, datetime.now(UTC) - timedelta(days=3), i="old"), trade(ANN, datetime.now(UTC), i="ann")]
    live_reads(monkeypatch, detail_of(trades, {"0xcid:0": 0.50}))
    engine.alert_pass()
    restart(monkeypatch)
    user(follows=(SAM, ANN))  # ANN followed while it was down: nobody watched her before
    assert engine.alert_pass() == 0  # her trade from before the follow is only noted
    user(follows=(ANN,))  # SAM unfollowed
    engine._cache.clear()
    engine.alert_pass()
    with engine._db() as con:
        assert {r[0] for r in con.execute("SELECT trader FROM watched")} == {ANN}
    trades.insert(0, trade(SAM, datetime.now(UTC), i="later"))
    restart(monkeypatch)
    user(follows=(SAM, ANN))  # following SAM again starts fresh: what he did meanwhile is only noted
    assert engine.alert_pass() == 0


def test_a_restart_longer_than_the_alert_window_doesnt_alert_stale_trades(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    trades = [trade(SAM, datetime.now(UTC) - timedelta(days=3), i="old")]
    live_reads(monkeypatch, detail_of(trades, {"0xcid:0": 0.50}))
    engine.alert_pass()
    restart(monkeypatch)
    trades.insert(0, trade(SAM, datetime.now(UTC) - timedelta(seconds=engine.ALERT_MAX_AGE_S + 60), i="stale"))
    assert engine.alert_pass() == 0  # first seen over 30 minutes after it was made: as without a restart


def test_more_trades_while_down_than_one_read_holds_are_read_further_back(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    trades = [trade(SAM, datetime.now(UTC) - timedelta(days=3), i="old")]
    asked: list[int] = []
    monkeypatch.setattr(engine.reads.whales, "trades", lambda venue, w, limit=50, **k: asked.append(limit) or trades[:limit])
    monkeypatch.setattr(engine, "_open_positions", lambda w, markets=None: [
        WhalePosition(f"0xm{i}:0", None, "Yes", 1, 0.49, 0.50, None, None) for i in range(150)])  # fmt: skip
    engine.alert_pass()
    restart(monkeypatch)
    trades[:0] = [trade(SAM, datetime.now(UTC), market=f"0xm{i}:0", i=str(i)) for i in range(150)]
    asked.clear()
    engine.alert_pass()
    assert engine.LIVE_GAP_TRADES in asked  # none of the last 100 was seen before: read further back
    assert len({m for (w, m) in engine._alerted if w == SAM}) == 150  # every one of them (the daily cap limits messages)


def test_big_trades_carry_on_after_a_restart_too(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user("politics", "1", follows=(), interests=["Politics"])
    rows: list[WhaleTrade] = [big(WHALE, i="0")]
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda usd, **k: list(rows))
    held = [WhalePosition(m, None, "Yes", 1, 0.49, 0.50, None, None) for m in ("0xw:0", "0xw2:0")]
    monkeypatch.setattr(engine, "_live_read", lambda w, *a, **k: TraderDetail(
        Trader(venue="polymarket", id=w, name=None), "visible", {}, held, []))  # fmt: skip
    assert engine.alert_pass() == 0  # first round: noted
    restart(monkeypatch)
    rows.insert(0, big(WHALE, market="0xw2:0", i="during"))
    assert engine.alert_pass() == 1  # made while it was down: alerted
    restart(monkeypatch)
    assert engine.alert_pass() == 0  # never twice


class _Crash(BaseException):
    """The process dying (a deploy's kill, an out-of-memory): nothing after it runs, not even ``except Exception``."""


@pytest.mark.parametrize("when", ["after_telegram_took_it", "before_telegram_was_asked"])
def test_a_crash_mid_send_never_sends_an_alert_twice(api: TestClient, monkeypatch: pytest.MonkeyPatch, when: str) -> None:
    """Telegram can't tell a repeat, so the guarantee is at most once. The engine dies mid-send, for a buy, a sell
    and a big trade: after Telegram took the message (it reached the chat, nothing after was recorded), or just
    before asking (it never did). Restarted, the next rounds see the same trades as new, and send none of them
    again: a crash can lose an alert, never repeat one."""
    user(follows=(SAM,), interests=["Politics"])
    trades = [trade(SAM, datetime.now(UTC) - timedelta(days=3), i="old")]
    prices = {"0xcid:0": 0.50, "0xsold:0": 0.40, "0xw:0": 0.50}
    live_reads(monkeypatch, detail_of(trades, prices))
    rows: list[WhaleTrade] = []
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda usd, **k: list(rows))
    real = engine.tg
    assert engine.alert_pass() == 0  # noted
    now = datetime.now(UTC)
    trades[:0] = [trade(SAM, now, i="b"), sold(SAM, now, 0.41, 90, "0xsold:0", "s")]
    rows.insert(0, big(WHALE, market="0xw:0", i="w"))

    def dies(method: str, **p: Any) -> Any:
        if method != "sendMessage":
            return real(method, **p)
        if when == "after_telegram_took_it":
            real(method, **p)  # it reached the chat...
        raise _Crash  # ...and the engine died before anything after it

    for _ in range(3):  # the buy, the sell, the big trade: each one killing the engine in turn
        monkeypatch.setattr(engine, "tg", dies)
        engine._cache.clear()
        with pytest.raises(_Crash):
            engine.alert_pass()
        restart(monkeypatch)
        monkeypatch.setattr(engine, "tg", real)
    engine._cache.clear()
    engine.alert_pass()
    engine._cache.clear()
    engine.alert_pass()
    texts = [m["text"] for m in real.sent]
    assert len(texts) == len(set(texts))  # never the same alert twice
    kinds = sorted("big" if t.startswith("<b>Big trade") else "sold" if "</b> sold " in t else "bought" for t in texts)
    if when == "after_telegram_took_it":
        assert kinds == ["big", "bought", "sold"]  # each reached the chat exactly once
    else:
        assert kinds == []  # never sent, and not sent later either: lost, not repeated


# ---- delivery: a temporary Telegram failure is tried again, a chat that's gone is disconnected ----


class FlakyTelegram(Telegram):
    """Telegram that fails the next ``failures`` sendMessage calls with ``error`` (after which it delivers)."""

    def __init__(self, error: Exception, failures: int = 1) -> None:
        super().__init__()
        self.error, self.failures, self.tried = error, failures, 0

    def __call__(self, method: str, **params: Any) -> Any:
        if method == "sendMessage":
            self.tried += 1
            if self.failures:
                self.failures -= 1
                raise self.error
        return super().__call__(method, **params)


def _owner_metrics(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """/api/metrics as the owner sees it locally (same machine, not hosted, no key)."""
    monkeypatch.setattr(engine, "METRICS_KEY", "")
    monkeypatch.setattr(engine, "HOSTED", False)
    return TestClient(engine.app, client=("127.0.0.1", 50000)).get("/api/metrics").json()  # type: ignore[no-any-return]


def _state(aid: str) -> tuple[Any, ...]:
    with engine._db() as con:
        return tuple(con.execute("SELECT state, tries, next_at, error FROM alerts WHERE id = ?", (aid,)).fetchone())


def _one_alert(monkeypatch: pytest.MonkeyPatch, tg: Any) -> str | None:
    user(follows=(SAM,))
    monkeypatch.setattr(engine, "tg", tg)
    return engine._send("user_1", "555", "buy", lambda aid: f"alert {aid}", trader=SAM, outcome="o", trade_at=time.time())


@pytest.mark.parametrize("error, wait", [
    (engine.TelegramError("Telegram: Too Many Requests: retry after 7", 429, 7.0), 7.0),  # as long as it asks
    (engine.TelegramError("Telegram couldn't be reached (ConnectionRefusedError)"), engine.RETRY_S[0]),  # never got it
])  # fmt: skip
def test_a_temporary_failure_is_tried_again_after_a_wait_and_sent_once(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception, wait: float
) -> None:
    tg = FlakyTelegram(error)
    t0 = time.time()
    assert _one_alert(monkeypatch, tg) is None  # not now...
    with engine._db() as con:
        aid = con.execute("SELECT id FROM alerts").fetchone()[0]
    state, tries, next_at, err = _state(aid)
    assert (state, tries) == ("retry", 1) and next_at == pytest.approx(t0 + wait, abs=2) and err == str(error)
    assert engine._retry_due(t0 + wait - 2) == 0 and tg.tried == 1  # not before its time
    assert engine._retry_due(t0 + wait + 1) == 1 and _state(aid)[:2] == ("sent", 2)  # ...then once
    assert engine._retry_due(t0 + wait + 600) == 0 and len(tg.sent) == 1
    assert _owner_metrics(monkeypatch)["alerts"]["delivery"] | {"failed_why": {}} == {
        "sent": 1, "sent_after_retry": 1, "waiting": 0, "failed": 0, "failed_why": {}, "unknown": 0, "chats_disconnected": 0}


def test_a_waiting_alert_survives_a_restart_and_is_sent_once_after(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    tg = FlakyTelegram(engine.TelegramError("Telegram: Too Many Requests: retry after 20", 429, 20.0))
    _one_alert(monkeypatch, tg)
    restart(monkeypatch)
    assert engine._retry_due(time.time() + 31) == 1  # its message was kept: sent as it was meant
    assert [m["text"].startswith("alert ") for m in tg.sent] == [True] and tg.sent[0]["reply_markup"]["inline_keyboard"]
    restart(monkeypatch)
    assert engine._retry_due(time.time() + 3600) == 0 and len(tg.sent) == 1


def test_a_retry_that_would_arrive_too_late_fails_and_frees_its_place_in_the_cap(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tg = FlakyTelegram(engine.TelegramError("Telegram couldn't be reached (gaierror)"), failures=99)
    _one_alert(monkeypatch, tg)
    assert engine._sent_today("user_1") == 1  # on its way: it counts
    t0 = t = time.time()
    while t < t0 + engine.ALERT_MAX_AGE_S + 300:  # keeps failing, every try, for over 30 minutes
        t += engine.RETRY_S[-1] + 1
        engine._retry_due(t)
    with engine._db() as con:
        state, err = con.execute("SELECT state, error FROM alerts").fetchone()
    assert state == "failed" and "too late" in err and tg.sent == []  # never more than 30 minutes after: not news
    assert engine._sent_today("user_1") == 0  # it never went out: today's 20 are still all there


@pytest.mark.parametrize("error", [
    engine.TelegramError("Telegram: Bad Gateway", 502, maybe_sent=True),
    engine.TelegramError("Telegram didn't answer (TimeoutError)", maybe_sent=True),  # the request went out
    engine.TelegramError("Telegram's answer couldn't be read", maybe_sent=True),
])  # fmt: skip
def test_when_telegram_may_have_taken_it_its_recorded_as_unknown_never_sent_again(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A 5xx, or no answer after the request went out: it may be in the chat already, and Telegram can't tell a
    repeat. So it's never asked again (no duplicate), and it's recorded as unknown with why (not silently lost);
    it counts toward the day's 20, as it may have arrived."""
    tg = FlakyTelegram(error)
    assert _one_alert(monkeypatch, tg) is None
    with engine._db() as con:
        state, err = con.execute("SELECT state, error FROM alerts").fetchone()
    assert state == "unknown" and err == str(error)
    assert engine._retry_due(time.time() + 3600) == 0 and tg.tried == 1  # never asked again
    assert engine._sent_today("user_1") == 1
    d = _owner_metrics(monkeypatch)["alerts"]["delivery"]
    assert (d["unknown"], d["sent"], d["failed"], d["waiting"]) == (1, 0, 0, 0)


def test_tg_says_whether_telegram_may_have_taken_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """How each way a call can fail is read: urllib wraps a failure to connect or send in URLError (Telegram never
    got it: safe to try again), and raises one while waiting for the answer as it is (it may have: never again)."""
    import io
    import socket
    import urllib.error

    real = engine.tg  # the fixture-free module function

    def failing(exc: BaseException | None = None, body: bytes = b"") -> Any:
        def urlopen(req: Any, timeout: float = 0) -> Any:
            if exc is not None:
                raise exc
            return io.BytesIO(body)
        return urlopen

    def http(code: int, body: dict[str, Any]) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))  # type: ignore[arg-type]

    cases = {
        "refused": urllib.error.URLError(ConnectionRefusedError()),
        "dns": urllib.error.URLError(socket.gaierror()),
        "connect timeout": urllib.error.URLError(TimeoutError()),
        "read timeout": TimeoutError(),
        "reset after send": ConnectionResetError(),
        "429": http(429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 9",
                          "parameters": {"retry_after": 9}}),  # fmt: skip
        "502": http(502, {"ok": False, "error_code": 502, "description": "Bad Gateway"}),
        "403": http(403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}),
        "400": http(400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}),
    }
    got = {}
    for name, exc in cases.items():
        monkeypatch.setattr(engine.urllib.request, "urlopen", failing(exc))
        with pytest.raises(engine.TelegramError) as e:
            real("sendMessage", chat_id="1", text="x")
        got[name] = (e.value.temporary, e.value.maybe_sent, e.value.chat_gone, e.value.retry_after)
    monkeypatch.setattr(engine.urllib.request, "urlopen", lambda req, timeout=0: _Ctx(b"<html>oops</html>"))
    with pytest.raises(engine.TelegramError) as e:
        real("sendMessage", chat_id="1", text="x")
    got["200 unreadable"] = (e.value.temporary, e.value.maybe_sent, e.value.chat_gone, e.value.retry_after)
    #                    try again, may have sent, chat gone, wait
    assert got == {"refused": (True, False, False, None), "dns": (True, False, False, None),
                   "connect timeout": (True, False, False, None),
                   "read timeout": (False, True, False, None), "reset after send": (False, True, False, None),
                   "429": (True, False, False, 9.0), "502": (False, True, False, None),
                   "403": (False, False, True, None), "400": (False, False, False, None),
                   "200 unreadable": (False, True, False, None)}  # fmt: skip


class _Ctx:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_a_chat_that_blocked_the_bot_is_disconnected_not_retried(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    tg = FlakyTelegram(engine.TelegramError("Telegram: Forbidden: bot was blocked by the user", 403), failures=99)
    _one_alert(monkeypatch, tg)
    assert _owner_metrics(monkeypatch)["alerts"]["delivery"]["failed"] == 1
    assert engine._load_follows()["users"]["user_1"]["chat"] is None  # the page offers "Connect Telegram" again
    assert engine._load_follows()["users"]["user_1"]["follows"] == [SAM]  # following stays as it was
    assert engine._retry_due(time.time() + 3600) == 0 and tg.tried == 1  # never asked again
    assert _owner_metrics(monkeypatch)["alerts"]["delivery"]["chats_disconnected"] == 1
    assert engine.alert_pass() == 0  # and no more rounds for a chat that's gone


def test_a_message_telegram_refuses_for_good_fails_alone(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    tg = FlakyTelegram(engine.TelegramError("Telegram: Bad Request: can't parse entities", 400))
    _one_alert(monkeypatch, tg)
    with engine._db() as con:
        assert con.execute("SELECT state FROM alerts").fetchone()[0] == "failed"
    assert engine._load_follows()["users"]["user_1"]["chat"] == "555"  # the chat is fine: only that message failed
    assert engine._retry_due(time.time() + 3600) == 0 and engine._sent_today("user_1") == 0


def test_retries_never_take_anyone_past_the_daily_cap(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    day = {"d": "2026-10-13"}
    monkeypatch.setattr(engine, "_today", lambda now=None: day["d"])
    user()
    tg = FlakyTelegram(engine.TelegramError("Telegram: Too Many Requests: retry after 5", 429, 5.0), failures=3)
    monkeypatch.setattr(engine, "tg", tg)
    for i in range(engine.DAILY_CAP + 3):
        engine._send("user_1", "555", "buy", lambda aid, i=i: f"alert {i} {aid}")
    engine._retry_due(time.time() + 10)
    alerts = [m for m in tg.sent if m["text"].startswith("alert ")]
    assert len(alerts) == engine.DAILY_CAP  # the three that failed first went out later, within the cap
    assert [m["text"][: len(CAP)] for m in tg.sent if not m["text"].startswith("alert ")] == [CAP]


def test_a_crash_after_an_alert_was_kept_but_before_telegram_was_asked_sends_it_once_after(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third point an engine can die mid-send: the alert is kept (queued) but Telegram wasn't asked. After the
    restart it is sent, once, as it was meant; the round's trades seen again don't make a second one."""
    user(follows=(SAM,))
    trades = [trade(SAM, datetime.now(UTC) - timedelta(days=3), i="old")]
    live_reads(monkeypatch, detail_of(trades, {"0xsold:0": 0.40}))
    engine.alert_pass()
    trades.insert(0, sold(SAM, datetime.now(UTC), 0.41, 90, "0xsold:0", "s"))
    attempt = engine._attempt

    def dies(*a: Any, **k: Any) -> bool:
        raise _Crash

    monkeypatch.setattr(engine, "_attempt", dies)
    engine._cache.clear()
    with pytest.raises(_Crash):
        engine.alert_pass()
    monkeypatch.setattr(engine, "_attempt", attempt)
    restart(monkeypatch)
    engine.alert_pass()
    engine._cache.clear()
    engine.alert_pass()
    assert [m["text"].split("</b> ")[1].split()[0] for m in engine.tg.sent] == ["sold"]  # once


def test_an_alerts_db_from_before_delivery_states_keeps_its_alerts_as_sent(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hosted alerts.db has alerts without a delivery state: opened, it gets the columns, and those alerts
    count as sent (the daily cap, the metrics), as they were."""
    import sqlite3

    engine._db_ready.discard(str(engine.alerts_db()))
    con = sqlite3.connect(engine.alerts_db())
    con.execute("CREATE TABLE alerts (id TEXT PRIMARY KEY, uid TEXT NOT NULL, chat TEXT, kind TEXT NOT NULL, trader TEXT,"
                " outcome TEXT, message_id INTEGER, sent_at REAL NOT NULL, trade_at REAL, day TEXT NOT NULL, trigger_at REAL)")
    con.execute("INSERT INTO alerts VALUES ('old1', 'user_1', '555', 'buy', ?, 'o', 7, ?, ?, ?, NULL)",
                (SAM, time.time(), time.time() - 60, engine._today()))  # fmt: skip
    con.commit()
    con.close()
    assert engine._sent_today("user_1") == 1
    with engine._db() as c:
        assert {"state", "tries", "queued_at", "next_at", "error", "body"} <= {r[1] for r in c.execute("PRAGMA table_info(alerts)")}
    assert engine._retry_due() == 0  # nothing of it is sent again
    m = _owner_metrics(monkeypatch)
    assert m["alerts"]["sent"] == 1 and m["alerts"]["delivery"]["sent"] == 1


# ---- every trade by default; per trader: only bets over an amount, or a summary; busy traders get a warning ----


def _buys(w: str, n: int, start: int = 0) -> list[int]:
    """``n`` buys by ``w``, each of a different outcome (so none is grouped or left out as a repeat)."""
    t0 = datetime.now(UTC)
    return [engine._deliver("user_1", "555", new_buy(w, t0, market=f"0x{w[2]}{i}:0"), "Politics") for i in range(start, start + n)]


def _held(uid: str = "user_1") -> list[tuple[Any, ...]]:
    with engine._db() as con:
        return con.execute("SELECT trader, outcome, side, usd FROM held WHERE uid = ? ORDER BY rowid", (uid,)).fetchall()


def _choose(uid: str, w: str, mode: str, usd: float | None = None) -> None:
    d = engine._load_follows()
    d["users"][uid].setdefault("alert_for", {})[w] = {"mode": mode} | ({"usd": usd} if usd else {})
    engine._save_follows(d)


def test_every_trade_of_a_busy_trader_alerts_with_no_limit_per_trader(api: TestClient) -> None:
    user()
    assert _buys(SAM, 12) == [1] * 12 and _held() == []
    assert not any("alerts from Sam today" in m["text"] for m in engine.tg.sent)
    assert engine.DAILY_CAP == 200 and not hasattr(engine, "TRADER_DAILY")


def test_the_limit_message_says_when_alerts_resume() -> None:
    at = datetime(2026, 10, 13, 21, 40, 30, tzinfo=UTC).timestamp()
    text = engine.cap_text(at)
    assert text.startswith(CAP + "2 h 19 min.") and "still in your Feed" in text
    assert engine.cap_text(datetime(2026, 10, 13, 23, 55, tzinfo=UTC).timestamp()).startswith(CAP + "5 min.")


def test_what_one_trade_does_for_each_choice() -> None:
    u: dict[str, Any] = {"alert_for": {SAM: {"mode": "over", "usd": 1500}, ANN: {"mode": "summary"}}}
    assert engine._wants(u, BOB, 5) == "send"  # every trade by default
    assert engine._wants(u, SAM, 1499) == "skip" and engine._wants(u, SAM, 1500) == "send"
    assert engine._wants(u, ANN, 50_000) == "hold"
    assert engine._wants(u, SAM, 10, told=True) == "send" and engine._wants(u, ANN, 10, told=True) == "send"


def test_only_bets_over_an_amount_and_a_sale_you_were_told_about_still_comes(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user(follows=(SAM,))
    _choose("user_1", SAM, "over", 1500)
    trades: list[WhaleTrade] = []
    live_reads(monkeypatch, detail_of(trades, {"0xa:0": 0.5, "0xb:0": 0.5, "0xc:0": 0.5}))
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    engine.alert_pass(now=t0.timestamp() - 60)  # first round: noted only
    trades += [trade(SAM, t0, usd=1000, market="0xa:0", i="a"), trade(SAM, t0, usd=2000, market="0xb:0", i="b")]
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 60) == 1  # the $2,000 buy; the $1,000 one is only in the Feed
    assert "($2,000)" in engine.tg.sent[-1]["text"] and _held() == []
    trades += [sold(SAM, t0 + timedelta(minutes=2), usd=300, market="0xb:0"), sold(SAM, t0 + timedelta(minutes=2), usd=300, market="0xa:0")]
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 3 * 60) == 1  # the sale of 0xb, which their alert told you about
    assert "<b>Sam</b> sold" in engine.tg.sent[-1]["text"] and "($300)" in engine.tg.sent[-1]["text"]


def test_summary_only_holds_their_trades_for_one_summary_every_six_hours(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user(follows=(SAM, ANN))
    _choose("user_1", SAM, "summary")
    trades: list[WhaleTrade] = []
    live_reads(monkeypatch, detail_of(trades, {"0xa:0": 0.5, "0xb:0": 0.5}))
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    engine.alert_pass(now=t0.timestamp() - 60)
    trades += [trade(SAM, t0, usd=700, market="0xa:0", i="a"), trade(SAM, t0, usd=300, market="0xb:0", i="b")]
    engine._cache.clear()
    assert engine.alert_pass(now=t0.timestamp() + 60) == 0 and engine.tg.sent == []
    assert [(t, s, u) for t, _, s, u in _held()] == [(SAM, "buy", 700.0), (SAM, "buy", 300.0)]
    users = engine._load_follows()["users"]
    held_at = t0.timestamp() + 60  # the round that held them
    assert engine.SUMMARY_S == 6 * 3600 and engine._summaries(users, held_at + engine.SUMMARY_S - 60) == 0  # not yet
    assert engine._summaries(users, held_at + engine.SUMMARY_S + 1) == 1
    now = held_at
    lines = engine.tg.sent[-1]["text"].splitlines()
    assert lines[0].startswith("From traders you follow, since ") and lines[1] == "• <b>Sam</b>: 2 trades, 2 buys ($1,000)"
    assert lines[2] == "You chose a summary every 6 hours for these traders. Every trade is in your Feed."
    assert ">Open your Feed</a>" in lines[3] and engine._sent_today("user_1") == 1 and _held() == []  # counts once
    assert engine._summaries(users, now + 2 * engine.SUMMARY_S) == 0


def test_a_summary_past_the_daily_cap_isnt_sent_and_the_limit_message_goes_once(api: TestClient) -> None:
    user()
    engine._hold("user_1", SAM, "tok-x", "buy", 100.0, time.time())
    for _ in range(engine.DAILY_CAP):
        engine._send("user_1", "555", "buy", lambda aid: "x")
    assert engine._summaries({"user_1": {"chat": "555"}}, time.time() + engine.SUMMARY_S + 1) == 0
    assert engine.tg.sent[-1]["text"].startswith(CAP) and len(engine.tg.sent) == engine.DAILY_CAP + 1 and _held() == []


def test_a_summary_waiting_on_telegram_is_kept_across_a_restart_and_sent_once(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user()
    engine._hold("user_1", SAM, "tok-x", "buy", 100.0, time.time())
    tg = FlakyTelegram(engine.TelegramError("Telegram: Too Many Requests: retry after 20", 429, 20.0))
    monkeypatch.setattr(engine, "tg", tg)
    assert engine._summaries({"user_1": {"chat": "555"}}, time.time() + engine.SUMMARY_S + 1) == 0
    with engine._db() as con:
        assert con.execute("SELECT kind, state FROM alerts WHERE kind = 'summary'").fetchall() == [("summary", "retry")]
    assert _held() == []  # cleared with it, so it's never summed up twice
    restart(monkeypatch)
    assert engine._retry_due(time.time() + 31) == 1 and tg.sent[0]["text"].startswith("From traders you follow")
    restart(monkeypatch)
    assert engine._retry_due(time.time() + 3600) == 0 and len(tg.sent) == 1


def test_a_held_buy_since_told_by_a_group_is_left_out_of_the_summary(api: TestClient) -> None:
    user()
    t0 = datetime.now(UTC)
    engine._hold("user_1", SAM, "tok-0xg:0", "buy", 100.0, t0.timestamp() - 60)
    engine._deliver("user_1", "555", new_buy(ANN, t0, market="0xg:0"), "Politics")
    engine._deliver("user_1", "555", new_buy(BOB, t0 + timedelta(minutes=5), market="0xg:0"), "Politics")  # the group
    assert engine._summaries({"user_1": {"chat": "555"}}, time.time() + engine.SUMMARY_S + 1) == 0  # the group told you
    assert _held() == []


def test_told_means_their_buy_alert_or_a_group_with_their_buy_in_it(api: TestClient) -> None:
    user()
    now = time.time()
    _buys(SAM, 1)
    assert engine._told("user_1", SAM, "tok-0xa0:0", now) and not engine._told("user_1", ANN, "tok-0xa0:0", now)
    assert not engine._told("user_1", SAM, "tok-0xa0:0", now + engine.TOLD_S + 60)  # too long ago
    t0 = datetime.now(UTC)
    engine._deliver("user_1", "555", new_buy(ANN, t0, market="0xg:0"), "Politics")
    engine._deliver("user_1", "555", new_buy(BOB, t0, market="0xg:0"), "Politics")  # the group
    assert engine._told("user_1", BOB, "tok-0xg:0", now) and not engine._told("user_1", SAM, "tok-0xg:0", now)


def test_choosing_what_you_get_from_a_trader_on_the_page(api: TestClient) -> None:
    user(follows=(SAM,))
    r = api.put(f"/api/me/follows/{SAM}/alerts", json={"mode": "over", "usd": 1700}, headers=auth())
    assert r.status_code == 200 and r.json()["alert_for"] == {SAM: {"mode": "over", "usd": 1700.0}}
    assert r.json()["daily_cap"] == 200 and r.json()["summary_hours"] == 6
    assert api.put(f"/api/me/follows/{SAM}/alerts", json={"mode": "over"}, headers=auth()).status_code == 400  # no amount
    assert api.put(f"/api/me/follows/{SAM}/alerts", json={"mode": "over", "usd": -5}, headers=auth()).status_code == 400
    assert api.put(f"/api/me/follows/{SAM}/alerts", json={"mode": "some"}, headers=auth()).status_code == 400
    assert api.put(f"/api/me/follows/{ANN}/alerts", json={"mode": "summary"}, headers=auth()).status_code == 404  # not followed
    r = api.put(f"/api/me/follows/{SAM}/alerts", json={"mode": "all"}, headers=auth())
    assert r.json()["alert_for"] == {SAM: {"mode": "all", "usd": None}}
    api.put(f"/api/me/follows/{SAM}/alerts", json={"mode": "summary"}, headers=auth())
    api.delete(f"/api/me/follows/{SAM}", headers=auth())
    r = api.post("/api/me/follows", json={"wallet": SAM}, headers=auth())
    assert r.json()["alert_for"] == {SAM: {"mode": "all", "usd": None}}  # following again starts at every trade
    with engine._db() as con:
        assert [r[0] for r in con.execute("SELECT target FROM events WHERE kind = 'alert_mode'")] == ["over", "all", "summary"]


def _rows(*bets: tuple[float, float, float, str, str]) -> list[dict[str, Any]]:
    """/activity trade rows: (time, dollars, price, outcome token, side)."""
    return [{"timestamp": ts, "usdcSize": usd, "size": usd / p, "price": p, "asset": tok, "side": side,
             "transactionHash": f"0x{i}"} for i, (ts, usd, p, tok, side) in enumerate(bets)]  # fmt: skip


def test_a_traders_bets_add_up_as_alerts_do() -> None:
    t = 1_791_000_000.0
    buys, sells = engine._pace_bets(_rows(
        (t, 100, 0.5, "a", "BUY"), (t + 30, 50, 0.5, "a", "BUY"),  # one bet: fills within a minute add up
        (t + 300, 999, 0.5, "a", "BUY"),  # the same outcome again within 6 hours: no new bet, too late to add up
        (t + 6 * 3600, 70, 0.5, "a", "BUY"),  # 6 hours on: a new bet
        (t, 500, 0.99, "b", "BUY"),  # at a settled price: never alerted, never counted
        (t, 40, 0.6, "a", "SELL"),
    ))  # fmt: skip
    assert sorted(buys) == [70, 150] and sells == [40]


class _Activity:
    """A stand-in for the SDK's /activity reads: ``rows`` newest first, 500 a page, paged back by ``end``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.reads = sorted(rows, key=lambda r: -r["timestamp"]), 0

    def activity(self, wallet: str, *, type: str, start: int, end: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        self.reads += 1
        return [r for r in self.rows if r["timestamp"] >= start and (end is None or r["timestamp"] <= end)][:limit]


def _busy(monkeypatch: pytest.MonkeyPatch, per_day: int, now: float, big: int = 0) -> _Activity:
    """A trader who made ``per_day`` $50 bets a day for a week (each its own outcome), and ``big`` $2,000 ones a day."""
    rows = []
    for d in range(7):
        for i in range(per_day):
            rows += _rows((now - d * 86400 - i * 60 - 30, 50, 0.5, f"t{d}-{i}", "BUY"))
        for i in range(big):
            rows += _rows((now - d * 86400 - i * 60 - 45, 2000, 0.5, f"big{d}-{i}", "BUY"))
    for i, r in enumerate(rows):
        r["transactionHash"] = f"0x{i}"
    act = _Activity(rows)
    monkeypatch.setattr(engine, "background", lambda: type("W", (), {"_poly": act})())
    return act


def test_how_busy_a_trader_is_and_the_amount_suggested(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    act = _busy(monkeypatch, 100, now, big=3)
    p = engine._measure_pace(SAM, now)
    assert p["per_day"] == pytest.approx(103) and p["days"] == 7 and p["usd"] == 2000  # about 3 a day reach $2,000
    assert act.reads == 2 and engine._pace(SAM)["per_day"] == pytest.approx(103)  # 721 trades: two reads of 500
    monkeypatch.setattr(engine, "background", lambda: type("W", (), {"_poly": _Activity([])})())  # no trades
    assert engine._measure_pace(BOB, now) == {"per_day": 0, "usd": None, "days": 7, "at": now}
    assert engine._about(1679) == "$1,700" and engine._about(404) == "$400" and engine._about(37) == "$37"


def test_following_a_busy_trader_says_so_with_an_estimate_not_a_promise(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    _busy(monkeypatch, 100, time.time(), big=3)
    engine._pace_after_follow("user_1", SAM)
    m = engine.tg.sent[-1]
    assert m["text"].startswith("<b>Sam</b> is a busy trader: about 103 alerts a day over the past week.")
    assert "Based on their past week, bets of about $2,000 or more came roughly 3 times a day." in m["text"]
    assert "That's an estimate from last week, not a promise of how many alerts you'll get." in m["text"]
    assert "At most 200 alerts a day in all." in m["text"]
    keys = [[(b["text"], b["callback_data"]) for b in row] for row in m["reply_markup"]["inline_keyboard"]]
    assert keys == [[("Every trade", f"am:{SAM}:a"), ("Only bets over $2,000", f"am:{SAM}:o")],
                    [("A summary every 6 hours", f"am:{SAM}:s")]]  # fmt: skip


def test_a_quiet_trader_or_no_telegram_gets_no_warning(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    _busy(monkeypatch, 10, time.time())
    engine._pace_after_follow("user_1", SAM)  # 10 a day: not busy
    user("user_2", chat=None, follows=(ANN,))  # type: ignore[arg-type]
    _busy(monkeypatch, 100, time.time())
    engine._pace_after_follow("user_2", ANN)  # busy, but no Telegram to tell
    assert engine.tg.sent == [] and engine._pace(ANN)["per_day"] == pytest.approx(100)
    r = api.get("/api/me", headers=auth("user_2")).json()
    assert r["pace"] == {ANN: {"per_day": 100.0, "usd": 50.0, "busy": True}}  # the page shows it instead


def test_a_trader_busy_enough_to_reach_the_limit_says_so(api: TestClient) -> None:
    p = {"per_day": 506.0, "usd": 2100.0, "days": 7.0, "at": time.time()}
    text = engine._busy_text(SAM, p)
    assert "about 506 alerts a day" in text and "At their pace, every trade would reach your limit of 200 alerts a day." in text
    assert "At most 200 alerts a day in all." in engine._busy_text(SAM, p | {"per_day": 103.0})


def _tap(data: str, chat: int = 555) -> None:
    engine._on_update({"callback_query": {"id": "q", "data": data, "message": {"chat": {"id": chat}, "message_id": 1}}})


def test_fewer_alerts_under_an_alert_offers_the_choices_and_a_tap_saves_one(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    user(follows=(SAM,))
    assert engine._keyboard("abc", SAM)["inline_keyboard"][1] == [
        {"text": "Stop alerts from Sam", "callback_data": f"unf:{SAM}"}, {"text": "Fewer alerts", "callback_data": f"few:{SAM}"}]
    _busy(monkeypatch, 100, time.time(), big=3)
    engine._measure_pace(SAM)
    _tap(f"few:{SAM}")
    text = engine.tg.sent[-1]["text"]
    assert text.startswith("From <b>Sam</b> you get every trade.") and "about 103 alerts a day" in text and "not a promise" in text
    _tap(f"am:{SAM}:o")
    assert engine.tg.sent[-1]["text"] == "Done: from <b>Sam</b> you'll get only bets of $2,000 or more."
    assert engine._choice(engine._load_follows()["users"]["user_1"], SAM) == ("over", 2000.0)
    _tap(f"am:{SAM}:s")
    assert engine._choice(engine._load_follows()["users"]["user_1"], SAM) == ("summary", None)
    _tap(f"am:{SAM}:a")
    assert engine._choice(engine._load_follows()["users"]["user_1"], SAM) == ("all", None)
    n = len(engine.tg.sent)
    _tap(f"am:{ANN}:s")  # not someone this chat follows: nothing
    _tap(f"am:{SAM}:s", chat=999)  # not a connected chat: nothing
    assert len(engine.tg.sent) == n and engine._load_follows()["users"]["user_1"].get("alert_for") == {}


def test_a_trader_page_shows_the_choice_and_a_busy_traders_estimate(run_js: Any) -> None:
    got = run_js(("esc", "alertSetHtml"), f"""(() => {{
      globalThis.auth = {{ me: {{ summary_hours: 6,
        alert_for: {{ "{SAM}": {{ mode: "over", usd: 1700 }}, "{ANN}": {{ mode: "all" }} }},
        pace: {{ "{SAM}": {{ per_day: 440.2, usd: 1700, busy: true }}, "{ANN}": {{ per_day: 4, usd: null, busy: false }} }} }} }};
      return [alertSetHtml("{SAM}"), alertSetHtml("{ANN}")];
    }})()""")  # fmt: skip
    busy, quiet = got
    assert "They trade a lot: about 440 alerts a day over the past week." in busy
    assert "bets of about $1,700 or more came roughly 3 times a day. That's an estimate from last week, not a promise." in busy
    assert 'value="over" selected' in busy and 'value="1700"' in busy and "A summary every 6 hours" in busy
    assert 'data-over hidden' not in busy  # the amount shows with "only bets over"
    assert "They trade a lot" not in quiet and 'value="all" selected' in quiet and "data-over hidden" in quiet
