"""What's noteworthy about one trade, from what Signals already holds: the trader's last trades and open
positions (the Feed's poller keeps both) and their last score. No reads of its own.

Three levels, kept apart (each note says which it is):

- observation: what the data shows about this trade ("New position.", "Sold about 40% of their position.",
  "Their first trade in 12 days.").
- context: this trade against the trader's own record ("About 6× their usual buy ($2,000, last 30 days).").
- interpretation: what that adds up to, said with what we don't know. Only one, for a buy far bigger than their
  usual that opens or adds to a position: an unusual increase in their exposure, "We don't know why, and it isn't a
  sign they're right." Unusual is never informed: nothing here says a trade is skilled or will pay.

Nothing is said that the data can't back: a position note needs the trades read since this one began (so what they
held before is known), the size note a score with their usual buy, the category note enough checked buys. Otherwise
the note is left out, never guessed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from uselayer import WhalePosition, WhaleTrade

Level = Literal["observation", "context", "interpretation"]

MERGE_S = 60  # fills on one outcome within a minute are one buy: the unit of the SDK's typical_usd
SIZE_UP = 2.0  # "about N× their usual buy" from this many times typical_usd
SIZE_DOWN = 0.5  # "smaller than their usual buy" at or under this share of it
UNUSUAL = 3.0  # the interpretation, for a new or added position at least this many times their usual buy
QUIET_DAYS = 7  # "their first trade in N days" after at least this long without one
NEW_SHARE = 0.02  # held before at or under this share of the position now: a new position (rounding, dust)
CATEGORY_MIN = 10  # checked buys needed before "none of them were in Politics" says anything
EXPOSURE = "That's an unusual increase in their exposure to this outcome. We don't know why, and it isn't a sign they're right."


@dataclass(frozen=True)
class Note:
    level: Level
    kind: str  # new, adds, sold_part, quiet, size, category, exposure
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _money(v: float) -> str:
    return f"${v:,.0f}"


def _times(r: float) -> str:
    return f"{r:.1f}".rstrip("0").rstrip(".") + "×" if r < 3 else f"{r:.0f}×"


def _days(score: dict[str, Any]) -> int:
    """How many days the score's figures cover (its window), 30 when it doesn't say."""
    try:
        a, b = (datetime.fromisoformat(x) for x in score["window"])
        return max(1, round((b - a).total_seconds() / 86400))
    except (KeyError, TypeError, ValueError):
        return 30


def _biggest_buy(buys: list[WhaleTrade]) -> float:
    """Their biggest single buy here: fills within MERGE_S of a buy's first are that buy (the SDK's typical_usd
    counts the same way), so a 30-minute run of fills isn't compared with a one-minute median."""
    best, start, total = 0.0, None, 0.0
    for t in sorted(buys, key=lambda t: t.at):
        if start is None or (t.at - start).total_seconds() > MERGE_S:
            start, total = t.at, 0.0
        total += t.usd
        best = max(best, total)
    return best


def trade_notes(side: str, market: str, since: float | None, trades: list[WhaleTrade],
                positions: list[WhalePosition], whole: bool, score: dict[str, Any] | None = None,
                category: str | None = None) -> list[Note]:  # fmt: skip
    """The notes for a trader's buys (or sells) of ``market`` (``<conditionId>:<outcome>``) from ``since`` (epoch
    seconds; None: every one in ``trades``), as a Feed card or an alert merges them. ``trades``: their latest trades,
    any market; ``whole``: whether that's all they've made (a read that came back short of its limit), else only
    what's from after the oldest of them is known. ``positions``: their open positions now. ``score``: their last
    score (``typical_usd``, ``categories``, ``window``), if they have one; ``category``: this market's."""
    mine = [t for t in trades if t.market == market and t.size and (since is None or t.at.timestamp() >= since)]
    these = [t for t in mine if t.action == side]
    if not these:
        return []
    first = min(t.at for t in these)
    # what they held before this began is known only when the read reaches back past it
    covered = whole or any(t.at < first for t in trades)
    pos = next((p for p in positions if p.market == market), None)
    bought = sum(t.size for t in mine if t.action == "buy" and t.at >= first)
    sold = sum(t.size for t in mine if t.action == "sell" and t.at >= first)
    out: list[Note] = []

    held = None  # "new" or "adds", for the interpretation
    if side == "buy" and covered and pos is not None and pos.size > 0:
        before = pos.size - bought + sold
        if abs(before) <= NEW_SHARE * pos.size:
            held = "new"
            out.append(Note("observation", "new", "New position."))
        elif before > 0:
            held = "adds"
            worth = f" (about {_money(before * pos.current_price)} at the price now)" if pos.current_price else ""
            out.append(Note("observation", "adds", f"Adds to a position they already held{worth}."))
        # less than nothing before: the positions read is behind the trades, so nothing is said
    if side == "sell" and covered and pos is not None and pos.size > 0:
        before = pos.size + sold - bought
        if before > 0 and 0 < sold < before:
            share = sold / before
            text = ("Sold nearly all of their position." if share >= 0.95
                    else f"Sold about {max(5, round(share * 20) * 5)}% of their position.")  # fmt: skip
            out.append(Note("observation", "sold_part", text))

    prior = [t for t in trades if t.at < first]
    if prior:
        gap = (first - max(t.at for t in prior)).total_seconds() / 86400
        if gap >= QUIET_DAYS:
            out.append(Note("observation", "quiet", f"Their first trade in {int(gap)} days."))

    ratio = None
    if side == "buy" and score and (typical := score.get("typical_usd")):
        ratio = _biggest_buy(these) / typical
        days = _days(score)
        if ratio >= SIZE_UP:
            out.append(Note("context", "size", f"About {_times(ratio)} their usual buy ({_money(typical)}, last {days} days)."))
        elif ratio <= SIZE_DOWN:
            out.append(Note("context", "size", f"Smaller than their usual buy ({_money(typical)}, last {days} days)."))

    if side == "buy" and score and category and category != "Other":
        cats = [c for c in score.get("categories") or [] if c.get("bets")]
        checked = sum(c["bets"] for c in cats)
        if checked >= CATEGORY_MIN and category not in {c.get("category") for c in cats}:
            out.append(Note("context", "category",
                            f"None of the {checked} buys we checked from their last {_days(score)} days were in {category}."))  # fmt: skip

    if held and ratio is not None and ratio >= UNUSUAL:
        out.append(Note("interpretation", "exposure", EXPOSURE))
    return out
