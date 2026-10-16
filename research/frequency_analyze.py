"""The analysis in frequency-preregistered.md (with amendment 1): select on A, measure on B, three outcomes kept
apart. Part 1 (A30 / A90 before 2026-08-14) or Part 2 (A30 before 2026-09-13), from the file given.

    uv run python research/frequency_analyze.py research/frequency-scores.jsonl.gz
    uv run python research/frequency_analyze.py research/frequency-scores-2.jsonl.gz
"""

from __future__ import annotations

import gzip
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from uselayer.whale_scores import RECOMMENDED

opener = gzip.open if sys.argv[1].endswith(".gz") else open
with opener(sys.argv[1], "rt") as fh:
    lines = [json.loads(line) for line in fh if line.strip()]
errors = [r for r in lines if "error" in r]
R = [r for r in lines if "A30" in r and "B" in r]
Row = dict[str, Any]
Pooled = tuple[float, float, int] | None


# ---- outcomes ----


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 0.0
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def two_prop_z(k1: int, n1: int, k2: int, n2: int) -> float:
    if not n1 or not n2:
        return float("nan")
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) or 1e-9
    return (k1 / n1 - k2 / n2) / se


def profit(rows: list[Row]) -> dict[str, Any]:
    """1. Profitability: B trading P&L per wallet."""
    pnl = [r["B"]["trading_pnl_window"] for r in rows if r["B"].get("trading_pnl_window") is not None]
    vol = [r["B"].get("volume_window") or 0 for r in rows if r["B"].get("trading_pnl_window") is not None]
    up = sum(1 for p in pnl if p > 0)
    return {"n": len(pnl), "up": up, "share": up / len(pnl) if pnl else None, "range": wilson(up, len(pnl)),
            "median": st.median(pnl) if pnl else None, "per_dollar": sum(pnl) / sum(vol) if sum(vol) else None,
            "trading": sum(1 for r in rows if (r["B"].get("decisions") or 0) > 0)}  # fmt: skip


def pooled(rows: list[Row], key: str) -> Pooled:
    """A B per-event stat (edge, copy), pooled over wallets: mean, standard error, events."""
    g = [r["B"][key] for r in rows if r["B"].get(key) and r["B"][key]["n"] > 0 and r["B"][key]["margin"] > 0]
    n = sum(s["n"] for s in g)
    if not n:
        return None
    m = sum(s["mean"] * s["n"] for s in g) / n
    se = math.sqrt(sum(((s["margin"] / 1.96) * s["n"]) ** 2 for s in g)) / n
    return m, se, n


def per_event(rows: list[Row], value: Callable[[Row], float | None]) -> Pooled:
    ev: list[float] = []
    for r in rows:
        by: dict[str, list[float]] = defaultdict(list)
        for b in r["B"].get("sample") or []:
            v = value(b)
            if v is not None:
                by[b["event"]].append(v)
        ev += [st.mean(v) for v in by.values()]
    if len(ev) < 3:
        return None
    return st.mean(ev), (st.stdev(ev) / math.sqrt(len(ev))) or 1e-9, len(ev)


def trader_settle(b: Row) -> float | None:
    """2b. The trader's own hold-to-settlement return per contract."""
    return None if b.get("payout") is None else b["payout"] - b["price"]


def copier_settle(b: Row) -> float | None:
    """3b. A copier 1 minute late, holding to settlement, after the taker fee."""
    m1 = (b.get("moved") or {}).get("60")
    if b.get("payout") is None or m1 is None or b.get("fee") is None:
        return None
    return b["payout"] - (b["price"] + m1) - b["fee"]


def fmt(p: Pooled) -> str:
    if p is None:
        return "—"
    m, se, n = p
    return f"{m * 100:+.2f}¢ ±{1.96 * se * 100:.2f} (z {m / se:+.1f}, {n} ev)"


def passes(p: Pooled) -> bool:
    return p is not None and p[0] > 0 and p[0] / p[1] >= 2


# ---- rules, on A only ----


def skill(a: Row) -> bool:
    return a.get("skill") in ("yes", "early")


def own_settle(a: Row) -> Pooled:
    """The trader's own hold-to-settlement return per contract over a window's settled checked bets, per event.
    Needs no short-horizon prices, so it can be judged as far back as Polymarket keeps trades."""
    return per_event([{"B": a}], trader_settle)


def settle_ok(a: Row) -> bool:
    p = own_settle(a)
    return p is not None and p[2] >= 8 and passes(p)


def made_money(a: Row) -> bool:
    return (a.get("trading_pnl_window") or 0) > 0


# Amendment 1: Part 1 selects on what stays available 30 and 90 days back; the SDK's verdicts there are invalid
# (no 5-minute prices past ~60 days) and are printed apart. Part 2 is today's method where its tests can run.
PART = 2 if "A90" not in next(iter(R), {}) else 1
RULES: dict[str, Callable[[Row], bool]] = {"all": lambda r: True}
if PART == 1:
    RULES |= {
        "R30-settle": lambda r: settle_ok(r["A30"]), "R90-settle": lambda r: settle_ok(r["A90"]),
        "R30-profit": lambda r: made_money(r["A30"]), "R90-profit": lambda r: made_money(r["A90"]),
        "R30-both": lambda r: settle_ok(r["A30"]) and made_money(r["A30"]),
        "R90-both": lambda r: settle_ok(r["A90"]) and made_money(r["A90"]),
    }  # fmt: skip
    INVALID = {
        "R30-rec*": lambda r: r["A30"]["segment"] in RECOMMENDED, "R30-skill*": lambda r: skill(r["A30"]),
        "R90-rec*": lambda r: r["A90"]["segment"] in (*RECOMMENDED, "inactive"), "R90-skill*": lambda r: skill(r["A90"]),
    }  # fmt: skip
else:
    RULES |= {
        "R30-rec": lambda r: r["A30"]["segment"] in RECOMMENDED, "R30-skill": lambda r: skill(r["A30"]),
        "R30-settle": lambda r: settle_ok(r["A30"]), "R30-profit": lambda r: made_money(r["A30"]),
    }  # fmt: skip
    INVALID = {}
WINDOWS = ("A30", "A90") if PART == 1 else ("A30",)


def corr(pairs: list[tuple[float, float]]) -> float:
    if len(pairs) < 3:
        return float("nan")
    xs, ys = zip(*pairs)
    mx, my, sx, sy = st.mean(xs), st.mean(ys), st.pstdev(xs), st.pstdev(ys)
    return sum((x - mx) * (y - my) for x, y in pairs) / len(pairs) / (sx * sy) if sx and sy else float("nan")


def _ranks(xs: list[float]) -> list[float]:
    """Ranks with ties sharing their average rank (many wallets have exactly $0 in a window)."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2
        i = j + 1
    return out


def rank_corr(pairs: list[tuple[float, float]]) -> float:
    """Spearman's rank correlation, ties averaged."""
    if len(pairs) < 3:
        return float("nan")
    return corr(list(zip(_ranks([a for a, _ in pairs]), _ranks([b for _, b in pairs]))))


def priced(rows: list[Row], win: str) -> str:
    bets = [b for r in rows for b in r[win].get("sample") or []]
    return f"{sum(1 for b in bets if '300' in (b.get('moved') or {})) / len(bets):.0%}" if bets else "—"


def report(name: str, S: list[Row], base: dict[str, Any]) -> None:
    if not S:
        print(f"  {name:11} picks   0")
        return
    p = profit(S)
    z = two_prop_z(p["up"], p["n"], base["up"], base["n"]) if name != "all" else float("nan")
    prof_ok = name != "all" and z >= 2 and (p["median"] or 0) > 0
    edge, tset = pooled(S, "edge"), per_event(S, trader_settle)
    copy, cset = pooled(S, "copy"), per_event(S, copier_settle)
    lo, hi = p["range"]
    print(f"  {name:11} picks {len(S):3} | 1 PROFIT {p['share'] or 0:4.0%} up [{lo:.0%}–{hi:.0%}] vs all z {z:+.1f}, "
          f"median ${p['median'] or 0:,.0f}, {(p['per_dollar'] or 0) * 100:+.2f}¢/$, still trading {p['trading']}/{len(S)}"
          f"{'  PASS' if prof_ok else ''}")  # fmt: skip
    print(f"  {'':11}           | 2 SKILL  edge {fmt(edge)}{'  PASS' if passes(edge) else ''} | own settle {fmt(tset)}")
    print(f"  {'':11}           | 3 COPY   copy {fmt(copy)}{'  PASS' if passes(copy) else ''} | copier settle {fmt(cset)}")


print(f"Part {PART}: wallets scored in every window: {len(R)}  (errors: {len(errors)})")
for grp in ("frequent", "selective", "sparse"):
    G = [r for r in R if r["group"] == grp]
    if not G:
        continue
    base = profit(G)
    print(f"\n=== {grp}: {len(G)} wallets")
    for win in (*WINDOWS, "B"):
        print(f"  {win:3} segments: {dict(Counter(r[win]['segment'] for r in G))}  (bets with a 5-min price: {priced(G, win)})")
    for name, rule in RULES.items():
        report(name, [r for r in G if rule(r)], base)
    if INVALID:
        print("  (* SDK verdicts on windows past Polymarket's 5-minute price history: not valid, shown for the record)")
        for name, rule in INVALID.items():
            print(f"  {name:11} picks {sum(1 for r in G if rule(r)):3}")
    for win in WINDOWS:
        st_pairs = [(own_settle(r[win])[0], own_settle(r["B"])[0]) for r in G  # type: ignore[index]
                    if own_settle(r[win]) and own_settle(r["B"]) and own_settle(r[win])[2] >= 8 and own_settle(r["B"])[2] >= 8]  # type: ignore[index]  # fmt: skip
        print(f"  persistence {win}→B own settle return across wallets (8+ events both): {corr(st_pairs):+.2f}  n={len(st_pairs)}")
        pp = [(r[win]["trading_pnl_window"], r["B"]["trading_pnl_window"]) for r in G
              if r[win].get("trading_pnl_window") is not None and r["B"].get("trading_pnl_window") is not None]  # fmt: skip
        print(f"  persistence {win}→B trading P&L, rank correlation: {rank_corr(pp):+.2f}  n={len(pp)}")
        if PART == 2:
            pairs = [(r[win]["edge"]["mean"], r["B"]["edge"]["mean"]) for r in G
                     if r[win].get("edge") and r["B"].get("edge") and r[win]["bets"] >= 8 and r["B"]["bets"] >= 8]  # fmt: skip
            print(f"  persistence {win}→B 5-min edge across wallets (8+ bets both): {corr(pairs):+.2f}  n={len(pairs)}")

if PART == 1:
    print("\nSelective traders picked by R90-both, A90 then B:")
    for r in R:
        if r["group"] != "selective" or not RULES["R90-both"](r):
            continue
        a, b = r["A90"], r["B"]
        sa, sb = own_settle(a), own_settle(b)
        print(f"  {str(a.get('name'))[:20]:20} A90 settle {fmt(sa)} P&L ${a.get('trading_pnl_window') or 0:>10,.0f}"
              f"  →  B settle {fmt(sb)} P&L ${b.get('trading_pnl_window') or 0:>10,.0f} edge {fmt(pooled([r], 'edge'))}")  # fmt: skip

# Exploratory, not pre-registered: how much money is at stake. Most wallets in this pool trade small, so "in profit"
# can mean a few cents; this counts wallets whose window-B trading P&L was at least $100 either way.
print("\nExploratory (not pre-registered): B trading P&L of at least $100, by band and rule")
for grp in ("frequent", "selective", "sparse"):
    G = [r for r in R if r["group"] == grp]
    for name, rule in RULES.items():
        S = [r["B"].get("trading_pnl_window") or 0 for r in G if rule(r)]
        if S:
            print(f"  {grp:9} {name:11} picks {len(S):3} | made $100+: {sum(p >= 100 for p in S):3}  lost $100+: "
                  f"{sum(p <= -100 for p in S):3} | total ${sum(S):>11,.0f}")
