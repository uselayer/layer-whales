"""The November follow-up in scoring79-preregistered.md: does an edge seen in two months (A and B) predict the
next month (C) better than one seen in the latest month alone? Run on or after 2026-11-13.

Scores window C (B_END to C_END) for every wallet already in scoring79-scores.jsonl.gz with the scorer as it is
installed (rules M1–M3 are judged on the kept A and B bets, so the scorer version only affects C's measurements;
it is printed). Resumable. Then prints the analysis.

    uv run python research/scoring79_followup.py
"""

from __future__ import annotations

import gzip
import json
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uselayer
from uselayer import Client

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import scoring79_analyze as an  # noqa: E402

B_END = datetime(2026, 10, 13, 18, tzinfo=UTC)
C_END = datetime(2026, 11, 12, 18, tzinfo=UTC)
SRC = HERE / "scoring79-scores.jsonl.gz"
OUT = HERE / "scoring79-scores-C.jsonl"

if datetime.now(UTC) < C_END:
    sys.exit(f"Too early: window C ends {C_END.isoformat()}.")

rows = [json.loads(x) for x in gzip.open(SRC, "rt") if x.strip()]
rows = [r for r in rows if "A" in r and "B" in r and "error" not in r]
done: dict[str, dict[str, Any]] = {}
if OUT.exists():
    done = {(d := json.loads(x))["wallet"]: d for x in OUT.read_text().splitlines() if x.strip()}
print(f"uselayer {uselayer.__version__}; {len(rows)} wallets, {len(done)} already scored for C", flush=True)

sc = Client(store=":memory:").whales.scorer(cache_dir=str(HERE / ".cache" / "prices"))
days = (C_END - B_END).total_seconds() / 86400
t0 = time.time()


def one(r: dict[str, Any]) -> dict[str, Any] | None:
    if r["wallet"] in done:
        return None
    out: dict[str, Any] = {"wallet": r["wallet"]}
    try:
        out["C"] = sc.score(r["wallet"], days=days, end=C_END, sample=60, name=r["A"].get("name"),
                            on_leaderboard=True).to_dict(sample=True)  # fmt: skip
    except Exception as e:
        out["error"] = repr(e)
    return out


with ThreadPoolExecutor(4) as ex, OUT.open("a") as f:
    for i, c in enumerate(ex.map(one, rows)):
        if c is None:
            continue
        f.write(json.dumps(c, default=str) + "\n")
        f.flush()
        done[c["wallet"]] = c
        print(f"{i + 1}/{len(rows)} {time.time() - t0:5.0f}s C={(c.get('C') or {}).get('segment', 'err')}", flush=True)

# -- analysis --
R = []
for r in rows:
    c = done.get(r["wallet"]) or {}
    if "C" in c:
        R.append(r | {"C": c["C"]})
print(f"\nwallets with A, B and C: {len(R)}")


def win_stat(s: dict[str, Any]) -> tuple[Any, Any]:
    smp = s.get("sample") or []
    e = an.stat(an.ev_means(smp, an.edge_of))
    with_e = [b for b in smp if an.edge_of(b) is not None]
    best2 = sorted(with_e, key=an.edge_of)[-2:]  # type: ignore[arg-type]
    return e, an.stat(an.ev_means([b for b in smp if b not in best2], an.edge_of))


def edge_ok(s: dict[str, Any]) -> bool:
    e, t = win_stat(s)
    return bool(an.takes_side(s) and e and e[2] >= an.MIN_N and e[0] > 0 and e[1] >= 2 and t and t[0] > 0)


def pooled_ab(r: dict[str, Any]) -> tuple[float, float, int] | None:
    xs = an.ev_means(r["A"].get("sample") or [], an.edge_of) + an.ev_means(r["B"].get("sample") or [], an.edge_of)
    return an.stat(xs)


def report(rows_: list[dict[str, Any]], label: str) -> None:
    tag = "  (too few to judge)" if len(rows_) < 10 else ""
    print(f"\n  {label}: {len(rows_)} wallets{tag}")
    if not rows_:
        return
    ps = [an.profit(r["C"]) for r in rows_]
    ps = [p for p in ps if p is not None]
    if ps:
        print(f"    1 profitability  C profit > 0: {an.wilson(sum(1 for p in ps if p > 0), len(ps))}, median ${st.median(ps):,.0f}")
    vals = [an.stat(an.ev_means(r["C"].get("sample") or [], an.edge_of)) for r in rows_]
    vals = [v for v in vals if v]
    print(f"    2 skill (C edge) {an.pooled_boot(rows_, an.edge_of, 'C')}   wallets with C edge > 0: "
          f"{an.wilson(sum(1 for v in vals if v[0] > 0), len(vals))}")  # fmt: skip
    print(f"    3 copy (C)       {an.pooled_boot(rows_, an.copy_of, 'C')}")


M1 = [r for r in R if edge_ok(r["B"]) and (an.profit(r["B"]) or 0) > 0]
M2 = [r for r in R if edge_ok(r["A"]) and edge_ok(r["B"])]
M3 = []
for r in R:
    ea, eb, p = an.stat(an.ev_means(r["A"].get("sample") or [], an.edge_of)), an.stat(an.ev_means(r["B"].get("sample") or [], an.edge_of)), pooled_ab(r)  # fmt: skip
    if an.takes_side(r["B"]) and ea and eb and p and ea[0] > 0 and eb[0] > 0 and p[2] >= an.MIN_N and p[1] >= 2.83:
        M3.append(r)
base = [r for r in R if an.takes_side(r["B"]) and (win_stat(r["B"])[0] or (0, 0, 0))[2] >= an.MIN_N]
report(base, f"baseline: takes a side, {an.MIN_N}+ events in B")
report(M1, "M1  skill yes in B alone (today's method)")
report(M2, "M2  EDGE in A and in B (z >= 2 both)")
report([r for r in M1 if r not in M2], "M1 \\ M2  latest-month picks a two-month rule drops")
report(M3, "M3  edge > 0 in A and B, pooled A+B z >= 2.83")
report([r for r in M1 if r not in M3], "M1 \\ M3  latest-month picks M3 drops")
print("\nBars (pre-registered): a rule meets the skill bar when its pooled C edge's bootstrap lower bound is > 0. M2/M3 is"
      " supported over M1 if it meets the bar and the M1 picks it drops don't, with a lower pooled C edge.")  # fmt: skip
