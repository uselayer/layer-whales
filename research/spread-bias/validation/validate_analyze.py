"""Existing vs corrected classification (the maker-spread branch), checked against independent history.

Classification: the branch's scores (validate_run.py). "Existing" = the same bets, same rules, edge from the
fill price (raw ``moved``); "corrected" = the branch's segment (edge less ``spread_earned``).
References, from the OTHER 30-day window (spread_study.py rows, 1-minute mids), neither using the correction:
  midmid  = mid 5 min later − mid just before the fill (spread-free by construction)
  settle  = payout − mid just before the fill, settled bets only (forecast to resolution, spread-free)

    PYTHONPATH=<uselayer-sdk maker-spread-skill>/python/src uv run --frozen python \
        research/spread-bias/validation/validate_analyze.py research/spread-bias/validation
"""
from __future__ import annotations

import json, math, statistics as st, sys
from collections import Counter, defaultdict
from pathlib import Path

from uselayer import whale_scores as W

D = Path(sys.argv[1])
REC = {"proven", "quiet", "rising"}


def load(fn):
    """A run's rows: from ``fn`` in D, else from the compressed copies kept in research/spread-bias."""
    p = D / fn
    if p.exists():
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    import gzip
    here = Path(__file__).parent
    kept = {"v_rec.jsonl": ("validation/branch-scores.jsonl.gz", "rec"), "v_rec_prior.jsonl": ("validation/branch-scores.jsonl.gz", "rec_prior"),
            "v_ctrl.jsonl": ("validation/branch-scores.jsonl.gz", "ctrl"), "v_ctrl_prior.jsonl": ("validation/branch-scores.jsonl.gz", "ctrl_prior"),
            "rows.jsonl": ("rows.jsonl.gz", None), "rows_prior.jsonl": None}
    if kept.get(fn) is None:
        return []
    src, group = kept[fn]
    rows = [json.loads(l) for l in gzip.open(here.parent / src if src.startswith("rows") else here / src.split("/", 1)[1], "rt") if l.strip()]
    return [r for r in rows if group is None or r.get("group") == group]


branch = {(r["wallet"], r["group"]): r for fn in ("v_rec.jsonl", "v_rec_prior.jsonl", "v_ctrl.jsonl", "v_ctrl_prior.jsonl")
          for r in load(fn) if "score" in r}
study = {}
for fn in ("rows.jsonl", "rows_prior.jsonl"):
    for r in load(fn):
        if "score" not in r:
            continue
        g = r["group"]
        g = "rec" if g == "rec" else "rec_prior" if g == "rec_prior" else "ctrl_prior" if g == "ctrl_prior" else "ctrl"
        study[(r["wallet"], g)] = r


def stat(xs_by_event):
    return W._stat(xs_by_event)


def existing_tier(s: dict) -> str:
    """The same rules on the same bets, edge from the fill price: what the score was before the correction."""
    bets = [b for b in s["sample"] if "300" in (b.get("moved") or {})]
    style = s.get("style") or "takes_a_side"
    by = defaultdict(list)
    for b in bets:
        by[b["event"]].append(b["moved"]["300"])
    edge = stat(by)
    best_two = sorted(bets, key=lambda b: b["moved"]["300"])[-2:]
    tb = defaultdict(list)
    for b in bets:
        if not any(b is x for x in best_two):
            tb[b["event"]].append(b["moved"]["300"])
    trimmed = stat(tb)
    n_events = len({b["event"] for b in bets})
    tp, big = s.get("trading_pnl"), s.get("biggest_win")
    pwb = None if tp is None or big is None else tp - big
    steady = trimmed is not None and trimmed.mean > 0 and (pwb is None or pwb > 0)
    tw = s.get("trading_pnl_window")
    profitable = None if tw is None else tw > 0
    skill, _ = W._verdicts(style=style, edge=edge, copy=None, steady=steady, n_events=n_events, profitable=profitable)
    ev = W._edge_verdict(style=style, edge=edge, trimmed=trimmed, n_events=n_events)
    bigacct = bool(s.get("on_leaderboard")) or (s.get("volume") or 0) >= W.BIG_VOLUME or (tp or 0) >= 2 * W.BIG_PROFIT
    beats = edge is not None and edge.mean > 0 and edge.z >= 2
    return W._segment(style=style, skill=skill, n_events=n_events, big=bigacct, trading_pnl=tp, profit_wo_best=pwb,
                      beats_and_steady=beats and steady, active=bool(s.get("active")), edge_verdict=ev,
                      profitable=profitable, parlays=W._mostly_parlays(s.get("coverage", {}).get("combos", 0), s.get("decisions") or 0))


def reference(r: dict) -> dict:
    """The other window's spread-free figures for one wallet."""
    pay = {b["market"]: b.get("payout") for b in r["score"].get("sample") or []}
    mm, se = defaultdict(list), defaultdict(list)
    for b in r.get("bets") or []:
        ev = b.get("event") or b["market"]
        if b.get("mid_before") is not None and b.get("mid_5m") is not None:
            mm[ev].append(b["mid_5m"] - b["mid_before"])
        p = pay.get(b["market"])
        if p is not None and b.get("mid_before") is not None:
            se[ev].append(p - b["mid_before"])
    return {"midmid": stat(mm), "settle": stat(se), "n_settle": sum(len(v) for v in se.values())}


OTHER = {"rec": "rec_prior", "rec_prior": "rec", "ctrl": "ctrl_prior", "ctrl_prior": "ctrl"}
rows = []
agree = Counter()
for (w, g), r in branch.items():
    s = r["score"]
    old, new = existing_tier(s), s["segment"]
    # sanity: the existing-tier rebuild matches the branch when nothing came off
    if not s.get("spread_earned"):
        agree[old == new] += 1
    ref_row = study.get((w, OTHER[g]))
    ref = reference(ref_row) if ref_row else None
    rows.append(dict(w=w, g=g, old=old, new=new, s=s, ref=ref))
print(f"branch scores: {len(rows)}  by group {Counter(r['g'] for r in rows)}")
print(f"rebuild check (no spread taken off): existing rebuild == branch tier for {agree[True]} of {sum(agree.values())}")


def summarize(label, sel, key):
    xs = [r["ref"][key] for r in sel if r["ref"] and r["ref"][key] is not None and r["ref"][key].n >= 5]
    if not xs:
        return f"{label:34} n=0"
    pos = sum(x.mean > 0 for x in xs) / len(xs)
    mz = st.mean(x.z for x in xs)
    pooled = st.mean(x.mean for x in xs)
    return f"{label:34} n={len(xs):3}  ref edge>0 {pos:4.0%}  mean ref z {mz:+5.2f}  mean ref edge {100*pooled:+5.2f}¢"


for key, name in (("midmid", "mid-to-mid 5 min (other window)"), ("settle", "settlement vs mid (other window)")):
    print(f"\n=== reference: {name} ===")
    for scope in ("rec", "all"):
        pool = [r for r in rows if scope == "all" or r["g"] in ("rec", "rec_prior")]
        print(f"[{scope}]")
        print("  " + summarize("passes existing", [r for r in pool if r["old"] in REC], key))
        print("  " + summarize("passes corrected", [r for r in pool if r["new"] in REC], key))
        print("  " + summarize("removed by the correction", [r for r in pool if r["old"] in REC and r["new"] not in REC], key))
        print("  " + summarize("kept by both", [r for r in pool if r["old"] in REC and r["new"] in REC], key))
        print("  " + summarize("passes neither (8+ bets)", [r for r in pool if r["old"] not in REC and r["new"] not in REC and (r["s"].get("bets") or 0) >= 8], key))


def spearman(a, b):
    def ranks(v):
        o = sorted(range(len(v)), key=lambda i: v[i]); rk = [0.0] * len(v)
        for k, i in enumerate(o): rk[i] = k
        return rk
    ra, rb = ranks(a), ranks(b)
    return st.correlation(ra, rb)


print("\n=== does the current z predict the other window's spread-free edge? (Spearman, wallets with 8+ bets both) ===")
for key in ("midmid", "settle"):
    pts = [(r["s"]["edge_at_fill"]["z"], r["s"]["edge"]["z"], r["ref"][key].z) for r in rows
           if r["ref"] and r["ref"][key] is not None and r["ref"][key].n >= 8 and r["s"].get("edge") and r["s"].get("edge_at_fill")
           and (r["s"].get("bets") or 0) >= 8]
    if len(pts) > 10:
        print(f"  {key}: n={len(pts)}  existing z ρ={spearman([p[0] for p in pts], [p[2] for p in pts]):+.3f}"
              f"   corrected z ρ={spearman([p[1] for p in pts], [p[2] for p in pts]):+.3f}")

print("\n=== the 43 recommended (production windows): who changes, and why ===")
cnt = Counter()
for r in sorted([r for r in rows if r["g"] == "rec"], key=lambda r: (r["old"], r["new"])):
    s = r["s"]; e, f = s.get("edge") or {}, s.get("edge_at_fill") or {}
    cnt[(r["old"], r["new"])] += 1
    if r["old"] == r["new"]:
        continue
    ref = r["ref"] or {}
    mm, sl = ref.get("midmid"), ref.get("settle")
    print(f"  {r['w'][:10]} {r['old']:>10} → {r['new']:<11} resting {s.get('maker_bets') or 0:4.0%}  spread {100*(s.get('spread_earned') or 0):.2f}¢/bet"
          f"  edge {100*f.get('mean',0):+.2f}¢ (z {f.get('z',0):+.1f}) → {100*e.get('mean',0):+.2f}¢ (z {e.get('z',0):+.1f})"
          f" | prior mid-mid {('%+.2f¢ z%+.1f' % (100*mm.mean, mm.z)) if mm else 'n/a'}  settle {('%+.1f¢ n%d' % (100*sl.mean, sl.n)) if sl else 'n/a'}")
print("  transitions:", sorted(cnt.items(), key=lambda kv: -kv[1]))

print("\n=== reads per score ===")
rd = [r for fn in ("v_rec.jsonl", "v_rec_prior.jsonl", "v_ctrl.jsonl", "v_ctrl_prior.jsonl") for r in load(fn) if "score" in r]
tot = [sum(r["reads"].values()) for r in rd]; tk = [r["reads"].get("/trades?market", 0) for r in rd]
print(f"  scores {len(rd)}: reads mean {st.mean(tot):.1f} median {st.median(tot)}; taker-feed reads mean {st.mean(tk):.2f} max {max(tk)}"
      f" → +{st.mean(tk)/(st.mean(tot)-st.mean(tk)):.1%} on the existing {st.mean(tot)-st.mean(tk):.1f}")
known = [r["score"]["coverage"].get("resting_known", 0) / max(1, r["score"]["coverage"].get("priced_5m", 0)) for r in rd if r["score"]["coverage"].get("priced_5m")]
print(f"  buys classified: mean {st.mean(known):.1%}, scores with any unknown {sum(k < 1 for k in known)} of {len(known)}")
print(f"  seconds per score (sequential, cache warming): median {st.median(r['secs'] for r in rd):.1f}")
