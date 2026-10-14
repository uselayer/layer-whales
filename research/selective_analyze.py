"""The analysis in selective-preregistered.md: long-record rules judged on S, outcomes on O, per band.

    uv run python research/selective_analyze.py research/selective-bets.jsonl[.gz]
"""

from __future__ import annotations

import gzip
import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

T = int(datetime(2026, 7, 14, tzinfo=UTC).timestamp())
DAY = 86400
BOOT = 2000

op = gzip.open if sys.argv[1].endswith(".gz") else open
with op(sys.argv[1], "rt") as fh:
    rows = [json.loads(x) for x in fh if x.strip()]
errors = [r for r in rows if "error" in r]
R = [r for r in rows if "error" not in r]


# ---- per-bet values ----
def settled(b: dict[str, Any]) -> float | None:
    return None if b["payout"] is None else b["payout"] - b["price"]


def move12(b: dict[str, Any]) -> float | None:
    return None if b["p12"] is None else b["p12"] - b["price"]


def copy(b: dict[str, Any]) -> float | None:
    if b["payout"] is None or b["next"] is None or b["fee"] is None:
        return None
    return b["payout"] - b["next"] - b["fee"]


def events(bets: list[dict[str, Any]], f: Any) -> list[tuple[float, float]]:
    """Per event: (mean value, mean p(1-p)) over its bets with a value."""
    by: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for b in bets:
        v = f(b)
        if v is not None:
            by[b["event"]].append((v, b["price"] * (1 - b["price"])))
    return [(st.mean(x for x, _ in v), st.mean(y for _, y in v)) for v in by.values()]


def trimmed_ok(bets: list[dict[str, Any]], f: Any) -> bool:
    best2 = sorted((b for b in bets if f(b) is not None), key=f)[-2:]
    ev = events([b for b in bets if b not in best2], f)
    return bool(ev) and st.mean(x for x, _ in ev) > 0


def z_settle(ev: list[tuple[float, float]]) -> float | None:
    v = sum(y for _, y in ev)
    return sum(x for x, _ in ev) / math.sqrt(v) if v > 0 else None


def z_t(ev: list[tuple[float, float]]) -> float | None:
    xs = [x for x, _ in ev]
    if len(xs) < 3:
        return None
    sd = st.stdev(xs)
    return st.mean(xs) / (sd / math.sqrt(len(xs))) if sd else None


def p_one_sided(z: float | None) -> float:
    return 1.0 if z is None else 0.5 * math.erfc(z / math.sqrt(2))


def bh(ps: dict[str, float], q: float = 0.10) -> set[str]:
    """Benjamini–Hochberg: the wallets significant at false discovery rate q."""
    items = sorted(ps.items(), key=lambda kv: kv[1])
    m, keep = len(items), 0
    for i, (_, p) in enumerate(items, 1):
        if p <= q * i / m:
            keep = i
    return {w for w, _ in items[:keep]}


for r in R:
    S = [b for b in r["bets"] if b["w"] == "S"]
    r["S"], r["O"] = S, [b for b in r["bets"] if b["w"] == "O"]
    es, em = events(S, settled), events(S, move12)
    r["zs"], r["ns"], r["zm"], r["nm"] = z_settle(es), len(es), z_t(em), len(em)
    r["ok_s"] = bool(r["zs"] is not None and r["zs"] >= 2 and r["ns"] >= 20 and trimmed_ok(S, settled))
    r["ok_m"] = bool(r["zm"] is not None and r["zm"] >= 2 and r["nm"] >= 20 and trimmed_ok(S, move12))


# ---- outcomes ----
def wilson(k: int, n: int) -> str:
    if not n:
        return "—"
    p, z = k / n, 1.96
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return f"{k}/{n} = {p:.0%} ({max(0, c - h):.0%}–{min(1, c + h):.0%})"


def pooled(group: list[dict[str, Any]], f: Any) -> tuple[float, float, float, int, int] | None:
    per = [[x for x, _ in events(r["O"], f)] for r in group]
    per = [x for x in per if x]
    if not per:
        return None
    flat = [v for x in per for v in x]
    rnd = random.Random(1)
    boots = sorted(st.mean([v for x in (per[rnd.randrange(len(per))] for _ in per) for v in x]) for _ in range(BOOT))
    return st.mean(flat), boots[int(0.025 * BOOT)], boots[int(0.975 * BOOT) - 1], len(flat), len(per)


def fmt(p: tuple[float, float, float, int, int] | None) -> str:
    if p is None:
        return "— (no O events)"
    m, lo, hi, n, k = p
    return f"{m * 100:+.2f}¢ ({lo * 100:+.2f}¢ to {hi * 100:+.2f}¢) {n} events, {k} wallets [{'PASS' if lo > 0 else 'fail'}]"


def report(label: str, group: list[dict[str, Any]], base: tuple[float, float, float, int, int] | None) -> None:
    tag = "  (too few to judge)" if len(group) < 10 else ""
    print(f"\n  {label}: {len(group)} wallets{tag}")
    if not group:
        return
    pro = [r["profit"]["O"] for r in group if r["profit"]["O"] is not None]
    active = sum(1 for r in group if r["O"])
    print(f"    1 profitability  O profit > 0: {wilson(sum(1 for p in pro if p > 0), len(pro))}"
          + (f", median ${st.median(pro):,.0f}" if pro else "") + f"; still trading in O: {wilson(active, len(group))}")  # fmt: skip
    s = pooled(group, settled)
    above = "" if s is None or base is None else f"  (baseline {base[0] * 100:+.2f}¢: {'above' if s[0] > base[0] else 'NOT above'})"
    print(f"    2 skill, settled {fmt(s)}{above}")
    print(f"      12-hour move   {fmt(pooled(group, move12))}")
    pos = [st.mean(x for x, _ in e) for r in group if (e := events(r["O"], settled))]
    print(f"      wallets with a positive settled edge in O: {wilson(sum(1 for x in pos if x > 0), len(pos))}")
    print(f"    3 copy (next price, to settlement, after fee) {fmt(pooled(group, copy))}")


print(f"wallets collected: {len(rows)} (errors {len(errors)})")
for band in ("selective", "frequent"):
    G = [r for r in R if r["band"] == band]
    print(f"\n{'=' * 100}\n{band}: {len(G)} wallets")
    fdr_s = bh({r["wallet"]: p_one_sided(r["zs"]) for r in G if r["ns"] >= 20})
    fdr_m = bh({r["wallet"]: p_one_sided(r["zm"]) for r in G if r["nm"] >= 20})
    base = pooled(G, settled)
    rules = {
        "all (baseline)": G,
        "P-profit: trading profit over S > 0": [r for r in G if (r["profit"]["S"] or 0) > 0],
        "L-settle: settled z>=2, 20+ events, trimmed > 0": [r for r in G if r["ok_s"]],
        "L-move: 12-hour z>=2, 20+ events, trimmed > 0": [r for r in G if r["ok_m"]],
        "L-both": [r for r in G if r["ok_s"] and r["ok_m"]],
        "L-settle-FDR (BH q=0.10)": [r for r in G if r["ok_s"] and r["wallet"] in fdr_s],
        "L-move-FDR (BH q=0.10)": [r for r in G if r["ok_m"] and r["wallet"] in fdr_m],
    }
    for label, group in rules.items():
        report(label, group, None if label.startswith("all") else base)

    print("\n  -- calibration --")
    for name, zk, nk in (("settled", "zs", "ns"), ("12-hour", "zm", "nm")):
        zs = [r[zk] for r in G if r[zk] is not None and r[nk] >= 20]
        if zs:
            print(f"  {name} z over wallets with 20+ events: n={len(zs)}, mean {st.mean(zs):+.2f}, sd {st.pstdev(zs):.2f}, "
                  f"z>=2 in {wilson(sum(1 for z in zs if z >= 2), len(zs))} (nominal 2.3%)")  # fmt: skip
    print(f"  FDR kept: settled {len(fdr_s)} of {sum(1 for r in G if r['ok_s'])} passes; 12-hour {len(fdr_m)} of {sum(1 for r in G if r['ok_m'])}")

    print("\n  -- 10 more bets before confirming --")
    for label in ("L-settle: settled z>=2, 20+ events, trimmed > 0", "L-move: 12-hour z>=2, 20+ events, trimmed > 0", "all (baseline)"):
        group = rules[label]
        days = []
        for r in group:
            ts = sorted({b["event"]: b["at"] for b in sorted(r["O"], key=lambda b: -b["at"])}.values())
            days.append((ts[9] - T) / DAY if len(ts) >= 10 else None)
        if group:
            reached = [d for d in days if d is not None]
            print(f"  {label[:9]:9} {len(group):3} wallets: 10th O event within 30 d {wilson(sum(1 for d in reached if d <= 30), len(group))}, "
                  f"60 d {wilson(sum(1 for d in reached if d <= 60), len(group))}, 91 d {wilson(len(reached), len(group))}")  # fmt: skip
    agree = []
    for r in rules["L-settle: settled z>=2, 20+ events, trimmed > 0"] + rules["L-move: 12-hour z>=2, 20+ events, trimmed > 0"]:
        ev = sorted(r["O"], key=lambda b: b["at"])
        first = [settled(b) for b in ev[:10] if settled(b) is not None]
        rest = [settled(b) for b in ev[10:] if settled(b) is not None]
        if len(first) >= 5 and len(rest) >= 5:
            agree.append((st.mean(first) > 0) == (st.mean(rest) > 0))
    print(f"  exploratory: first-10 sign agrees with the rest of O: {wilson(sum(agree), len(agree))}")
