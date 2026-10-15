"""Spread bias + correction comparison over rows from spread_study.py. Read-only, local files only.

    PYTHONPATH=<uselayer-sdk main>/python/src uv run --frozen python research/spread-bias/analyze.py research/spread-bias/rows.jsonl.gz
"""

from __future__ import annotations

import gzip
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict

from uselayer import whale_scores as W

REC = {"proven", "quiet", "rising"}
PROD_JSON = __import__("os").path.join(__import__("os").path.dirname(__file__), "recommended-2026-10-15.json")
rows: list[dict] = []
for fn in sys.argv[1:]:
    for line in (gzip.open(fn, "rt") if fn.endswith(".gz") else open(fn)):
        if line.strip():
            r = json.loads(line)
            if "score" in r:
                rows.append(r)

# ---- per bet measures ----
def vals(b: dict) -> dict:
    mv = b.get("moved") or {}
    v0 = mv.get("300")
    mb, m1, m5 = b.get("mid_before"), b.get("mid_1m"), b.get("mid_5m")
    tot = (b.get("maker_size") or 0) + (b.get("taker_size") or 0)
    ref = mb if mb is not None else m1
    mk = None if not tot else (b.get("maker_size") or 0) / tot
    hs = None if ref is None else W.half_spread(ref)
    return {
        "vs": None if v0 is None or hs is None or mk is None else v0 - (2 * mk - 1) * hs,  # spread-neutral: edge vs mid at a touch fill
        "hs": hs,
        "vm": None if v0 is None or hs is None or mk is None else v0 - mk * hs,  # maker-only: the spread a maker earned taken off, takers as now
        "v0": v0,                                                   # production: mid 5–10 min later − fill price
        "v1": None if v0 is None or mb is None else v0 - (mb - b["price"]),  # production move, measured from the mid before
        "v2": None if m5 is None or m1 is None else m5 - m1,        # mid 1 min after → mid 5 min after (no spread, no own impact)
        "off": None if mb is None else mb - b["price"],             # mid before − fill: + bought below mid (maker), − above (taker)
        "maker": None if not tot else (b.get("maker_size") or 0) / tot,
        "event": b.get("event") or b["market"],
    }


def stat(bets: list[dict], key: str) -> W.Stat | None:
    by = defaultdict(list)
    for v in bets:
        if v[key] is not None:
            by[v["event"]].append(v[key])
    return W._stat(by)


def tier(r: dict, bets: list[dict], key: str, rising_z: float = 1.0, sharp_z: float = 2.0, gate: bool = True) -> str:
    """The SDK's tier, with the edge from ``key``. rising_z / sharp_z replace the 1 and 2 cuts; gate=False forces a fail."""
    s = r["score"]
    style = s.get("style") or "takes_a_side"
    usable = [v for v in bets if v[key] is not None]
    edge = stat(usable, key)
    best_two = sorted(usable, key=lambda v: v[key])[-2:]
    trimmed = stat([v for v in usable if not any(v is x for x in best_two)], key)
    n_events = len({v["event"] for v in usable})
    tp, big_win = s.get("trading_pnl"), (s.get("biggest_win") or {}).get("profit") if isinstance(s.get("biggest_win"), dict) else s.get("biggest_win")
    profit_wo_best = None if tp is None or big_win is None else tp - big_win
    steady = trimmed is not None and trimmed.mean > 0 and (profit_wo_best is None or profit_wo_best > 0)
    tw = s.get("trading_pnl_window")
    profitable = None if tw is None else tw > 0
    # thresholds: scale z by moving the cut (z' = z - (cut - default))
    def shift(e, cut, default):
        return None if e is None else W.Stat(mean=e.mean, margin=e.margin, n=e.n, z=e.z - (cut - default))
    e_r = shift(edge, rising_z, 1.0)
    skill, _ = W._verdicts(style=style, edge=e_r, copy=None, steady=steady, n_events=n_events, profitable=profitable)
    if skill == "early" and edge is not None and edge.z >= sharp_z and n_events >= W.QUIET_BETS:
        skill = "yes"
    elif skill == "yes" and edge is not None and edge.z < sharp_z:
        skill = "early"
    if not gate and skill in ("yes", "early"):
        skill = "no"
    ev = W._edge_verdict(style=style, edge=edge, trimmed=trimmed, n_events=n_events)
    big = bool(s.get("on_leaderboard")) or (s.get("volume") or 0) >= W.BIG_VOLUME or (tp or 0) >= 2 * W.BIG_PROFIT
    beats = edge is not None and edge.mean > 0 and edge.z >= sharp_z
    seg = W._segment(style=style, skill=skill, n_events=n_events, big=big, trading_pnl=tp, profit_wo_best=profit_wo_best,
                     beats_and_steady=beats and steady, active=bool(s.get("active", True)) or True, edge_verdict=ev,
                     profitable=profitable)
    return seg


def p_one_sided(e: W.Stat | None) -> float:
    if e is None or e.n < 2:
        return 1.0
    # Student t upper tail, normal approx with df correction (good enough for ranking)
    z = e.z * (1 - 1 / (4 * max(e.n - 1, 1)))
    return 0.5 * math.erfc(z / math.sqrt(2))


def _erfcinv(y: float) -> float:
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if math.erfc(mid) > y:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


data = []
for r in rows:
    bets = [vals(b) for b in r.get("bets") or []]
    full = [v for v in bets if v["v0"] is not None and v["vs"] is not None]
    data.append({"r": r, "bets": bets, "full": full})

VAL = "vs"
by_key = {(d["r"]["wallet"], d["r"]["group"]): d for d in data}
print(f"rows {len(data)}", Counter(d["r"]["group"] for d in data))

# ---- 1. spread bias ----
print("\n=== 1. How much of the measured edge is the spread ===")
for grp in ("rec", "ctrl"):
    ds = [d for d in data if d["r"]["group"].startswith(grp) and "prior" not in d["r"]["group"]]
    allb = [v for d in ds for v in d["bets"] if v["off"] is not None and v["v0"] is not None]
    mk = [v for v in allb if v["maker"] is not None and v["maker"] >= 0.5]
    tk = [v for v in allb if v["maker"] is not None and v["maker"] < 0.5]
    cov = sum(1 for d in ds for v in d["bets"] if v["maker"] is not None) / max(1, sum(len(d["bets"]) for d in ds))
    print(f"{grp}: wallets {len(ds)}, bets with mid-before {len(allb)}, maker/taker known {cov:.0%}")
    for name, xs in (("maker", mk), ("taker", tk)):
        if xs:
            print(f"   {name:5} bets {len(xs):5}  fill vs mid before: mean {100*st.mean(v['off'] for v in xs):+.2f}¢ median {100*st.median(v['off'] for v in xs):+.2f}¢"
                  f"  | prod edge {100*st.mean(v['v0'] for v in xs):+.2f}¢  from mid {100*st.mean(v['v1'] for v in xs if v['v1'] is not None):+.2f}¢"
                  f"  1m→5m {100*st.mean(v['v2'] for v in xs if v['v2'] is not None):+.2f}¢  spread-neutral {100*st.mean(v['vs'] for v in xs if v['vs'] is not None):+.2f}¢  half-spread {100*st.mean(v['hs'] for v in xs if v['hs'] is not None):.2f}¢")

print("\nper recommended trader (current window, SDK main sample): prod tier | edge prod / from-mid / 1m→5m (z) | maker share of checked bets | spread share")
recs = sorted([d for d in data if d["r"]["group"] == "rec"], key=lambda d: d["r"]["score"]["segment"])
for d in recs:
    f = d["full"]
    if len(f) < 2:
        print("  ", d["r"]["wallet"][:10], "too few bets with mids")
        continue
    e0, e1, e2, es = stat(f, "v0"), stat(f, "v1"), stat(f, "v2"), stat(f, "vs")
    mk = [v["maker"] for v in f if v["maker"] is not None]
    t0, t1, t2, ts = tier(d["r"], f, "v0"), tier(d["r"], f, "v1"), tier(d["r"], f, "v2"), tier(d["r"], f, "vs")
    off = st.mean([v["off"] for v in f if v["off"] is not None] or [0])
    fmt = lambda e: "   n/a      " if e is None else f"{100*e.mean:+5.2f}¢ ({e.z:+5.1f})"
    print(f"  {d['r']['wallet'][:10]} {d['r']['score']['segment'][:7]:7} {fmt(e0)} {fmt(es)} | {fmt(e1)} {fmt(e2)}  maker {st.mean(mk) if mk else float('nan'):.2f}"
          f"  offset {100*off:+.2f}¢  tiers {t0[:6]}/{ts[:6]} | {t1[:6]}/{t2[:6]}")


print("\n=== 1b. Production's own edge, less this trader's measured maker spread per bet ===")
prod = {s["wallet"]: s for s in json.load(open(PROD_JSON))}
cnt = Counter()
for d in sorted([d for d in data if d["r"]["group"] == "rec"], key=lambda d: prod[d["r"]["wallet"]]["segment"]):
    s = prod[d["r"]["wallet"]]; e = s["edge"]; f = d["full"]
    mk_sp = st.mean([v["maker"] * v["hs"] for v in f]) if f else 0.0          # what maker fills earned from the spread, per bet
    sym = st.mean([(2 * v["maker"] - 1) * v["hs"] for v in f]) if f else 0.0  # signed: makers +, takers −
    se = e["mean"] / e["z"] if e["z"] else None
    zm = None if not se else (e["mean"] - mk_sp) / se
    zs = None if not se else (e["mean"] - sym) / se
    seg = s["segment"]; n = e["n"]
    def tier_at(z):
        if z is None: return "?"
        if z >= 2 and n >= (40 if seg == "proven" else 25): return "sharp"
        return "rising" if z >= 1 else "out"
    tm, ts = tier_at(zm), tier_at(zs)
    cnt[(seg, "maker-only", tm)] += 1; cnt[(seg, "symmetric", ts)] += 1
    print(f"  {d['r']['wallet'][:10]} {seg:6} prod {100*e['mean']:+6.2f}¢ z {e['z']:+5.2f} n {n:2} | maker spread/bet {100*mk_sp:4.2f}¢ ({mk_sp/e['mean'] if e['mean'] else 0:4.0%} of edge)"
          f" → z {zm:+5.2f} {tm:6} | symmetric → z {zs:+5.2f} {ts}")
print("  summary:", sorted(cnt.items()))

# ---- 2/3. correction rules ----
pop = [d for d in data if "prior" not in d["r"]["group"] and len(d["full"]) >= 2]
rules = {
    "R0 current (prod edge, z≥1/2)": dict(key="v0"),
    "R1 spread-neutral (±half spread by role)": dict(key="vs"),
    "R1m maker-only (−half spread on maker fills)": dict(key="vm"),
    "R1m2 maker-only, Rising z≥1.645": dict(key="vm", rising_z=1.645),
    "R1m3 maker-only, Rising z≥2": dict(key="vm", rising_z=2.0),
    "R1b spread-neutral, Rising z≥1.645": dict(key="vs", rising_z=1.645),
    "R1c spread-neutral, Rising z≥2": dict(key="vs", rising_z=2.0),
    "S1 edge from mid before (sensitivity)": dict(key="v1"),
    "S2 edge 1m→5m (sensitivity)": dict(key="v2"),
    "R3 prod edge, Rising z≥2": dict(key="v0", rising_z=2.0),
    "R4 prod edge, Rising z≥1.645": dict(key="v0", rising_z=1.645),
}


def bh(ps: list[float], q: float) -> float:
    m = len(ps)
    cut = 0.0
    for i, p in enumerate(sorted(ps), 1):
        if p <= q * i / m:
            cut = p
    return cut


def prior_of(d):
    g = d["r"]["group"]
    pg = "rec_prior" if g == "rec" else "ctrl_prior"
    return by_key.get((d["r"]["wallet"], pg))


print("\n=== 2/3. Corrections: who passes now, and how they did on their prior 30 days (spread-free edge from mid) ===")
print("validation = prior-window edge from mid before (v1), wallets with 8+ prior events. 'kept' passers whose prior edge >0;"
      " 'excluded genuine' = passes R0 but not the rule while its prior v1 z ≥ 2.")
for name, kw in list(rules.items()) + [("R7 prod edge + BH q=0.10", dict(key="v0", bh=0.10)), ("R8 spread-neutral + BH q=0.10", dict(key="vs", bh=0.10)), ("R9 maker-only + BH q=0.10", dict(key="vm", bh=0.10))]:
    key = kw["key"]
    cut = None
    if "bh" in kw:
        cut = bh([p_one_sided(stat(d["full"], key)) for d in pop], kw["bh"])
    res = Counter()
    passers = []
    for d in pop:
        k2 = {k: v for k, v in kw.items() if k in ("key", "rising_z", "sharp_z")}
        seg = tier(d["r"], d["full"], **k2)
        if cut is not None and seg in REC and p_one_sided(stat(d["full"], key)) > cut:
            seg = "fdr_out"
        base = tier(d["r"], d["full"], "v0")
        pr = prior_of(d)
        pv = None
        if pr is not None and len(pr["full"]) >= 8:
            pv = stat(pr["full"], VAL)
        g = "rec" if d["r"]["group"] == "rec" else "ctrl"
        res[(g, "pass")] += seg in REC
        res[(g, "sharp")] += seg in ("proven", "quiet")
        if seg in REC and pv is not None:
            res[(g, "val")] += 1
            res[(g, "prior_pos")] += pv.mean > 0
            res[(g, "prior_z2")] += pv.z >= 2
        if base in REC and seg not in REC and pv is not None and pv.z >= 2:
            res[(g, "excl_genuine")] += 1
        if base in REC and seg not in REC:
            res[(g, "excl")] += 1
    extra = f" (BH p≤{cut:.4f}, z≈{math.sqrt(2)*_erfcinv(2*cut) if cut else float('nan'):.2f})" if cut is not None else ""
    print(f"{name}{extra}")
    for g in ("rec", "ctrl"):
        print(f"   {g:4} pass {res[(g,'pass')]:3} (sharp {res[(g,'sharp')]:3})  excluded vs R0 {res[(g,'excl')]:3}"
              f"  | validated {res[(g,'val')]:3}: prior edge>0 {res[(g,'prior_pos')]:3}, prior z≥2 {res[(g,'prior_z2')]:3}"
              f"  | excluded with prior z≥2: {res[(g,'excl_genuine')]}")
