"""The directory, phase 5: Traders, the Feed's Recommended, alerts and trader pages from the directory's latest
scores (SIGNALS_DISCOVERY=directory), in the shape of the Worth following check's result, with the check kept
beside it to go back to, and the two compared for the owner (no network: scores are kept as the engine keeps them)."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from uselayer.whale_scores import segment_of

from signals import app as engine
from signals.directory import Directory

AMY, BEN, CAL, DAN, EVE = ("0x" + c * 20 for c in ("a1", "b2", "c3", "d4", "e5"))
NOW = time.time()
SECTIONED = ("segment", "section", "copy_label", "copy_ok", "copy_why", "copy_return", "copy_rank", "won", "settled",
             "win_low", "typical_usd", "last_buy", "trades_per_day", "covered_days", "weeks_up", "consistency_low")  # fmt: skip


def stat(z: float, n: int = 38) -> dict[str, Any]:
    return {"mean": 0.025, "margin": 0.035, "n": n, "z": z, "low": -0.01, "high": 0.06}


def score(wallet: str, segment: str = "rising", main: str = "Sports", strong: tuple[str, ...] = (), z: float = 1.4,
          **kw: Any) -> dict[str, Any]:  # fmt: skip
    """A score as the engine keeps it: the SDK's ``to_dict`` with the check's won/settled/odds and its format."""
    cats = [{"category": main, "bets": 30, "edge": stat(z, 30), "copy": None, "strong": main in strong}]
    cats += [{"category": c, "bets": 8, "edge": stat(2.5, 8), "copy": None, "strong": True} for c in strong if c != main]
    return {
        "wallet": wallet, "name": f"trader-{wallet[2:4]}", "segment": segment, "label": "Rising", "skill": "early",
        "copyable": "no", "style": "takes_a_side", "confidence": "low", "active": True, "bets": 38, "decisions": 143,
        "on_leaderboard": wallet == AMY, "edge": stat(z), "copy": stat(0.9), "categories": cats,
        "strong_categories": list(strong), "trading_pnl_window": 1200.0, "pnl": 5000.0, "volume": 90000.0,
        "window": ["2026-09-12T15:00:00+00:00", "2026-10-12T15:00:00+00:00"], "last_buy": "2026-10-12T14:00:00+00:00",
        "recent": [], "coverage": {"bets": 38, "priced_1m": 38, "priced_5m": 38, "priced_1h": 30, "combos": 0},
        "sample": [{"market": f"m{i}", "price": 0.5, "usd": 100.0, "payout": i % 2, "at": "2026-10-01T00:00:00+00:00"}
                   for i in range(40)],
        "won": 20, "settled": 40, "odds": 0.5, "format": engine.WORTH_FORMAT,
    } | kw  # fmt: skip


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat()


def ran_check(*scores: dict[str, Any], at: float = NOW - 1800) -> None:
    """A Worth following check that finished at ``at``: its file, and its scores kept in the directory, as
    ``_worth_run`` keeps them."""
    data = {"scores": [{k: v for k, v in s.items() if k != "format"} for s in scores], "counts": {}, "coverage": {},
            "sources": {}, "failed": 0, "started": iso(at - 1500), "finished": iso(at), "format": engine.WORTH_FORMAT}  # fmt: skip
    engine.worth_file().write_text(json.dumps(data))
    engine._worth_data = None
    engine.directory().keep_scores(engine._with_format(data), "check", at)
    engine._cache.clear()


def scored(*scores: dict[str, Any], at: float = NOW - 600, via: str = "background") -> None:
    """Wallets scored apart from the check (the background evaluation, or a trader page) at ``at``."""
    engine.directory().keep_scores(list(scores), via, at)
    engine._cache.clear()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "_worth_data", None)
    monkeypatch.setattr(engine, "_worth_one", {})
    monkeypatch.setattr(engine, "DISCOVERY", "directory")
    engine._discovery_state["error"] = None
    return TestClient(engine.app, client=("127.0.0.1", 50000))  # this machine: /api/discovery is the owner's


def result(api: TestClient, markets: str = "") -> dict[str, Any]:
    return api.get("/api/worth", params={"markets": markets} if markets else None).json()["result"]  # type: ignore[no-any-return]


def wallets(api: TestClient, markets: str = "") -> list[str]:
    return [s["wallet"] for s in result(api, markets)["scores"]]


# ---- running alongside, and going back ----


def test_by_default_traders_is_the_checks_result_as_before(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "DISCOVERY", "check")
    ran_check(score(AMY))
    scored(score(BEN, "proven"))
    r = result(api)
    assert [s["wallet"] for s in r["scores"]] == [AMY] and "source" not in r
    assert engine._scored(BEN) is False


def test_going_back_to_the_check_is_one_switch_and_shows_its_result_at_once(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ran_check(score(AMY))
    scored(score(BEN, "proven"))
    assert wallets(api) == [BEN, AMY] and result(api)["source"] == "directory"
    monkeypatch.setattr(engine, "DISCOVERY", "check")  # SIGNALS_DISCOVERY=check: the check kept running all along
    assert wallets(api) == [AMY]


def test_with_no_fresh_score_in_the_directory_the_checks_result_is_shown(api: TestClient) -> None:
    ran_check(score(AMY), at=NOW - engine.DISCOVERY_FRESH_S - 3600)
    scored(score(BEN, "proven"), at=NOW - engine.DISCOVERY_FRESH_S - 60)
    r = result(api)
    assert [s["wallet"] for s in r["scores"]] == [AMY] and "source" not in r


def test_when_the_directory_cant_be_read_the_checks_result_is_shown(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ran_check(score(AMY))

    def broken(*a: Any, **k: Any) -> Any:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Directory, "latest_scores", broken)
    assert wallets(api) == [AMY] and "disk I/O error" in engine._discovery_state["error"]


# ---- the same response, the same classifications ----


def test_from_the_directory_worth_keeps_the_checks_shape(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ran_check(score(AMY), score(CAL, "no_edge"), at=NOW - 3600)
    scored(score(BEN, "proven", z=3.0), score(DAN, "rising", z=2.2), at=NOW - 600)
    monkeypatch.setattr(engine, "DISCOVERY", "check")
    before = api.get("/api/worth").json()
    monkeypatch.setattr(engine, "DISCOVERY", "directory")
    engine._cache.clear()
    after = api.get("/api/worth").json()
    assert after.keys() == before.keys() and after["status"] == before["status"]
    assert set(before["result"]) <= set(after["result"])
    assert set(before["result"]["scores"][0]) <= set(after["result"]["scores"][0])
    r = after["result"]
    # as the check orders them: by tier, then the strength of their edge
    assert [s["wallet"] for s in r["scores"]] == [BEN, DAN, AMY, CAL]
    assert r["counts"]["proven"] == 1 and r["counts"]["rising"] == 2 and r["counts"]["no_edge"] == 1
    assert r["sources"] == {"leaderboard": 1, "beyond_leaderboards": 3} and r["coverage"]["bets"] == 4 * 38
    assert (r["started"], r["finished"]) == (iso(NOW - 3600), iso(NOW - 600)) and r["considered"] == 4
    assert all("sample" not in s for s in r["scores"])


def test_the_same_score_gets_the_same_tier_section_and_copy_status_from_either(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ran_check(score(AMY, "proven", copyable="yes", copy={"mean": 0.02, "margin": 0.01, "n": 38, "z": 3.0, "low": 0.01,
                                                         "high": 0.03}), score(BEN, "no_edge"))  # fmt: skip
    monkeypatch.setattr(engine, "DISCOVERY", "check")
    check = {s["wallet"]: s for s in result(api)["scores"]}
    page = api.get(f"/api/worth/{AMY}").json()
    monkeypatch.setattr(engine, "DISCOVERY", "directory")
    engine._cache.clear()
    served = {s["wallet"]: s for s in result(api)["scores"]}
    assert result(api)["source"] == "directory"
    for w in (AMY, BEN):
        assert {k: check[w].get(k) for k in SECTIONED} == {k: served[w].get(k) for k in SECTIONED}
        assert {k for k in served[w] if served[w][k] != check[w].get(k)} <= {"scored_at", "via", "format"}
    assert (served[AMY]["section"], served[AMY]["copy_label"], served[BEN]["section"]) == ("sharp", "Copyable", None)
    assert api.get(f"/api/worth/{AMY}").json()["why"] == page["why"]
    assert engine._copy_offered(AMY) is True


def test_the_latest_score_is_shown_whoever_worked_it_out(api: TestClient) -> None:
    ran_check(score(AMY, "rising"), at=NOW - 3 * 3600)
    scored(score(AMY, "no_edge"), at=NOW - 3600)
    (s,) = result(api)["scores"]
    assert (s["segment"], s["section"], s["via"]) == ("no_edge", None, "background")


def test_a_score_kept_by_an_older_uselayer_is_tiered_by_todays_rules(api: TestClient) -> None:
    old = score(AMY, "too_fast", format=7)
    scored(old)
    (s,) = result(api)["scores"]
    assert s["segment"] == segment_of(old) != "too_fast"


def test_a_trader_without_a_username_takes_the_one_the_directory_saw(api: TestClient) -> None:
    from signals.directory import Seen

    engine.directory().add([Seen(BEN, "leaderboard", "PNL MONTH OVERALL", name="benny", rank=4)])
    scored(score(BEN, "proven", name=BEN))
    assert result(api)["scores"][0]["name"] == "benny"


# ---- the expanded directory: the Feed, alerts, trader pages, and picks ----


def test_the_feed_and_alerts_follow_the_directorys_traders(api: TestClient) -> None:
    ran_check(score(AMY, "no_edge"))
    scored(score(BEN, "proven", copyable="yes", copy={"mean": 0.02, "margin": 0.01, "n": 38, "z": 3.0, "low": 0.01,
                                                      "high": 0.03}))  # fmt: skip
    assert engine._scored(BEN) and engine._copy_offered(BEN)
    rec = [s["wallet"] for s in (engine._worth_load() or {})["scores"] if s["segment"] in engine.FEED_SEGMENTS]
    assert rec == [BEN]


def test_a_trader_page_shows_the_directorys_fresh_score_without_scoring_again(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "DISCOVERY_LIMIT", 1)
    scored(score(AMY, "proven", z=3.0), score(DAN, "no_edge"))

    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("scored again")

    monkeypatch.setattr(engine.reads.whales, "score", never)
    assert wallets(api) == [AMY]
    s = api.get(f"/api/worth/{DAN}").json()
    assert (s["wallet"], s["segment"], s["section"]) == (DAN, "no_edge", None) and len(s["sample"]) == 30


def test_every_recommended_trader_is_shown_whatever_the_limit(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "DISCOVERY_LIMIT", 1)
    scored(score(AMY, "proven", z=3.0), score(BEN, "rising"), score(CAL, "no_edge"))
    assert wallets(api) == [AMY, BEN]  # the limit only bounds the others


def test_a_pick_fills_the_rest_with_traders_in_its_markets_from_the_whole_directory(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "DISCOVERY_LIMIT", 3)
    scored(score(AMY, "proven", "Sports", z=3.0), score(CAL, "rising", "Esports", z=1.0),
           score(BEN, "no_edge", "Sports", z=1.4), score(DAN, "no_edge", "Politics", strong=("Esports",), z=1.0),
           score(EVE, "no_edge", "Crypto", z=0.5))  # fmt: skip
    assert wallets(api) == [AMY, CAL, BEN]  # no pick: every recommended trader, then the best of the rest
    # an interest is their most-traded category or one they're strong in, as the page matches it
    assert wallets(api, "Esports") == [AMY, CAL, DAN] and result(api, "Esports")["interests"] == ["Esports"]
    assert wallets(api, "Crypto") == [AMY, CAL, EVE]
    assert wallets(api, "Nonsense") == [AMY, CAL, BEN]  # only the markets onboarding offers count


def test_explore_lists_what_traders_leaves_out(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from signals.directory import Seen

    monkeypatch.setattr(engine, "DISCOVERY_LIMIT", 1)
    monkeypatch.setattr(engine, "EXPLORE_ON", True)
    engine.directory().add([Seen(w, "leaderboard", "PNL MONTH OVERALL", rank=i) for i, w in enumerate((AMY, BEN, CAL))],
                           at=NOW - 3600)  # fmt: skip
    scored(score(AMY, "proven"), score(BEN, "no_edge"), score(CAL, "edge_losing"))
    assert wallets(api) == [AMY]
    listed = {r["wallet"] for r in api.get("/api/traders", params={"group": "checked"}).json()["rows"]}
    assert listed == {BEN, CAL}  # beyond the limit, still browsable


def test_the_page_sends_the_picked_markets(run_js: Any) -> None:
    got = run_js(("worthUrl",), '[worthUrl(null), worthUrl([]), worthUrl(["Politics", "Sports"])]')
    assert got == ["/api/worth", "/api/worth", "/api/worth?markets=Politics%2CSports"]


# ---- validation: the two compared, for the owner ----


def test_the_owner_sees_the_check_and_the_directory_compared(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    at = NOW - 4 * 3600
    ran_check(score(AMY, "rising"), score(BEN, "rising"), score(DAN, "no_edge"), at=at)
    scored(score(AMY, "rising"), at=at + 3600)  # scored apart from the check: the same
    scored(score(BEN, "no_edge"), at=at + 2 * 3600, via="page")  # the same wallet, another tier
    scored(score(CAL, "proven"), at=at + 600)  # only the directory has them
    from signals.directory import Seen

    engine.directory().add([Seen(AMY, "leaderboard", "PNL MONTH OVERALL", rank=3)])
    r = api.get("/api/discovery").json()
    c, d = r["check"], r["directory"]
    # where each list's traders come from, who scored them, and how fresh
    assert (c["on_leaderboard"], c["seen_on_any_board"], c["recommended_on_leaderboard"], c["recommended_on_no_board"]) == (1, 1, 1, 1)
    assert c["scored_by"] == {"check": 3} and c["age_hours"]["oldest"] == pytest.approx(4.0, abs=0.1)
    assert d["scored_by"] == {"background": 2, "page": 1, "check": 1} and d["recommended_on_no_board"] == 1
    assert d["recommended_by_category"] == {"Sports": 2} and d["recommended_older_than_12h"] == 0
    assert d["age_hours"]["newest"] == pytest.approx(2.0, abs=0.1) and d["age_hours"]["oldest"] == pytest.approx(4.0, abs=0.1)
    assert r["pool"] == {"scores": 4, "recommended": 2, "recommended_seen_on_any_board": 1,
                         "scored_by": {"background": 2, "page": 1, "check": 1}, "recommended_by_category": {"Sports": 2}}  # fmt: skip
    assert r["by_interest"]["Sports"] == {"check": 2, "directory": 2} and r["by_interest"]["Esports"] == {"check": 0, "directory": 0}
    assert (r["flag"], r["serving"], r["error"]) == ("directory", "directory", None)
    assert r["check"]["traders"] == 3 and r["check"]["recommended"] == 2
    assert r["directory"]["traders"] == 4 and r["directory"]["recommended"] == 2 and r["directory"]["considered"] == 4
    assert r["directory"]["by_section"] == {"sharp": 1, "rising": 1}
    assert r["recommended"] == {"both": 1, "only_check": {"scored_since_another_tier": 1},
                                "only_directory": {"not_in_checks_list": 1}}  # fmt: skip
    apart = r["scored_apart"]
    assert (apart["pairs"], apart["same_tier"], apart["same_section"], apart["same_copyable"]) == (2, 1, 1, 2)
    assert apart["differ"] == [{"wallet": BEN, "check": "rising", "apart": "no_edge", "via": "page",
                                "check_copyable": "no", "apart_copyable": "no", "check_bets": 38, "apart_bets": 38,
                                "hours_apart": 2.0}]  # fmt: skip
    monkeypatch.setattr(engine, "DISCOVERY", "check")
    engine._cache.clear()
    r = api.get("/api/discovery").json()
    assert (r["serving"], r["directory"]["traders"]) == ("check", 4)  # the directory is still worked out alongside


def test_the_comparison_is_the_owners_only(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert TestClient(engine.app, client=("203.0.113.9", 4000)).get("/api/discovery").status_code == 404
    monkeypatch.setattr(engine, "HOSTED", True)
    assert api.get("/api/discovery").status_code == 404
