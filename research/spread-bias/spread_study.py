"""Spread-bias measurement: score each wallet with SDK main (stable sample, #26), then for every checked bet
classify its fills as maker/taker (Polymarket's taker-only trade feed) and read the 1-minute mid around the fill.

    PYTHONPATH=$TMP/sdk/python/src uv run --frozen python $TMP/spread_study.py wallets.json --out rows.jsonl

wallets.json: [{wallet, group, end (iso), on_leaderboard, name}]. Resumable (skips (wallet, group) in --out).
Read-only: public Polymarket endpoints only.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx
from uselayer import Client

ap = argparse.ArgumentParser()
ap.add_argument("wallets")
ap.add_argument("--out", required=True)
ap.add_argument("--cache", default=str(Path(__file__).parent / "pricecache"))
ap.add_argument("--threads", type=int, default=5)
ap.add_argument("--group", help="override every wallet's group")
ap.add_argument("--end", help="override every wallet's window end (iso)")
args = ap.parse_args()

DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
http = httpx.Client(timeout=30, headers={"User-Agent": "signals-research"})
_gate = threading.Semaphore(8)


def get(url: str, params: dict) -> object:
    for i in range(6):
        try:
            with _gate:
                r = http.get(url, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** i)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, ValueError):
            time.sleep(2 ** i)
    raise RuntimeError(f"failed {url} {params}")


sc = Client(store=":memory:").whales.scorer(cache_dir=args.cache)
out = Path(args.out)
done = set()
if out.exists():
    for line in out.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            done.add((r["wallet"], r["group"]))
lock = threading.Lock()


def fills_for(wallet: str, conds: list[str], taker_only: bool) -> list[dict]:
    rows: list[dict] = []
    for i in range(0, len(conds), 10):
        part = conds[i : i + 10]
        got = get(f"{DATA}/trades", {"user": wallet, "market": ",".join(part), "takerOnly": str(taker_only).lower(), "limit": 1000})
        if isinstance(got, list) and len(got) >= 1000:  # too many: one market at a time
            got = []
            for c in part:
                g = get(f"{DATA}/trades", {"user": wallet, "market": c, "takerOnly": str(taker_only).lower(), "limit": 1000})
                got += g if isinstance(g, list) else []
        rows += got if isinstance(got, list) else []
    return rows


def mids(token: str, at: int) -> dict:
    h = get(f"{CLOB}/prices-history", {"market": token, "startTs": at - 600, "endTs": at + 900, "fidelity": 1})
    pts = sorted((int(x["t"]), float(x["p"])) for x in (h or {}).get("history", []))
    before = [(t, p) for t, p in pts if t <= at and at - t <= 180]
    def first(after: int, tol: int):
        for t, p in pts:
            if t >= after:
                return p if t - after <= tol else None
        return None
    return {"mid_before": before[-1][1] if before else None, "mid_before_age": at - before[-1][0] if before else None,
            "mid_1m": first(at + 60, 120), "mid_5m": first(at + 300, 330), "n_pts": len(pts)}


def one(w: dict) -> dict | None:
    key = (w["wallet"], w["group"])
    if key in done:
        return None
    row: dict = {"wallet": w["wallet"], "group": w["group"], "end": w["end"]}
    try:
        end = datetime.fromisoformat(w["end"])
        s = sc.score(w["wallet"], days=30, end=end, sample=60, on_leaderboard=bool(w.get("on_leaderboard")),
                     name=w.get("name"), source="research")
        d = s.to_dict(sample=True)
        row["score"] = d
        bets = [b for b in d["sample"] if "300" in {str(k) for k in (b.get("moved") or {})}]
        conds = sorted({b["market"].split(":")[0] for b in d["sample"]})
        info = sc.markets.get(conds)
        allf = fills_for(w["wallet"], conds, False)
        takf = fills_for(w["wallet"], conds, True)
        tk = Counter((f.get("transactionHash"), f.get("asset"), round(float(f.get("size") or 0), 4)) for f in takf)
        extra = []
        for b in d["sample"]:
            cond, oc = b["market"].split(":")
            m = info.get(cond) or {}
            try:
                token = json.loads(m.get("clobTokenIds") or "[]")[int(oc)]
            except (IndexError, ValueError):
                token = None
            at = int(datetime.fromisoformat(b["at"]).timestamp())
            mk = tkn = 0.0
            for f in allf:
                if str(f.get("side")).upper() != "BUY" or f.get("asset") != token:
                    continue
                t = int(f.get("timestamp") or 0)
                if not (at <= t <= at + 60):
                    continue
                k = (f.get("transactionHash"), f.get("asset"), round(float(f.get("size") or 0), 4))
                sz = float(f.get("size") or 0)
                if tk[k] > 0:
                    tk[k] -= 1
                    tkn += sz
                else:
                    mk += sz
            e = {"market": b["market"], "at": at, "price": b["price"], "moved": b.get("moved"), "event": b.get("event"),
                 "maker_size": mk, "taker_size": tkn, "category": b.get("category")}
            if token:
                e |= mids(token, at)
            extra.append(e)
        row["bets"] = extra
    except Exception as ex:  # keep going; the row records it
        row["error"] = repr(ex)
    return row


ws = json.loads(Path(args.wallets).read_text())
for w in ws:
    if args.group:
        w["group"] = args.group
    if args.end:
        w["end"] = args.end
done_now = {(w["wallet"], w["group"]) for w in ws}
t0 = time.time()
with ThreadPoolExecutor(args.threads) as ex, out.open("a") as f:
    for i, r in enumerate(ex.map(one, ws)):
        if r is None:
            continue
        with lock:
            f.write(json.dumps(r) + "\n")
            f.flush()
        sd = r.get("score") or {}
        print(f"{i + 1}/{len(ws)} {time.time() - t0:5.0f}s {r['group']:10} {r['wallet'][:10]} {sd.get('segment', 'err'):10} "
              f"bets={len(r.get('bets') or [])} {r.get('error', '')[:80]}", flush=True)
