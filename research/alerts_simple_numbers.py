"""Numbers for the simple alerts plan (alerts-simple-plan.md): per trader, how many alerts a day "every trade" sends,
the "only bets over $X" amount Signals would suggest (X = their 3rd-biggest a day over the last 7 days), and whether
one read of their last 500 trades suggests about the same X. From notable-cache/, no reads.

    uv run python research/alerts_simple_numbers.py --to 2026-10-14T16:00 > research/alerts-simple-numbers.md
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from notable_replay import CACHE, _polymarket_trade, _units, load_jsonl

PER_DAY = 3  # the suggestion keeps about this many bets a day


def suggest(units: list[tuple[float, float]], days: float) -> float | None:
    """The dollar amount that about PER_DAY of these bets a day reach."""
    k = max(1, round(PER_DAY * days))
    xs = sorted((u for _, u in units), reverse=True)
    return xs[k - 1] if len(xs) >= k else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    a = ap.parse_args()
    end = datetime.fromisoformat(a.to).replace(tzinfo=UTC).timestamp()
    scen = json.loads((Path(__file__).parent / "notable-scenarios.json").read_text())["users"]
    live = set(scen["owner (live follows: NM-P + 0xf2f6)"]["follows"])
    wallets = sorted({w for u in scen.values() for w in u["follows"]})
    rows = []
    for w in wallets:
        trades = [t for t in (_polymarket_trade(r) for r in load_jsonl(CACHE / f"activity-{w}.jsonl.gz") if r.get("type") == "TRADE")
                  if t.size and t.at.timestamp() <= end]  # fmt: skip
        if not trades:
            continue
        name = next((t.name for t in reversed(trades) if t.name), w[:8])
        week = [t for t in trades if t.at.timestamp() >= end - 7 * 86400]
        days7 = min(7, (end - min(t.at.timestamp() for t in trades)) / 86400)
        buys7 = _units(week, "buy")
        sells7 = _units(week, "sell")
        per_day = (len(buys7) + len(sells7)) / days7
        x7 = suggest(buys7, days7)
        last500 = sorted(trades, key=lambda t: t.at)[-500:]
        span = max(1 / 24, (end - last500[0].at.timestamp()) / 86400)
        x500 = suggest(_units(last500, "buy"), span)
        kept = sum(1 for _, u in buys7 if x7 is not None and u >= x7) / days7
        rows.append((name, w in live, per_day, x7, kept, x500, span))
    rows.sort(key=lambda r: -r[2])
    print(f"# Simple alerts plan: numbers per trader (7 days to {a.to} UTC)\n")
    print("\"Every trade\" = buy and sell alerts a day after today's de-duplication (one per outcome per 6 h, nothing at a"
          f" settled price). \"Over $X\" = the amount about {PER_DAY} of their buys a day reach. \"From one read\" = the same"
          " amount worked out from their last 500 trades only (one Polymarket read at follow time).\n")
    print("| Trader | Every trade: alerts a day | Suggested \"only over $X\" | Alerts a day over $X | From one read | One read covers |")
    print("|---|---|---|---|---|---|")
    for name, mine, per_day, x7, kept, x500, span in rows:
        f = lambda x: "–" if x is None else f"${x:,.0f}"  # noqa: E731
        cover = f"{span * 24:.0f} h" if span < 2 else f"{span:.0f} days"
        print(f"| {name}{' (you follow)' if mine else ''} | {per_day:,.1f} | {f(x7)} | {kept:.1f} | {f(x500)} | {cover} |")


if __name__ == "__main__":
    main()
