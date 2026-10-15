"""A trader's record as Polymarket reports it (/api/record), for a page without a fresh score: Polymarket's own
figures, open positions biggest first, the categories they hold and lately traded, read on the checks' budget and
kept for minutes, and a page that keeps the record apart from our skill test (no network: the reads are stand-ins)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from uselayer import VenueError
from uselayer.whales import WhalePosition

from signals import app as engine

W = "0x" + "a" * 40
STATS = {"trading_pnl": 5200.0, "realized_pnl": 6000.0, "unrealized_pnl": -800.0, "pnl": 5300.0, "income": 100.0,
         "volume_usd": 410000.0, "trades": 1234, "biggest_win": 2500.0, "fees_paid": 12.0}  # fmt: skip


def pos(market: str, value: float | None, title: str = "A market") -> WhalePosition:
    return WhalePosition(market, title, "Yes", 100.0, 0.4, 0.5, value, None)


class FakeChecker:
    """The checks' client: counts its reads of a trader's page."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def trader(self, venue: str, wallet: str, *, trades: int, closed: int) -> Any:
        self.calls.append((wallet, trades, closed))
        held = [pos("0xs:0", 50.0, "Small"), pos("0xb:1", 9000.0, "Big"), pos("0xn:0", None, "Unpriced")]
        held += [pos(f"0x{i}:0", 10.0) for i in range(7)]
        return SimpleNamespace(trader=SimpleNamespace(name="Rec"), joined="2024-03-01T00:00:00Z", stats=STATS,
                               positions=held, trades=[SimpleNamespace(market="0xb:0"), SimpleNamespace(market="0xt:1")])  # fmt: skip


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeChecker]:
    engine._cache.clear()
    fake = FakeChecker()
    monkeypatch.setattr(engine, "checker", lambda: fake)
    cats = {"0xb:1": "Sports", "0xb:0": "Sports", "0xt:1": "Politics", "0xs:0": "Sports"}
    monkeypatch.setattr(engine, "_categories", lambda ms: {m: cats[m] for m in ms if m in cats})
    monkeypatch.setattr(engine, "_pnl_points", lambda w: PNL)
    return TestClient(engine.app), fake


DAY = 86400
T = 1_792_000_000
# their profit from trading by day: −$300 a year ago, +$1,000 90 days ago, +$1,400 now
PNL = [{"timestamp": T - 400 * DAY, "position_pnl": 0.0}, {"timestamp": T - 365 * DAY, "position_pnl": -300.0},
       {"timestamp": T - 90 * DAY, "position_pnl": 1000.0}, {"timestamp": T, "position_pnl": 1400.0}]  # fmt: skip


def test_the_record_is_polymarkets_figures_with_open_positions_biggest_first(api: tuple[TestClient, FakeChecker]) -> None:
    client, fake = api
    r = client.get(f"/api/record/{W.upper().replace('0X', '0x')}").json()
    assert fake.calls == [(W, engine.RECORD_TRADES, 0)]  # their trades for categories; no closed positions read
    assert r["stats"] == {k: STATS[k] for k in r["stats"]} and "fees_paid" not in r["stats"]
    assert r["open"] == {"count": 10, "at_least": False, "value": 9120.0}  # an unpriced one isn't counted as $0
    assert [p["title"] for p in r["positions"][:2]] == ["Big", "Small"] and len(r["positions"]) == engine.RECORD_POSITIONS
    assert r["positions"][0]["category"] == "Sports"
    # markets (not outcomes) per category: 0xb held and traded counts once; 0xs held; 0xt traded
    assert r["categories"] == [{"category": "Sports", "markets": 2}, {"category": "Politics", "markets": 1}]


def test_the_record_is_kept_for_minutes_and_bad_input_or_a_failed_read_says_so(
    api: tuple[TestClient, FakeChecker], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = api
    client.get(f"/api/record/{W}")
    client.get(f"/api/record/{W}")
    assert len(fake.calls) == 1  # one read of their page per RECORD_TTL_S
    assert client.get("/api/record/someone").status_code == 400

    def down(*a: Any, **k: Any) -> Any:
        raise VenueError("unavailable", "Polymarket is down", venue="polymarket", retryable=True)

    monkeypatch.setattr(fake, "trader", down)
    assert client.get("/api/record/0x" + "b" * 40).status_code >= 400

    def no_categories(ms: Any) -> Any:
        raise VenueError("unavailable", "gamma is down", venue="polymarket", retryable=True)

    monkeypatch.setattr(fake, "trader", FakeChecker().trader)
    monkeypatch.setattr(engine, "_categories", no_categories)
    r = client.get("/api/record/0x" + "c" * 40).json()
    assert r["categories"] == [] and r["open"]["count"] == 10  # the record still shows, without categories


def test_profit_over_90_days_and_12_months_is_the_change_since_each_periods_start() -> None:
    assert engine.period_profit(PNL) == [{"days": 90, "pnl": 400.0, "since": None}, {"days": 365, "pnl": 1700.0, "since": None}]
    # in between two days: the last point on or before the start; out of order, or missing a figure, still fine
    gaps = [PNL[3], {"timestamp": T - 100 * DAY, "position_pnl": 900.0}, {"timestamp": T - 5 * DAY, "position_pnl": None}]
    assert engine.period_profit(gaps, (90,)) == [{"days": 90, "pnl": 500.0, "since": None}]
    # history from their first trade, 30 days ago: both periods are their whole history (from 0), said with `since`
    new = [{"timestamp": T - 30 * DAY, "position_pnl": -20.0}, {"timestamp": T, "position_pnl": 80.0}]
    assert engine.period_profit(new) == [{"days": 90, "pnl": 80.0, "since": T - 30 * DAY},
                                         {"days": 365, "pnl": 80.0, "since": T - 30 * DAY}]  # fmt: skip
    assert engine.period_profit([]) == []


def test_the_record_has_its_periods_and_shows_without_them_when_they_cant_be_read(
    api: tuple[TestClient, FakeChecker], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    assert client.get(f"/api/record/{W}").json()["periods"][0] == {"days": 90, "pnl": 400.0, "since": None}

    def down(w: str) -> Any:
        raise VenueError("unavailable", "Polymarket is down", venue="polymarket", retryable=True)

    monkeypatch.setattr(engine, "_pnl_points", down)
    r = client.get("/api/record/0x" + "d" * 40).json()
    assert r["periods"] is None and r["stats"]["trading_pnl"] == STATS["trading_pnl"]


# ---- the page ----

FACTS = ("usd", "pl", "esc", "cents", "tenths", "centsPair", "RECORD_PERIOD_NAMES", "recordPeriods", "recordFacts",
         "recordPositions", "RECORD_NOTE")  # fmt: skip


def test_the_page_shows_the_last_90_days_and_12_months_after_all_time_never_their_whole_history_twice(run_js: Any) -> None:
    both = '{stats: {trading_pnl: 5200}, periods: [{days: 90, pnl: -400, since: null}, {days: 365, pnl: 1700, since: null}]}'
    got = run_js(FACTS, f"recordFacts({both}).map(([k]) => k)")
    assert got[:3] == ["Profit from trading", "Last 90 days", "Last 12 months"]
    rows = dict(run_js(FACTS, f"recordFacts({both})"))
    assert rows["Last 90 days"].startswith('<span class="loss">−$400</span>') and "from trading" in rows["Last 90 days"]
    # joined 30 days ago: 12 months is the all-time figure again, so only what's a real period shows
    young = "{stats: {trading_pnl: 80}, periods: [{days: 90, pnl: 80, since: 1}, {days: 365, pnl: 80, since: 1}]}"
    assert "Last 12 months" not in dict(run_js(FACTS, f"recordFacts({young})"))
    assert "Last 90 days" not in dict(run_js(FACTS, "recordFacts({periods: null})"))


def test_the_page_says_its_a_record_not_a_skill_test_and_unknown_stays_a_dash(run_js: Any) -> None:
    rec = ('{stats: {trading_pnl: 5200, realized_pnl: 6000, unrealized_pnl: -800, volume_usd: 410000, trades: 1234,'
           ' biggest_win: null}, open: {count: 50, at_least: true, value: 9120}, joined: "2024-03-01T00:00:00Z",'
           ' categories: [{category: "Sports", markets: 2}], positions: [{title: "Big <b>", outcome: "Yes", value: 9000,'
           ' avg_price: 0.4, current_price: 0.5, category: "Sports"}]}')  # fmt: skip
    got = run_js(FACTS, f"[recordFacts({rec}), recordPositions({rec}), RECORD_NOTE, recordFacts({{}})]")
    facts = dict(got[0])
    assert facts["Profit from trading"].startswith('<span class="gain">+$5.2K</span> all time')
    assert "+$6K closed, −$800 still open" in facts["Profit from trading"]
    assert facts["Volume"] == "$410K traded, all time" and facts["Trades"] == "1,234" and facts["Biggest win"] == "—"
    assert facts["Open now"].startswith("50+ positions over 1 contract, worth $9.1K")
    assert "smaller ones can still add to the −$800 still open" in facts["Open now"]
    assert facts["Joined"].startswith("Mar 2024")  # in UTC
    # none listed, yet profit still open: positions under 1 contract (Polymarket's list leaves them out), never "None"
    small = dict(run_js(FACTS, "recordFacts({stats: {realized_pnl: 424, unrealized_pnl: 7.71}, open: {count: 0}})"))
    assert small["Open now"].startswith("No positions over 1 contract") and "+$7.71 still open" in small["Open now"]
    tiny = run_js(FACTS, "recordFacts({stats: {trading_pnl: 1162, realized_pnl: 1162, unrealized_pnl: -0.0035}})")
    assert "+$1.2K closed, nothing still open" in dict(tiny)["Profit from trading"]  # never "−$0.00 still open"
    assert got[1][0][0].startswith("Big &lt;b&gt;") and "40¢ → 50¢ · Sports" in got[1][0][1]  # titles escaped
    assert "not a test of skill" in got[2] and "buying after them" in got[2]
    assert dict(got[3]) == {"Profit from trading": "—", "Volume": "—", "Trades": "—", "Biggest win": "—",
                            "Open now": "No positions over 1 contract", "Markets": "—"}  # fmt: skip
