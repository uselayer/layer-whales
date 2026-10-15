"""Uses analyze.py's data and functions:
    PYTHONPATH=<uselayer-sdk main>/python/src uv run --frozen python research/spread-bias/exclusions.py research/spread-bias/analyze.py research/spread-bias/rows.jsonl.gz
"""
import sys, runpy, math
lib = sys.argv[1]; sys.argv = sys.argv[1:]; ns = runpy.run_path(lib, run_name="lib")
data, stat, tier, by_key, REC, bh, p1 = ns["data"], ns["stat"], ns["tier"], ns["by_key"], ns["REC"], ns["bh"], ns["p_one_sided"]
cur = [d for d in data if "prior" not in d["r"]["group"] and len(d["full"]) >= 2]
def prior(d):
    return by_key.get((d["r"]["wallet"], "rec_prior" if d["r"]["group"] == "rec" else "ctrl_prior"))
null = []
for d in cur:
    p = prior(d); e = stat(d["full"], "vm")
    if p and len(p["full"]) >= 8 and e and abs(e.z) < 1: null.append(stat(p["full"], "vs").mean > 0)
q0 = sum(null) / len(null)
cut = {k: bh([p1(stat(d["full"], k)) for d in cur], 0.10) for k in ("vm", "v0")}
def seg(d, rule):
    key, rz, b = rule
    s = tier(d["r"], d["full"], key, rising_z=rz)
    if b and s in REC and p1(stat(d["full"], key)) > cut[key]: s = "out"
    return s
base = ("vm", 1.0, False)
rules = {"spread fix only (z≥1)": base, "+ Rising z≥1.645": ("vm", 1.645, False), "+ Rising z≥2": ("vm", 2.0, False),
         "+ BH FDR 10% (all tiers)": ("vm", 1.0, True), "no spread fix, Rising z≥2": ("v0", 2.0, False), "no spread fix, BH": ("v0", 1.0, True)}
print(f"null proxy q0={q0:.2f} (n={len(null)}); genuine-excluded estimate = n_val × max(0,(share prior>0 − q0)/(1 − q0))")
for name, rule in rules.items():
    ex = [d for d in cur if seg(d, ("v0", 1.0, False)) in REC and seg(d, rule) not in REC]
    passn = sum(seg(d, rule) in REC for d in cur)
    val = [stat(prior(d)["full"], "vs") for d in ex if prior(d) and len(prior(d)["full"]) >= 8]
    pos = sum(v.mean > 0 for v in val); z2 = sum(v.z >= 2 for v in val)
    est = len(val) * max(0, (pos / len(val) - q0) / (1 - q0)) if val else 0
    # among those the spread fix keeps, what extra the Rising rule removes
    ex2 = [d for d in cur if seg(d, base) in REC and seg(d, rule) not in REC]
    val2 = [stat(prior(d)["full"], "vs") for d in ex2 if prior(d) and len(prior(d)["full"]) >= 8]
    pos2 = sum(v.mean > 0 for v in val2)
    est2 = len(val2) * max(0, (pos2 / len(val2) - q0) / (1 - q0)) if val2 else 0
    print(f"{name:28} pass {passn:3} | removed vs today {len(ex):3} (checked {len(val):2}: prior>0 {pos:2}, z≥2 {z2:2}; est. genuine {est:4.1f})"
          f" | beyond spread fix {len(ex2):3} (checked {len(val2):2}: prior>0 {pos2:2}; est. genuine {est2:4.1f})")
print("BH cuts:", {k: round(v, 4) for k, v in cut.items()})
