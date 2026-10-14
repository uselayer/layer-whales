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
from signals.directory import RESCORE_S, Directory, QuickLook, Seen, quick_look

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
    assert d.explore_counts() == {"limited": 2, "checked": 2, "unchecked": 1}


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


def test_a_quick_look_keeps_the_usual_market_size_and_whether_its_counts_are_at_least(tmp_path: Any) -> None:
    trades = [(NOW - 3600, True, "0xa", 100.0), (NOW - 3500, True, "0xa", 200.0), (NOW - 2 * DAY, True, "0xb", 1000.0),
              (NOW - 3 * DAY, True, "0xc", 50.0), (NOW - DAY, False, "0xd", 9999.0)]  # fmt: skip
    q = quick_look(trades, NOW, read_all=True)
    assert (q.market_usd, q.cut) == (300.0, False)  # $300, $1,000 and $50 a market: the middle one
    assert quick_look(trades, NOW, read_all=False).cut is True  # stopped 3 days back: counts are "at least"
    assert quick_look([(NOW, True, "0xa")], NOW, read_all=True).market_usd is None  # no dollars read: unknown


def test_recommended_tiers_stay_on_the_recommended_tab_and_a_search_still_finds_them(d: Directory) -> None:
    star = "0x" + "7" * 40
    d.add([Seen(star, "leaderboard", "PNL MONTH OVERALL", name="StarSue", rank=5)], at=NOW - DAY)
    d.keep_scores([score(star, "rising")], "background", at=NOW)
    for g in ("limited", "checked", "unchecked"):
        assert star not in wallets(d.browse(g))
    found = d.browse("limited", q="starsue")["rows"]
    assert [(r["wallet"], r["group"]) for r in found] == [(star, "recommended")]
    assert d.explore_counts() == {"limited": 2, "checked": 2, "unchecked": 1}  # not counted in Explore's groups


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
    r = api.get("/api/traders").json()
    assert {x["wallet"] for x in r["rows"]} == {SEL, PAID} and r["counts"] == {"limited": 2, "checked": 2, "unchecked": 1}
    assert r["more"] is False and r["unchecked_matches"] is None
    s = api.get("/api/traders", params={"q": "ned"}).json()
    assert s["rows"] == [] and s["unchecked_matches"] == 1  # "1 more match among traders we haven't checked"
    assert [x["wallet"] for x in api.get("/api/traders", params={"q": "ned", "unchecked": 1}).json()["rows"]] == [NEW]
    assert api.get("/api/traders", params={"q": "n"}).status_code == 400
    assert api.get("/api/traders", params={"group": "best"}).status_code == 400


def test_a_page_opened_from_explore_isnt_checked_until_asked(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine.reads, "whales", NoReads())
    r = api.get(f"/api/worth/{SEL}", params={"score": 0}).json()
    assert r["unchecked"] is True and r["directory"]["group"] == "limited" and r["directory"]["buys_90d"] == 14


def test_a_fresh_stored_score_is_shown_as_it_is_and_a_stale_one_is_checked_again(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine.reads, "whales", NoReads())
    r = api.get(f"/api/worth/{PAID}", params={"score": 0}).json()
    assert r["segment"] == "too_few" and r["stored_at"] > 0 and r["directory"]["evidence"] == "scored"
    engine.directory().keep_scores([score(DORM, "rising")], "background", at=NOW - RESCORE_S - 60)  # a tier: 6 h
    assert api.get(f"/api/worth/{DORM}", params={"score": 0}).json()["unchecked"] is True
    checked: list[str] = []
    monkeypatch.setattr(engine.reads, "whales", type("W", (), {"score": lambda self, w: checked.append(w) or
                        type("S", (), {"to_dict": lambda s: score(w, "rising")})()})())  # fmt: skip
    assert api.get(f"/api/worth/{DORM}").json()["segment"] == "rising" and checked == [DORM]  # "Check now"


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
