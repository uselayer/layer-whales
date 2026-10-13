"""The trader directory, phase 3: every wallet evaluated in the background. A quick check before a score, the
schedule (recommended traders every six hours, inactive and thin wallets less often, failures backing off), the
queue's order and its categories taking turns, one read budget for the background with retries and rate limits,
and carrying on after a restart (no network: the SDK's reads are stand-ins)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from uselayer import VenueError

from signals import app as engine
from signals.budget import COOL_S, RECOVER_S, BackgroundHttp, Budget
from signals.directory import (
    RECHECK_INACTIVE_S,
    RECHECK_INSUFFICIENT_S,
    RECHECK_SCORED_S,
    RESCORE_S,
    RETRY_S,
    Directory,
    Seen,
    next_check_at,
    quick_verdict,
)  # fmt: skip

AMY = "0x" + "a1" * 20
BEN = "0x" + "b2" * 20
CAL = "0x" + "c3" * 20
DAN = "0x" + "d4" * 20
T0 = datetime(2026, 10, 13, 15, 0, tzinfo=UTC).timestamp()
DAY = 86400


def score(wallet: str, segment: str = "rising", **kw: Any) -> dict[str, Any]:
    return {"wallet": wallet, "name": wallet, "segment": segment, "skill": "early", "copyable": "no", "bets": 12,
            "categories": [{"category": "Sports", "bets": 12}], "last_buy": "2026-10-13T14:00:00+00:00",
            "sample": [{"price": 0.4, "payout": 1}, {"price": 0.6, "payout": 0}, {"price": 0.5}]} | kw  # fmt: skip


def buys(n: int, at: float, every: float = 3600) -> list[tuple[float, bool, str]]:
    """``n`` buys in different markets, newest first, from ``at`` back."""
    return [(at - i * every, True, f"0xm{i}") for i in range(n)]


@pytest.fixture
def d(tmp_path: Any) -> Directory:
    return Directory(tmp_path / "directory.db")


# ---- the quick check ----


def test_no_buy_in_the_last_week_is_inactive_whatever_else_they_did() -> None:
    sold = [(T0 - DAY, False, "0xm1")]  # a sale yesterday isn't a buy
    assert quick_verdict(sold + buys(20, T0 - 8 * DAY), T0, read_all=True) == "inactive"
    assert quick_verdict([], T0, read_all=True) == "inactive"  # nothing at all


def test_fewer_than_eight_markets_bought_in_the_window_is_too_little_to_score() -> None:
    again = [(T0 - 60 * i, True, "0xm0") for i in range(30)]  # thirty buys of one market is one bet
    assert quick_verdict(again + buys(6, T0 - DAY), T0, read_all=True) == "insufficient"
    assert quick_verdict(buys(7, T0) + buys(5, T0 - 31 * DAY), T0, read_all=True) == "insufficient"  # older ones don't count
    assert quick_verdict(buys(8, T0), T0, read_all=True) == "eligible"


def test_a_read_cut_short_only_judges_the_time_it_covers() -> None:
    # 100 sales in the last two days, and the read stopped there: a buy before them can't be ruled out
    busy = [(T0 - i * 1700, False, f"0xm{i}") for i in range(100)]
    assert quick_verdict(busy, T0, read_all=False) == "eligible"
    # ...but 100 trades reaching back past the week with no buy among them is inactive
    slow = [(T0 - 8 * DAY - i, False, "0xm1") for i in range(100)]
    assert quick_verdict(slow, T0, read_all=False) == "inactive"
    # 3 markets in the last few hours, read cut short: the rest of the window is unknown, so it's scored
    assert quick_verdict(buys(3, T0, every=60) * 30, T0, read_all=False) == "eligible"


# ---- the schedule ----


def test_each_kind_of_wallet_is_looked_at_again_on_its_own_schedule() -> None:
    assert next_check_at("scored", "proven", T0) == T0 + RESCORE_S == T0 + 6 * 3600
    assert next_check_at("scored", "too_fast", T0) == T0 + RESCORE_S
    assert next_check_at("scored", "no_edge", T0) == T0 + RECHECK_SCORED_S
    assert next_check_at("scored", "inactive", T0) == next_check_at("scored", "no_view", T0) == T0 + RECHECK_INACTIVE_S
    assert next_check_at("insufficient", None, T0) == T0 + RECHECK_INSUFFICIENT_S
    assert next_check_at("inactive", "proven", T0) == T0 + RECHECK_INACTIVE_S
    assert RESCORE_S < RECHECK_SCORED_S < RECHECK_INSUFFICIENT_S < RECHECK_INACTIVE_S  # the quiet ones, less often
    assert [next_check_at("failed", None, T0, n) - T0 for n in (1, 2, 3, 9)] == [*RETRY_S[:3], RETRY_S[-1]]


def test_a_new_wallet_is_due_at_once_and_a_score_schedules_the_next(d: Directory) -> None:
    d.add([Seen(AMY, "search")], at=T0)
    assert d.wallet(AMY)["next_check"] == T0  # type: ignore[index]
    d.keep_scores([score(AMY, "rising")], "background", at=T0 + 10)
    assert d.wallet(AMY)["next_check"] == T0 + 10 + RESCORE_S  # type: ignore[index]
    assert d.due(10, now=T0 + 20) == []


def test_failures_back_off_and_a_score_clears_them(d: Directory) -> None:
    d.add([Seen(AMY, "search")], at=T0)
    d.score_failed(AMY, "VenueError: timeout", at=T0)
    d.score_failed(AMY, "VenueError: timeout", at=T0 + 1)
    w = d.wallet(AMY)
    assert (w["status"], w["failures"], w["next_check"]) == ("failed", 2, T0 + 1 + RETRY_S[1])  # type: ignore[index]
    d.keep_scores([score(AMY, "no_edge")], "background", at=T0 + 2)
    w = d.wallet(AMY)
    assert (w["status"], w["failures"], w["next_check"]) == ("scored", 0, T0 + 2 + RECHECK_SCORED_S)  # type: ignore[index]


def test_a_quick_check_keeps_its_verdict_and_their_last_trade_and_leaves_an_earlier_score(d: Directory) -> None:
    d.keep_scores([score(AMY, "no_edge")], "check", at=T0)
    d.quick_checked(AMY, "inactive", last_trade=T0 + 5, buys=0, at=T0 + DAY)
    w = d.wallet(AMY)
    assert (w["status"], w["next_check"], w["recent_buys"]) == ("inactive", T0 + DAY + RECHECK_INACTIVE_S, 0)  # type: ignore[index]
    assert w["last_active"] == T0 + 5 and w["segment"] == "no_edge" and d.score(AMY)["segment"] == "no_edge"  # type: ignore[index]
    d.quick_checked(BEN, "eligible", last_trade=T0, buys=12, at=T0)  # eligible: what it read, nothing else
    assert d.wallet(BEN)["status"] == "found" and d.wallet(BEN)["recent_buys"] == 12  # type: ignore[index]


def test_the_check_scoring_a_wallet_after_a_quick_check_doesnt_undo_the_quick_checks_verdict(d: Directory) -> None:
    d.quick_checked(AMY, "insufficient", last_trade=None, buys=3, at=T0 + 100)
    d.keep_scores([score(AMY, "no_edge")], "check", at=T0)  # a check that finished before the quick check
    w = d.wallet(AMY)
    assert (w["status"], w["next_check"]) == ("insufficient", T0 + 100 + RECHECK_INSUFFICIENT_S)  # type: ignore[index]


# ---- the queue ----


def test_recommended_traders_come_first_then_the_newest_found_then_the_rest(d: Directory) -> None:
    d.add([Seen(BEN, "search")], at=T0 - 3 * DAY)  # an old find, never looked at
    d.add([Seen(CAL, "search")], at=T0 - DAY)  # a newer one
    d.keep_scores([score(AMY, "proven")], "check", at=T0 - RESCORE_S - 1)  # recommended, due again
    d.keep_scores([score(DAN, "no_edge")], "check", at=T0 - RECHECK_SCORED_S - 1)  # a recheck
    got = d.due(10, now=T0)
    assert [(g["wallet"], g["kind"]) for g in got] == [(AMY, "recommended"), (CAL, "new"), (BEN, "new"), (DAN, "recheck")]
    assert d.due_counts(now=T0) == {"recommended": 1, "new": 2, "recheck": 1}
    assert [g["wallet"] for g in d.due(2, now=T0)] == [AMY, CAL]


def test_the_categories_take_turns_so_a_big_one_doesnt_hold_up_the_rest(d: Directory) -> None:
    sports = [f"0x{i:040x}" for i in range(1, 6)]
    d.add([Seen(w, "market", "Sports", category="Sports") for w in sports], at=T0 - 10)  # five Sports wallets first
    d.add([Seen(AMY, "market", "Weather", category="Weather")], at=T0 - 20)
    d.add([Seen(BEN, "leaderboard", "PNL MONTH OVERALL", rank=3)], at=T0 - 30)  # no category: Other
    got = d.due(4, now=T0)
    assert {g["category"] for g in got[:3]} == {"Sports", "Weather", "Other"}  # one of each before a second Sports
    assert got[3]["category"] == "Sports"


def test_on_leaderboard_is_passed_as_the_worth_following_check_would(d: Directory) -> None:
    d.add([Seen(AMY, "leaderboard", "PNL MONTH OVERALL", rank=900)], at=T0)  # top 1,000 of the monthly board
    d.add([Seen(BEN, "leaderboard", "PNL MONTH OVERALL", rank=1500)], at=T0)  # past it
    d.add([Seen(CAL, "leaderboard", "PNL MONTH SPORTS", category="Sports", rank=40)], at=T0)  # a category's top 50
    d.add([Seen(DAN, "leaderboard", "PNL MONTH SPORTS", category="Sports", rank=40)], at=T0 - 5 * DAY)  # long gone
    listed = {g["wallet"]: g["on_leaderboard"] for g in d.due(10, now=T0)}
    assert listed == {AMY: True, BEN: False, CAL: True, DAN: False}


def test_a_phase_2_directory_gets_a_schedule_from_what_it_already_knows(tmp_path: Any) -> None:
    path = tmp_path / "directory.db"
    old = Directory(path)
    old.add([Seen(BEN, "market", "Weather", category="Weather")], at=T0 - DAY)
    old.keep_scores([score(AMY, "proven")], "check", at=T0 - 3600)
    with sqlite3.connect(path) as con:  # as phase 2 left it: no schedule, no first category
        con.execute("UPDATE wallets SET next_check = NULL, seen_category = NULL")
    again = Directory(path)
    assert again.wallet(AMY)["next_check"] == T0 - 3600 + RESCORE_S  # type: ignore[index]  # not rescored at once
    assert again.wallet(BEN)["next_check"] == T0 - DAY and again.wallet(BEN)["seen_category"] == "Weather"  # type: ignore[index]
    assert [g["wallet"] for g in again.due(10, now=T0)] == [BEN]


# ---- the budget ----


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def test_the_budget_holds_the_rate_however_many_threads_ask() -> None:
    c = Clock()
    b = Budget(4, clock=c, sleep=c.sleep)
    for _ in range(9):
        b.take()
    assert c.t == pytest.approx(1002.0)  # 9 reads at 4 a second: the first at once, then 0.25 s apart
    threads = [threading.Thread(target=Budget(4).take) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_a_rate_limit_halves_the_rate_pauses_the_background_and_it_recovers() -> None:
    c = Clock()
    b = Budget(4, clock=c, sleep=c.sleep)
    b.take()
    b.rate_limited(None)
    assert b.state()["per_second"] == 2 and b.state()["paused_s"] == COOL_S and b.state()["rate_limited"] == 1
    b.take()
    assert c.t >= 1000 + COOL_S  # nothing reads during the pause
    b.rate_limited(90)  # Polymarket asked for longer
    assert b.state()["paused_s"] == 90 and b.state()["per_second"] == 1
    c.t += 90 + RECOVER_S
    b.take()
    assert b.state()["per_second"] == 1.5  # climbing back, a step at a time
    for _ in range(4):
        c.t += RECOVER_S
        b.take()
    assert b.state()["per_second"] == 4


class FakeHttp:
    def __init__(self, answers: list[Any]) -> None:
        self.answers, self.asked = answers, []

    def request_with_headers(self, method: str, url: str, **kw: Any) -> Any:
        self.asked.append((url, kw.get("retries")))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a, {}


def limited(wait: float | None = None) -> VenueError:
    return VenueError("rate_limited", "too many", venue="polymarket", status=429, retry_after_s=wait)


def test_background_reads_retry_on_their_own_budget_and_slow_down_on_a_429() -> None:
    c = Clock()
    budget = Budget(4, clock=c, sleep=c.sleep)
    down = VenueError("venue_unavailable", "polymarket answered 502.", venue="polymarket", status=502)
    http = FakeHttp([down, limited(5), [1, 2]])
    bg = BackgroundHttp(http, budget, sleep=c.sleep)
    assert bg.request("GET", "https://data-api.polymarket.com/activity", venue="polymarket") == [1, 2]
    assert [r for _, r in http.asked] == [0, 0, 0]  # the SDK doesn't retry: each try here takes its turn
    assert budget.reads == 3 and budget.limited == 1 and budget.rate == 2
    assert c.t >= 1000 + COOL_S  # the 429's pause was kept


def test_a_read_that_cant_succeed_isnt_retried_and_other_hosts_go_straight_through() -> None:
    budget = Budget(4)
    refused = VenueError("invalid_order", "polymarket rejected the request: bad", venue="polymarket", status=400,
                         retryable=False)  # fmt: skip
    http = FakeHttp([refused])
    with pytest.raises(VenueError):
        BackgroundHttp(http, budget).request("GET", "https://data-api.polymarket.com/activity", venue="polymarket")
    assert len(http.asked) == 1
    rpc = FakeHttp([{"result": "0x0"}])
    BackgroundHttp(rpc, budget).request("POST", "https://1rpc.io/matic", venue="polygon", retries=3)
    assert budget.reads == 1 and rpc.asked == [("https://1rpc.io/matic", 3)]  # Polygon isn't Polymarket's budget


def test_too_many_retries_give_up_with_the_error() -> None:
    c = Clock()
    down = VenueError("venue_unavailable", "polymarket answered 503.", venue="polymarket", status=503)
    http = FakeHttp([down] * 5)
    with pytest.raises(VenueError):
        BackgroundHttp(http, Budget(4, clock=c, sleep=c.sleep), sleep=c.sleep).request(
            "GET", "https://gamma-api.polymarket.com/markets", venue="polymarket")  # fmt: skip
    assert len(http.asked) == 5  # the first try and four retries


# ---- the evaluation ----


def trade(at: float, action: str = "buy", market: str = "0xm1") -> Any:
    return SimpleNamespace(at=datetime.fromtimestamp(at, UTC), action=action, market=f"{market}:0")


class FakeSdk:
    """The background's SDK reads: each wallet's latest trades, and its score (or an error)."""

    def __init__(self, trades: dict[str, list[Any]], scores: dict[str, Any]) -> None:
        self._trades, self._scores = trades, scores
        self.scored: list[tuple[str, bool]] = []
        self.checked: list[str] = []

    def trades(self, venue: str, wallet: str, limit: int) -> list[Any]:
        self.checked.append(wallet)
        return self._trades.get(wallet, [])

    def score(self, wallet: str, on_leaderboard: bool = False) -> Any:
        self.scored.append((wallet, on_leaderboard))
        got = self._scores[wallet]
        if isinstance(got, Exception):
            raise got
        return SimpleNamespace(to_dict=lambda: dict(got))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    engine._cache.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setitem(engine._evaluate_state, "since_launch", dict.fromkeys(engine.OUTCOMES, 0))
    return tmp_path


def test_a_new_wallet_gets_the_quick_check_and_only_an_eligible_one_is_scored(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    now = time.time()
    sdk = FakeSdk(
        {AMY: [trade(now - 3600 * i, market=f"0xm{i}") for i in range(10)],  # ten markets this week
         BEN: [trade(now - 9 * DAY)],  # nothing this week
         CAL: [trade(now - 3600, market="0xm1"), trade(now - 7200, market="0xm2")]},  # two markets
        {AMY: score(AMY, "rising")},
    )  # fmt: skip
    monkeypatch.setattr(engine, "background", lambda: sdk)
    d = engine.directory()
    d.add([Seen(AMY, "leaderboard", "PNL MONTH OVERALL", rank=12), Seen(BEN, "search"), Seen(CAL, "search")])
    r = engine.evaluate_round()
    assert r["due"] == 3 and r["done"] == {"scored": 1, "inactive": 1, "insufficient": 1, "failed": 0}
    assert sdk.scored == [(AMY, True)]  # on the monthly board's top 1,000: scored as the check would
    assert {w: d.wallet(w)["status"] for w in (AMY, BEN, CAL)} == {AMY: "scored", BEN: "inactive", CAL: "insufficient"}  # type: ignore[index]
    kept = d.score(AMY)
    assert (kept["via"], kept["format"], kept["won"], kept["settled"]) == ("background", engine.WORTH_FORMAT, 1, 2)  # type: ignore[index]
    assert d.wallet(CAL)["recent_buys"] == 2 and d.wallet(BEN)["last_active"] == pytest.approx(now - 9 * DAY)  # type: ignore[index]
    assert engine.directory().due(10) == []  # each scheduled for later
    assert engine._evaluate_state["since_launch"]["scored"] == 1


def test_a_recommended_trader_is_scored_again_without_the_quick_check(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    sdk = FakeSdk({}, {AMY: score(AMY, "proven")})
    monkeypatch.setattr(engine, "background", lambda: sdk)
    engine.directory().keep_scores([score(AMY, "proven")], "check", at=time.time() - RESCORE_S - 60)
    assert engine.evaluate_round()["done"]["scored"] == 1
    assert sdk.checked == [] and sdk.scored == [(AMY, False)]
    assert [h["via"] for h in engine.directory().history(AMY)] == ["check", "background"]


def test_a_wallet_that_cant_be_scored_is_tried_later_and_a_run_of_failures_stops_the_round(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    now = time.time()
    many = [f"0x{i:040x}" for i in range(1, 9)]
    down = VenueError("venue_unavailable", "Polymarket is down.", venue="polymarket")
    sdk = FakeSdk({w: [trade(now - 60 * i, market=f"0xm{i}") for i in range(10)] for w in many},
                  dict.fromkeys(many, down))  # fmt: skip
    monkeypatch.setattr(engine, "background", lambda: sdk)
    monkeypatch.setattr(engine, "EVALUATE_WORKERS", 1)
    engine.directory().add([Seen(w, "search") for w in many])
    r = engine.evaluate_round()
    assert r["done"]["failed"] == engine.EVALUATE_FAILS_IN_A_ROW and r["failed_in_a_row"] == engine.EVALUATE_FAILS_IN_A_ROW
    failed = [w for w in many if engine.directory().wallet(w)["status"] == "failed"]  # type: ignore[index]
    assert len(failed) == engine.EVALUATE_FAILS_IN_A_ROW
    w = engine.directory().wallet(failed[0])
    assert "Polymarket is down" in w["score_error"] and w["next_check"] == pytest.approx(w["tried_at"] + RETRY_S[0])  # type: ignore[index]
    # the ones not reached are still due, the failed ones later: nothing is lost
    assert {g["wallet"] for g in engine.directory().due(10)} == set(many) - set(failed)


def test_after_a_restart_the_evaluation_carries_on_where_it_was(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    now = time.time()
    sdk = FakeSdk({AMY: [trade(now - 9 * DAY)], BEN: [trade(now - 9 * DAY)]}, {})
    monkeypatch.setattr(engine, "background", lambda: sdk)
    monkeypatch.setattr(engine, "EVALUATE_BATCH", 1)
    engine.directory().add([Seen(AMY, "search")], at=now - 10)
    engine.directory().add([Seen(BEN, "search")], at=now - 20)
    engine.evaluate_round()
    engine._directories.clear()  # a new process: only directory.db carries over
    engine.evaluate_round()
    assert sdk.checked == [AMY, BEN] and engine.directory().due(10) == []


def test_the_evaluation_stops_when_the_app_shuts_down(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = FakeSdk({}, {})
    monkeypatch.setattr(engine, "background", lambda: sdk)
    engine.directory().add([Seen(AMY, "search")])
    stop = threading.Event()
    stop.set()
    assert engine.evaluate_round(stop)["done"] == dict.fromkeys(engine.OUTCOMES, 0) and sdk.checked == []


def test_the_owner_sees_how_the_evaluation_is_going_in_counts_only(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    engine.directory().add([Seen(AMY, "search")])
    engine.directory().quick_checked(BEN, "insufficient", last_trade=None, buys=2)
    s = TestClient(engine.app, client=("127.0.0.1", 50000)).get("/api/directory").json()
    e = s["evaluation"]
    assert e["due_now"] == {"recommended": 0, "new": 1, "recheck": 0}
    assert e["by_status"]["insufficient"] == 1 and e["looked_at_last_day"]["insufficient"] == 1
    assert s["evaluating"]["on"] is engine.EVALUATE_ON and "since_launch" in s["evaluating"]
    assert AMY not in repr(s) and BEN not in repr(s)
