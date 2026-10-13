"""Collection for selective-preregistered.md: the pool, each wallet's band from its trades before T, and every bet
in S and O with its settlement, its price 12 hours later, the next price after it and the taker fee. Read-only:
public Polymarket data, no keys. Resumable: wallets already in --out are skipped. The analysis is
selective_analyze.py.

    uv run python research/selective_collect.py --out research/selective-bets.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from uselayer import Client
from uselayer import whale_scores as ws

T = int(datetime(2026, 7, 14, tzinfo=UTC).timestamp())
O_END = int(datetime(2026, 10, 13, tzinfo=UTC).timestamp())
DAY = 86400
GAMMA = "https://gamma-api.polymarket.com"

ap = argparse.ArgumentParser()
ap.add_argument("--out", default=str(Path(__file__).parent / "selective-bets.jsonl"))
ap.add_argument("--pool", default=str(Path(__file__).parent / "selective-pool.json"))
ap.add_argument("--selective", type=int, default=250)
ap.add_argument("--frequent", type=int, default=150)
ap.add_argument("--threads", type=int, default=4)
args = ap.parse_args()

client = Client(store=":memory:")
poly = client.whales._poly
book = ws.MarketBook(poly)
http = httpx.Client(timeout=30, headers={"user-agent": "layer-research"})


def gamma(**params: Any) -> list[dict[str, Any]]:
    for k in range(4):
        r = http.get(f"{GAMMA}/markets", params=params)
        if r.status_code == 200:
            return list(r.json() or [])
        time.sleep(1 + k)
    return []


def trades(w: str, start: int, end: int, cap: int = 6000) -> list[dict[str, Any]]:
    """Their trades in [start, end), newest first, paged by time past the offset cap."""
    out: list[dict[str, Any]] = []
    upper = end
    while len(out) < cap:
        got = []
        for off in range(0, 5000, 500):
            page = poly.activity(w, start=start, end=upper, offset=off)
            got += page
            if len(page) < 500:
                break
        out += got
        if len(got) < 5000 or not got:
            break
        upper = int(got[-1]["timestamp"]) - 1
    return out


def single_markets(rows: list[dict[str, Any]], a: int, b: int) -> set[str]:
    return {str(r["conditionId"]) for r in rows if str(r.get("side")).upper() == "BUY"
            and str(r.get("isCombo")).lower() != "true" and a <= int(r["timestamp"]) < b}  # fmt: skip


# ---- the pool: wallets in the last trades of markets that closed between 1 Jun and T ----
pool_path = Path(args.pool)
if pool_path.exists():
    pool = json.loads(pool_path.read_text())
else:
    markets: list[dict[str, Any]] = []
    for off in range(0, 3000, 100):
        page = gamma(closed="true", end_date_min="2026-06-01", end_date_max="2026-07-13", limit=100, offset=off,
                     order="volumeNum", ascending="false")  # fmt: skip
        markets += [m for m in page if m.get("conditionId")]
        if len(page) < 100:
            break
    random.seed(31)
    q = len(markets) // 4
    bands = [markets[:q], markets[q : 2 * q], markets[2 * q : 3 * q], markets[3 * q :]]
    picked = [m for b in bands for m in random.sample(b, min(60, len(b)))]
    wallets: set[str] = set()
    for m in picked:
        try:
            for t in poly.market_trades(m["conditionId"], 500):
                if t.get("proxyWallet"):
                    wallets.add(str(t["proxyWallet"]).lower())
        except Exception as e:  # noqa: BLE001
            print("market read failed", m["conditionId"], e, flush=True)
    order = sorted(wallets)
    random.shuffle(order)
    pool = {"markets": len(markets), "picked": [m["conditionId"] for m in picked], "wallets": order,
            "classified": {}}  # fmt: skip
    pool_path.write_text(json.dumps(pool))
print(f"pool: {len(pool['wallets'])} wallets from {len(pool['picked'])} markets", flush=True)

# ---- classify (before T only), in the pool's random order, until both bands are full ----
lock = threading.Lock()


def classify(w: str) -> str:
    # Newest first, a page at a time, stopping once the band is settled: the 30 days before T are read and
    # there are 20 single markets in the year (more pages can't change either count's verdict).
    rows: list[dict[str, Any]] = []
    upper, off = T, 0
    while True:
        page = poly.activity(w, start=T - 365 * DAY, end=upper, offset=off)
        rows += page
        oldest = int(page[-1]["timestamp"]) if page else T - 365 * DAY
        if len(page) < 500 or (oldest <= T - 30 * DAY and len(single_markets(rows, T - 365 * DAY, T)) >= 20):
            break
        off += 500
        if off >= 4500:
            upper, off = oldest - 1, 0
    m30, m365 = single_markets(rows, T - 30 * DAY, T), single_markets(rows, T - 365 * DAY, T)
    m90 = single_markets(rows, T - 90 * DAY, T)
    if len(m365) < 20:
        return "short"
    if len(m30) >= 8:
        return "frequent"
    if 1 <= len(m30) and m90:
        return "selective"
    return "other"


def classify_safe(w: str) -> str:
    try:
        return classify(w)
    except Exception:  # noqa: BLE001  (a wallet that can't be read is left out, and counted)
        return "error"


cls: dict[str, str] = pool["classified"]
need = {"selective": args.selective, "frequent": args.frequent}


def have(band: str) -> int:
    return sum(1 for v in cls.values() if v == band)


todo = [w for w in pool["wallets"] if w not in cls]
with ThreadPoolExecutor(args.threads) as ex:
    for i in range(0, len(todo), 40):
        if all(have(b) >= n for b, n in need.items()):
            break
        chunk = todo[i : i + 40]
        for w, band in zip(chunk, ex.map(classify_safe, chunk)):
            cls[w] = band
        pool_path.write_text(json.dumps(pool))
        print(f"classified {len(cls)}: selective {have('selective')}, frequent {have('frequent')}", flush=True)
picks = {b: [w for w in pool["wallets"] if cls.get(w) == b][:n] for b, n in need.items()}

# ---- every bet in S and O, with what's needed to judge it ----
_series: dict[str, list[tuple[int, float]]] = {}


def series(token: str) -> list[tuple[int, float]]:
    with lock:
        if token in _series:
            return _series[token]
    body = poly._get(poly._data + "/v2/prices-history", {"token_id": token, "interval": "max"})
    rows = (body or {}).get("data") if isinstance(body, dict) else None
    pts = sorted((int(r["timestamp"]), float(r["price"])) for r in rows or [] if "timestamp" in r and "price" in r)
    with lock:
        _series[token] = pts
    return pts


def after(pts: list[tuple[int, float]], t: int, lo: int, hi: int) -> float | None:
    for ts, p in pts:
        if t + lo <= ts <= t + hi:
            return p
        if ts > t + hi:
            break
    return None


def bets(w: str, band: str) -> list[dict[str, Any]]:
    rows = [r for r in trades(w, T - 365 * DAY, O_END) if str(r.get("isCombo")).lower() != "true"]
    ds = [d for d in ws._decisions(rows) if 0.02 <= d.price <= 0.98 and d.token]
    s_ds = [d for d in ds if d.at < T]
    o_ds = [d for d in ds if d.at >= T]
    if band == "frequent" and len(s_ds) > 120:
        step = len(s_ds) / 120
        s_ds = [s_ds[int(i * step)] for i in range(120)]
    if band == "frequent" and len(o_ds) > 120:
        step = len(o_ds) / 120
        o_ds = [o_ds[int(i * step)] for i in range(120)]
    use = s_ds + o_ds
    info = book.get(d.condition for d in use)
    out = []
    for d in use:
        m = info.get(d.condition)
        if not m:
            continue
        payout = ws._payout(m, d.outcome)
        pts = series(d.token)
        p12 = after(pts, d.at, 12 * 3600, 36 * 3600)
        if p12 is None and payout is not None and pts and pts[-1][0] < d.at + 12 * 3600:
            p12 = payout  # settled before then
        nxt = after(pts, d.at, 1, 12 * 3600)
        fee = ws.taker_fee(m, nxt) if nxt is not None and 0 < nxt < 1 else None
        out.append({"w": "S" if d.at < T else "O", "at": d.at, "event": d.event, "cond": d.condition,
                    "price": round(d.price, 4), "usd": round(d.usd, 2), "payout": payout,
                    "p12": p12, "next": nxt, "fee": None if fee is None else round(fee, 5),
                    "category": ws.category_of(m)})  # fmt: skip
    return out


def profit(w: str) -> dict[str, float | None]:
    c = ws._Curve(poly.pnl_history(w))
    return {"S": c.change(T - 365 * DAY, T, ws._trading), "O": c.change(T, O_END, ws._trading),
            "since": c.since}  # fmt: skip


out = Path(args.out)
done = {json.loads(x)["wallet"] for x in out.read_text().splitlines() if x.strip()} if out.exists() else set()
jobs = [(w, b) for b, ws_ in picks.items() for w in ws_ if w not in done]
print(f"picked: selective {len(picks['selective'])}, frequent {len(picks['frequent'])}; to collect {len(jobs)}", flush=True)
t0 = time.time()


def one(job: tuple[str, str]) -> dict[str, Any]:
    w, band = job
    r: dict[str, Any] = {"wallet": w, "band": band}
    try:
        r["bets"] = bets(w, band)
        r["profit"] = profit(w)
    except Exception as e:  # noqa: BLE001  (the row records it)
        r["error"] = repr(e)
    return r


with ThreadPoolExecutor(args.threads) as ex, out.open("a") as f:
    for i, r in enumerate(ex.map(one, jobs)):
        f.write(json.dumps(r) + "\n")
        f.flush()
        n = r.get("bets") or []
        print(f"{i + 1}/{len(jobs)} {time.time() - t0:5.0f}s {r['band'][:4]} S={sum(1 for b in n if b['w'] == 'S')} "
              f"O={sum(1 for b in n if b['w'] == 'O')} {r.get('error', '')[:60]}", flush=True)  # fmt: skip
