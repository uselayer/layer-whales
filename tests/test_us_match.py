"""Polymarket US twins: Layer's International↔Kalshi and Kalshi↔Polymarket US matches, joined and checked end to end."""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from uselayer import Trader, TraderDetail, WhalePosition, WhaleTrade

from signals import app as engine
from signals import us_match
from signals.us_match import UsTwins

GAME = "0xgame"  # a game on International: outcome 0 Cleveland, 1 Orlando
BTTS = "0xbtts"  # a plain Yes/No market: both teams to score?


def intl(market_id: str, kalshi: str, token: str, **kw: Any) -> dict[str, Any]:
    """A Layer match of a Polymarket International market to Kalshi, as ``Client.matches(...).to_dict()`` gives it."""
    return {"tier": "auto_verified", "basis": "identical", "confidence": 0.97, "caveats": [], "caveat_notes": {},
            "kalshi": {"venue": "kalshi", "market_id": kalshi},
            "polymarket": {"venue": "polymarket", "market_id": market_id, "yes_token_id": token,
                           "url": "https://polymarket.com/market/x"}} | kw  # fmt: skip


def us(market_id: str, kalshi: str, outcome: str, **kw: Any) -> dict[str, Any]:
    """A Layer match of Kalshi to Polymarket US."""
    return {"tier": "auto_verified", "basis": "identical", "confidence": 0.95, "caveats": [], "caveat_notes": {},
            "kalshi": {"venue": "kalshi", "market_id": kalshi},
            "polymarket_us": {"venue": "polymarket_us", "market_id": market_id, "outcome": outcome, "event": "CLE vs ORL",
                              "url": "https://polymarket.us/event/nba-cle-orl"}} | kw  # fmt: skip


def layer() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        [intl(f"{GAME}:0", "K-CLE", "tok-cle", basis="equivalent_with_caveats", caveats=["source_differs"], confidence=0.85),
         intl(f"{GAME}:1", "K-ORL", "tok-orl"),
         intl(BTTS, "K-BTTS", "tok-btts-yes")],
        [us("aec-nba-cle-orl:long", "K-CLE", "Cavaliers", caveats=["carveout_differs"],
            caveat_notes={"carveout_differs": "Polymarket US sends a tie to manual review."}),
         us("aec-nba-cle-orl:short", "K-ORL", "Magic"),
         us("astatc-btts", "K-BTTS", "Both teams to score")],
    )  # fmt: skip


def built(intl_rows: list[dict[str, Any]] | None = None, us_rows: list[dict[str, Any]] | None = None) -> UsTwins:
    a, b = layer()
    t = UsTwins()
    t.build(a if intl_rows is None else intl_rows, b if us_rows is None else us_rows, now=0.0)
    return t


# ---- the index: which trades have a verified twin ----


def test_a_trade_on_a_matched_outcome_has_its_twin_with_every_stated_difference_from_both_matches() -> None:
    t = built()
    got = t.lookup(f"{GAME}:0", "tok-cle", "Cleveland Cavaliers", now=1.0)
    assert got is not None and got["found"] is True
    assert (got["url"], got["outcome"], got["via"], got["confidence"]) == ("https://polymarket.us/event/nba-cle-orl", "Cavaliers", "kalshi", 0.85)
    # International↔Kalshi states a code only (its plain words); Kalshi↔US its own note, which wins over the code's words
    assert got["differences"] == [{"code": "source_differs", "text": us_match.DIFFERENCES["source_differs"]},
                                  {"code": "carveout_differs", "text": "Polymarket US sends a tie to manual review."}]  # fmt: skip
    other = t.lookup(f"{GAME}:1", "tok-orl", "Orlando Magic", now=1.0)
    assert other is not None and other["outcome"] == "Magic" and other["differences"] == []
    assert t.counts == {"international": 3, "us": 3, "twins": 3, "dropped": 0}


def test_the_outcome_must_be_the_one_layer_matched() -> None:
    t = built()
    # The token decides: Orlando's token on outcome index 0 (the index is sometimes junk) is Orlando's twin
    assert t.lookup(f"{GAME}:0", "tok-orl", "Orlando Magic", now=1.0)["outcome"] == "Magic"  # type: ignore[index]
    # A token Layer never matched: no twin, even though the game has twins for its other outcomes
    assert t.lookup(f"{GAME}:2", "tok-draw", "Draw", now=1.0) == {"found": False}
    # No token: Layer's id for the outcome
    assert t.lookup(f"{GAME}:1", None, "Orlando Magic", now=1.0)["outcome"] == "Magic"  # type: ignore[index]
    assert t.lookup("0xelse:0", "tok-else", "Yes", now=1.0) == {"found": False}


def test_in_a_plain_yes_no_market_the_no_is_the_us_markets_no_and_only_when_both_are_plain_yes_no() -> None:
    t = built()
    yes = t.lookup(f"{BTTS}:0", "tok-btts-yes", "Yes", now=1.0)
    assert yes is not None and yes["outcome"] == "Both teams to score"
    no = t.lookup(f"{BTTS}:1", "tok-btts-no", "No", now=1.0)
    assert no is not None and no["found"] and no["outcome"] == "No on Both teams to score" and no["url"] == yes["url"]
    assert t.lookup(f"{BTTS}:0", None, "Yes", now=1.0)["found"] is True  # type: ignore[index]  # no token: outcome 0, named Yes
    assert t.lookup(f"{BTTS}:1", None, "No", now=1.0)["outcome"] == "No on Both teams to score"  # type: ignore[index]
    # The outcome's name has to agree: a junk index with no token and a name that isn't No says nothing
    assert t.lookup(f"{BTTS}:1", None, "Yes", now=1.0) == {"found": False}
    assert t.lookup(f"{BTTS}:1", "tok-btts-no", "Maybe", now=1.0) == {"found": False}
    # The same International Yes/No market matched to one side of a two-sided US market: its No has no verified twin
    a, b = layer()
    b[2] = us("astatc-btts:long", "K-BTTS", "Both teams to score")
    t2 = built(a, b)
    assert t2.lookup(f"{BTTS}:0", "tok-btts-yes", "Yes", now=1.0)["found"] is True  # type: ignore[index]
    assert t2.lookup(f"{BTTS}:1", "tok-btts-no", "No", now=1.0) == {"found": False}


@pytest.mark.parametrize("which,change", [
    ("intl", {"tier": "candidate"}),  # not verified yet: Layer's review queue
    ("us", {"tier": "pending"}),
    ("intl", {"basis": "different"}),
    ("us", {"basis": None}),
    ("us", {"polymarket_us": {"venue": "polymarket_us", "market_id": "x:long", "outcome": "Cavaliers",
                              "url": "https://polymarket.example/event/x"}}),  # a link that isn't polymarket.us
    ("us", {"kalshi": {"venue": "kalshi", "market_id": "K-CLE-OTHER"}}),  # another Kalshi outcome: the chain breaks
])  # fmt: skip
def test_a_twin_needs_both_matches_verified_the_same_bet_on_the_same_kalshi_market_and_a_polymarket_us_link(
    which: str, change: dict[str, Any]
) -> None:
    a, b = layer()
    if which == "intl":
        a[0] |= change
    else:
        b[0] |= change
    t = built(a, b)
    assert t.lookup(f"{GAME}:0", "tok-cle", "Cleveland Cavaliers", now=1.0) == {"found": False}
    assert t.lookup(f"{GAME}:1", "tok-orl", "Orlando Magic", now=1.0)["found"] is True  # type: ignore[index]  # the rest stand


def test_nothing_is_claimed_before_the_index_is_built_or_once_it_goes_stale() -> None:
    t = UsTwins()
    assert t.lookup(f"{GAME}:0", "tok-cle", "Cleveland Cavaliers") is None  # never read: no claim either way
    t = built()
    assert t.lookup(f"{GAME}:0", "tok-cle", "x", now=us_match.STALE_S - 1) is not None
    assert t.lookup(f"{GAME}:0", "tok-cle", "x", now=us_match.STALE_S + 1) is None
    assert t.lookup(None, "tok-cle", "x", now=1.0) is None


def test_refresh_reads_every_page_of_both_lists_and_a_failure_keeps_the_last_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us_match, "PAGE", 2)
    a, b = layer()
    asked: list[tuple[str, int]] = []

    def read(*, titles: bool, venue: str, limit: int, offset: int) -> list[dict[str, Any]]:
        assert titles is False and limit == 2
        asked.append((venue, offset))
        rows = a if venue == "polymarket" else b
        return rows[offset : offset + limit]

    us_match.refresh(read)
    assert asked == [("polymarket", 0), ("polymarket", 2), ("polymarket_us", 0), ("polymarket_us", 2)]
    assert us_match.twins.counts["twins"] == 3 and us_match.twins.error is None
    assert us_match.twins.lookup(f"{GAME}:0", "tok-cle", "x")["found"] is True  # type: ignore[index]

    def down(**_: Any) -> list[Any]:
        raise RuntimeError("Layer is down")

    us_match.refresh(down)
    assert us_match.twins.error == "RuntimeError: Layer is down"
    assert us_match.twins.lookup(f"{GAME}:0", "tok-cle", "x")["found"] is True  # type: ignore[index]  # until it goes stale



def test_more_matches_than_the_page_cap_keeps_the_last_index_instead_of_part_of_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # On 10/14 Layer served 9,780 International matches against a cap of 10,000: past it, the twins of every
    # match after the cap would have turned into "no verified US match" with nothing said.
    monkeypatch.setattr(us_match, "PAGE", 2)
    a, b = layer()
    us_match.refresh(lambda *, titles, venue, limit, offset: (a if venue == "polymarket" else b)[offset : offset + limit])
    assert us_match.twins.error is None and us_match.twins.counts["twins"] == 3

    monkeypatch.setattr(us_match, "MAX_PAGES", 1)  # a full first page and no room for the rest
    us_match.refresh(lambda *, titles, venue, limit, offset: (a if venue == "polymarket" else b)[offset : offset + limit])
    assert us_match.twins.error == "RuntimeError: Layer has more than 2 polymarket matches; raise us_match.MAX_PAGES"
    assert us_match.twins.counts["twins"] == 3  # the last full index, not one built from the first page
    assert us_match.twins.lookup(f"{GAME}:0", "tok-cle", "x")["found"] is True  # type: ignore[index]

# ---- what the engine sends the page ----


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": []})
    monkeypatch.setattr(engine, "_worth_one", {})
    monkeypatch.setattr(engine, "_categories", lambda ids: {})
    monkeypatch.setattr(engine, "position_links", lambda wallet: {})
    monkeypatch.setattr(us_match, "twins", built())
    monkeypatch.setattr(us_match.twins, "fresh", lambda now=None: True)
    return TestClient(engine.app)


def poly(market: str, token: str | None, outcome: str, action: str = "buy") -> WhaleTrade:
    return WhaleTrade("polymarket", "0xabc", None, market, "yes", action, 0.4, 100, 40.0, datetime.now(UTC), f"t-{market}",
                      title="CLE vs ORL", outcome=outcome, url="https://polymarket.com/event/nba-cle-orl", token=token)  # fmt: skip


def test_big_trades_and_a_traders_trades_and_positions_carry_the_us_twin(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    w = engine.reads.whales
    monkeypatch.setattr(w, "big_trades", lambda *a, **k: [poly(f"{GAME}:0", "tok-cle", "Cleveland"), poly("0xelse:0", "tok-x", "Yes")])
    rows = api.get("/api/trading-now").json()
    assert rows[0]["us"]["found"] is True and rows[0]["us"]["url"] == "https://polymarket.us/event/nba-cle-orl"
    assert rows[1]["us"] == {"found": False}
    monkeypatch.setattr(w, "trader", lambda *a, **k: TraderDetail(
        Trader(venue="polymarket", id="0xabc", name=None), "visible", {},
        [WhalePosition(f"{GAME}:1", "CLE vs ORL", "Orlando Magic", 10, 0.5, 0.5, None, None)],
        [poly(f"{GAME}:1", "tok-orl", "Orlando Magic")]))  # fmt: skip
    d = api.get("/api/trader/polymarket/0xabc").json()
    assert d["positions"][0]["us"]["outcome"] == "Magic" and d["trades"][0]["us"]["outcome"] == "Magic"


def test_a_feed_bet_carries_its_twin_and_none_while_layer_cant_be_read(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    d = TraderDetail(Trader(venue="polymarket", id="0xabc", name=None), "visible", {}, [], [poly(f"{GAME}:0", "tok-cle", "Cleveland")])
    who = {"wallet": "0xabc"}
    assert engine._bets_of(d, who)[0]["us"]["outcome"] == "Cavaliers"
    monkeypatch.setattr(us_match, "twins", UsTwins())  # not read yet
    assert engine._bets_of(d, who)[0]["us"] is None


# ---- alerts ----


def test_an_alert_says_where_they_traded_and_links_the_us_twin_through_go(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_copy_offered", lambda wallet: False)
    monkeypatch.setattr(engine, "_who", lambda wallet: "Sam")
    at = datetime(2026, 10, 13, 14, 2, tzinfo=UTC).timestamp()
    text = engine._alert_text("0xabc", poly(f"{GAME}:0", "tok-cle", "Cleveland"), 0.4, 0.41, 40, at, at + 60, aid="a1")
    assert engine.US_TWIN_LINE + engine.US_DIFFERS in text.splitlines()
    us_go = f"/go/a1/market_us?to={urllib.parse.quote('https://polymarket.us/event/nba-cle-orl', safe='')}"
    assert us_go in text and ">View on Polymarket US</a>" in text and ">Buy on Polymarket International</a>" in text
    plain = engine._alert_text("0xabc", poly(f"{GAME}:1", "tok-orl", "Orlando"), 0.4, 0.41, 40, at, at + 60)
    assert engine.US_TWIN_LINE in plain.splitlines()  # no stated differences: none claimed
    none = engine._alert_text("0xabc", poly("0xelse:0", "tok-x", "Yes"), 0.4, 0.41, 40, at, at + 60)
    assert engine.NO_US_TWIN_LINE in none.splitlines() and "Polymarket US</a>" not in none
    sold = engine._sell_text("0xabc", poly(f"{GAME}:0", "tok-cle", "Cleveland", "sell"), 0.4, 40, False, None, at, at + 60)
    assert ">View on Polymarket US</a>" in sold and ">Open on Polymarket International</a>" in sold
    # the words never offer their price on Polymarket US, nor suggest Signals sees trades there
    for line in (engine.US_TWIN_LINE, engine.NO_US_TWIN_LINE):
        assert "may differ" in line or "No verified US match found" in line
        assert "Not on Polymarket US" not in line and "US trader" not in line


def test_go_sends_a_tap_to_polymarket_us_and_nowhere_else(api: TestClient) -> None:
    assert engine._allowed("https://polymarket.us/event/nba-cle-orl")
    assert not engine._allowed("https://polymarket.us.evil.example/x") and not engine._allowed("http://polymarket.us/x")
    r = api.get("/go/a1/market_us", params={"to": "https://polymarket.us/event/nba-cle-orl"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (302, "https://polymarket.us/event/nba-cle-orl")


# ---- the page ----

PAGE_DEFS = ("esc", "INTL", "usLink", "venueNote", "usInline", "usBox")
TWIN = {"found": True, "url": "https://polymarket.us/event/nba-cle-orl", "outcome": "Cavaliers",
        "differences": [{"code": "carveout_differs", "text": "Polymarket US sends a tie to <manual> review."}]}  # fmt: skip


def test_the_page_names_international_and_shows_the_us_twin_or_says_no_verified_match(run_js: Any) -> None:
    found, none, unknown = run_js(PAGE_DEFS, f"[{json.dumps(TWIN)}, {{found: false}}, null].map(venueNote)")
    assert "Traded on Polymarket International" in found and "View on Polymarket US ↗" in found
    assert "prices and liquidity may differ" in found and 'href="https://polymarket.us/event/nba-cle-orl"' in found
    assert "Traded on Polymarket International · No verified US match found" in none and "polymarket.us" not in none
    assert unknown == '<p class="venue">Traded on Polymarket International</p>'  # couldn't check: no claim either way
    rows = run_js(PAGE_DEFS, f"[{json.dumps(TWIN)}, {{found: false}}, null, undefined].map(usInline)")
    assert "View on Polymarket US ↗" in rows[0] and rows[1] == " · No verified US match found" and rows[2:] == ["", ""]


def test_the_trade_sheet_explains_the_twin_its_differences_and_that_their_price_is_international(run_js: Any) -> None:
    box = run_js(PAGE_DEFS, f"usBox({{us: {json.dumps(TWIN)}}})")
    assert "This trade was made on Polymarket International" in box and "doesn't see trades on Polymarket US" in box
    assert "<b>Cavaliers</b>" in box and "prices and liquidity there may differ" in box
    assert "this trader's price isn't one to expect there" in box
    assert "Polymarket US sends a tie to &lt;manual&gt; review." in box  # Layer's words, escaped
    assert "View on Polymarket US ↗" in box
    none = run_js(PAGE_DEFS, "usBox({us: {found: false}})")
    assert "No verified US match found." in none and "View on Polymarket US" not in none
    assert "couldn't check" in run_js(PAGE_DEFS, "usBox({us: null})")


def test_the_page_never_says_a_market_is_not_on_polymarket_us_and_scopes_quotes_to_international() -> None:
    page = (engine.HERE / "static" / "index.html").read_text()
    assert "Not on Polymarket US" not in page and "US traders" not in page
    assert "This estimate is for ${INTL} only. Polymarket US has its own order book and fees, so it doesn't apply there." in page


# ---- the quote: the numbers in view, the working folded ----

QUOTE_DEFS = ("esc", "INTL", "c1", "money", "slipOf", "slippageLine", "readAt", "quoteNote", "quoteFold")


def test_the_quote_folds_its_working_and_shows_slippage_only_when_there_is_some(run_js: Any) -> None:
    p = {"price": 0.948, "best_price": 0.948, "limit": 0.978, "max_spend": 440282.83, "depth_checked": True,
         "contracts": 10.0, "total": 9.5, "priced_at": None}  # fmt: skip
    warn, note, fold = run_js(QUOTE_DEFS, f"((p) => [slippageLine(p), quoteNote(p), quoteFold(p)])({json.dumps(p)})")
    assert warn == ""  # no slippage: nothing in view, a row in the fold
    assert note == '<p class="qnote">Polymarket International estimate · not a guaranteed fill</p>'
    assert fold.startswith('<details class="qmore"><summary>How this estimate works</summary>')
    for words in ("<dt>Best price for sale</dt><dd>94.8¢</dd>", "<dd>94.8¢ (no slippage)</dd>", "<dd>95¢</dd>",
                  "<dd>97.8¢ at most</dd>", "<dd>$440,282.83, fee included</dd>", "offers above 97.8¢ aren't counted",
                  "An estimate, not a guaranteed fill", "Polymarket US has its own order book and fees"):  # fmt: skip
        assert words in fold, words
    slipped = run_js(QUOTE_DEFS, f"slippageLine({json.dumps(p | {'price': 0.956})})")
    assert slipped.startswith('<p class="qwarn">Estimated slippage: your average fill, 95.6¢, is 0.8¢ above')
