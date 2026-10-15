"""What's noteworthy about a trade (signals/notes.py): observation, context and interpretation kept apart, and
nothing said that the data can't back. No network: trades and positions are made up here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from uselayer import WhalePosition, WhaleTrade

from signals.notes import EXPOSURE, trade_notes

W, M, OTHER = "0x" + "a" * 40, "0xcid:0", "0xelse:1"
T0 = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
SCORE = {"typical_usd": 150.0, "window": ["2026-09-14T12:00:00+00:00", "2026-10-14T12:00:00+00:00"],
         "categories": [{"category": "Sports", "bets": 40}, {"category": "Esports", "bets": 0}]}  # fmt: skip


def t(side: str, usd: float, at: datetime, market: str = M, price: float = 0.5, i: str = "") -> WhaleTrade:
    return WhaleTrade("polymarket", W, None, market, "yes", side, price, usd / price, usd, at, f"{market}{side}{at}{i}")


def pos(size: float, market: str = M, now: float | None = 0.6) -> WhalePosition:
    return WhalePosition(market, None, "Yes", size, 0.5, now, None, None)


def notes(side: str, trades: list[WhaleTrade], positions: list[WhalePosition], whole: bool = True,
          score: dict[str, Any] | None = None, category: str | None = None, since: float | None = None) -> list[tuple[str, str, str]]:  # fmt: skip
    return [(n.level, n.kind, n.text) for n in trade_notes(side, M, since, trades, positions, whole, score, category)]


def test_a_new_position_far_bigger_than_usual_gets_all_three_levels() -> None:
    got = notes("buy", [t("buy", 1000, T0)], [pos(2000)], score=SCORE)
    assert got == [("observation", "new", "New position."),
                   ("context", "size", "About 7× their usual buy ($150, last 30 days)."),
                   ("interpretation", "exposure", EXPOSURE)]  # fmt: skip
    # the interpretation says what we don't know, and never that it's skill or will pay
    assert "We don't know why" in EXPOSURE and "isn't a sign they're right" in EXPOSURE


def test_adding_to_a_position_says_what_they_held_before_at_the_price_now() -> None:
    got = notes("buy", [t("buy", 1000, T0), t("buy", 500, T0 - timedelta(days=1))], [pos(3000)], since=T0.timestamp())
    assert got == [("observation", "adds", "Adds to a position they already held (about $600 at the price now).")]


def test_no_position_note_without_the_trades_from_before_or_when_positions_lag() -> None:
    # a read that doesn't reach back past the buy (and isn't all they've made): what they held before isn't known
    assert notes("buy", [t("buy", 1000, T0)], [pos(3000)], whole=False) == []
    # the positions read is behind the trades (it holds less than they just bought): nothing is said
    assert notes("buy", [t("buy", 1000, T0)], [pos(500)]) == []
    # no open position at all (sold since, or never priced): nothing is said
    assert notes("buy", [t("buy", 1000, T0)], []) == []


def test_a_partial_sell_says_how_much_of_the_position_went() -> None:
    assert notes("sell", [t("sell", 200, T0)], [pos(600)]) == [("observation", "sold_part", "Sold about 40% of their position.")]
    assert notes("sell", [t("sell", 1000, T0)], [pos(20)]) == [("observation", "sold_part", "Sold nearly all of their position.")]
    assert notes("sell", [t("sell", 200, T0)], []) == []  # sold all of it: the alert's "They no longer hold it." says so


def test_their_first_trade_in_a_while_counts_any_market() -> None:
    old = [t("buy", 50, T0 - timedelta(days=12, hours=1), OTHER)]
    assert ("observation", "quiet", "Their first trade in 12 days.") in notes("buy", [t("buy", 1000, T0), *old], [pos(2000)])
    recent = [t("sell", 50, T0 - timedelta(days=2), OTHER)]
    assert all(k != "quiet" for _, k, _ in notes("buy", [t("buy", 1000, T0), *recent], [pos(2000)]))


def test_size_compares_one_buy_with_their_usual_one_not_a_run_of_fills() -> None:
    # $200 + $200 within a minute is one $400 buy; another $400 ten minutes later is a second: about 2.7×, not 5.3×
    fills = [t("buy", 200, T0, i="a"), t("buy", 200, T0 + timedelta(seconds=30), i="b"), t("buy", 400, T0 + timedelta(minutes=10))]
    got = notes("buy", fills, [pos(1600)], score=SCORE)
    assert ("context", "size", "About 2.7× their usual buy ($150, last 30 days).") in got
    assert all(k != "exposure" for _, k, _ in got)  # under UNUSUAL: no interpretation
    small = notes("buy", [t("buy", 50, T0)], [pos(100)], score=SCORE)
    assert ("context", "size", "Smaller than their usual buy ($150, last 30 days).") in small
    assert all(k != "size" for _, k, _ in notes("buy", [t("buy", 200, T0)], [pos(400)], score=SCORE))  # about usual: nothing


def test_no_interpretation_without_knowing_the_position_or_without_a_score() -> None:
    big = [t("buy", 5000, T0)]
    assert [k for _, k, _ in notes("buy", big, [pos(10_000)], whole=False, score=SCORE)] == ["size"]
    assert [k for _, k, _ in notes("buy", big, [pos(10_000)])] == ["new"]  # no score: no usual size to compare with


def test_a_category_none_of_their_checked_buys_were_in() -> None:
    got = notes("buy", [t("buy", 150, T0)], [pos(300)], score=SCORE, category="Politics")
    assert ("context", "category", "None of the 40 buys we checked from their last 30 days were in Politics.") in got
    assert all(k != "category" for _, k, _ in notes("buy", [t("buy", 150, T0)], [pos(300)], score=SCORE, category="Sports"))
    assert all(k != "category" for _, k, _ in notes("buy", [t("buy", 150, T0)], [pos(300)], score=SCORE, category="Other"))
    few = SCORE | {"categories": [{"category": "Sports", "bets": 6}]}  # too few checked to say
    assert all(k != "category" for _, k, _ in notes("buy", [t("buy", 150, T0)], [pos(300)], score=few, category="Politics"))


def test_no_note_ever_claims_skill_information_or_a_win() -> None:
    every = [*notes("buy", [t("buy", 5000, T0), t("buy", 9, T0 - timedelta(days=30), OTHER)], [pos(10_000)], score=SCORE, category="Politics"),
             *notes("buy", [t("buy", 1000, T0), t("buy", 500, T0 - timedelta(hours=1))], [pos(3000)], since=T0.timestamp(), score=SCORE),
             *notes("sell", [t("sell", 200, T0)], [pos(600)])]  # fmt: skip
    assert {k for _, k, _ in every} == {"new", "adds", "quiet", "size", "category", "exposure", "sold_part"}
    words = " ".join(text.lower() for _, _, text in every)
    for claim in ("skill", "smart", "informed", "insider", "conviction", "sharp", "profit", "will win", "edge", "bet"):
        assert claim not in words
