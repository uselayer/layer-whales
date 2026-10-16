"""Uses analyze.py's data and functions:
    PYTHONPATH=<uselayer-sdk main>/python/src uv run --frozen python research/spread-bias/chance_share.py research/spread-bias/analyze.py research/spread-bias/rows.jsonl.gz
"""
import sys, random, runpy
lib = sys.argv[1]; sys.argv = sys.argv[1:]; ns = runpy.run_path(lib, run_name="lib")  # analyze.py prints its report; we reuse its data and functions
data, stat, tier, by_key, REC = ns["data"], ns["stat"], ns["tier"], ns["by_key"], ns["REC"]
def prior(d):
    pg = "rec_prior" if d["r"]["group"] == "rec" else "ctrl_prior"
    return by_key.get((d["r"]["wallet"], pg))
cur = [d for d in data if "prior" not in d["r"]["group"] and len(d["full"]) >= 2]
def rows(key, rz=1.0):
    out = []
    for d in cur:
        p = prior(d)
        if p is None or len(p["full"]) < 8: continue
        pv = stat(p["full"], "vs"); e = stat(d["full"], key)
        if pv is None or e is None: continue
        out.append((tier(d["r"], d["full"], key, rising_z=rz), e, pv, d["r"]["group"]))
    return out
for key, label in (("v0", "current edge"), ("vm", "maker spread removed")):
    rs = rows(key)
    null = [pv for t, e, pv, g in rs if abs(e.z) < 1]
    q0 = sum(pv.mean > 0 for pv in null) / len(null)
    print(f"\n[{label}] null proxy (current |z|<1): n={len(null)}, prior spread-neutral edge >0: {q0:.0%}")
    for grp_name, pick in (("Sharp", ("proven", "quiet")), ("Rising", ("rising",)), ("All rec", ("proven", "quiet", "rising"))):
        sel = [pv for t, e, pv, g in rs if t in pick]
        if not sel: continue
        pos = sum(pv.mean > 0 for pv in sel) / len(sel)
        boots = []
        for _ in range(4000):
            s = [random.choice(sel) for _ in sel]; nb = [random.choice(null) for _ in null]
            pp = sum(x.mean > 0 for x in s) / len(s); qq = sum(x.mean > 0 for x in nb) / len(nb)
            boots.append(min(1, max(0, (1 - pp) / (1 - qq))))
        boots.sort()
        f = min(1, max(0, (1 - pos) / (1 - q0)))
        print(f"  {grp_name:7} n={len(sel):3} prior edge>0 {pos:.0%}  prior z≥2 {sum(pv.z>=2 for pv in sel)/len(sel):.0%}"
              f"  → share by chance ≈ {f:.0%} (90% range {boots[200]:.0%}–{boots[3800]:.0%})  [rec {sum(1 for t,e,pv,g in rs if t in pick and g=='rec')}]")
