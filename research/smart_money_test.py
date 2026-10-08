"""Test C: would copying proven Polymarket traders have made money?

For the top Polymarket wallets by volume this month, split them by the profit they had made BEFORE the test
window (so the test doesn't pick winners with hindsight). For each buy they made in the window, on a market
that has since settled, ask:

- What the trader made: payout minus their price.
- What a copier would have made buying the same outcome 1 or 5 minutes later: payout minus that later price
  minus Polymarket's taker fee (a copier always takes). Also with 1 extra cent of slippage.
- How much the price had already moved by then (the edge that leaked).

Fills on the same outcome within 60 seconds count as one decision. Every number is per decision, with equal
money on each, so one huge bet doesn't decide the result.

    uv run python research/smart_money_test.py --days 14 --wallets 100
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CACHE = Path(__file__).parent / ".cache"
DELAYS = (60, 300)

http = httpx.Client(timeout=30, headers={"user-agent": "layer-research"})
_last = [0.0]


def get(url: str, params: dict[str, Any]) -> Any:
    """GET with a disk cache (so reruns are free) and gentle pacing."""
    key = hashlib.sha1((url + json.dumps(params, sort_keys=True, default=str)).encode()).hexdigest()
    f = CACHE / f"{key}.json"
    if f.exists():
        return json.loads(f.read_text())
    for attempt in range(5):
        wait = 0.12 - (time.monotonic() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
        try:
            r = http.get(url, params=params)
        except httpx.HTTPError:
            time.sleep(2**attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2**attempt)
            continue
        r.raise_for_status()
        body = r.json()
        CACHE.mkdir(exist_ok=True)
        f.write_text(json.dumps(body))
        return body
    raise RuntimeError(f"gave up on {url} {params}")


@dataclass
class Decision:
    wallet: str
    condition: str
    outcome: int
    at: int
    price: float  # their average price
    usd: float


def pool(n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for offset in range(0, n, 50):
        out += get(f"{DATA}/v1/leaderboard", {"timePeriod": "MONTH", "orderBy": "VOL", "limit": 50, "offset": offset})
    return out[:n]


def pnl_before(wallet: str, cutoff: int) -> float | None:
    body = get(f"{DATA}/v2/user-pnl", {"user": wallet, "interval": "all"})
    pts = [p for p in (body.get("data") or {}).get("points") or [] if p["timestamp"] <= cutoff]
    return pts[-1]["economic_pnl"] if pts else None


def decisions(wallet: str, start: int, end: int, max_pages: int) -> list[Decision]:
    fills: list[dict[str, Any]] = []
    for page in range(max_pages):
        rows = get(
            f"{DATA}/activity",
            {"user": wallet, "type": "TRADE", "limit": 500, "offset": page * 500, "start": start, "end": end,
             "sortDirection": "ASC"},
        )
        fills += rows
        if len(rows) < 500:
            break
    out: list[Decision] = []
    open_: dict[tuple[str, int], Decision] = {}
    sizes: dict[tuple[str, int], float] = {}
    for f in fills:
        if f.get("side") != "BUY" or not f.get("price"):
            continue
        k = (f["conditionId"], int(f.get("outcomeIndex") or 0))
        d = open_.get(k)
        size, price = float(f["size"]), float(f["price"])
        if d and f["timestamp"] - d.at <= 60:
            total = sizes[k] + size
            d.price = (d.price * sizes[k] + price * size) / total
            d.usd += float(f.get("usdcSize") or price * size)
            sizes[k] = total
            continue
        d = Decision(wallet, k[0], k[1], int(f["timestamp"]), price, float(f.get("usdcSize") or price * size))
        open_[k], sizes[k] = d, size
        out.append(d)
    return out


def markets(conditions: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    ids = sorted(conditions)
    for i in range(0, len(ids), 20):
        chunk = ids[i : i + 20]
        for closed in ("true", "false"):
            for m in get(f"{GAMMA}/markets", {"condition_ids": chunk, "closed": closed, "limit": 50}):
                out[m["conditionId"]] = m
    return out


def payout(m: dict[str, Any], outcome: int) -> float | None:
    """1 or 0 once settled; None if open, voided or split."""
    if not m.get("closed"):
        return None
    prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    if len(prices) <= outcome or prices[outcome] not in (0.0, 1.0):
        return None
    return prices[outcome]


def fee(m: dict[str, Any], p: float) -> float:
    s = m.get("feeSchedule") or {}
    if not m.get("feesEnabled") or not s.get("rate"):
        return 0.0
    return float(s["rate"]) * (p * (1 - p)) ** float(s.get("exponent") or 1)


def price_after(token: str, at: int) -> dict[int, float]:
    body = get(
        f"{DATA}/v2/prices-history",
        {"token_id": token, "start": at - 60, "end": at + max(DELAYS) + 120, "bucketSeconds": 60, "limit": 20},
    )
    pts = sorted((p["timestamp"], float(p["price"])) for p in body.get("data") or [])
    out: dict[int, float] = {}
    for delay in DELAYS:
        later = [p for t, p in pts if t >= at + delay]
        if later:
            out[delay] = later[0]
    return out


def summarize(name: str, rows: list[dict[str, float]]) -> dict[str, Any]:
    def stat(key: str) -> str:
        xs = [r[key] for r in rows if key in r]
        if len(xs) < 2:
            return "n/a"
        m, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(len(xs))
        unit = "¢" if key.startswith("drift") else "%"
        return f"{m * 100:+.1f}{unit} ± {1.96 * se * 100:.1f}{unit} (n={len(xs)})"

    return {
        "group": name,
        "wallets": len({r["wallet"] for r in rows}),
        "decisions": len(rows),
        "trader_return": stat("trader"),
        **{f"copy_{d // 60}m": stat(f"copy_{d}") for d in DELAYS},
        **{f"copy_{d // 60}m_plus_1c": stat(f"copy_{d}_slip") for d in DELAYS},
        **{f"price_moved_by_{d // 60}m_cents": stat(f"drift_{d}") for d in DELAYS},
        "copy_1m_win_rate": f"{statistics.mean([r['copy_60'] > 0 for r in rows if 'copy_60' in r]) * 100:.0f}%"
        if any("copy_60" in r for r in rows)
        else "n/a",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--wallets", type=int, default=100)
    ap.add_argument("--pages", type=int, default=6, help="max 500-fill pages per wallet")
    ap.add_argument("--per-wallet", type=int, default=150, help="max decisions sampled per wallet")
    args = ap.parse_args()

    now = int(time.time())
    cutoff = now - args.days * 86400
    wallets = pool(args.wallets)
    print(f"pool: {len(wallets)} wallets (top by volume this month); window: last {args.days} days", flush=True)

    before: dict[str, float | None] = {}
    for w in wallets:
        before[w["proxyWallet"]] = pnl_before(w["proxyWallet"], cutoff)
    known = sorted((v, k) for k, v in before.items() if v is not None)
    winners = [k for v, k in known if v > 0]
    top = set(winners[-max(1, len(winners) // 3) :])  # the best third of those already in profit
    print(f"profit before window known for {len(known)}; in profit {len(winners)}; 'proven' = top {len(top)}", flush=True)

    ds: list[Decision] = []
    for i, w in enumerate(wallets):
        # One entry per bet: a whale adding to the same game 20 times isn't 20 independent calls.
        firsts: dict[tuple[str, int], Decision] = {}
        for d in decisions(w["proxyWallet"], cutoff, now, args.pages):
            firsts.setdefault((d.condition, d.outcome), d)
        mine = list(firsts.values())
        step = max(1, len(mine) // args.per_wallet)
        ds += mine[::step][: args.per_wallet]
        print(f"  {i + 1}/{len(wallets)} {w.get('userName') or w['proxyWallet'][:10]}: {len(mine)} decisions", flush=True)

    ms = markets({d.condition for d in ds})
    rows: dict[str, list[dict[str, float]]] = defaultdict(list)
    for i, d in enumerate(ds):
        m = ms.get(d.condition)
        if not m:
            continue
        pay = payout(m, d.outcome)
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if pay is None or len(tokens) <= d.outcome or not 0.02 <= d.price <= 0.98:
            continue
        r: dict[str, float] = {"wallet": d.wallet, "trader": (pay - d.price) / d.price}  # type: ignore[dict-item]
        for delay, p in price_after(tokens[d.outcome], d.at).items():
            if not 0 < p < 1:
                continue
            cost = p + fee(m, p)
            r[f"copy_{delay}"] = (pay - cost) / cost
            r[f"copy_{delay}_slip"] = (pay - cost - 0.01) / (cost + 0.01)
            r[f"drift_{delay}"] = p - d.price  # dollars per contract; shown in cents
        prior = before.get(d.wallet)
        group = "proven" if d.wallet in top else ("in_profit_rest" if prior and prior > 0 else "not_proven")
        rows[group].append(r)
        rows["all"].append(r)
        if i % 200 == 0:
            print(f"  priced {i}/{len(ds)}", flush=True)

    out = [summarize(g, rows[g]) for g in ("proven", "in_profit_rest", "not_proven", "all") if rows[g]]
    print(json.dumps(out, indent=2))
    (Path(__file__).parent / "results.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
