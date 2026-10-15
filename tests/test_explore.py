"""Explore (SIGNALS_EXPLORE=1): the directory on the Traders page. Its three groups, the privacy guard, unknown
evidence shown as unknown (never 0), directory-wide search and fact-only filters and sorts, browsing that never
reads Polymarket, a trader page that reuses a fresh stored score and checks one only when asked, and "Check now"
kept to a few at a time (no network: the SDK's reads are stand-ins)."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from signals import app as engine
from signals.directory import PASSED_FRESH_S, Directory, QuickLook, Seen, quick_look

NOW = time.time()
DAY = 86400
SEL, PAID, DORM, NEW, HIDDEN, PRO = ("0x" + c * 40 for c in "123456")  # selective, scored, dormant, unchecked...


def score(wallet: str, segment: str, **kw: Any) -> dict[str, Any]:
    return {"wallet": wallet, "name": None, "segment": segment, "skill": "unknown", "copyable": "unknown", "bets": 5,
            "typical_usd": 2500.0, "categories": [{"category": "Politics", "bets": 5}],
            "last_buy": "2026-10-12T00:00:00+00:00", "sample": [], "format": engine.WORTH_FORMAT} | kw  # fmt: skip


def fill(d: Directory, at: float = NOW) -> None:
    """One wallet of each kind, each found in public (a market or a board), plus one known only from a search."""
    d.add([Seen(SEL, "market", "Politics", name="SelectiveSam", category="Politics", example_title="Will it rain?"),
           Seen(PAID, "leaderboard", "PNL MONTH POLITICS", name="PaidPat", category="Politics", rank=312),
           Seen(DORM, "market", "Sports", name="DormantDee", category="Sports"),
           Seen(NEW, "leaderboard", "VOL ALL OVERALL", name="NewNed", rank=40),
           Seen(PRO, "market", "Crypto", name="Pro_100%", category="Crypto"),
           Seen(HIDDEN, "search", name="HiddenHal")], at=at - DAY)  # fmt: skip
    d.quick_checked(SEL, QuickLook(False, "not_enough_recent_evidence", 5, 14, at - 3600, at - 3600, 12000.0, False), at=at)
    d.keep_scores([score(PAID, "too_few")], "background", at=at)
    d.quick_checked(DORM, QuickLook(False, "dormant", 0, 0, None, at - 100 * DAY, None, False), at=at)
    d.keep_scores([score(PRO, "no_edge", typical_usd=50.0)], "background", at=at)
    d.keep_scores([score(HIDDEN, "too_few")], "page", at=at)  # scored on a page, but only ever found by a search


@pytest.fixture
def d(tmp_path: Any) -> Directory:
    x = Directory(tmp_path / "directory.db")
    fill(x)
    return x


def wallets(page: dict[str, Any]) -> list[str]:
    return [r["wallet"] for r in page["rows"]]


# ---- the directory ----


def test_each_wallet_is_in_one_group_and_says_how_we_know(d: Directory) -> None:
    limited = {r["wallet"]: r for r in d.browse("limited")["rows"]}
    assert set(limited) == {SEL, PAID}
    assert (limited[SEL]["evidence"], limited[PAID]["evidence"]) == ("quick", "scored")  # a quick look vs a full check
    assert set(wallets(d.browse("checked"))) == {DORM, PRO} and wallets(d.browse("unchecked")) == [NEW]
    assert d.explore_counts() == {"passed": 0, "limited": 2, "checked": 2, "unchecked": 1, "whales": 2}


def test_a_wallet_known_only_from_a_search_or_a_follow_is_never_listed_or_found(d: Directory) -> None:
    d.add([Seen(SEL, "follow")])
    for g in ("limited", "checked", "unchecked"):
        assert HIDDEN not in wallets(d.browse(g))
    assert d.browse("limited", q="hidden")["rows"] == [] and d.browse("limited", q=HIDDEN[:10])["rows"] == []
    assert d.explore_row(HIDDEN)["public"] is False and d.explore_row(SEL)["public"] is True  # type: ignore[index]
    d.add([Seen(HIDDEN, "market", "Politics")])  # seen trading in public later: listed from then on
    assert HIDDEN in wallets(d.browse("limited"))


def test_what_wasnt_read_stays_unknown_never_zero(d: Directory) -> None:
    new = d.explore_row(NEW)
    assert new is not None
    assert (new["buys_30d"], new["buys_90d"], new["market_usd"], new["typical_usd"], new["last_active"]) == (None,) * 5
    assert (new["evidence"], new["evidence_cut"]) == (None, None)
    dorm = d.explore_row(DORM)
    assert dorm is not None and (dorm["buys_30d"], dorm["market_usd"]) == (0, None)  # read: none bought; no size known


def test_search_covers_every_public_wallet_by_name_or_wallet_and_takes_wildcards_literally(d: Directory) -> None:
    assert wallets(d.browse("limited", q="sam")) == [SEL]  # any group, case-insensitive, inside the name
    assert wallets(d.browse("limited", q="DORMANT")) == [DORM]
    assert wallets(d.browse("limited", q=SEL[:8])) == [SEL]
    assert wallets(d.browse("limited", q="100%")) == [PRO]  # a literal %
    assert d.browse("limited", q="p%o")["rows"] == [] and d.browse("limited", q="__")["rows"] == []  # not wildcards
    assert d.browse("limited", q="ned")["rows"] == []  # not checked yet: only when asked for
    assert wallets(d.browse("limited", q="ned", search_in=("limited", "checked", "unchecked"))) == [NEW]


def test_traders_scored_before_explore_get_their_usual_trade_from_the_kept_score(tmp_path: Any) -> None:
    """A directory from before Explore kept scores but not the usual trade on the wallet (all of the hosted one's
    ~1,500): opened again, it's filled from each wallet's latest kept score, with no read and no new score, so the
    $100+/$1K+/$10K+ filters find them. A score without one, and a quick look with no dollars, stay unknown."""
    path = tmp_path / "directory.db"
    old = Directory(path)
    sizes = {"0x" + c * 40: usd for c, usd in zip("abcdef", (50.0, 100.0, 999.0, 1000.0, 9999.0, 25000.0))}
    old.add([Seen(w, "leaderboard", "PNL MONTH OVERALL", rank=9) for w in [*sizes, "0x" + "7" * 40, "0x" + "8" * 40]],
            at=NOW - DAY)  # fmt: skip
    old.keep_scores([score(w, "no_edge", typical_usd=usd) for w, usd in sizes.items()]
                    + [score("0x" + "7" * 40, "no_edge", typical_usd=None)], "background", at=NOW)  # fmt: skip
    old.quick_checked("0x" + "8" * 40, QuickLook(False, "dormant", 0, 0, None, NOW - 100 * DAY, None, False), at=NOW)
    with old._db() as con:  # as kept before Explore: the score, but no usual trade on the wallet
        con.execute("UPDATE wallets SET typical_usd = NULL")
    scored_at = {w: old.score(w)["scored_at"] for w in sizes}

    d = Directory(path)  # the next launch
    rows = {r["wallet"]: r for r in d.browse("checked")["rows"]}
    assert {w: rows[w]["typical_usd"] for w in sizes} == sizes  # exactly the kept score's figure
    assert rows["0x" + "7" * 40]["typical_usd"] is None and rows["0x" + "8" * 40]["typical_usd"] is None  # unknown
    for floor, expect in ((100, {"b", "c", "d", "e", "f"}), (1000, {"d", "e", "f"}), (10_000, {"f"})):
        assert set(wallets(d.browse("checked", min_usd=floor))) == {"0x" + c * 40 for c in expect}, floor
    assert {w: d.score(w)["scored_at"] for w in sizes} == scored_at  # no score was redone or replaced
    assert wallets(d.browse("checked", sort="size"))[:2] == ["0x" + "f" * 40, "0x" + "e" * 40]  # unknown last
    assert set(wallets(d.browse("checked", sort="size"))[-2:]) == {"0x" + "7" * 40, "0x" + "8" * 40}


def test_filters_are_facts_and_sorts_put_the_unknown_last(d: Directory) -> None:
    assert set(wallets(d.browse("limited", category="Politics"))) == {SEL, PAID}
    assert wallets(d.browse("checked", category="Sports")) == [DORM]
    assert wallets(d.browse("limited", min_usd=10_000)) == [SEL]  # their usual market: $12K
    assert wallets(d.browse("limited", board="on")) == [PAID] and wallets(d.browse("limited", board="off")) == [SEL]
    assert set(wallets(d.browse("limited", active_days=7, now=NOW))) == {SEL, PAID}
    assert wallets(d.browse("checked", active_days=7, now=NOW)) == [PRO]  # DORM's last trade: 100 days ago
    assert wallets(d.browse("limited", sort="recent", now=NOW)) == [SEL, PAID]  # SEL traded an hour ago, PAID on 12 Oct
    assert wallets(d.browse("checked", sort="size")) == [PRO, DORM]  # $50 a trade, then unknown
    d.keep_scores([score(PRO, "no_view")], "background", at=NOW + 1)
    assert PRO not in wallets(d.browse("checked")) and PRO in wallets(d.browse("checked", makers=True))  # makers hidden


def test_full_checks_can_be_picked_out_and_the_longest_records_come_first(d: Directory) -> None:
    """``evidence="full"``: wallets whose latest look is a full check (a score), not a quick look; ``sort="record"``:
    markets bought in 90 days, most first, unknown (no quick check yet) last. Facts, never a grade."""
    assert wallets(d.browse("checked", evidence="full")) == [PRO]  # DORM: a quick look only
    assert wallets(d.browse("limited", evidence="full")) == [PAID]  # a score that found too few, not SEL's quick look
    more = "0x" + "9" * 40
    d.add([Seen(more, "market", "Politics", name="MoreMo", category="Politics")], at=NOW - DAY)
    d.quick_checked(more, QuickLook(False, "not_enough_recent_evidence", 7, 40, NOW - DAY, NOW - DAY, 80.0, False), at=NOW)
    assert wallets(d.browse("limited", sort="record", now=NOW)) == [more, SEL, PAID]  # 40, 14, then unknown
    d.quick_checked(PRO, QuickLook(False, "not_enough_recent_evidence", 2, 3, NOW, NOW, 10.0, False), at=NOW + 1)
    assert PRO not in wallets(d.browse("checked", evidence="full"))  # its latest look is a quick one now


def test_a_quick_look_keeps_the_usual_market_size_and_whether_its_counts_are_at_least(tmp_path: Any) -> None:
    trades = [(NOW - 3600, True, "0xa", 100.0), (NOW - 3500, True, "0xa", 200.0), (NOW - 2 * DAY, True, "0xb", 1000.0),
              (NOW - 3 * DAY, True, "0xc", 50.0), (NOW - DAY, False, "0xd", 9999.0)]  # fmt: skip
    q = quick_look(trades, NOW, read_all=True)
    assert (q.market_usd, q.cut) == (300.0, False)  # $300, $1,000 and $50 a market: the middle one
    assert quick_look(trades, NOW, read_all=False).cut is True  # stopped 3 days back: counts are "at least"
    assert quick_look([(NOW, True, "0xa")], NOW, read_all=True).market_usd is None  # no dollars read: unknown


def test_a_recommended_tier_from_the_last_48_hours_passed_our_checks_and_an_older_one_only_a_search_finds(d: Directory) -> None:
    star, old = "0x" + "7" * 40, "0x" + "8" * 40
    d.add([Seen(star, "leaderboard", "PNL MONTH OVERALL", name="StarSue", rank=5),
           Seen(old, "market", "Sports", name="OldOllie", category="Sports")], at=NOW - 5 * DAY)  # fmt: skip
    d.keep_scores([score(star, "rising")], "background", at=NOW - PASSED_FRESH_S + 3600)  # 47 hours ago
    d.keep_scores([score(old, "quiet")], "background", at=NOW - PASSED_FRESH_S - 3600)  # 49: due, not shown as passed
    assert wallets(d.browse("passed", now=NOW)) == [star]
    for g in ("limited", "checked", "unchecked"):
        assert star not in wallets(d.browse(g, now=NOW)) and old not in wallets(d.browse(g, now=NOW))
    row = d.browse("passed", now=NOW)["rows"][0]
    # the three answers apart: skill (tier, edge, bets), profit (30 days of trading), when it was scored
    assert (row["segment"], row["bets_checked"], row["scored_at"]) == ("rising", 5, NOW - PASSED_FRESH_S + 3600)
    assert "trading_pnl_window" in row and "edge" in row and "copyable" in row
    found = {r["wallet"]: r["group"] for r in d.browse("limited", q="o", now=NOW)["rows"] if r["wallet"] in (star, old)}
    assert found == {old: "recommended"} and d.browse("limited", q="starsue", now=NOW)["rows"][0]["group"] == "passed"
    assert d.explore_counts(now=NOW) == {"passed": 1, "limited": 2, "checked": 2, "unchecked": 1, "whales": 4}


def test_whales_are_big_bettors_whatever_our_checks_found_and_each_keeps_its_own_group(d: Directory) -> None:
    star, maker = "0x" + "7" * 40, "0x" + "9" * 40
    d.add([Seen(star, "leaderboard", "PNL MONTH OVERALL", name="StarSue", rank=5),
           Seen(maker, "market", "Crypto", name="MakerMo", category="Crypto")], at=NOW - DAY)  # fmt: skip
    d.keep_scores([score(star, "rising", typical_usd=40_000.0)], "background", at=NOW)
    d.keep_scores([score(maker, "no_view", typical_usd=90_000.0)], "background", at=NOW)  # a market maker: left out
    rows = d.browse("whales", sort="size", now=NOW)["rows"]
    # $40K a trade (passed), a quick look's $12K a market (limited), $2.5K a trade (limited); $50 PRO isn't one
    assert [(r["wallet"], r["group"]) for r in rows] == [(star, "passed"), (SEL, "limited"), (PAID, "limited")]
    assert wallets(d.browse("whales", min_usd=10_000, sort="size", now=NOW)) == [star, SEL]
    assert wallets(d.browse("whales", board="off", sort="size", now=NOW)) == [SEL]
    assert d.explore_counts(now=NOW)["whales"] == 3  # counted as listed: the market maker left out
    assert star in wallets(d.browse("passed", now=NOW))  # still in its own group too


# ---- the engine ----


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    engine._worth_one.clear()
    engine._check_times.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": []})
    monkeypatch.setattr(engine, "EXPLORE_ON", True)
    fill(engine.directory())
    return TestClient(engine.app)


class NoReads:
    """Polymarket, but any read fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"read Polymarket: {name}")


def test_explore_is_off_unless_switched_on(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "EXPLORE_ON", False)
    assert api.get("/api/traders").status_code == 404 and api.get("/api/config").json()["explore"] is False


def test_browsing_and_searching_read_only_the_directory(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine.reads, "whales", NoReads())
    r = api.get("/api/traders", params={"group": "limited"}).json()
    assert {x["wallet"] for x in r["rows"]} == {SEL, PAID}
    assert r["counts"] == {"passed": 0, "limited": 2, "checked": 2, "unchecked": 1, "whales": 2}
    assert r["more"] is False and r["unchecked_matches"] is None
    s = api.get("/api/traders", params={"q": "ned"}).json()
    assert s["rows"] == [] and s["unchecked_matches"] == 1  # "1 more match among traders we haven't checked"
    assert [x["wallet"] for x in api.get("/api/traders", params={"q": "ned", "unchecked": 1}).json()["rows"]] == [NEW]
    assert api.get("/api/traders", params={"q": "n"}).status_code == 400
    assert api.get("/api/traders", params={"group": "best"}).status_code == 400
    full = api.get("/api/traders", params={"group": "checked", "evidence": "full", "sort": "record"}).json()
    assert [x["wallet"] for x in full["rows"]] == [PRO]
    assert api.get("/api/traders", params={"evidence": "skilled"}).status_code == 400


def test_a_page_opened_from_explore_isnt_checked_until_asked(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine.reads, "whales", NoReads())
    r = api.get(f"/api/worth/{SEL}", params={"score": 0}).json()
    assert r["unchecked"] is True and r["directory"]["group"] == "limited" and r["directory"]["buys_90d"] == 14


def test_a_fresh_stored_score_is_shown_as_it_is_and_a_stale_one_is_checked_again(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine.reads, "whales", NoReads())
    r = api.get(f"/api/worth/{PAID}", params={"score": 0}).json()
    assert r["segment"] == "too_few" and r["stored_at"] > 0 and r["directory"]["evidence"] == "scored"
    # a recommended tier's score: fresh 48 hours for one the directory found, 12 for one people are shown
    engine.directory().keep_scores([score(DORM, "rising")], "background", at=time.time() - 13 * 3600)
    assert api.get(f"/api/worth/{DORM}", params={"score": 0}).json()["segment"] == "rising"
    engine._save_follows({"users": {"u1": {"follows": [DORM], "chat": None}}})
    engine._cache.clear()
    assert api.get(f"/api/worth/{DORM}", params={"score": 0}).json()["unchecked"] is True  # followed: 13 h is stale
    checked: list[str] = []
    monkeypatch.setattr(engine.reads, "whales", type("W", (), {"score": lambda self, w: checked.append(w) or
                        type("S", (), {"to_dict": lambda s: score(w, "rising")})()})())  # fmt: skip
    assert api.get(f"/api/worth/{DORM}").json()["segment"] == "rising" and checked == [DORM]  # "Check now"


def test_passed_our_checks_rows_keep_profit_skill_and_copying_apart(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine.reads, "whales", NoReads())
    star = "0x" + "7" * 40
    engine.directory().add([Seen(star, "market", "Politics", name="StarSue", category="Politics")])
    engine.directory().keep_scores([score(star, "rising", copyable="no", trading_pnl_window=4200.0,
                                          edge={"mean": 0.03, "margin": 0.02, "n": 30, "z": 2.4})], "background")  # fmt: skip
    r = api.get("/api/traders").json()  # the default group: Passed our checks
    assert r["counts"]["passed"] == 1
    row = r["rows"][0]
    assert (row["wallet"], row["group"], row["segment"]) == (star, "passed", "rising")  # skill: the tier...
    assert (row["edge"], row["edge_z"], row["bets_checked"]) == (0.03, 2.4, 5)  # ...and its evidence
    assert row["trading_pnl_window"] == 4200.0  # profitability, apart
    assert row["copy_label"] == "Copy not proven" and row["on_recommended"] is False  # copyability, apart
    assert row["scored_at"] > 0  # when it was last scored


def test_check_now_scores_a_wallet_once_however_many_ask(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    gate = threading.Event()

    def slow(self: Any, w: str) -> Any:
        calls.append(w)
        gate.wait(2)
        return type("S", (), {"to_dict": lambda s: score(w, "no_edge")})()

    monkeypatch.setattr(engine.reads, "whales", type("W", (), {"score": slow})())
    out: list[int] = []
    threads = [threading.Thread(target=lambda: out.append(api.get(f"/api/worth/{NEW}").status_code)) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join()
    assert out == [200, 200, 200] and calls == [NEW]


def test_check_now_says_try_again_when_too_many_are_running(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "CHECK_WAIT_S", 0.05)
    monkeypatch.setattr(engine, "_checks", threading.BoundedSemaphore(1))
    engine._checks.acquire()  # someone else's check is running
    try:
        r = api.get(f"/api/worth/{NEW}")
        assert r.status_code == 429 and "Try again" in r.json()["detail"]
    finally:
        engine._checks.release()


def test_hosted_one_address_can_check_a_few_traders_at_a_time(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "HOSTED", True)
    monkeypatch.setattr(engine, "CHECKS_PER_CLIENT", (2, 600))
    monkeypatch.setattr(engine.reads, "whales", type("W", (), {"score": lambda self, w:
                        type("S", (), {"to_dict": lambda s: score(w, "no_edge")})()})())  # fmt: skip
    ws = ["0x" + f"{i:040x}" for i in range(1, 4)]
    codes = [api.get(f"/api/worth/{w}", headers={"x-forwarded-for": "1.2.3.4"}).status_code for w in ws]
    assert codes == [200, 200, 429]
    assert api.get(f"/api/worth/{ws[2]}", headers={"x-forwarded-for": "5.6.7.8"}).status_code == 200  # someone else
    assert api.get(f"/api/worth/{ws[0]}", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 200  # already checked


def test_following_works_for_any_trader_explore_lists(api: TestClient) -> None:
    # Following and its alerts never depend on a tier: the engine's follow takes any wallet (see test_follow /
    # test_alerts); here, a limited-evidence trader from Explore has a page with no copy offer and nothing hidden.
    r = api.get(f"/api/worth/{PAID}", params={"score": 0}).json()
    assert r["copy_ok"] is False and r["wallet"] == PAID


# ---- the page's rules ----


def test_the_page_says_unknown_for_what_we_never_read(run_js: Any) -> None:
    got = run_js(("exCount",), "[exCount(null, false), exCount(12, true), exCount(0, false)]")
    assert got == ["unknown", "12+", "0"]
    assert run_js(("boardText",), 'boardText("PNL MONTH POLITICS", 312)') == "#312 on the monthly profit board, Politics"
    assert run_js(("boardText",), 'boardText("VOL ALL OVERALL", 1500)') == "#1,500 on the all-time volume board"




STATS = ("usd", "pl", "centsSigned", "ago", "exCount", "exStats")


def kv(stats: list[dict[str, Any]]) -> list[list[Any]]:
    return [[x["k"], x["v"], x.get("sub")] for x in stats]


def test_a_passed_row_keeps_profit_the_price_move_and_copying_apart_and_leaves_the_z_to_their_page(run_js: Any) -> None:
    row = '{group: "passed", trading_pnl_window: 4200, edge: 0.031, edge_z: 2.44, bets_checked: 31, copy_label: "Copy not proven"}'
    got = run_js(STATS, f"exStats({row})")
    assert kv(got) == [["Profit, 30d", "+$4.2K", None], ["Price after buys", "+3.1¢", "31 bets"], ["Copying", "Copy not proven", None]]
    assert "2.4" not in str(got)  # the z is on their page, not the row
    unknown = run_js(STATS, 'exStats({group: "passed"})')
    assert [x["v"] for x in unknown] == ["unknown", "unknown", "unknown"]  # never a made-up 0


def test_each_group_shows_figures_for_what_its_for(run_js: Any) -> None:
    quick = '{group: "limited", evidence: "quick", market_usd: 12000, buys_30d: 5, evidence_cut: true}'
    assert kv(run_js(STATS, f"exStats({quick})")) == [["Markets, 30d", "5+", None], ["Usual bet", "$12K", "a market"], ["Last trade", "unknown", None]]
    deep = '{group: "limited", evidence: "quick", buys_30d: 6, buys_90d: 31, evidence_cut: false}'
    assert kv(run_js(STATS, f"exStats({deep})"))[0] == ["Markets, 30d", "6", "31 in 90d"]  # their 90 days too
    # a full check: its record (profit, bets checked) first; the label beside it says what skill it showed
    checked = '{group: "checked", evidence: "scored", typical_usd: 50, bets_checked: 12, trading_pnl_window: 300}'
    assert kv(run_js(STATS, f"exStats({checked})")) == [["Profit, 30d", "+$300", None], ["Bets checked", "12", "30d"], ["Last trade", "unknown", None]]
    dormant = '{group: "checked", evidence: "quick", buys_30d: 0, buys_90d: 0}'
    assert kv(run_js(STATS, f"exStats({dormant})")) == [["Usual bet", "unknown", None], ["Last trade", "unknown", None], ["Markets, 30d", "0", "0 in 90d"]]
    # Whales: size and recency first, whatever the group; then profit when scored, a quick look's count when not
    passed = '{group: "passed", typical_usd: 40000, trading_pnl_window: -900}'
    assert kv(run_js(STATS, f"exStats({passed}, true)")) == [["Usual bet", "$40K", "a trade"], ["Last trade", "unknown", None], ["Profit, 30d", "−$900", None]]
    assert kv(run_js(STATS, f"exStats({quick}, true)"))[2] == ["Markets, 30d", "5+", None]


SUB = ("ago", "boardText", "foundText", "BOARD_SHOWN", "boardShort", "exCategory", "exSub")


def test_a_rows_line_under_the_name_is_short_and_shows_only_a_top_100_board_place(run_js: Any) -> None:
    got = run_js(SUB, """[
      exSub({group: "limited", evidence: "quick", category: "Sports", board: {place: "PNL WEEK SPORTS", rank: 23}}),
      exSub({group: "checked", evidence: "scored", category: "Politics", board: {place: "VOL WEEK MENTIONS", rank: 89}}),
      exSub({group: "limited", evidence: "quick", category: "Sports", board: {place: "VOL DAY SPORTS", rank: 9389}}),
      exSub({group: "unchecked", category: "Sports", board: {place: "VOL DAY SPORTS", rank: 9389}}),
    ]""")
    assert got[0] == ["Sports", "#23 weekly profit", "quick look"]  # the market first, not said twice
    assert got[1] == ["Politics", "#89 weekly volume, Mentions", "full check"]  # another board's category stays
    assert got[2] == ["Sports", "quick look"]  # #9,389 is on their page, not the row
    assert got[3] == ["Sports", "Found #9,389 on the daily volume board, Sports"]  # nothing else known: where we found them


def test_a_category_known_only_from_where_we_found_them_is_named_as_that(run_js: Any) -> None:
    got = run_js(SUB, """[
      exSub({group: "limited", evidence: "quick", category: "Tech", category_from: "seen"}),
      exSub({group: "checked", evidence: "scored", category: "Politics", category_from: "score"}),
      exSub({group: "unchecked", category: "Sports", category_from: "seen", board: {place: "VOL DAY SPORTS", rank: 9389}}),
      exSub({group: "unchecked", category: "Crypto", category_from: "seen", seen_in: "BTC up?"}),
    ]""")
    assert got[0] == ["found in Tech", "quick look"]  # a Tech board lists people who mostly trade Sports
    assert got[1] == ["Politics", "full check"]  # a score: what they trade
    assert got[2] == ["Found #9,389 on the daily volume board, Sports"]  # the board already says where
    assert got[3] == ["found in Crypto", "Seen trading in “BTC up?”"]


def test_explore_rows_say_where_their_category_comes_from(d: Directory) -> None:
    rows = {r["wallet"]: r for g in ("limited", "checked", "unchecked") for r in d.browse(g)["rows"]}
    assert (rows[SEL]["category"], rows[SEL]["category_from"]) == ("Politics", "seen")  # a quick look: where we found them
    assert (rows[PAID]["category"], rows[PAID]["category_from"]) == ("Politics", "score")  # a score: what they trade
    assert rows[NEW]["category_from"] is None  # an overall board: nothing known


DEFAULTS = ("ex", "EX_DEFAULTS", "exDefaults", "EX_EMPTY", "exPick", "usd", "EX_CHIP", "exChips", "exChanged")


def test_each_group_has_its_own_defaults_and_the_market_carries_across(run_js: Any) -> None:
    got = run_js(DEFAULTS, """(() => {
      const at = () => [ex.group, ex.active, ex.size, ex.sort, ex.category];
      const out = [at(), exChips(), exChanged()];
      ex.category = "Politics"; exPick("limited"); out.push(at(), exChips());
      exPick("whales"); ex.board = "on"; out.push(at(), exChips(), exChanged());
      return out;
    })()""")
    assert got[0] == ["whales", 30, 1000, "size", ""]  # Explore opens on Whales
    assert got[1] == [["active", "Traded in 30 days"], ["size", "Bets $1K+"]] and got[2] is False  # what it shows, said
    assert got[3] == ["limited", 0, 0, "record", "Politics"] and got[4] == [["category", "Politics"]]  # longest records first
    assert got[5] == ["whales", 30, 1000, "size", "Politics"]
    assert got[6][-1] == ["board", "On a leaderboard"] and got[7] is True
    checked = run_js(DEFAULTS, """(() => { exPick("checked"); const a = [ex.evidence, exChips(), exChanged()];
      ex.evidence = ""; return [...a, exChanged()]; })()""")
    # Checked opens on full checks, said in a chip; showing every look (mostly dormant quick looks) is a change
    assert checked == ["full", [["evidence", "Full checks only"]], False, True]


def test_search_looks_through_every_trader_we_found_whatever_the_filters(run_js: Any) -> None:
    url = run_js(("ex", "exSearching", "exQuery"), """(() => {
      Object.assign(ex, { q: "sam", category: "Politics", board: "on" }); return exQuery();
    })()""")
    assert "q=sam" in url and "unchecked=1" in url
    assert "category" not in url and "min_usd" not in url and "board" not in url and "active" not in url
