"""A trader's page ("Why they're here" in five short lines) and the price check (Evaluate): no network, the
SDK's reads are fixed answers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from uselayer import Whales, WhaleTrade
from uselayer.whales import CopyPreview

from signals import app as engine

SCORE = {"wallet": "0xabc", "name": "sharpie", "segment": "quiet", "action": "follow", "sample": []}
COPYABLE = SCORE | {"label": "Quiet sharps", "copyable": "yes",
                    "copy": {"mean": 0.02, "margin": 0.01, "n": 30, "z": 4.0, "low": 0.01, "high": 0.03}}  # fmt: skip


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": [COPYABLE]})
    monkeypatch.setattr(engine, "_worth_one", {})
    return TestClient(engine.app)


# ---- "Why they're here" ----


def test_why_theyre_here_says_what_how_why_listed_and_copying_in_plain_lines() -> None:
    s = COPYABLE | {
        "categories": [{"category": "Politics", "bets": 85}, {"category": "Economy", "bets": 7},
                       {"category": "Crypto", "bets": 4}, {"category": "Sports", "bets": 2}, {"category": "Weather", "bets": 2}],  # as the SDK sorts them
        "price_band": [0.55, 0.84], "typical_usd": 855.0, "held_share": 0.56, "decisions": 60,
        "window": ["2026-09-07T00:00:00+00:00", "2026-10-07T00:00:00+00:00"],
        "edge": {"mean": 0.02, "margin": 0.01, "n": 43, "z": 3},
        "periods": {"30d": {"trading_pnl": 12345.0}}, "pnl_weeks": [10.0] * 8 + [-5.0] * 4,
    }  # fmt: skip
    why = engine.why_lines(s | engine._board(s))
    assert why["trades"] == "Politics 85%, Economy 7%"  # the top three by share; a 4% sideline isn't listed
    assert why["how"] == ("Buys around 55–84¢, usually $855, 2 new markets a day, holds 56% of closed positions until"
                          " the market settles.")  # fmt: skip
    assert why["listed"] == ("The price tended to move their way within minutes after they bought (over 43 trades), and"
                             " in the trades we checked it was still moving when a copy a few minutes late got in."
                             " Trading profit +$12.3K in the last 30 days; up in 8 of the last 12 weeks.")  # fmt: skip
    assert why["copying"].startswith("Offered, because") and "not a promise" in why["copying"]

    fast = s | {"segment": "too_fast", "copyable": "no"}
    why = engine.why_lines(fast | engine._board(fast))
    assert why["listed"].startswith(
        "The price tends to move their way within minutes after they buy (over 43 trades), though usually before a copy could get in.")
    assert why["copying"] == engine.copy_offer(fast)[1] and "isn't offered" in why["copying"]

    rising = s | {"segment": "rising", "copyable": "unknown", "edge": {"mean": 0.02, "margin": 0.03, "n": 12, "z": 1}}
    assert "over 12 trades so far, too few to be sure yet" in engine.why_lines(rising | engine._board(rising))["listed"]

    lucky = SCORE | {"segment": "lucky", "reason": "Big profit, but it rests on one bet."}
    assert engine.why_lines(lucky | engine._board(lucky))["listed"] == "Not on our list: Big profit, but it rests on one trade."

    young = s | {"pnl_weeks": [None] * 9 + [5.0, -1.0, 3.0]}
    assert "up in 2 of the 3 weeks since their first trade" in engine.why_lines(young | engine._board(young))["listed"]

    bare = engine.why_lines(SCORE | {"trades_per_day": 0.3})
    assert bare["trades"] is None and bare["how"] == "2 new markets a week."  # nothing made up


def test_a_traders_page_carries_the_why_lines(api: TestClient) -> None:
    why = api.get("/api/worth/0xabc").json()["why"]
    assert set(why) == {"trades", "how", "listed", "copying"} and why["copying"].startswith("Offered")


def test_the_profile_card_and_the_price_check_have_the_new_lines(api: TestClient) -> None:
    page = api.get("/").text
    for words in ("<b>What they trade</b>", "<b>How they trade</b>", "<b>Lately</b>", "<b>Why they're on the list</b>",
                  "<b>Copying</b>", "<b>Worth knowing</b>", "Last bought ", "How we measure this",
                  "Prices can change before you buy on Polymarket.", "is available within 3¢ of their price.",
                  "Slippage: your average fill", "They bought "):  # fmt: skip
        assert words in page, words
    assert "<b>Consistent</b>" not in page and "<b>Can you copy them</b>" not in page  # the old three lines


# ---- the price check ----


def _preview(**kw: Any) -> CopyPreview:
    t = WhaleTrade("polymarket", "0xabc", None, "0xcid:1", "yes", "buy", 0.52, 0, 0, datetime.now(UTC), "x")
    base: dict[str, Any] = {"trade": t, "ok": True, "reason": "", "venue": "polymarket", "market": "0xcid:1",
                            "side": "yes", "contracts": 100.0, "price": 0.53, "worst_price": 0.53, "cost": 53.0,
                            "fee": 0.5, "total": 53.5, "max_spend": 400.0, "depth_checked": True}  # fmt: skip
    return CopyPreview(**(base | kw))


def test_the_check_names_the_slippage_against_the_best_price(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict[str, Any]] = []
    main = {"p": _preview()}

    def preview(self: Any, trade: Any, **kw: Any) -> CopyPreview:
        asked.append(kw)
        return main["p"] if "spend" in kw else _preview(contracts=1.0, price=0.51, worst_price=0.51)

    monkeypatch.setattr(Whales, "preview_copy", preview)
    body = {"wallet": "0xabc", "market": "0xcid:1", "price": 0.52, "spend": 50}
    assert api.post("/api/trade/preview", json=body).json()["best_price"] == 0.53  # one level: the average is the best
    assert len(asked) == 1  # and nothing more is read
    main["p"] = _preview(price=0.534, worst_price=0.55)  # the walk went deeper into the book
    assert api.post("/api/trade/preview", json=body).json()["best_price"] == 0.51
    assert asked[-1] == {"size": 1.0, "max_slippage": 0.03}  # one contract: the best ask
    main["p"] = _preview(depth_checked=False, worst_price=None)
    assert api.post("/api/trade/preview", json=body).json()["best_price"] is None  # no book read: no slippage claimed


def test_the_check_says_how_much_the_book_holds_near_their_price(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The SDK's max_spend is everything for sale up to 3¢ above their price, fee included; the page turns it into
    # "Only $340 is available within 3¢ of their price."
    short = _preview(ok=False, reason="Only $340.00 is for sale at 0.55 or better right now", price=0.52,
                     max_spend=340.0, contracts=None, worst_price=None)  # fmt: skip
    monkeypatch.setattr(Whales, "preview_copy", lambda self, t, **kw: short)
    r = api.post("/api/trade/preview", json={"wallet": "0xabc", "market": "0xcid:1", "price": 0.52, "spend": 5000}).json()
    assert (r["ok"], r["max_spend"], r["depth_checked"], r["best_price"]) == (False, 340.0, True, None)


def test_rising_stays_hedged_even_when_copying_is_offered() -> None:
    rising = COPYABLE | {"segment": "rising", "edge": {"mean": 0.02, "margin": 0.03, "n": 12, "z": 1}}
    b = engine._board(rising)
    assert b["section"] == "copy"  # listed in Worth copying
    assert engine.why_lines(rising | b)["listed"].startswith("Early signs: the price has tended to move their way after they buy, over 12 trades so far")


def test_why_lines_take_trading_profit_as_the_page_does() -> None:
    # periods without a 30-day figure: the window's trading profit, as the page's profitOf, never Polymarket's own
    s = SCORE | {"periods": {"30d": {"trading_pnl": None}}, "trading_pnl_window": -420.0, "pnl_window": 9999.0}
    assert "Trading profit −$420 in the last 30 days." in engine.why_lines(s | engine._board(s))["listed"]


def test_dollars_and_markets_a_week_are_never_made_up() -> None:
    assert [engine._usd(x) for x in (855, 999.6, 1000, 1234, 99_960, 999_600, 1_250_000, 3.5)] == [
        "$855", "$1,000", "$1K", "$1.2K", "$100K", "$1M", "$1.2M", "$3.50"]  # fmt: skip
    assert engine._per_day(0) == "fewer than one new market a week" and engine._per_day(0.05) == "fewer than one new market a week"
    assert engine._per_day(0.3) == "2 new markets a week" and engine._per_day(1) == "1 new market a day"


def test_the_page_formats_the_same_way(run_js: Any) -> None:
    got = run_js(("plural", "usd", "perDayText"), "[[999.6, 999600, 1000].map((x) => usd(x)), [0, 0.3, 2].map(perDayText)]")
    assert got == [["$1,000", "$1M", "$1K"], ["fewer than one market a week", "2 markets a week", "2 markets a day"]]


def test_only_an_amount_above_what_the_book_holds_says_only_x_is_available(run_js: Any) -> None:
    deps = ("sdkText", "money", "availableLine", "notPriced")
    cases = {
        # $2 typed, under Polymarket's smallest order: the SDK's own reason, no "Use $340 instead"
        "small": ({"ok": False, "depth_checked": True, "max_spend": 340, "reason": "$2.00 doesn't buy Polymarket's smallest order here: 5 contracts at 0.52 cost $2.65 with the fee."}, 2),
        "short": ({"ok": False, "depth_checked": True, "max_spend": 340.55, "reason": "Only $340.55 is for sale"}, 5000),
        "crumbs": ({"ok": False, "depth_checked": True, "max_spend": 0.4, "reason": "Only $0.40 is for sale"}, 50),
        "none": ({"ok": False, "depth_checked": True, "max_spend": 0, "reason": "Nothing to buy at 0.55 or better right now."}, 50),
        "nobook": ({"ok": False, "depth_checked": False, "max_spend": None, "reason": "Polymarket isn't taking orders on this market now."}, 50),
    }  # fmt: skip
    import json

    got = run_js(deps, "Object.fromEntries(Object.entries(" + json.dumps(cases) + ").map(([k, [p, spend]]) => [k, notPriced(p, spend)]))")
    assert got["small"] == {"status": None, "line": cases["small"][0]["reason"], "room": 0}
    assert got["short"] == {"status": "Not enough for sale for this amount", "line": "Only $340.55 is available within 3¢ of their price.", "room": 340.55}
    assert got["crumbs"] == {"status": "Not enough for sale for this amount", "line": "Only $0.40 is available within 3¢ of their price.", "room": 0.4}
    assert got["none"]["status"] == "Price moved away" and got["none"]["room"] == 0
    assert got["nobook"] == {"status": None, "line": cases["nobook"][0]["reason"], "room": 0}


def test_lately_tops_up_from_the_checks_buys_to_three(run_js: Any) -> None:
    deps = ("esc", "tenths", "cents", "ago", "latelyLine")
    d = {"trades": [{"action": "buy", "market": "m1", "title": "One", "outcome": "Yes", "price": 0.5, "usd": 50, "size": 100, "at": "2026-10-13T10:00:00Z"},
                    {"action": "sell", "market": "m9", "title": "Sold", "price": 0.5, "usd": 5, "size": 10, "at": "2026-10-13T11:00:00Z"}]}
    s = {"recent": [{"market": "m1", "title": "One", "outcome": "Yes", "price": 0.4, "at": "2026-10-01T00:00:00Z"},
                    {"market": "m2", "title": "Two", "outcome": "No", "price": 0.3, "at": "2026-10-05T00:00:00Z"},
                    {"market": "m3", "title": "Three", "outcome": "Yes", "price": 0.7, "at": "2026-10-04T00:00:00Z"},
                    {"market": "m4", "title": "Four", "outcome": "Yes", "price": 0.7, "at": "2026-10-03T00:00:00Z"}]}  # fmt: skip
    import json

    line = run_js(deps, f"latelyLine({json.dumps(s)}, {json.dumps(d)})")
    assert "Yes on One at 50¢; No on Two at 30¢; Yes on Three at 70¢." in line  # m1 once, from the fresh read
    assert run_js(deps, "latelyLine({recent: []}, {trades: []})") == "No recent buys."


def test_the_sdks_best_price_is_used_when_it_has_one(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # uselayer 0.8.2's CopyPreview carries best_price (the best ask, before the fee): no one-contract read then.
    from dataclasses import replace

    newer = replace(_preview(price=0.534, worst_price=0.55), best_price=0.52)
    asked: list[dict[str, Any]] = []

    def preview(self: Any, trade: Any, **kw: Any) -> CopyPreview:
        asked.append(kw)
        return newer

    monkeypatch.setattr(Whales, "preview_copy", preview)
    body = {"wallet": "0xabc", "market": "0xcid:1", "price": 0.52, "spend": 50}
    assert api.post("/api/trade/preview", json=body).json()["best_price"] == 0.52
    assert len(asked) == 1  # the SDK said it: no one-contract read
    assert _preview().best_price is None  # a preview without it (no book read): the one-contract fallback (above) stays
