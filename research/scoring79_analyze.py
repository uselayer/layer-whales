"""The analysis in scoring79-preregistered.md: rules judged on window A from the kept bets, outcomes on B.

    uv run python research/scoring79_analyze.py research/scoring79-scores.jsonl.gz research/overlooked-scores.jsonl.gz [more.jsonl ...]

Each file is analysed on its own (pools are never mixed), then the primary pool's parlay section.
"""

from __future__ import annotations

import gzip
import json
import math
import random
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

BOOT = 2000
SKILL, MIN_N, HALF_N, BIG_N = "300", 25, 8, 40


def load(path: str) -> list[dict[str, Any]]:
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as fh:
        rows = [json.loads(x) for x in fh if x.strip()]
    for r in rows:  # frequency part 2 names its selection window A30
        if "A30" in r and "A" not in r:
            r["A"] = r["A30"]
    return [r for r in rows if "A" in r and "B" in r and "error" not in r]


def ev_means(sample: list[dict[str, Any]], key: Any) -> list[float]:
    """Per independent event, the mean of key(bet), as the SDK weighs bets."""
    by: dict[str, list[float]] = defaultdict(list)
    for b in sample:
        v = key(b)
        if v is not None:
            by[b["event"]].append(v)
    return [st.mean(v) for v in by.values()]


def stat(xs: list[float]) -> tuple[float, float, int] | None:
    if len(xs) < 2:
        return None
    m, sd = st.mean(xs), st.stdev(xs)
    se = sd / math.sqrt(len(xs))
    return m, (m / se if se else 0.0), len(xs)


def edge_of(b: dict[str, Any]) -> float | None:
    return b["moved"].get(SKILL)


def copy_of(b: dict[str, Any]) -> float | None:
    return b.get("copy")


def profit(s: dict[str, Any]) -> float | None:
    v = s.get("trading_pnl_window")
    return s.get("pnl_window") if v is None and "trading_pnl_window" not in s else v


def takes_side(s: dict[str, Any]) -> bool:
    return (s.get("style") or ("takes_a_side" if s.get("segment") != "no_view" else "x")) == "takes_a_side"


def features(r: dict[str, Any]) -> dict[str, Any]:
    a = r["A"]
    smp = a.get("sample") or []
    e = stat(ev_means(smp, edge_of))
    with_e = [b for b in smp if edge_of(b) is not None]
    best2 = sorted(with_e, key=edge_of)[-2:]  # type: ignore[arg-type]
    trimmed = stat(ev_means([b for b in smp if b not in best2], edge_of))
    lo, hi = (datetime.fromisoformat(x) for x in a["window"])
    mid = lo + (hi - lo) / 2
    first = [b for b in smp if datetime.fromisoformat(b["at"]) < mid]
    h1, h2 = stat(ev_means(first, edge_of)), stat(ev_means([b for b in smp if b not in first], edge_of))
    p = profit(a)
    p90 = ((a.get("periods") or {}).get("90d") or {}).get("trading_pnl")
    edge_ok = bool(takes_side(a) and e and e[2] >= MIN_N and e[0] > 0 and e[1] >= 2 and trimmed and trimmed[0] > 0)
    return {
        "edge": e, "n": e[2] if e else 0, "edge_ok": edge_ok, "profit": p, "profit90": p90,
        "h1": h1, "h2": h2,
        "halves_ok": bool(h1 and h2 and h1[2] >= HALF_N and h2[2] >= HALF_N and h1[0] > 0 and h2[0] > 0),
    }  # fmt: skip


def wilson(k: int, n: int) -> str:
    if not n:
        return "—"
    p, z = k / n, 1.96
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return f"{k}/{n} = {p:.0%} ({max(0, c - h):.0%}–{min(1, c + h):.0%})"


def pooled_boot(rows: list[dict[str, Any]], key: Any, win: str = "B") -> str:
    per = [ev_means(r[win].get("sample") or [], key) for r in rows]
    per = [x for x in per if x]
    if not per:
        return "— (no priced events)"
    allx = [v for x in per for v in x]
    m = st.mean(allx)
    rnd = random.Random(1)
    boots = []
    for _ in range(BOOT):
        pick = [per[rnd.randrange(len(per))] for _ in per]
        flat = [v for x in pick for v in x]
        boots.append(st.mean(flat))
    boots.sort()
    lo, hi = boots[int(0.025 * BOOT)], boots[int(0.975 * BOOT) - 1]
    flag = "PASS" if lo > 0 else "fail"
    return f"{m * 100:+.2f}¢ ({lo * 100:+.2f}¢ to {hi * 100:+.2f}¢) {len(allx)} events, {len(per)} wallets [{flag}]"


def share_pos(rows: list[dict[str, Any]], key: Any) -> str:
    vals = [stat(ev_means(r["B"].get("sample") or [], key)) for r in rows]
    vals = [v for v in vals if v]
    return wilson(sum(1 for v in vals if v[0] > 0), len(vals))


def profit_block(rows: list[dict[str, Any]]) -> str:
    ps = [(profit(r["B"]), r["B"].get("volume_window")) for r in rows]
    known = [p for p, _ in ps if p is not None]
    if not known:
        return "—"
    vol = sum(v for p, v in ps if p is not None and v)
    rpd = f", return per $ traded {sum(p for p, v in ps if p is not None and v) / vol:+.2%}" if vol else ""
    return f"B profit > 0: {wilson(sum(1 for p in known if p > 0), len(known))}, median ${st.median(known):,.0f}{rpd}"


def report(name: str, rows: list[dict[str, Any]], label: str) -> None:
    tag = "  (too few to judge)" if len(rows) < 10 else ""
    print(f"\n  {label}: {len(rows)} wallets{tag}")
    if not rows:
        return
    print(f"    1 profitability  {profit_block(rows)}")
    print(f"    2 skill (B edge) {pooled_boot(rows, edge_of)}   wallets with B edge > 0: {share_pos(rows, edge_of)}")
    print(f"    3 copy (B)       {pooled_boot(rows, copy_of)}")


def analyse(path: str) -> list[dict[str, Any]]:
    R = load(path)
    for r in R:
        r["f"] = features(r)
    print(f"\n{'=' * 100}\n{path}: {len(R)} wallets scored in both windows")
    active = [r for r in R if r["f"]["n"] >= 8]
    print(f"  with 8+ priced events in A: {len(active)};  with {MIN_N}+: {sum(1 for r in R if r['f']['n'] >= MIN_N)}")
    base = [r for r in R if takes_side(r["A"]) and r["f"]["n"] >= MIN_N]

    print("\n-- Fix 7: the 30-day profit gate --")
    E = [r for r in R if r["f"]["edge_ok"]]
    Ep = [r for r in E if (r["f"]["profit"] or 0) > 0]
    Em = [r for r in E if r["f"]["profit"] is not None and r["f"]["profit"] <= 0]
    Em90 = [r for r in Em if (r["f"]["profit90"] or 0) > 0]
    report(path, base, f"baseline: takes a side, {MIN_N}+ events in A")
    report(path, Ep, "E+  edge z>=2 and profitable in A (today's skill yes)")
    report(path, Em, "E−  edge z>=2 but lost in A (what fix 7 adds)")
    report(path, Em90, "E−90  E− with 90-day trading profit > 0 at T")
    if Em:
        print("    E− wallets:")
        for r in Em:
            f, b = r["f"], stat(ev_means(r["B"].get("sample") or [], edge_of))
            bs = f"{b[0] * 100:+.2f}¢ z{b[1]:+.1f} n{b[2]}" if b else "—"
            print(f"      {str(r['A'].get('name'))[:22]:22} A edge {f['edge'][0] * 100:+.2f}¢ z{f['edge'][1]:+.1f} n{f['n']}  A profit ${f['profit']:,.0f}"
                  f"  90d {('$' + format(f['profit90'], ',.0f')) if f['profit90'] is not None else '—'}  →  B edge {bs}  B profit {profit(r['B'])}")  # fmt: skip

    print("\n-- Fix 8: one window or repeated --")
    S1 = Ep
    S2 = [r for r in S1 if r["f"]["halves_ok"]]
    S3 = [r for r in S1 if r["f"]["edge"][1] >= 3]
    report(path, S1, "S1  today's single-window standard (= E+)")
    report(path, S2, "S2  S1 and edge > 0 in both halves of A (8+ events each)")
    report(path, [r for r in S1 if r not in S2], "S1 \\ S2  dropped by requiring both halves")
    report(path, S3, "S3  S1 and edge z >= 3")
    report(path, [r for r in S1 if r not in S3], "S1 \\ S3  dropped by z >= 3")
    big = [r for r in S1 if r["f"]["n"] >= BIG_N]
    print(f"    S1 with {BIG_N}+ events (Sharp on a big account): {len(big)};  S2 of those: {sum(1 for r in big if r in S2)}")

    print("\n-- Calibration and persistence --")
    null = [r for r in R if r["f"]["edge"] and r["f"]["n"] >= 8 and abs(r["f"]["edge"][1]) < 1]
    bz = [stat(ev_means(r["B"].get("sample") or [], edge_of)) for r in null]
    bz = [b for b in bz if b and b[2] >= 8]
    print(f"  A |z|<1 (8+ events): B z>=2 in {wilson(sum(1 for b in bz if b[1] >= 2), len(bz))}  (nominal 2.3%)")
    aa = [r for r in R if r["f"]["edge"] and r["f"]["n"] >= 8]
    bz2 = [stat(ev_means(r["B"].get("sample") or [], edge_of)) for r in aa]
    bz2 = [b for b in bz2 if b and b[2] >= 8]
    print(f"  any wallet (8+ events both): B z>=2 in {wilson(sum(1 for b in bz2 if b[1] >= 2), len(bz2))};  A z>=2 in "
          f"{wilson(sum(1 for r in aa if r['f']['edge'][1] >= 2), len(aa))}")  # fmt: skip
    pairs = []
    for r in R:
        b = stat(ev_means(r["B"].get("sample") or [], edge_of))
        if r["f"]["edge"] and b and r["f"]["n"] >= 15 and b[2] >= 15:
            pairs.append((r["f"]["edge"][0], b[0], r["f"]["edge"][1], b[1]))
    if len(pairs) > 3:
        print(f"  A→B correlation, edge mean: {corr([p[0] for p in pairs], [p[1] for p in pairs]):+.2f}; z: "
              f"{corr([p[2] for p in pairs], [p[3] for p in pairs]):+.2f}  (n={len(pairs)}, 15+ events both)")  # fmt: skip
        for lo, hi in ((-99, 0), (0, 1), (1, 2), (2, 3), (3, 99)):
            g = [p for p in pairs if lo <= p[2] < hi]
            if g:
                print(f"    A z in [{lo:>3},{hi:>3}): {len(g):3} wallets, mean B z {st.mean(p[3] for p in g):+.2f}, B edge > 0 in "
                      f"{sum(1 for p in g if p[1] > 0)}/{len(g)}")  # fmt: skip

    print("\n-- Coverage on the latest window (B) of this pool --")
    seg = Counter(r["B"].get("segment") for r in R)
    print(f"  segments B: {dict(seg)}")
    return R


def corr(xs: list[float], ys: list[float]) -> float:
    mx, my, sx, sy = st.mean(xs), st.mean(ys), st.pstdev(xs), st.pstdev(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs) / (sx * sy) if sx and sy else float("nan")


def parlays(R: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 100}\n-- Fix 9: parlays (primary pool) --")
    combo_any = [r for r in R if (r["A"]["coverage"].get("combos") or 0) + (r["B"]["coverage"].get("combos") or 0) > 0]
    lost = [r for r in R if r["A"]["coverage"].get("combos") and not r["A"]["coverage"].get("bets")]
    print(f"  wallets with a parlay in either sample: {len(combo_any)} of {len(R)};  A sample all parlays: {len(lost)}")
    for r in combo_any:
        if "A_P" not in r:
            continue
        cc = r["A_P"].get("combo_counts") or {}
        share = cc["combos"] / cc["decisions"] if cc.get("decisions") else None
        a, ap, b, bp = r["A"], r["A_P"], r["B"], r["B_P"]
        ne = lambda s: (s.get("edge") or {}).get("n", 0)  # noqa: E731
        bstat = stat(ev_means(bp.get("sample") or [], edge_of))
        print(f"    {str(a.get('name'))[:20]:20} parlay share A {share if share is None else format(share, '.0%'):>4}  A events {ne(a):2}→{ne(ap):2} "
              f"skill {a.get('skill')}→{ap.get('skill')}  B events {ne(b):2}→{ne(bp):2} skill {b.get('skill')}→{bp.get('skill')}  "
              f"B_P edge {('%+.2f¢ z%+.1f' % (bstat[0] * 100, bstat[1])) if bstat else '—'}")  # fmt: skip
    newly = [r for r in combo_any if "A_P" in r and (r["A"].get("edge") or {}).get("n", 0) < MIN_N <= (r["A_P"].get("edge") or {}).get("n", 0)]
    print(f"  newly measurable under P (A events < {MIN_N} → >= {MIN_N}): {len(newly)}")
    if newly:
        print(f"    their B_P edge: {pooled_boot(newly, edge_of, 'B_P')}")
    print("  mostly-parlay wallets, settled parlay record (unredeemed = lost, so a lower bound):")
    for r in R:
        if "parlays_A" not in r:
            continue
        line = []
        for k in ("parlays_A", "parlays_B"):
            ps = r[k]
            if not ps:
                line.append(f"{k[-1]}: none")
                continue
            spent, got = sum(p["usd"] for p in ps), sum(p["paid"] for p in ps)
            days: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
            for p in ps:
                days[p["day"]][0] += p["usd"]
                days[p["day"]][1] += p["paid"]
            dr = [g / s - 1 for s, g in days.values() if s > 0]
            s = stat(dr)
            line.append(f"{k[-1]}: {len(ps)} parlays ${spent:,.0f} → {got / spent - 1:+.1%}"
                        + (f" (by day {s[0]:+.1%}, z {s[1]:+.1f}, {s[2]} days)" if s else ""))  # fmt: skip
        print(f"    {str(r['A'].get('name'))[:20]:20} " + " | ".join(line) + f"  B profit {profit(r['B'])}")


if __name__ == "__main__":
    first = None
    for p in sys.argv[1:]:
        R = analyse(p)
        first = first or R
    if first:
        parlays(first)
