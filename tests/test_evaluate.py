"""A trader's page ("Why they're here" in five short lines) and the price check (Evaluate): no network, the
SDK's reads are fixed answers."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
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
    # Why they're listed is skill and track record only; copying is its own line.
    assert why["listed"] == ("The price tends to move their way within minutes after they buy (over 43 trades), and it"
                             " holds without their best trades. Trading profit +$12.3K in the last 30 days; up in 8 of the"
                             " last 12 weeks.")  # fmt: skip
    assert why["copying"].startswith("Copyable: ") and "not a promise" in why["copying"]

    # NM-P, 13 October: a sharp whose copies lost money. The same verdict line; copying says it plainly.
    costly = s | {"copyable": "no", "copy": {"mean": -0.026, "margin": 0.022, "n": 53, "z": -2.4}}
    why_costly = engine.why_lines(costly | engine._board(costly))
    assert why_costly["listed"] == why["listed"] and "copy" not in why_costly["listed"]
    assert why_costly["copying"] == engine.copy_offer(costly)[1] and why_costly["copying"].startswith("Costly to copy: ")

    rising = s | {"segment": "rising", "copyable": "unknown", "edge": {"mean": 0.02, "margin": 0.03, "n": 12, "z": 1}}
    assert "over 12 trades so far, too few to be sure yet" in engine.why_lines(rising | engine._board(rising))["listed"]

    lucky = SCORE | {"segment": "lucky", "reason": "Big profit, but it rests on one bet."}
    assert engine.why_lines(lucky | engine._board(lucky))["listed"] == "Not on our list: Big profit, but it rests on one trade."

    young = s | {"pnl_weeks": [None] * 9 + [5.0, -1.0, 3.0]}
    assert "up in 2 of the 3 weeks since their first trade" in engine.why_lines(young | engine._board(young))["listed"]

    bare = engine.why_lines(SCORE | {"trades_per_day": 0.3})
    assert bare["trades"] is None and bare["how"] == "2 new markets a week."  # nothing made up


def test_a_wallet_scored_on_its_page_counts_wins_over_every_checked_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    """As the check and the background count them: over all 45 checked trades, not the 30 the directory keeps, so
    the same wallet's "Won" doesn't change with where its score came from."""
    w = "0x" + "5" * 40
    bets = [{"price": 0.8, "payout": 1 if i < 40 else 0} for i in range(45)]
    monkeypatch.setattr(engine, "_worth_one", {})
    monkeypatch.setattr(engine, "_directory_later", lambda f: None)
    monkeypatch.setattr(engine.reads, "whales", type("W", (), {"score": lambda self, x: type("S", (), {
        "to_dict": lambda _: {"wallet": x, "segment": "no_edge", "sample": list(bets)}})()})())  # fmt: skip
    s = engine._score_of(w)
    assert (s["won"], s["settled"]) == (40, 45)
    kept = s | {"sample": s["sample"][:30]}  # as directory.db keeps it
    assert {k: engine._board(kept).get(k, kept.get(k)) for k in ("won", "settled")} == {"won": 40, "settled": 45}


def test_the_profit_chart_ends_when_the_score_was_worked_out_not_today() -> None:
    page = (engine.HERE / "static" / "index.html").read_text()
    assert ">Today</text>" not in page and 'text-anchor="end">${s.window?.[1] ? ago(s.window[1]) : "Latest"}' in page


def test_a_traders_page_carries_the_why_lines(api: TestClient) -> None:
    why = api.get("/api/worth/0xabc").json()["why"]
    assert set(why) == {"trades", "how", "listed", "copying"} and why["copying"].startswith("Copyable: ")


def test_a_score_made_on_a_traders_page_says_when_and_the_page_shows_it(monkeypatch: pytest.MonkeyPatch, api: TestClient) -> None:
    """Users see how old a profile's figures are: a score worked out on the spot (kept until the next check)
    carries when, as kept ones do, and the header's "checked X ago" reads either."""
    w = "0x" + "7" * 40
    monkeypatch.setattr(engine, "checker", lambda: SimpleNamespace(score=lambda x, **k: SimpleNamespace(to_dict=lambda: {"wallet": x})))
    monkeypatch.setattr(engine, "_directory_later", lambda write: None)
    before = time.time()
    s = engine._score_of(w)
    assert before <= s["scored_at"] <= time.time() and engine._score_of(w)["scored_at"] == s["scored_at"]  # kept, with when
    assert "const checkedAt = s.stored_at ?? s.scored_at ?? null;" in api.get("/").text


def test_the_profile_card_and_the_price_check_have_the_new_lines(api: TestClient) -> None:
    page = api.get("/").text
    for words in ('class="pf-verdict"', 'id="pfcopy"', 'class="pf-warn"', 'class="pf-facts"', "Their numbers", 'class="pf-grid"', 'class="pf-rows"',
                  "<dt>Last bought</dt>", "See activity", 'id="whycopy"', "Why?", "How we measure this",
                  "What buying now costs", "An estimate, not a guaranteed fill", "is for sale within 3¢ of the cheapest price",
                  "Priced up to 3¢ above the cheapest price for sale", "Estimated slippage: your average fill",
                  "Order book and market status read at", "for context."):  # fmt: skip
        assert words in page, words
    for gone in ("<b>Lately</b>", "<b>Worth knowing</b>", "<b>Consistent</b>", "<b>Can you copy them</b>",
                 "What copying it costs", "within 3¢ of their price", 'class="pf-stats"', '["stats", "Stats"]', "Held until the market settles:", "Worst drop",
                 "<b>Won:</b>", "<b>Resting orders:</b>"):  # the old lines
        assert gone not in page, gone


NUMBERS = ("usd", "pl", "esc", "cents", "centsSigned", "ago", "plural", "lbPct", "readDays", "readPeriod", "weeksUp",
           "weeksOf", "PERIOD_DAYS", "PF_PERIOD", "pf", "pfChart", "profileNumbers")  # fmt: skip


def test_their_numbers_lead_with_realized_and_open_pnl_wallet_and_the_win_rate_slot(run_js: Any) -> None:
    """The profile's numbers (the trader profile audit's definitions): realized P&L for the period leads, with
    Polymarket's all-time realized; open P&L is Polymarket's figure now ("…" while the trader read loads, "—" if
    it failed, never the score's 30-day change in open value); the wallet is a total only when both parts were
    read; the switch moves realized P&L only; the closed-positions record and held-to-settlement aren't shown."""
    s = {"periods": {"7d": {"realized": -20, "unrealized": -30}, "30d": {"realized": 336000, "unrealized": 0}},
         "unrealized_window": 999, "trading_pnl": 900000, "biggest_win": 120000, "weeks_up": 4, "weeks_known": 6,
         "pnl_weeks": [1, -1, -2, 3, 4, 5], "won": 30, "settled": 58, "typical_usd": 24800, "trades_per_day": 2.4,
         "held_share": 1, "price_band": [0.35, 0.54], "categories": [{"category": "Sports", "bets": 41}, {"category": "Tennis", "bets": 1}]}
    detail = {"stats": {"realized_pnl": 3100000, "unrealized_pnl": 5400}, "positions": [], "closed": [{"result": "won"}]}
    got = run_js(NUMBERS, f"(() => {{ pf.account = {{}}; return ["
                          f"profileNumbers({s}, {detail}, {{cash_usd: 800000, positions_usd: 300000}}, '30d'),"
                          f"profileNumbers({s}, null, {{cash_usd: 5}}, '7d'),"
                          f"profileNumbers({s}, {{error: 'x'}}, {{}}, '30d')]; }})()")
    month, week, failed = got

    def rows(r: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {x["k"]: x for g in [{"rows": r["summary"]}, *r["groups"], {"rows": r["fold"]}] for x in g["rows"]}

    m = rows(month)
    assert month["hero"]["k"] == "Realized P&L, 30 days" and "+$336K" in month["hero"]["v"]
    assert month["hero"]["sub"] == "taken in the last 30 days · +$3.1M all time"
    assert [r["k"] for r in month["summary"]] == ["Open P&L", "Wallet", "Made money on"]
    assert "+$5.4K" in m["Open P&L"]["v"] and m["Open P&L"]["sub"] == "on positions still open · Polymarket's figure"
    assert m["Wallet"]["v"] == "$1.1M" and m["Wallet"]["sub"].startswith("cash $800K + open positions $300K")
    assert m["Made money on"]["v"] is None  # "—" until the SDK reads every settled position
    assert [g["title"] for g in month["groups"]] == ["Record", "How they trade"]
    # worst drop is hidden until the SDK works it out from the whole hourly curve: one reading a day can miss or
    # exaggerate a drop, depending on the hour the score was made
    assert not [k for k in m if "drop" in k.lower()]
    assert m["Weeks up"]["v"] == "4 of 6" and m["Weeks up"]["sub"] == "since their first trade" and m["Weeks up"]["weeks"]
    assert m["Markets"]["v"] == "Sports 98%" and m["Markets"]["sub"] == "of 42 events checked, last 30 days"  # under 5% left out
    assert m["Usual opening buy"]["v"] == "$24.8K" and m["Usual price paid"]["v"] == "35¢–54¢"
    assert m["New positions"]["v"] == "about 2 a day"
    assert "+$780K" in m["All time without their biggest win"]["v"]  # in the fold
    assert m["How far the price moved before you could buy"]["v"] is None  # not read: "—", never 0
    # hidden until fixed, not folded: the checked trades' win record (the Traders list keeps it) and resting orders
    assert not [k for k in m if k.startswith("Won") or "Resting" in k]
    shown = " ".join(m)
    assert "closed positions" not in shown.lower() and "settles" not in shown and "Held" not in shown
    # 7D moves realized P&L only; the trader read still loading is "…"; a wallet read in part isn't the wallet
    w = rows(week)
    assert week["hero"]["k"] == "Realized P&L, 7 days" and "−$20" in week["hero"]["v"] and week["hero"]["sub"].endswith("all time …")
    assert w["Open P&L"]["v"] == "…" and w["Usual opening buy"]["v"] == "$24.8K"
    assert w["Wallet"]["v"] is None and w["Wallet"]["sub"] == "cash $5.00; open positions couldn't be read"
    # the trader read failed: "—", never the score's change in open value over 30 days
    f = rows(failed)
    assert f["Open P&L"]["v"] is None and failed["hero"]["sub"] == "taken in the last 30 days"


# ---- the price check ----


def _preview(**kw: Any) -> CopyPreview:
    t = WhaleTrade("polymarket", "0xabc", None, "0xcid:1", "yes", "buy", 0.52, 0, 0, datetime.now(UTC), "x")
    base: dict[str, Any] = {"trade": t, "ok": True, "reason": "", "venue": "polymarket", "market": "0xcid:1",
                            "side": "yes", "contracts": 100.0, "price": 0.53, "worst_price": 0.53, "cost": 53.0,
                            "fee": 0.5, "total": 53.5, "max_spend": 400.0, "depth_checked": True}  # fmt: skip
    return CopyPreview(**(base | kw))


def test_the_check_names_the_slippage_against_the_best_price(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The SDK's best_price (the cheapest ask, from the same book read as the amount) names the slippage.
    from dataclasses import replace

    main = {"p": replace(_preview(price=0.534, worst_price=0.55), best_price=0.52)}
    asked: list[dict[str, Any]] = []

    def preview(self: Any, trade: Any, **kw: Any) -> CopyPreview:
        asked.append(kw)
        return main["p"]

    monkeypatch.setattr(Whales, "preview_copy", preview)
    body = {"wallet": "0xabc", "market": "0xcid:1", "price": 0.52, "spend": 50}
    assert api.post("/api/trade/preview", json=body).json()["best_price"] == 0.52
    assert len(asked) == 1  # the best price is their price: nothing more is read
    main["p"] = _preview(depth_checked=False, worst_price=None, price=0.52)
    assert api.post("/api/trade/preview", json=body).json()["best_price"] is None  # no book read: no slippage claimed


def test_the_check_says_how_much_the_book_holds_near_their_price(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The SDK's max_spend is everything for sale up to 3¢ above the cheapest price, fee included; the page turns it
    # into "Only $340 is for sale within 3¢ of the cheapest price".
    short = _preview(ok=False, reason="Only $340.00 is for sale at 0.55 or better right now", price=0.52,
                     max_spend=340.0, contracts=None, worst_price=None)  # fmt: skip
    monkeypatch.setattr(Whales, "preview_copy", lambda self, t, **kw: short)
    r = api.post("/api/trade/preview", json={"wallet": "0xabc", "market": "0xcid:1", "price": 0.52, "spend": 5000}).json()
    assert (r["ok"], r["max_spend"], r["depth_checked"], r["best_price"]) == (False, 340.0, True, None)


def test_rising_stays_hedged_even_when_copying_is_offered() -> None:
    rising = COPYABLE | {"segment": "rising", "edge": {"mean": 0.02, "margin": 0.03, "n": 12, "z": 1}}
    b = engine._board(rising)
    assert (b["section"], b["copy_ok"]) == ("rising", True)  # listed by tier; copying is its status
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
    deps = ("sdkText", "money", "notPriced")
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
    short = "is for sale within 3¢ of the cheapest price, fee included: not enough for this amount."
    assert got["small"] == {"line": cases["small"][0]["reason"], "room": 0}
    assert got["short"] == {"line": f"Only $340.55 {short}", "room": 340.55}
    assert got["crumbs"] == {"line": f"Only $0.40 {short}", "room": 0.4}
    assert got["none"] == {"line": "Nothing is for sale near the price right now.", "room": 0}
    assert got["nobook"] == {"line": cases["nobook"][0]["reason"], "room": 0}  # a closed market: Polymarket's status


def test_last_bought_is_their_newest_buy_from_any_source(run_js: Any) -> None:
    d = {"trades": [{"action": "buy", "at": "2026-10-13T10:00:00Z"}, {"action": "sell", "at": "2026-10-13T11:00:00Z"}]}
    s = {"recent": [{"at": "2026-10-01T00:00:00Z"}], "last_buy": "2026-10-12T00:00:00Z"}
    import json

    assert run_js(("lastBuyAt",), f"lastBuyAt({json.dumps(s)}, {json.dumps(d)})") == "2026-10-13T10:00:00Z"  # a sale isn't a buy
    assert run_js(("lastBuyAt",), f"lastBuyAt({json.dumps(s)}, {{error: 'x'}})") == "2026-10-12T00:00:00Z"
    assert run_js(("lastBuyAt",), "lastBuyAt({recent: []}, null)") is None


def test_a_copy_status_is_one_of_four_words_everywhere(run_js: Any) -> None:
    deps = ("isCopyable", "COPY_LABELS", "copyLabel", "esc", "boldStatus")
    losing = {"mean": -0.026, "margin": 0.022, "n": 53, "z": -2.4}
    # the engine's words, which the page shows as they come (copy_label) and with the status in bold
    for s in ({"segment": "proven", "copyable": "no", "copy": losing}, {"segment": "proven", "copyable": "unknown"},
              {"segment": "quiet", "copyable": "no"}, {"segment": "lucky", "copyable": "yes"},
              {"segment": "inactive", "copyable": "yes"}):  # fmt: skip
        b = engine._board(s | {"sample": []})
        why = b["copy_why"] or ""
        label = run_js(deps, f"copyLabel({{copy_ok: false, copy_label: {b['copy_label']!r}}})")
        assert label == b["copy_label"] and why.startswith(f"{label}: "), (s, why)
        assert run_js(deps, f"boldStatus({why!r})").startswith(f"<b>{label}</b>: ")
    got = run_js(deps, "[copyLabel({copy_ok: true}), copyLabel({copy_ok: false}), copyLabel({copy_label: 'Lucky'})]")
    assert got == ["Copyable", "Copy not proven", "Copy not proven"]  # never a word the engine doesn't say
    assert [engine._board(s | {"sample": []})["copy_label"] for s in (COPYABLE, SCORE | {"copyable": "no", "copy": losing})] == [
        "Copyable", "Costly to copy"]  # fmt: skip
