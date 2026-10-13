"""The trader directory: wallets found from leaderboards, markets, the Feed, searches and follows, kept once each with
every place they were seen (no network: the SDK's reads are stand-ins)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from uselayer import VenueError, WhaleTrade

from signals import app as engine
from signals.directory import Directory, Seen, from_sighting

AMY = "0x" + "a1" * 20
BEN = "0x" + "b2" * 20
CAL = "0x" + "c3" * 20


def board(wallet: str, where: str = "PNL MONTH POLITICS", rank: int = 1, name: str | None = None) -> Any:
    cat = {"POLITICS": "Politics", "SPORTS": "Sports"}.get(where.split()[-1])
    return SimpleNamespace(wallet=wallet, source="leaderboard", where=where, name=name, category=cat, rank=rank,
                           title=None, trades=0, usd=0.0)  # fmt: skip


def in_market(wallet: str, cid: str = "0xm1", cat: str = "Sports", trades: int = 2, usd: float = 40.0) -> Any:
    return SimpleNamespace(wallet=wallet, source="market", where=cid, name=None, category=cat, rank=None,
                           title=f"Market {cid}?", trades=trades, usd=usd)  # fmt: skip


def feed_trade(wallet: str, trade_id: str, market: str = "0xm9:0", usd: float = 500.0) -> WhaleTrade:
    return WhaleTrade("polymarket", wallet, f"{wallet[:6]}…{wallet[-4:]}", market, "yes", "buy", 0.5, usd * 2, usd,
                      datetime(2026, 10, 13, tzinfo=UTC), trade_id, title="Will it rain?")  # fmt: skip


@pytest.fixture
def d(tmp_path: Any) -> Directory:
    return Directory(tmp_path / "directory.db")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    engine._feed_noted.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": [
        {"wallet": AMY, "name": "amy", "segment": "rising", "copyable": "no", "copy": None, "sample": []}]})  # fmt: skip
    monkeypatch.setattr(engine, "_worth_one", {})
    monkeypatch.setattr(engine, "_categories", lambda markets: {m: "Weather" for m in markets})
    return TestClient(engine.app, client=("127.0.0.1", 50000))  # this machine: the counts are the owner's


# ---- the store ----


def test_a_wallet_seen_in_several_places_is_one_wallet_with_every_place(d: Directory) -> None:
    new = d.add([from_sighting(board(AMY, name="amy")), from_sighting(board(AMY, "VOL WEEK SPORTS", 7)),
                 from_sighting(in_market(AMY)), from_sighting(board(BEN))], at=100.0)  # fmt: skip
    assert new == 2
    w = d.wallet(AMY)
    assert w is not None and (w["name"], w["first_source"], w["first_place"]) == ("amy", "leaderboard", "PNL MONTH POLITICS")
    places = {(s["source"], s["place"]) for s in d.sources_of(AMY)}
    assert places == {("leaderboard", "PNL MONTH POLITICS"), ("leaderboard", "VOL WEEK SPORTS"), ("market", "Sports")}


def test_seeing_a_wallet_again_keeps_when_it_was_first_found_and_updates_the_rest(d: Directory) -> None:
    d.add([from_sighting(board(AMY, rank=9)), from_sighting(in_market(AMY, "0xm1"))], at=100.0)
    assert d.add([from_sighting(board(AMY, rank=3)), from_sighting(in_market(AMY, "0xm2", usd=10.0)),
                  Seen(AMY, "follow")], at=200.0) == 0  # fmt: skip
    w = d.wallet(AMY)
    assert w is not None and (w["first_seen"], w["last_seen"], w["first_source"]) == (100.0, 200.0, "leaderboard")
    by = {(s["source"], s["place"]): s for s in d.sources_of(AMY)}
    lb, mk = by[("leaderboard", "PNL MONTH POLITICS")], by[("market", "Sports")]
    assert (lb["rank"], lb["times"], lb["first_seen"], lb["last_seen"]) == (3, 2, 100.0, 200.0)  # their latest rank
    # markets are kept per category, the last one as the example: the directory grows with wallets, not markets
    assert (mk["times"], mk["trades"], mk["usd"], mk["example"], mk["example_title"]) == (2, 4, 50.0, "0xm2", "Market 0xm2?")
    assert ("follow", "") in by


def test_a_name_is_kept_when_a_later_sighting_has_none(d: Directory) -> None:
    d.add([from_sighting(board(AMY, name="amy"))])
    d.add([from_sighting(in_market(AMY))])
    w = d.wallet(AMY)
    assert w is not None and w["name"] == "amy"


def test_stats_count_wallets_by_source_and_category_without_naming_any(d: Directory) -> None:
    d.add([from_sighting(board(AMY)), from_sighting(in_market(AMY)), from_sighting(in_market(BEN, cat="Crypto")),
           Seen(CAL, "search")], at=1000.0)  # fmt: skip
    s = d.stats(now=1000.0 + 3600)
    assert s["wallets"] == 3 and s["new_last_day"] == 3
    assert s["by_source"] == {"leaderboard": 1, "market": 2, "feed": 0, "search": 1, "follow": 0}
    assert s["only_from"] == {"leaderboard": 0, "market": 1, "feed": 0, "search": 1, "follow": 0}
    assert s["first_found_by"]["leaderboard"] == 1 and s["first_found_by"]["market"] == 1
    assert s["by_category"] == {"Politics": 1, "Sports": 1, "Crypto": 1}
    assert s["leaderboards_seen"] == 1
    assert not any(w in repr(s) for w in (AMY, BEN, CAL))
    assert d.stats(now=1000.0 + 2 * 86400)["new_last_day"] == 0


def test_a_sweep_is_counted_only_once_it_finishes_without_an_error(d: Directory) -> None:
    assert d.last_sweep() is None
    a = d.sweep_started(at=10.0)
    d.sweep_progress(a, 5, 2)
    d.sweep_finished(a, at=20.0)
    b = d.sweep_started(at=30.0)
    d.sweep_finished(b, "VenueError: down", at=31.0)
    assert d.last_sweep() == {"started": 10.0, "finished": 20.0, "sightings": 5, "new": 2}
    assert d.last_error() == {"started": 30.0, "finished": 31.0, "error": "VenueError: down"}


# ---- what the app keeps ----


def test_the_sweep_keeps_followed_wallets_then_everything_the_sdk_finds(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    engine._save_follows({"users": {"user_1": {"follows": [CAL], "chat": None}}})
    asked: dict[str, Any] = {}

    def find_wallets(**kw: Any) -> Any:
        asked.update(kw)
        yield board(AMY)
        yield in_market(BEN)
        yield board(BEN, "PNL MONTH SPORTS", 4)

    monkeypatch.setattr(engine.reads.whales, "find_wallets", find_wallets, raising=False)
    monkeypatch.setattr(engine, "DIRECTORY_BATCH", 2)
    assert engine.directory_sweep() == {"sightings": 4, "new": 3, "error": None}
    assert asked == {"per_second": engine.DIRECTORY_RATE}  # held below what the SDK allows, so the site keeps reads
    s = api.get("/api/directory").json()
    assert s["wallets"] == 3 and s["by_source"]["follow"] == 1 and s["by_source"]["market"] == 1
    assert s["sweeps_finished"] == 1 and s["last_sweep"]["sightings"] == 4 and s["last_error"] is None


def test_a_sweep_that_fails_keeps_what_it_found_and_says_why(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def find_wallets(**kw: Any) -> Any:
        yield board(AMY)
        raise VenueError("venue_unavailable", "Polymarket is down.", venue="polymarket")

    monkeypatch.setattr(engine.reads.whales, "find_wallets", find_wallets, raising=False)
    r = engine.directory_sweep()
    assert r["sightings"] == 1 and "Polymarket is down" in r["error"]
    assert engine.directory().wallet(AMY) is not None
    assert engine.directory().last_sweep() is None and "Polymarket is down" in engine.directory().last_error()["error"]


def test_a_sweep_stops_when_the_app_shuts_down(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()

    def find_wallets(**kw: Any) -> Any:
        yield board(AMY)
        stop.set()
        yield board(BEN)

    monkeypatch.setattr(engine.reads.whales, "find_wallets", find_wallets, raising=False)
    r = engine.directory_sweep(stop)
    assert r["sightings"] == 1 and r["error"] is not None and engine.directory().wallet(BEN) is None


def test_traders_in_the_feeds_big_trades_are_kept_once_by_their_markets_category(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    trades = [feed_trade(AMY, "t1"), feed_trade(BEN, "t2", usd=250.0)]
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: list(trades))
    assert len(api.get("/api/trading-now").json()) == 2
    api.get("/api/trading-now")  # the same trades again: not kept twice
    s = engine.directory().sources_of(AMY)
    assert [(x["source"], x["place"], x["category"], x["times"], x["example"], x["example_title"], x["usd"]) for x in s] == [
        ("feed", "Weather", "Weather", 1, "0xm9", "Will it rain?", 500.0)]  # fmt: skip
    w = engine.directory().wallet(AMY)
    assert w is not None and w["name"] is None  # the SDK's short wallet stands in for no name: not theirs


def test_the_feed_still_works_when_the_directory_cant_be_written(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: [feed_trade(AMY, "t1")])

    def broken(self: Directory, seen: Any, at: float | None = None) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Directory, "add", broken)
    assert api.get("/api/trading-now").status_code == 200


def test_a_wallet_opened_that_the_last_check_didnt_have_is_kept_as_a_search(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    score = {"wallet": BEN, "name": None, "segment": "no_edge", "copyable": "unknown", "copy": None, "sample": []}
    monkeypatch.setattr(engine.reads.whales, "score", lambda w, **k: SimpleNamespace(to_dict=lambda: score | {"wallet": w}))
    assert api.get(f"/api/worth/{BEN.upper().replace('0X', '0x')}").status_code == 200
    assert [x["source"] for x in engine.directory().sources_of(BEN)] == ["search"]
    api.get(f"/api/worth/{AMY}")  # in the last check: opened from Traders, not a search
    assert engine.directory().wallet(AMY) is None


def test_the_directorys_counts_are_the_owners_only(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    r = api.get("/api/directory")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    assert TestClient(engine.app, client=("203.0.113.9", 4000)).get("/api/directory").status_code == 404
    monkeypatch.setattr(engine, "HOSTED", True)
    assert api.get("/api/directory").status_code == 404  # hosted: only with the owner's key, as /api/metrics
