"""The trader directory, phase 2: each wallet's evaluation (status, last scored, last active), its scores and their
categories, kept across checks and restarts, and the scores kept before the directory imported into it (no
network: the SDK's reads are stand-ins)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from uselayer import VenueError, WhaleTrade

from signals import app as engine
from signals.directory import SAMPLE_KEEP, Directory, Seen, from_sighting

AMY = "0x" + "a1" * 20
BEN = "0x" + "b2" * 20
CAL = "0x" + "c3" * 20
T0 = datetime(2026, 10, 12, 15, 0, tzinfo=UTC).timestamp()


def score(wallet: str, segment: str = "rising", **kw: Any) -> dict[str, Any]:
    """A score as the engine keeps it (the SDK's ``to_dict``, plus the check's won/settled/odds)."""
    return {
        "wallet": wallet, "name": "amy" if wallet == AMY else wallet, "segment": segment, "label": "Rising",
        "skill": "early", "copyable": "no", "style": "takes_a_side", "confidence": "low", "active": True,
        "bets": 38, "decisions": 143, "on_leaderboard": wallet == AMY,
        "edge": {"mean": 0.025, "margin": 0.036, "n": 38, "z": 1.36, "low": -0.011, "high": 0.061},
        "copy": {"mean": 0.009, "margin": 0.02, "n": 38, "z": 0.9, "low": -0.011, "high": 0.029},
        "categories": [
            {"category": "Sports", "bets": 30, "edge": {"mean": 0.03, "margin": 0.04, "n": 30, "z": 1.5},
             "copy": {"mean": 0.01, "margin": 0.02, "n": 30, "z": 1.0, "low": -0.01}, "strong": False},
            {"category": "Esports", "bets": 8, "edge": {"mean": 0.05, "margin": 0.01, "n": 8, "z": 2.4},
             "copy": None, "strong": True},
        ],
        "trading_pnl_window": 1200.0, "pnl": -146354.9, "volume": 15168943.2,
        "window": ["2026-09-12T15:00:00+00:00", "2026-10-12T15:00:00+00:00"],
        "last_buy": "2026-10-12T14:00:00+00:00", "recent": [],
        "sample": [{"market": f"m{i}", "price": 0.5, "at": "2026-10-01T00:00:00+00:00"} for i in range(40)],
    } | kw  # fmt: skip


@pytest.fixture
def d(tmp_path: Any) -> Directory:
    return Directory(tmp_path / "directory.db")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    engine._cache.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "_worth_data", None)
    monkeypatch.setattr(engine, "_worth_one", {})
    engine._directory_state.pop("note_error", None)
    return tmp_path


def flush() -> None:
    engine._directory_writer.submit(lambda: None).result()


# ---- the store ----


def test_a_score_marks_the_wallet_scored_with_its_segment_main_category_and_last_trade(d: Directory) -> None:
    d.add([from_sighting(SimpleNamespace(wallet=AMY, source="leaderboard", where="PNL MONTH SPORTS", name=None,
                                         category="Sports", rank=4, title=None, trades=0, usd=0.0))], at=T0 - 99)  # fmt: skip
    assert d.wallet(AMY)["status"] == "found"  # type: ignore[index]
    assert d.keep_scores([score(AMY)], "check", at=T0) == 1
    w = d.wallet(AMY)
    assert w is not None
    assert (w["status"], w["scored_at"], w["tried_at"], w["segment"], w["category"]) == ("scored", T0, T0, "rising", "Sports")
    assert w["last_active"] == datetime(2026, 10, 12, 14, tzinfo=UTC).timestamp()  # their last buy, not when we looked
    assert (w["first_source"], w["name"]) == ("leaderboard", "amy")  # found by the sweep first; the score adds the name


def test_the_whole_score_is_kept_with_its_checked_bets_cut_and_its_headline_figures_in_columns(d: Directory) -> None:
    d.keep_scores([score(AMY, format=7)], "check", at=T0)
    kept = d.score(AMY)
    assert kept is not None and kept["via"] == "check" and kept["scored_at"] == T0
    assert len(kept["sample"]) == SAMPLE_KEEP and kept["categories"][1]["strong"] is True
    with sqlite3.connect(d.path) as con:
        row = con.execute("SELECT format, segment, copyable, bets, edge, edge_z, copy, copy_low, window_start, window_end, "
                          "active FROM scores WHERE wallet = ?", (AMY,)).fetchone()  # fmt: skip
    assert row[:8] == (7, "rising", "no", 38, 0.025, 1.36, 0.009, -0.011) and row[10] == 1
    assert row[9] - row[8] == 30 * 86400


def test_each_categorys_price_test_is_kept_and_replaced_by_the_next_score(d: Directory) -> None:
    d.keep_scores([score(AMY)], "check", at=T0)
    cats = {c["category"]: c for c in d.categories_of(AMY)}
    assert set(cats) == {"Sports", "Esports"}
    assert (cats["Sports"]["bets"], cats["Sports"]["edge"], cats["Sports"]["copy_low"], cats["Sports"]["strong"]) == (30, 0.03, -0.01, 0)
    assert (cats["Esports"]["edge_z"], cats["Esports"]["copy"], cats["Esports"]["strong"]) == (2.4, None, 1)
    later = score(AMY, categories=[{"category": "Politics", "bets": 12, "edge": None, "copy": None, "strong": False}])
    d.keep_scores([later], "check", at=T0 + 3600)
    assert [c["category"] for c in d.categories_of(AMY)] == ["Politics"]
    assert d.wallet(AMY)["category"] == "Politics"  # type: ignore[index]


def test_an_older_score_never_replaces_a_newer_one_but_both_are_in_the_history(d: Directory) -> None:
    d.keep_scores([score(AMY, "proven")], "page", at=T0 + 3600)
    assert d.keep_scores([score(AMY, "lucky")], "import", at=T0) == 0
    w, kept = d.wallet(AMY), d.score(AMY)
    assert w is not None and kept is not None
    assert (w["segment"], w["scored_at"], kept["segment"], kept["via"]) == ("proven", T0 + 3600, "proven", "page")
    assert [(h["scored_at"], h["segment"], h["via"]) for h in d.history(AMY)] == [(T0, "lucky", "import"), (T0 + 3600, "proven", "page")]


def test_a_failed_try_keeps_the_earlier_score_and_a_later_score_clears_it(d: Directory) -> None:
    d.keep_scores([score(AMY)], "check", at=T0)
    d.score_failed(AMY, "VenueError: Polymarket didn't answer", at=T0 + 60)
    w = d.wallet(AMY)
    assert w is not None and (w["status"], w["scored_at"], w["tried_at"], w["score_error"]) == (
        "failed", T0, T0 + 60, "VenueError: Polymarket didn't answer")
    assert d.score(AMY) is not None  # still there
    d.keep_scores([score(AMY)], "import", at=T0)  # the import again, on the next launch: it's older than the try
    assert d.wallet(AMY)["status"] == "failed"  # type: ignore[index]
    d.keep_scores([score(AMY)], "page", at=T0 + 120)
    w = d.wallet(AMY)
    assert w is not None and (w["status"], w["score_error"], w["tried_at"]) == ("scored", None, T0 + 120)


def test_a_wallet_that_failed_before_it_was_ever_found_is_kept_as_a_search(d: Directory) -> None:
    d.score_failed(BEN, "VenueError: no trades", at=T0)
    w = d.wallet(BEN)
    assert w is not None and (w["status"], w["first_source"], w["scored_at"]) == ("failed", "search", None)


def test_a_wallet_first_found_by_a_score_says_where_the_check_found_it(d: Directory) -> None:
    d.keep_scores([score(AMY), score(BEN)], "check", at=T0)
    d.keep_scores([score(CAL)], "page", at=T0)
    places = {w: (d.wallet(w)["first_source"], d.wallet(w)["first_place"]) for w in (AMY, BEN, CAL)}  # type: ignore[index]
    assert places == {AMY: ("check", "leaderboard"), BEN: ("check", "market"), CAL: ("search", "")}
    assert d.wallet(BEN)["name"] is None  # type: ignore[index]  # no username: the wallet isn't a name


def test_last_activity_is_the_latest_trade_we_know_the_time_of_and_never_goes_back(d: Directory) -> None:
    d.add([from_sighting(SimpleNamespace(wallet=AMY, source="market", where="0xm1", name=None, category="Sports",
                                         rank=None, title="M?", trades=3, usd=90.0))], at=T0)  # fmt: skip
    assert d.wallet(AMY)["last_active"] is None  # type: ignore[index]  # a market sighting has no trade time
    d.add([Seen(AMY, "feed", "Sports", traded_at=T0 + 500)], at=T0 + 600)
    assert d.wallet(AMY)["last_active"] == T0 + 500  # type: ignore[index]
    d.keep_scores([score(AMY)], "check", at=T0 + 700)  # its last buy is older than the Feed trade
    d.add([Seen(AMY, "follow")], at=T0 + 800)
    assert d.wallet(AMY)["last_active"] == T0 + 500  # type: ignore[index]
    d.add([Seen(AMY, "feed", "Sports", traded_at=T0 + 900)], at=T0 + 950)
    assert d.wallet(AMY)["last_active"] == T0 + 900  # type: ignore[index]


def test_a_score_from_before_last_buy_takes_its_latest_checked_bet() -> None:
    from signals.directory import last_trade, main_category

    old = score(AMY, last_buy=None, recent=[{"at": "2026-10-03T00:00:00+00:00"}], sample=[{"at": "2026-10-05T00:00:00+00:00"}])
    assert last_trade(old) == datetime(2026, 10, 5, tzinfo=UTC).timestamp()
    assert last_trade(score(AMY, last_buy=None, recent=[], sample=[])) is None
    assert main_category(score(AMY, categories=[])) is None


def test_a_phase_1_directory_gets_the_new_columns_and_keeps_its_wallets(tmp_path: Any) -> None:
    path = tmp_path / "directory.db"
    with sqlite3.connect(path) as con:  # as phase 1 made it
        con.executescript(
            "CREATE TABLE wallets (wallet TEXT PRIMARY KEY, name TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL,"
            " first_source TEXT NOT NULL, first_place TEXT NOT NULL);"
            f"INSERT INTO wallets VALUES ('{AMY}', 'amy', 1.0, 2.0, 'feed', 'Sports');"
        )
    d = Directory(path)
    w = d.wallet(AMY)
    assert w is not None and (w["name"], w["status"], w["scored_at"], w["last_active"]) == ("amy", "found", None, None)
    d.keep_scores([score(AMY)], "check", at=T0)
    assert d.wallet(AMY)["status"] == "scored"  # type: ignore[index]


def test_everything_is_still_there_after_a_restart(tmp_path: Any) -> None:
    Directory(tmp_path / "directory.db").keep_scores([score(AMY)], "check", at=T0)
    again = Directory(tmp_path / "directory.db")  # a new process, the same file on the volume
    assert again.wallet(AMY)["status"] == "scored" and again.score(AMY)["segment"] == "rising"  # type: ignore[index]


def test_stats_count_evaluations_without_naming_any_wallet(d: Directory) -> None:
    d.add([Seen(CAL, "search")], at=T0)
    d.keep_scores([score(AMY), score(BEN, "lucky", last_buy="2026-09-01T00:00:00+00:00")], "check", at=T0)
    d.score_failed(BEN, "VenueError: timeout", at=T0 + 10)
    e = d.stats(now=T0 + 3600)["evaluation"]
    assert e["by_status"] == {"found": 1, "scored": 1, "checked": 0, "failed": 1}
    assert e["scored_last_day"] == 2 and e["latest_score_by"] == {"check": 2, "page": 0, "import": 0, "background": 0}
    assert e["by_segment"] == {"lucky": 1, "rising": 1} and e["by_category"] == {"Sports": 2}
    assert e["strong_by_category"] == {"Esports": 2}
    assert (e["last_active_known"], e["active_last_week"], e["scores_kept"]) == (2, 1, 2)
    assert not any(w in repr(d.stats()) for w in (AMY, BEN, CAL))


# ---- the engine ----


def test_the_last_checks_scores_are_imported_on_launch_once(store: Any) -> None:
    store.joinpath("worth-following.json").write_text(json.dumps({
        "format": 7, "finished": "2026-10-12T15:00:00+00:00", "scores": [score(AMY), score(BEN, "no_edge")]}))  # fmt: skip
    engine._directory_later(engine._directory_import)
    flush()
    d = engine.directory()
    assert d.wallet(AMY)["scored_at"] == T0 and d.score(AMY)["via"] == "import" and d.score(AMY)["format"] == 7  # type: ignore[index]
    assert d.stats()["evaluation"]["by_status"]["scored"] == 2
    engine._directory_later(engine._directory_import)  # the next launch
    flush()
    assert len(d.history(AMY)) == 1 and engine._directory_state.get("note_error") is None


def test_nothing_to_import_without_a_kept_check(store: Any) -> None:
    engine._directory_later(engine._directory_import)
    flush()
    assert engine.directory().stats()["wallets"] == 0 and engine._directory_state.get("note_error") is None


def test_a_check_keeps_every_score_and_wallets_it_dropped_keep_theirs(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = iter([[score(AMY), score(BEN)], [score(AMY, "proven")]])
    finished = iter(["2026-10-12T15:00:00+00:00", "2026-10-12T21:00:00+00:00"])

    def discover(wallets: int, on_progress: Any) -> Any:
        scores = next(runs)
        return SimpleNamespace(to_dict=lambda sample: {"scores": [dict(s) for s in scores], "counts": {}, "coverage": {},
                                                       "sources": {}, "failed": 0, "started": "x", "finished": next(finished)})  # fmt: skip

    monkeypatch.setattr(engine.reads.whales, "scorer", lambda **k: None)
    monkeypatch.setattr(engine, "background", lambda: SimpleNamespace(discover=discover))
    engine._worth_run()
    engine._worth_run()
    flush()
    d = engine.directory()
    assert engine._worth["error"] is None and len(engine._worth_load()["scores"]) == 1  # type: ignore[index]
    assert (d.wallet(AMY)["segment"], d.score(AMY)["via"], d.score(AMY)["format"]) == ("proven", "check", engine.WORTH_FORMAT)  # type: ignore[index]
    assert d.wallet(BEN)["status"] == "scored" and d.score(BEN)["segment"] == "rising"  # type: ignore[index]  # not dropped
    assert [h["segment"] for h in d.history(AMY)] == ["rising", "proven"]


def test_the_check_still_finishes_when_the_directory_cant_be_written(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def discover(wallets: int, on_progress: Any) -> Any:
        return SimpleNamespace(to_dict=lambda sample: {"scores": [score(AMY)], "finished": "2026-10-12T15:00:00+00:00"})

    def broken(*a: Any, **k: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(engine.reads.whales, "scorer", lambda **k: None)
    monkeypatch.setattr(engine, "background", lambda: SimpleNamespace(discover=discover))
    monkeypatch.setattr(Directory, "keep_scores", broken)
    engine._worth_run()
    flush()
    assert engine._worth["error"] is None and engine._worth_load()["scores"][0]["wallet"] == AMY  # type: ignore[index]
    assert "disk I/O error" in engine._directory_state["note_error"]


def test_a_wallet_scored_on_its_page_is_kept_and_one_that_cant_be_is_marked_failed(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine.reads.whales, "score", lambda w: SimpleNamespace(to_dict=lambda: score(w)))
    engine._score_of(CAL)
    flush()
    d = engine.directory()
    kept = d.score(CAL)
    assert kept is not None and kept["via"] == "page" and kept["format"] == engine.WORTH_FORMAT
    assert d.wallet(CAL)["status"] == "scored"  # type: ignore[index]

    def fails(w: str) -> Any:
        raise VenueError("venue_unavailable", "Polymarket didn't answer", venue="polymarket")

    monkeypatch.setattr(engine.reads.whales, "score", fails)
    with pytest.raises(engine.HTTPException):
        engine._score_of(BEN)
    flush()
    w = d.wallet(BEN)
    assert w is not None and w["status"] == "failed" and "didn't answer" in w["score_error"]


def test_a_feed_trade_sets_when_the_trader_was_last_active(store: Any) -> None:
    at = datetime(2026, 10, 13, 9, 30, tzinfo=UTC)
    t = WhaleTrade("polymarket", AMY, "amy", "0xm9:0", "yes", "buy", 0.5, 2000, 1000.0, at, "t1", title="Rain?")
    engine._feed_noted.clear()
    engine._feed_note([t], {"0xm9:0": "Weather"})
    flush()
    assert engine.directory().wallet(AMY)["last_active"] == at.timestamp()  # type: ignore[index]
