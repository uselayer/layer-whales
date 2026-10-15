"""The directory, phase 6: monitoring and measurement. The funnel (found, screened, fully evaluated, recommended,
qualified) overall, by category and by where wallets were found; each day's new wallets, scores and counts kept across
restarts; how fresh scores are and what's waiting; reads by host and how they ended; what the engine costs; and the
owner's /api/monitoring with its warnings (no network: reads are stand-ins)."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from signals import app as engine
from signals import monitoring
from signals.budget import BackgroundHttp, Budget
from signals.directory import Directory, QuickLook, Seen
from signals.monitoring import Usage, api_counts, cost, day_of

AMY = "0x" + "a1" * 20
BEN = "0x" + "b2" * 20
CAL = "0x" + "c3" * 20
DAN = "0x" + "d4" * 20
EVE = "0x" + "e5" * 20
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC).timestamp()
DAY = 86400


def score(wallet: str, segment: str = "rising", category: str = "Sports", strong: bool = False) -> dict[str, Any]:
    return {"wallet": wallet, "name": wallet, "segment": segment, "bets": 12,
            "categories": [{"category": category, "bets": 12, "strong": strong}]}  # fmt: skip


@pytest.fixture
def d(tmp_path: Any) -> Directory:
    return Directory(tmp_path / "directory.db")


@pytest.fixture
def counts(monkeypatch: pytest.MonkeyPatch) -> Usage:
    fresh = Usage(clock=lambda: NOW)
    monkeypatch.setattr(monitoring, "usage", fresh)
    return fresh


def directory_of_five(d: Directory, now: float = NOW) -> None:
    """AMY: on the monthly board (rank 40), recommended with a fresh score, strong in Sports. BEN: in a Sports market
    only, recommended but scored 3 days ago. CAL: in a market, quick-checked (too few bets). DAN: on a board (rank
    2,000), never looked at. EVE: followed only, scored and failed since."""
    d.add([Seen(AMY, "leaderboard", "PNL MONTH OVERALL", category=None, rank=40)], at=now - 3 * DAY)
    d.add([Seen(BEN, "market", "Sports", category="Sports"), Seen(CAL, "market", "Politics", category="Politics")],
          at=now - DAY + 60)  # fmt: skip
    d.add([Seen(DAN, "leaderboard", "VOL WEEK SPORTS", category="Sports", rank=2000), Seen(EVE, "follow")], at=now - 60)
    d.keep_scores([score(AMY, "proven", strong=True)], "background", at=now - 3600)
    d.keep_scores([score(BEN, "rising")], "check", at=now - 3 * DAY)
    d.quick_checked(CAL, QuickLook(False, "not_enough_recent_evidence", 2, 4, None, None), at=now - 7200)
    d.keep_scores([score(EVE, "no_edge", "Politics")], "page", at=now - 2 * DAY)
    d.score_failed(EVE, "VenueError: polymarket didn't answer in time (attempt 3).", at=now - 600)


# ---- the counts kept in memory, and by day in directory.db ----


def test_counts_are_summed_and_a_max_keeps_the_days_largest_across_flushes(d: Directory) -> None:
    u = Usage(clock=lambda: NOW)
    u.add("api", "data-api.polymarket.com 2xx", 3)
    u.add("api", "data-api.polymarket.com 2xx")
    u.add("infra_max", "ram_bytes", 500)
    u.add("infra_max", "ram_bytes", 300)
    d.tally(u.take())
    assert u.take() == []
    u.add("api", "data-api.polymarket.com 2xx", 2)
    u.add("infra_max", "ram_bytes", 400)
    u.add("api", "data-api.polymarket.com 2xx", at=NOW - DAY)  # yesterday's
    d.tally(u.take())
    t = {(day, m, k): n for day, m, k, n in sqlite3.connect(d.path).execute("SELECT * FROM daily")}
    today, yesterday = day_of(NOW), day_of(NOW - DAY)
    assert t == {(today, "api", "data-api.polymarket.com 2xx"): 6, (today, "infra_max", "ram_bytes"): 500,
                 (yesterday, "api", "data-api.polymarket.com 2xx"): 1}  # fmt: skip
    assert u.since_launch("api") == {"data-api.polymarket.com 2xx": 7} and u.since_launch("infra_max") == {"ram_bytes": 500}


def test_counts_that_couldnt_be_written_are_kept_for_the_next_try() -> None:
    u = Usage(clock=lambda: NOW)
    u.add("lane", "background", 2)
    rows = u.take()
    u.add("lane", "background", 1)
    u.put_back(rows)
    assert u.take() == [(day_of(NOW), "lane", "background", 3)]


def test_every_request_the_sdks_client_sends_is_counted_by_host_and_how_it_ended() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/down":
            raise httpx.ConnectError("no route", request=request)
        return httpx.Response({"/ok": 200, "/slow": 429, "/gone": 404, "/err": 503}.get(request.url.path, 404))

    u = Usage(clock=lambda: NOW)
    client = httpx.Client(transport=httpx.MockTransport(answer))
    monitoring.watch(client, u)
    for path in ("/ok", "/ok", "/slow", "/gone", "/err"):
        client.get(f"https://data-api.polymarket.com{path}")
    with pytest.raises(httpx.ConnectError):
        client.get("https://data-api.polymarket.com/down")
    client.get("https://api.telegram.org/ok")
    client.get(f"https://data-api.polymarket.com/positions?user={AMY}&market=0xabc")
    got = api_counts(u.since_launch("api_sent"), u.since_launch("api"))
    # which endpoint failed: the path's first part only (the wallet in the query never kept)
    assert u.since_launch("api_failed") == {"data-api.polymarket.com /slow 429": 1, "data-api.polymarket.com /gone 404": 1,
                                            "data-api.polymarket.com /err 503": 1, "data-api.polymarket.com /positions 404": 1}
    assert got == {"data-api.polymarket.com": {"requests": 7, "2xx": 2, "429": 1, "404": 2, "503": 1, "error": 1},
                   "api.telegram.org": {"requests": 1, "2xx": 1, "error": 0}}  # fmt: skip


def test_the_engines_own_client_is_watched() -> None:
    hooks = engine.reads._http._client.event_hooks
    assert len(hooks["request"]) >= 1 and len(hooks["response"]) >= 1


def test_reads_from_each_budget_are_counted_as_its_own(counts: Usage) -> None:
    class Http:
        def request_with_headers(self, method: str, url: str, **kw: Any) -> Any:
            return {}, {}

    BackgroundHttp(Http(), Budget(1000)).request("GET", "https://data-api.polymarket.com/trades")
    BackgroundHttp(Http(), Budget(1000, name="check")).request("GET", "https://gamma-api.polymarket.com/x")
    BackgroundHttp(Http(), Budget(1000, name="check")).request("GET", "https://polygon-rpc.com/")  # not Polymarket's
    assert counts.since_launch("lane") == {"background": 1, "check": 1}


def test_what_the_engine_uses_is_priced_at_railways_list_rates() -> None:
    month = monitoring.MONTH_S
    got = cost({"cpu_s": month, "ram_gb_s": 2 * month, "volume_gb_s": 10 * month, "egress_bytes": 4 * monitoring.GB})
    assert got == {"cpu": 20.0, "ram": 20.0, "volume": 1.5, "egress": 0.2, "total": 41.7}  # one vCPU, 2 GB, 10 GB


def test_the_sampler_counts_cpu_memory_and_the_volume(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    u = Usage(clock=lambda: NOW)
    clock = iter([0.0, 60.0, 120.0])
    monkeypatch.setattr(monitoring, "_rss_bytes", lambda: monitoring.GB // 2)
    monkeypatch.setattr(monitoring, "_container_bytes", lambda: None)  # not Linux: the process's own memory
    s = monitoring.Sampler(tmp_path, u, clock=lambda: next(clock))
    s.sample()
    s.sample()
    infra = u.since_launch("infra")
    assert infra["ram_gb_s"] == pytest.approx(60.0) and infra["cpu_s"] >= 0  # half a GB for two minutes
    assert infra["volume_gb_s"] > 0 and u.since_launch("infra_max")["ram_bytes"] == monitoring.GB // 2


def test_the_bytes_the_engine_serves_are_counted(counts: Usage, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    r = TestClient(engine.app).get("/favicon.ico")
    assert r.status_code == 200 and counts.since_launch("infra")["served_bytes"] == len(r.content)


def test_memory_is_the_containers_as_railway_bills_it_and_egress_every_byte_sent(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = tmp_path / "dev"
    dev.write_text("Inter-|   Receive  |  Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes ...\n"
                   "    lo: 900 9 0 0 0 0 0 0 5000 9 0 0 0 0 0 0\n  eth0: 100 1 0 0 0 0 0 0 2000 3 0 0 0 0 0 0\n")  # fmt: skip
    assert monitoring._tx_bytes(str(dev)) == 2000  # loopback left out
    sent = iter([2000, 2600, 2900])
    monkeypatch.setattr(monitoring, "_tx_bytes", lambda: next(sent))
    monkeypatch.setattr(monitoring, "_container_bytes", lambda: monitoring.GB)  # the file cache too
    monkeypatch.setattr(monitoring, "_rss_bytes", lambda: monitoring.GB // 2)
    u = Usage(clock=lambda: NOW)
    clock = iter([0.0, 60.0, 120.0])
    s = monitoring.Sampler(tmp_path, u, clock=lambda: next(clock))
    s.sample()
    s.sample()
    infra = u.since_launch("infra")
    assert infra["egress_bytes"] == 900 and infra["ram_gb_s"] == pytest.approx(120.0)
    assert cost({"served_bytes": monitoring.GB})["egress"] == 0.05  # off Linux, the pages served stand in


# ---- what the directory says ----


def test_the_funnel_goes_found_screened_evaluated_recommended_qualified(d: Directory) -> None:
    directory_of_five(d)
    r = d.monitoring(presented=[AMY], days=3, now=NOW)
    # screened: AMY, BEN, EVE (scores) and CAL (a quick check); evaluated: the three scored; recommended: AMY and
    # BEN; qualified: AMY only (BEN's score is past 48 h)
    assert {k: r["funnel"][k] for k in ("found", "screened", "fully_evaluated", "recommended", "qualified")} == {
        "found": 5, "screened": 4, "fully_evaluated": 3, "recommended": 2, "qualified": 1}  # fmt: skip
    assert r["funnel"]["qualified_per_1000_found"] == 200.0 and r["funnel"]["qualified_share_of_evaluated"] == 0.3333


def test_dropouts_entries_and_re_entries_as_traders_are_scored_again(d: Directory) -> None:
    """Each score against the wallet's score before it: AMY stays recommended (rising → proven is kept), BEN drops
    to no_edge then comes back (a re-entry), CAL enters on its first score, DAN's import-to-score pair isn't compared,
    and EVE's score before the window still counts as the one before (a dropout on the window's first day)."""
    d.add([Seen(w, "market", "Sports", category="Sports") for w in (AMY, BEN, CAL, DAN, EVE)], at=NOW - 9 * DAY)
    d.keep_scores([score(EVE, "rising")], "background", at=NOW - 5 * DAY)  # before the 3-day window
    d.keep_scores([score(EVE, "too_few")], "background", at=NOW - 2 * DAY)
    d.keep_scores([score(AMY, "rising")], "background", at=NOW - 2 * DAY + 60)
    d.keep_scores([score(AMY, "proven")], "background", at=NOW - DAY)
    d.keep_scores([score(BEN, "quiet")], "background", at=NOW - 2 * DAY + 120)
    d.keep_scores([score(BEN, "no_edge")], "background", at=NOW - DAY + 60)
    d.keep_scores([score(BEN, "quiet")], "background", at=NOW - 3600)
    d.keep_scores([score(CAL, "rising")], "background", at=NOW - 7200)
    d.keep_scores([score(DAN, "rising")], "import", at=NOW - 2 * DAY + 180)
    d.keep_scores([score(DAN, "no_edge")], "background", at=NOW - 1800)
    q = d.monitoring(days=3, now=NOW)["qualification"]
    days = {x["day"]: x for x in q["by_day"]}
    assert [x["day"] for x in q["by_day"]] == ["2026-10-14", "2026-10-13", "2026-10-12"]  # newest first
    first, second, today = days["2026-10-12"], days["2026-10-13"], days["2026-10-14"]
    # 10-12: EVE drops (her rising score was before the window); AMY and BEN enter; DAN's import isn't a pair
    assert (first["dropped"], first["dropped_to"], first["entered"], first["net"]) == (1, {"too_few": 1}, 2, 1)
    assert (second["rescored"], second["kept"], second["dropped"], second["dropped_to"]) == (2, 1, 1, {"no_edge": 1})
    # today: BEN back (a re-entry), CAL's first score (an entry); DAN's score after his import isn't compared
    assert (today["re_entered"], today["entered"], today["dropped"], today["net"]) == (1, 1, 0, 2)
    assert q["skipped_import"] == 2  # DAN's imported score (not an entry) and the score after it (not a dropout)
    assert (q["window"]["rescored"], q["window"]["kept"], q["window"]["dropped"]) == (3, 1, 2)
    assert q["window"]["kept_share"] == 0.333 and q["window"]["dropped_to"] == {"too_few": 1, "no_edge": 1}
    assert (q["recommended_now"], q["ever_recommended"]) == (3, 5)  # AMY, BEN, CAL now; EVE and DAN once


def test_coverage_and_qualified_traders_by_category(d: Directory) -> None:
    directory_of_five(d)
    c = d.monitoring(days=3, now=NOW)["by_category"]
    assert (c["Sports"]["found"], c["Sports"]["fully_evaluated"], c["Sports"]["qualified"], c["Sports"]["strong_in"]) == (3, 2, 1, 1)
    assert (c["Politics"]["found"], c["Politics"]["screened"], c["Politics"]["qualified"]) == (2, 2, 0)  # CAL and EVE


def test_which_sources_find_traders_who_qualify(d: Directory) -> None:
    directory_of_five(d)
    s = d.monitoring(days=3, now=NOW)["sources"]
    assert s["seen_in"]["leaderboard"]["found"] == 2 and s["seen_in"]["leaderboard"]["qualified"] == 1
    assert s["seen_in"]["market"]["found"] == 2 and s["seen_in"]["market"]["recommended"] == 1
    assert s["only_seen_in"]["follow"]["found"] == 1 and s["first_found_by"]["leaderboard"]["qualified"] == 1
    assert set(s["leaderboards"]) == {"PNL MONTH", "VOL WEEK"} and s["leaderboards"]["PNL MONTH"]["qualified"] == 1
    assert {k: v["found"] for k, v in s["best_rank"].items()} == {"top_100": 1, "beyond_1000": 1, "no_board": 3}


def test_each_day_new_wallets_scores_and_the_counts_kept(d: Directory) -> None:
    directory_of_five(d)
    d.tally([(day_of(NOW), "evaluated", "new:not_enough_recent_evidence", 4), (day_of(NOW), "evaluated", "new:scored", 2)])
    days = d.monitoring(days=4, now=NOW)["by_day"]
    assert [x["day"] for x in days] == ["2026-10-14", "2026-10-13", "2026-10-12", "2026-10-11"]
    today, yesterday = days[0], days[1]
    assert (today["new_wallets"], today["new_by_first_source"]) == (2, {"leaderboard": 1, "follow": 1})
    assert yesterday["new_wallets"] == 2 and days[3]["new_wallets"] == 1  # BEN and CAL; AMY three days back
    assert today["scores_by"] == {"background": 1} and days[3]["scores_by"] == {"check": 1}
    assert today["tallies"]["evaluated"] == {"new:not_enough_recent_evidence": 4, "new:scored": 2}


def test_freshness_and_what_is_waiting(d: Directory) -> None:
    directory_of_five(d)
    f = d.monitoring(presented=[AMY], days=3, now=NOW)["freshness"]
    assert f["recommended"]["shown"]["traders"] == 1 and f["recommended"]["shown"]["past_fresh"] == 0
    assert f["recommended"]["others"]["traders"] == 1 and f["recommended"]["others"]["past_fresh"] == 1  # BEN, 72 h
    assert f["score_age"] == {"under_1d": 1, "1_2d": 0, "2_7d": 2, "7_30d": 0, "over_30d": 0}
    assert f["quick_check_age"]["under_1d"] == 1
    assert f["due_now"]["new"]["wallets"] == 1 and f["due_now"]["recommended"]["wallets"] == 1  # DAN; BEN's daily rescore
    assert f["never_looked_at"] == {"wallets": 1, "oldest_waiting_days": 0.0}


def test_failures_are_counted_by_how_they_failed(d: Directory) -> None:
    directory_of_five(d)
    d.score_failed(DAN, "VenueError: polymarket didn't answer in time (attempt 5).", at=NOW - 60)
    f = d.monitoring(days=1, now=NOW)["failures"]
    assert f["failed_now"] == 2 and f["failed_last_day"] == 2
    assert f["why"] == {"VenueError: polymarket didn't answer in time (attempt …).": 2}


# ---- the owner's report ----


@pytest.fixture
def api(counts: Usage, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "presented_wallets", lambda: frozenset({AMY, BEN}))
    return TestClient(engine.app, client=("127.0.0.1", 50000))


def test_the_report_has_every_part_in_counts_only(api: TestClient, counts: Usage) -> None:
    directory_of_five(engine.directory(), time.time())  # as of now: the report reads the real clock
    counts.add("api_sent", "data-api.polymarket.com", 10)
    counts.add("api", "data-api.polymarket.com 2xx", 9)
    counts.add("lane", "background", 4)
    counts.add("evaluated", "new:dormant", 3, at=time.time())
    counts.add("evaluated_lane", "board:dormant", 2, at=time.time())
    counts.add("evaluated_lane", "sample:dormant", 1, at=time.time())
    counts.add("infra", "cpu_s", 864, at=time.time())
    engine._monitor_flush()
    r = api.get("/api/monitoring?days=2").json()
    assert set(r) >= {"funnel", "by_category", "sources", "by_day", "freshness", "failures", "sweeps", "api",
                      "infrastructure", "health", "flags"}  # fmt: skip
    assert r["api"]["since_launch"]["data-api.polymarket.com"] == {"requests": 10, "2xx": 9, "error": 1}
    assert r["api"]["polymarket_reads_since_launch"] == {"background": 4, "feed_and_pages": 6}
    today = r["by_day"][0]
    assert today["screened"] == 3 and today["evaluated"] == {"dormant": 3}
    assert today["evaluated_by_lane"] == {"board": {"dormant": 2}, "sample": {"dormant": 1}}
    assert today["infra"]["cost_usd"]["cpu"] == pytest.approx(864 / monitoring.MONTH_S * 20, abs=1e-4)
    assert all(w not in repr(r) for w in (AMY, BEN, CAL, DAN, EVE))


def test_health_says_in_sentences_what_looks_wrong(api: TestClient) -> None:
    d = engine.directory()
    d.keep_scores([score(BEN, "rising")], "check", at=time.time() - 13 * 3600)  # shown, and older than 12 h
    health = api.get("/api/monitoring").json()["health"]
    assert "1 recommended traders people are shown have a score older than 12 h." in health


def test_the_report_is_the_owners_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "HOSTED", True)
    monkeypatch.setattr(engine, "METRICS_KEY", "k" * 32)
    c = TestClient(engine.app, client=("10.0.0.1", 50000))
    assert c.get("/api/monitoring").status_code == 404
    assert c.get("/api/monitoring", headers={"Authorization": "Bearer nope"}).status_code == 404


def test_a_lanes_recommended_traders_are_counted_with_its_outcomes(counts: Usage, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)

    class Sdk:
        def trades(self, *a: Any, **k: Any) -> Any:
            return []

        def score(self, w: str, on_leaderboard: bool = False) -> Any:
            return SimpleNamespace(to_dict=lambda: score(w, "proven" if w == AMY else "no_edge"))

    monkeypatch.setattr(engine, "background", lambda: Sdk())
    for w in (AMY, BEN):  # recommended traders' rescoring needs no quick check, so the lane's score is all there is
        engine.evaluate_one({"wallet": w, "kind": "recommended", "lane": "board", "scored_at": None, "on_leaderboard": True})
    engine.evaluate_one({"wallet": CAL, "kind": "recommended", "scored_at": None, "on_leaderboard": True})  # no lane
    assert counts.since_launch("evaluated_lane") == {"board:recommended": 1}


def test_the_discovery_funnel_leads_the_report_in_explores_words(api: TestClient) -> None:
    directory_of_five(engine.directory(), time.time())  # as of now: the report reads the real clock
    r = api.get("/api/monitoring?days=2").json()
    assert list(r)[:3] == ["discovery_funnel", "health", "data"]
    f = r["discovery_funnel"]
    assert list(f["overall"]) == ["found", "screened", "scored", "passed_our_checks", "coverage", "yield"]
    assert f["overall"]["coverage"] == {"screened_of_found": 0.8, "scored_of_screened": 0.75, "never_looked_at": 1}
    y = f["overall"]["yield"]  # 1 of 3 scored passed: a wide range, and too few scored to know the yield
    assert y["passed_of_scored"] == 0.3333 and y["yield_known"] is False
    assert y["passed_of_scored_range"][0] < 0.3333 < y["passed_of_scored_range"][1]
    assert set(f) >= {"by_source", "by_source_only", "by_first_source", "by_leaderboard", "by_best_rank", "by_category"}
    assert f["by_source"]["market"]["found"] == 2 and "Sports" in f["by_category"]


def test_days_before_live_counting_say_not_counted_rather_than_zero(api: TestClient, counts: Usage) -> None:
    directory_of_five(engine.directory(), time.time())  # wallets found days ago: history, from directory.db
    counts.add("evaluated", "new:dormant", 1, at=time.time())
    engine._monitor_flush()
    days = api.get("/api/monitoring?days=4").json()["by_day"]
    assert days[0]["live_counts"] is True and days[0]["screened"] == 1
    assert all(d["live_counts"] is False and d["screened"] is None and d["api"] is None for d in days[1:])
    assert sum(d["new_wallets"] for d in days) >= 3  # history is there for every day


def test_a_sweep_a_restart_cut_off_says_so_and_errors_leave_addresses_out(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    d = engine.directory()
    d.sweep_finished(d.sweep_started(at=time.time() - 7200), error=f"VenueError: GET /positions?user={AMY} failed")
    d.sweep_finished(d.sweep_started(at=time.time() - 5400), error=engine.SWEEP_STOPPED)  # a deploy stopped it
    d.sweep_started(at=time.time() - 3600)  # cut off: never finished
    monkeypatch.setitem(engine._directory_state, "running", False)
    r = api.get("/api/monitoring?days=2").json()  # 2 days: sweeps an hour or two ago, whatever the hour (UTC)
    assert [s["status"] for s in r["sweeps"]] == ["cut_off_by_restart", "cut_off_by_restart", "failed"]
    assert AMY not in repr(r) and "user=0x…" in r["sweeps"][2]["error"]
    assert not any("stopped early" in h for h in r["health"])  # the latest ended by a deploy: no warning


def test_a_deploy_stopping_the_app_isnt_a_failed_try(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)

    class Shutdown:
        def trades(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    monkeypatch.setattr(engine, "background", lambda: Shutdown())
    engine.directory().add([Seen(AMY, "search")])
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(engine, "_directory_stop", stop)
    with pytest.raises(RuntimeError):
        engine.evaluate_one({"wallet": AMY, "kind": "new", "scored_at": None, "on_leaderboard": False})
    assert engine.directory().wallet(AMY)["status"] == "found"  # type: ignore[index]  # still due, not "failed"
