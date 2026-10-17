"""The trader directory, phase 3: every wallet evaluated in the background. A quick check before a score that keeps
the evidence and says why a wallet isn't recommended (every wallet kept), the schedule (recommended traders every six
hours, wallets short of evidence daily, dormant ones weekly, failures backing off), the queue's order and its
categories taking turns, one read budget for the background with retries and rate limits, and carrying on after a
restart (no network: the SDK's reads are stand-ins)."""

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

REAL_CHECKER = engine.checker  # before conftest stands it in with reads.whales
from signals.directory import (
    BACKGROUND_RESCORE_S,
    PRESENTED_RESCORE_S,
    RECHECK_EVIDENCE_S,
    RECHECK_QUIET_S,
    RECHECK_SCORED_S,
    RETRY_S,
    _LISTED,
    Directory,
    QuickLook,
    Seen,
    fresh_for,
    next_check_at,
    quick_look,
    reason_label,
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


def look(trades: list[tuple[float, bool, str]], read_all: bool = True, scored_at: float | None = None) -> tuple[Any, ...]:
    q = quick_look(trades, T0, read_all, scored_at)
    return q.score, q.reason, q.buys_30d, q.buys_90d


def test_fewer_than_eight_markets_bought_in_30_days_is_not_enough_evidence_and_the_90_days_are_kept() -> None:
    again = [(T0 - 60 * i, True, "0xm0") for i in range(30)]  # thirty buys of one market is one bet
    assert look(again + buys(6, T0 - DAY)) == (False, "not_enough_recent_evidence", 6, 6)  # 0xm0 is among the six
    # a selective trader: 5 bets this month, 20 over three months. Kept, labelled, not dismissed
    assert look(buys(5, T0) + buys(15, T0 - 40 * DAY, every=DAY)) == (False, "not_enough_recent_evidence", 5, 15)
    assert look(buys(8, T0))[:2] == (True, None)


def test_no_buy_in_90_days_is_dormant() -> None:
    sold = [(T0 - DAY, False, "0xm1")]  # a sale yesterday isn't a buy
    assert look(sold + buys(20, T0 - 95 * DAY)) == (False, "dormant", 0, 0)
    assert look([])[:2] == (False, "dormant")  # nothing at all


def test_a_paused_trader_is_scored_once_then_left_until_they_buy_again() -> None:
    paused = buys(12, T0 - 10 * DAY)  # 12 markets this month, none this week
    assert look(paused)[:2] == (True, None)  # never scored: the score is the evidence (it says "not trading lately")
    assert look(paused, scored_at=T0 - 9 * DAY)[:2] == (False, "not_trading_lately")  # nothing new since that score
    assert look(buys(1, T0) + paused, scored_at=T0 - 9 * DAY)[:2] == (True, None)  # bought again: scored again


def test_a_read_cut_short_only_judges_the_time_it_covers() -> None:
    # 100 sales in the last two days, and the read stopped there: a buy before them can't be ruled out
    busy = [(T0 - i * 1700, False, f"0xm{i}") for i in range(100)]
    assert look(busy, read_all=False)[:2] == (True, None)
    # ...but 100 trades reaching back past 90 days with no buy among them is dormant
    slow = [(T0 - 95 * DAY - i, False, "0xm1") for i in range(100)]
    assert look(slow, read_all=False)[:2] == (False, "dormant")
    # 3 markets in the last few hours, read cut short: the rest of the window is unknown, so it's scored
    assert look(buys(3, T0, every=60) * 30, read_all=False)[:2] == (True, None)


def test_each_reason_reads_as_a_sentence() -> None:
    assert reason_label("not_enough_recent_evidence", 5) == (
        "Not enough recent evidence: 5 markets traded in the last 30 days, and 8 are needed to judge a trader")
    assert reason_label("not_trading_lately") == "Not trading lately: no buy in the last 7 days"
    assert reason_label("dormant") == "Dormant: no buy in the last 90 days"
    assert reason_label(None) is None  # recommended
    # uselayer 0.10's tiers: every one that isn't recommended is a reason with a sentence, and big profit without a
    # clear edge is never called luck: only profit resting on one win is
    from uselayer.whale_scores import RECOMMENDED, SEGMENTS

    from signals.directory import REASON_LABELS, REASONS, reason_for

    for seg in set(SEGMENTS) - set(RECOMMENDED):
        assert reason_for(seg) in REASONS and reason_for(seg) in REASON_LABELS, seg
    assert reason_label("lucky") == "Scored: a big profit that rests on one win"
    assert reason_label("big_profit", 5).endswith("(and nothing shows luck)")


# ---- the schedule ----


def test_each_kind_of_wallet_is_looked_at_again_on_its_own_schedule() -> None:
    # a recommended trader the directory found: daily (one people are shown: every 6 hours, see the queue's tests)
    assert next_check_at("scored", None, "proven", T0) == T0 + BACKGROUND_RESCORE_S == T0 + DAY
    assert next_check_at("scored", None, "too_fast", T0) == T0 + BACKGROUND_RESCORE_S
    assert next_check_at("scored", "no_edge", "no_edge", T0) == T0 + RECHECK_SCORED_S
    assert next_check_at("scored", "no_view", "no_view", T0) == T0 + RECHECK_QUIET_S
    for reason in ("not_enough_recent_evidence", "not_trading_lately"):  # one read a day: scored as soon as they can be
        assert next_check_at("checked", reason, None, T0) == T0 + RECHECK_EVIDENCE_S == T0 + DAY
    assert next_check_at("scored", "not_trading_lately", "inactive", T0) == T0 + DAY
    assert next_check_at("checked", "dormant", "proven", T0) == T0 + RECHECK_QUIET_S
    assert PRESENTED_RESCORE_S < BACKGROUND_RESCORE_S == RECHECK_EVIDENCE_S < RECHECK_SCORED_S < RECHECK_QUIET_S
    assert [next_check_at("failed", None, None, T0, n) - T0 for n in (1, 2, 3, 9)] == [*RETRY_S[:3], RETRY_S[-1]]


def test_a_new_wallet_is_due_at_once_and_a_score_schedules_the_next_and_says_why(d: Directory) -> None:
    d.add([Seen(AMY, "search")], at=T0)
    assert d.wallet(AMY)["next_check"] == T0  # type: ignore[index]
    d.keep_scores([score(AMY, "rising")], "background", at=T0 + 10)
    w = d.wallet(AMY)
    assert (w["next_check"], w["not_recommended"]) == (T0 + 10 + BACKGROUND_RESCORE_S, None)  # type: ignore[index]
    assert d.due(10, now=T0 + 20) == []
    d.keep_scores([score(AMY, "inactive")], "background", at=T0 + 30)
    assert d.wallet(AMY)["not_recommended"] == "not_trading_lately"  # type: ignore[index]


def test_failures_back_off_and_a_score_clears_them(d: Directory) -> None:
    d.add([Seen(AMY, "search")], at=T0)
    d.score_failed(AMY, "VenueError: timeout", at=T0)
    d.score_failed(AMY, "VenueError: timeout", at=T0 + 1)
    w = d.wallet(AMY)
    assert (w["status"], w["failures"], w["next_check"]) == ("failed", 2, T0 + 1 + RETRY_S[1])  # type: ignore[index]
    d.keep_scores([score(AMY, "no_edge")], "background", at=T0 + 2)
    w = d.wallet(AMY)
    assert (w["status"], w["failures"], w["next_check"]) == ("scored", 0, T0 + 2 + RECHECK_SCORED_S)  # type: ignore[index]
    assert w["not_recommended"] == "no_edge"  # type: ignore[index]


def test_a_quick_check_keeps_the_evidence_and_why_and_leaves_an_earlier_score(d: Directory) -> None:
    d.keep_scores([score(AMY, "no_edge")], "check", at=T0)
    d.quick_checked(AMY, QuickLook(False, "not_enough_recent_evidence", 3, 11, T0 + 4, T0 + 5), at=T0 + DAY)
    w = d.wallet(AMY)
    assert (w["status"], w["not_recommended"], w["next_check"]) == ("checked", "not_enough_recent_evidence", T0 + 2 * DAY)  # type: ignore[index]
    assert (w["buys_30d"], w["buys_90d"], w["last_buy"], w["last_active"]) == (3, 11, T0 + 4, T0 + 5)  # type: ignore[index]
    assert w["segment"] == "no_edge" and d.score(AMY)["segment"] == "no_edge"  # type: ignore[index]  # the score stays
    d.quick_checked(BEN, QuickLook(True, None, 12, 30, T0, T0), at=T0)  # worth a score: the evidence, nothing else
    w = d.wallet(BEN)
    assert (w["status"], w["not_recommended"], w["buys_30d"]) == ("found", None, 12)  # type: ignore[index]


def test_the_check_scoring_a_wallet_after_a_quick_check_doesnt_undo_the_quick_checks_verdict(d: Directory) -> None:
    d.quick_checked(AMY, QuickLook(False, "dormant", 0, 0, None, None), at=T0 + 100)
    d.keep_scores([score(AMY, "no_edge")], "check", at=T0)  # a check that finished before the quick check
    w = d.wallet(AMY)
    assert (w["status"], w["not_recommended"], w["next_check"]) == ("checked", "dormant", T0 + 100 + RECHECK_QUIET_S)  # type: ignore[index]


# ---- the queue ----


def test_recommended_traders_come_first_then_the_newest_found_then_the_rest(d: Directory) -> None:
    d.add([Seen(BEN, "search")], at=T0 - 3 * DAY)  # an old find, never looked at
    d.add([Seen(CAL, "search")], at=T0 - DAY)  # a newer one
    d.keep_scores([score(AMY, "proven")], "check", at=T0 - BACKGROUND_RESCORE_S - 1)  # recommended, due again
    d.keep_scores([score(DAN, "no_edge")], "check", at=T0 - RECHECK_SCORED_S - 1)  # a recheck
    got = d.due(10, now=T0)
    assert [(g["wallet"], g["kind"]) for g in got] == [(AMY, "recommended"), (CAL, "new"), (BEN, "new"), (DAN, "recheck")]
    assert d.due_counts(now=T0) == {"presented": 0, "recommended": 1, "new": 2, "recheck": 1}
    assert [g["wallet"] for g in d.due(2, now=T0)] == [AMY, CAL]


def test_a_recommended_trader_people_are_shown_is_rescored_every_6_hours_any_other_daily(d: Directory) -> None:
    d.keep_scores([score(AMY, "rising")], "background", at=T0 - PRESENTED_RESCORE_S - 1)  # 6 h old
    d.keep_scores([score(BEN, "rising")], "background", at=T0 - PRESENTED_RESCORE_S - 1)
    d.keep_scores([score(CAL, "no_edge")], "background", at=T0 - PRESENTED_RESCORE_S - 1)  # not a recommended tier
    assert d.due(10, now=T0) == []  # daily for the directory's own finds
    got = d.due(10, now=T0, presented={AMY, CAL})
    # shown: every 6 hours, whatever tier the stored score says (the list may serve an old score re-tiered)
    assert sorted((g["wallet"], g["kind"]) for g in got) == [(AMY, "presented"), (CAL, "presented")]
    assert d.due_counts(now=T0, presented={AMY}) == {"presented": 1, "recommended": 0, "new": 0, "recheck": 0}
    assert d.due(10, now=T0, presented={BEN})[0]["kind"] == "presented"
    assert [g["wallet"] for g in d.due(10, now=T0 + DAY, presented={AMY})] == [AMY, BEN]


def test_new_first_puts_wallets_never_looked_at_before_other_recommended_but_never_before_presented(d: Directory) -> None:
    d.add([Seen(CAL, "search")], at=T0 - DAY)
    d.keep_scores([score(AMY, "rising")], "background", at=T0 - DAY - 1)
    d.keep_scores([score(BEN, "rising")], "background", at=T0 - DAY - 1)
    kinds = lambda **kw: [(g["wallet"], g["kind"]) for g in d.due(10, now=T0, presented={AMY}, **kw)]  # noqa: E731
    assert kinds() == [(AMY, "presented"), (BEN, "recommended"), (CAL, "new")]
    assert kinds(new_first=True) == [(AMY, "presented"), (CAL, "new"), (BEN, "recommended")]


def test_a_score_is_fresh_for_twice_its_cadence() -> None:
    assert fresh_for(True) == 12 * 3600 and fresh_for(False) == 2 * DAY


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
    assert again.wallet(AMY)["next_check"] == T0 - 3600 + BACKGROUND_RESCORE_S  # type: ignore[index]  # not at once
    assert again.wallet(BEN)["next_check"] == T0 - DAY and again.wallet(BEN)["seen_category"] == "Weather"  # type: ignore[index]
    assert [g["wallet"] for g in again.due(10, now=T0)] == [BEN]


def test_the_queues_leaderboard_check_looks_up_each_wallets_own_sightings(d: Directory) -> None:
    # on sightings_by_source it walked every recent leaderboard sighting for each wallet: 13 s a round in production
    d.add([Seen(AMY, "leaderboard", "PNL MONTH OVERALL", rank=3)], at=T0)
    with d._db() as con:
        plan = " / ".join(r[3] for r in con.execute(f"EXPLAIN QUERY PLAN SELECT wallet FROM wallets q WHERE {_LISTED}",
                                                     (T0 - DAY,)))  # fmt: skip
    assert "sightings_by_source" not in plan and "sqlite_autoindex_sightings_1 (wallet=? AND source=?)" in plan, plan
    assert [g["on_leaderboard"] for g in d.due(10, now=T0)] == [True]


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


# ---- "Check now" on its own budget; the background and checks give way to followed traders' reads ----


def test_a_budget_gives_way_at_half_its_rate_while_followed_traders_are_read() -> None:
    c, busy = Clock(), {"on": True}
    b = Budget(4, clock=c, sleep=c.sleep, yield_to=lambda: busy["on"])
    for _ in range(5):
        b.take()
    assert c.t == pytest.approx(1002.0)  # 0.5 s apart: 2 a second while the Feed reads followed traders
    busy["on"] = False
    for _ in range(4):
        b.take()
    assert c.t == pytest.approx(1003.25)  # the slot booked while giving way, then 4 a second again


def test_the_backgrounds_scorer_is_a_fresh_one_every_few_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    # the SDK's scorer keeps every market and price history it reads: the background's must not live as long as the app
    clock = {"t": 50_000.0}
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(engine, "_background", {})
    whales = engine.background()
    first, budget = whales.scorer(), engine._background["budget"]
    clock["t"] += engine.SCORER_FRESH_S - 1
    assert engine.background().scorer() is first  # within the interval: the same one, its markets and prices kept
    clock["t"] += 1
    assert engine.background() is whales and engine._background["budget"] is budget  # same client, same budget
    fresh = whales.scorer()
    assert fresh is not first and fresh.tape._dir == first.tape._dir  # a new one, past prices still on disk
    assert first.markets.get([]) == {}  # one already in use still works: a score running keeps the scorer it took
    clock["t"] += 60
    assert engine.background().scorer() is fresh


def test_check_now_scores_on_its_own_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    real = REAL_CHECKER()  # the engine's own client, each Polymarket read taken from the checks budget
    assert isinstance(real._poly._http, BackgroundHttp) and real._poly._http.budget is engine._check_budget
    assert real.scorer()._poly is real._poly  # the scorer's reads too, from whichever of its threads
    assert real._poly._http._http is engine.reads._http  # under the SDK's per-host pacing, shared with everything
    assert engine._check_budget.target == engine.CHECK_RATE == 3
    assert engine._check_budget._yield_to is engine._reading_followed
    assert engine.background() and engine._background["budget"]._yield_to is engine._reading_followed
    assert engine.BACKGROUND_RATE + engine.CHECK_RATE < 8  # the SDK's 8 a host: the Feed always keeps a share
    scored: list[str] = []
    monkeypatch.setattr(engine, "checker", lambda: SimpleNamespace(score=lambda w, **k: scored.append(w)
                                                                   or SimpleNamespace(to_dict=lambda: {"wallet": w})))  # fmt: skip
    monkeypatch.setattr(engine, "_worth_data", {"scores": []})
    monkeypatch.setattr(engine, "_directory_later", lambda write: None)
    engine._worth_one.pop("0x" + "7" * 40, None)
    engine._score_of("0x" + "7" * 40)
    assert scored == ["0x" + "7" * 40]  # "Check now" and a trader page score through it


# ---- the evaluation ----


def trade(at: float, action: str = "buy", market: str = "0xm1", usd: float = 100.0) -> Any:
    return SimpleNamespace(at=datetime.fromtimestamp(at, UTC), action=action, market=f"{market}:0", usd=usd)


class FakeSdk:
    """The background's SDK reads: each wallet's latest trades, and its score (or an error)."""

    def __init__(self, trades: dict[str, list[Any]], scores: dict[str, Any],
                 boards: dict[tuple[str, str, str | None], list[str]] | None = None) -> None:  # fmt: skip
        self._trades, self._scores, self._boards = trades, scores, boards or {}
        self.scored: list[tuple[str, bool]] = []
        self.checked: list[str] = []
        self.board_reads = 0
        self._poly = SimpleNamespace(leaderboard=self.leaderboard)

    def leaderboard(self, order: str, period: str, limit: int, *, category: str | None = None, offset: int = 0) -> list[Any]:
        self.board_reads += 1
        if self._boards is None:
            raise VenueError("venue_unavailable", "Polymarket is down.", venue="polymarket")
        rows = self._boards.get((order, period, category), [])[offset : offset + limit]
        return [{"proxyWallet": w} for w in rows]

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


def test_a_new_wallet_gets_the_quick_check_and_is_scored_only_with_the_evidence_for_it(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    now = time.time()
    sdk = FakeSdk(
        {AMY: [trade(now - 3600 * i, market=f"0xm{i}") for i in range(10)],  # ten markets this week
         BEN: [trade(now - 95 * DAY)],  # nothing in 90 days
         CAL: [trade(now - 3600, market="0xm1"), trade(now - 40 * DAY, market="0xm2")],  # selective
         DAN: [trade(now - 10 * DAY, market=f"0xm{i}") for i in range(9)]},  # paused, never scored
        {AMY: score(AMY, "rising"), DAN: score(DAN, "inactive")},
    )  # fmt: skip
    monkeypatch.setattr(engine, "background", lambda: sdk)
    d = engine.directory()
    d.add([Seen(AMY, "leaderboard", "PNL MONTH OVERALL", rank=12), Seen(BEN, "search"), Seen(CAL, "search"),
           Seen(DAN, "search")])  # fmt: skip
    r = engine.evaluate_round()
    assert r["due"] == 4 and r["done"] == {"scored": 2, "not_enough_recent_evidence": 1, "not_trading_lately": 0,
                                           "dormant": 1, "failed": 0}  # fmt: skip
    assert sorted(sdk.scored) == [(AMY, True), (DAN, False)]  # AMY on the monthly top 1,000, as the check would pass
    status = {w: (d.wallet(w)["status"], d.wallet(w)["not_recommended"]) for w in (AMY, BEN, CAL, DAN)}  # type: ignore[index]
    assert status == {AMY: ("scored", None), BEN: ("checked", "dormant"),
                      CAL: ("checked", "not_enough_recent_evidence"), DAN: ("scored", "not_trading_lately")}  # fmt: skip
    kept = d.score(AMY)
    assert (kept["via"], kept["format"], kept["won"], kept["settled"]) == ("background", engine.WORTH_FORMAT, 1, 2)  # type: ignore[index]
    assert (d.wallet(CAL)["buys_30d"], d.wallet(CAL)["buys_90d"]) == (1, 2)  # type: ignore[index]
    assert d.wallet(BEN)["last_active"] == pytest.approx(now - 95 * DAY)  # type: ignore[index]
    assert all(d.wallet(w) for w in (AMY, BEN, CAL, DAN)) and engine.directory().due(10) == []  # kept, for later
    assert engine._evaluate_state["since_launch"]["scored"] == 2


def test_a_recommended_trader_is_scored_again_without_the_quick_check(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    sdk = FakeSdk({}, {AMY: score(AMY, "proven")})
    monkeypatch.setattr(engine, "background", lambda: sdk)
    engine.directory().keep_scores([score(AMY, "proven")], "check", at=time.time() - BACKGROUND_RESCORE_S - 60)
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


def test_while_recommended_rescoring_takes_most_reads_new_wallets_go_first(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    now = time.time()
    sdk = FakeSdk({CAL: [trade(now - 95 * DAY)]}, {AMY: score(AMY, "rising")})
    monkeypatch.setattr(engine, "background", lambda: sdk)
    monkeypatch.setattr(engine, "EVALUATE_BATCH", 1)
    monkeypatch.setitem(engine._evaluate_state, "reads", {"new": 0.0, "other": 0.0})
    engine.directory().keep_scores([score(AMY, "rising")], "background", at=now - BACKGROUND_RESCORE_S - 60)
    engine.directory().add([Seen(CAL, "search")])
    engine.evaluate_round()  # no reads counted yet: recommended first
    assert sdk.scored == [(AMY, False)] and sdk.checked == []
    assert engine._evaluate_state["reads"]["other"] > 0 and engine._reserve_new() is True
    engine.directory().keep_scores([score(BEN, "rising")], "background", at=now - BACKGROUND_RESCORE_S - 60)
    engine.evaluate_round()  # new wallets' share is under NEW_SHARE: CAL before BEN
    assert sdk.checked == [CAL] and sdk.scored == [(AMY, False)]


def test_presented_traders_are_the_checks_recommended_and_everyone_followed(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": [
        {"wallet": AMY, "segment": "rising"}, {"wallet": BEN, "segment": "no_edge"}]})  # fmt: skip
    engine._save_follows({"users": {"u1": {"follows": [DAN], "chat": None}}})
    engine._cache.clear()
    assert engine.presented_wallets() == {AMY, DAN}


def test_after_a_restart_the_evaluation_carries_on_where_it_was(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    now = time.time()
    sdk = FakeSdk({AMY: [trade(now - 95 * DAY)], BEN: [trade(now - 95 * DAY)]}, {})
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
    engine.directory().quick_checked(BEN, QuickLook(False, "not_enough_recent_evidence", 2, 9, None, None))
    s = TestClient(engine.app, client=("127.0.0.1", 50000)).get("/api/directory").json()
    e = s["evaluation"]
    assert e["due_now"] == {"presented": 0, "recommended": 0, "new": 1, "recheck": 0}
    assert e["by_status"]["checked"] == 1 and e["looked_at_last_day"]["checked"] == 1
    assert e["not_recommended"]["not_enough_recent_evidence"] == 1 and e["selective"] == 1
    assert s["evaluating"]["on"] is engine.EVALUATE_ON and "since_launch" in s["evaluating"]
    assert AMY not in repr(s) and BEN not in repr(s)


# ---- on_leaderboard without the sweep ----


def test_with_the_sweep_off_on_leaderboard_comes_from_the_boards_read_directly(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    now = time.time()
    active = [trade(now - 3600 * i, market=f"0xm{i}") for i in range(10)]
    deep = [f"0x{i:040x}" for i in range(1, 900)] + [AMY]  # AMY at #900 on the monthly board
    sdk = FakeSdk({AMY: active, BEN: active}, {AMY: score(AMY, "rising"), BEN: score(BEN, "rising")},
                  boards={("PNL", "MONTH", None): deep, ("PNL", "MONTH", "SPORTS"): [CAL]})  # fmt: skip
    monkeypatch.setattr(engine, "background", lambda: sdk)
    engine.directory().add([Seen(AMY, "search"), Seen(BEN, "search")])  # no sweep: no leaderboard sightings
    engine.evaluate_round()
    assert sorted(sdk.scored) == [(AMY, True), (BEN, False)]  # as discover() would pass it
    assert engine._evaluate_state["leaderboards"] == "read"
    reads = sdk.board_reads
    assert reads == 19 + 3 + 8  # the monthly board 18 full pages (to #900) and the empty 19th, the others one page each
    engine.directory().add([Seen(CAL, "search")])
    sdk._trades[CAL] = active
    sdk._scores[CAL] = score(CAL, "rising")
    engine.evaluate_round()
    assert (CAL, True) in sdk.scored and sdk.board_reads == reads  # kept: not read again every round


def test_while_the_sweep_is_fresh_its_sightings_are_used_and_no_board_is_read(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    now = time.time()
    sdk = FakeSdk({AMY: [trade(now - 3600 * i, market=f"0xm{i}") for i in range(10)]}, {AMY: score(AMY, "rising")},
                  boards={})  # fmt: skip
    monkeypatch.setattr(engine, "background", lambda: sdk)
    engine.directory().add([Seen(AMY, "leaderboard", "PNL MONTH OVERALL", rank=40)])
    engine.evaluate_round()
    assert sdk.scored == [(AMY, True)] and sdk.board_reads == 0 and engine._evaluate_state["leaderboards"] == "sweep"


def test_when_the_boards_cant_be_read_either_the_sightings_stand_and_it_says_so(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    now = time.time()
    sdk = FakeSdk({AMY: [trade(now - 3600 * i, market=f"0xm{i}") for i in range(10)]}, {AMY: score(AMY, "rising")})
    sdk._boards = None  # type: ignore[assignment]  # Polymarket's leaderboard is down
    monkeypatch.setattr(engine, "background", lambda: sdk)
    engine.directory().add([Seen(AMY, "search")])
    assert engine.evaluate_round()["done"]["scored"] == 1
    assert sdk.scored == [(AMY, False)] and "couldn't be read" in engine._evaluate_state["leaderboards"]
