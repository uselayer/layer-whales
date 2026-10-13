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
                       {"category": "Sports", "bets": 2}, {"category": "Crypto", "bets": 6}],
        "price_band": [0.55, 0.84], "typical_usd": 855.0, "held_share": 0.56, "decisions": 60,
        "window": ["2026-09-07T00:00:00+00:00", "2026-10-07T00:00:00+00:00"],
        "edge": {"mean": 0.02, "margin": 0.01, "n": 43, "z": 3},
        "periods": {"30d": {"trading_pnl": 12345.0}}, "pnl_weeks": [10.0] * 8 + [-5.0] * 4,
    }  # fmt: skip
    why = engine.why_lines(s | engine._board(s))
    assert why["trades"] == "Politics 85%, Economy 7%"  # the top three by share; a 2% sideline isn't listed
    assert why["how"] == "Buys around 55–84¢, usually $855, 2 new markets a day, holds 56% until the market settles."
    assert why["listed"] == ("The price tends to move their way within minutes after they buy (over 43 trades), and it"
                             " was still moving when a copy a few minutes late got in. Trading profit +$12.3K in the"
                             " last 30 days; up in 8 of the last 12 weeks.")  # fmt: skip
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
