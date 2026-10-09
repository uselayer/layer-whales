"""The demo's local engine: a small HTTP service on your machine that runs the uselayer SDK.

Everything runs here, with your own keys. Nothing is hosted. Copies are paper trades (real books,
fake money) in their own store, ~/.uselayer/whales-demo/paper.db unless WHALES_STORE_DIR says otherwise.

    LAYER_API_KEY=lyr_... uv run whales-demo        # then open http://127.0.0.1:8790
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from uselayer import Client, Copier, Trader, VenueError

HERE = Path(__file__).parent
STORE_DIR = Path(os.environ.get("WHALES_STORE_DIR", Path.home() / ".uselayer" / "whales-demo"))

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    apply_rules()  # resume copying with the saved rules
    threading.Thread(target=_worth_loop, daemon=True).start()  # last result shows at once; refresh behind it
    yield
    _loop_stop.set()
    _worth_stop.set()


app = FastAPI(title="Layer Whales", lifespan=lifespan)
reads = Client(store=":memory:")  # leaderboards, traders, links: reads only
_paper: Client | None = None
_lock = threading.Lock()  # one copier and the paper store at a time


def paper() -> Client:
    global _paper
    if _paper is None:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        _paper = Client(store=str(STORE_DIR / "paper.db"))
    return _paper


# ---- a small cache, so clicking around doesn't re-read the venues ----

_cache: dict[str, tuple[float, Any]] = {}


def cached(key: str, ttl_s: float, fn: Callable[[], Any]) -> Any:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    value = fn()
    _cache[key] = (time.monotonic(), value)
    return value


def venue_error(e: VenueError) -> HTTPException:
    return HTTPException(502, detail={"message": e.message, "hint": e.hint})


# ---- reads ----


@app.get("/api/top")
def top(venue: str = "all", by: str = "pnl", limit: int = 25) -> list[dict[str, Any]]:
    try:
        rows = cached(
            f"top:{venue}:{by}:{limit}", 120, lambda: reads.whales.top(venue, by=by, limit=limit)  # type: ignore[arg-type]
        )
    except VenueError as e:
        raise venue_error(e) from e
    return [t.to_dict() for t in rows]


@app.get("/api/trading-now")
def trading_now(min_usd: float = 250) -> list[dict[str, Any]]:
    """Recent large trades, newest first. Named Kalshi traders here usually show their trades."""
    try:
        rows = cached(f"now:{min_usd}", 20, lambda: reads.whales.big_trades(min_usd, kalshi_pages=5))
    except VenueError as e:
        raise venue_error(e) from e
    return [t.to_dict() for t in rows[:100]]


@app.get("/api/trader/{venue}/{trader_id}")
def trader(venue: str, trader_id: str) -> dict[str, Any]:
    try:
        d = cached(f"trader:{venue}:{trader_id}", 15, lambda: reads.whales.trader(venue, trader_id, trades=40))  # type: ignore[arg-type]
    except VenueError as e:
        raise venue_error(e) from e
    return d.to_dict()


@app.get("/api/links/{venue}/{trader_id}")
def links(venue: str, trader_id: str) -> dict[str, Any]:
    """Accounts on the other venue that may be the same person. Takes 5–30 s: it reads both venues."""
    try:
        t = cached(f"trader:{venue}:{trader_id}", 15, lambda: reads.whales.trader(venue, trader_id, trades=40))  # type: ignore[arg-type]
        found = cached(f"links:{venue}:{trader_id}", 600, lambda: reads.whales.links(t.trader))
    except VenueError as e:
        raise venue_error(e) from e
    return {"needs_layer_key": not os.environ.get("LAYER_API_KEY"), "links": [x.to_dict() for x in found]}


# ---- worth following (Polymarket) ----
# Finding and scoring ~200 wallets takes 20-30 minutes, so it runs in the background: on launch when the
# kept result is older than WORTH_MAX_AGE_S, then again each time it gets that old. The page shows the last
# result meanwhile. A wallet that isn't in it is scored when its page is opened.

WORTH_WALLETS = int(os.environ.get("WHALES_WORTH_WALLETS", "200"))
WORTH_MAX_AGE_S = 6 * 3600
WORTH_RETRY_S = 30 * 60  # after a failed check
WORTH_FORMAT = 3  # bump when the kept result gains fields; an older one is re-checked on launch (3: won/settled/odds)
_worth_stop = threading.Event()
_worth: dict[str, Any] = {"running": False, "done": 0, "total": 0, "error": None}
_worth_data: dict[str, Any] | None = None
_worth_one: dict[str, dict[str, Any]] = {}


def worth_file() -> Path:
    return STORE_DIR / "worth-following.json"


def _worth_load() -> dict[str, Any] | None:
    global _worth_data
    if _worth_data is None and worth_file().exists():
        try:
            _worth_data = json.loads(worth_file().read_text())
        except ValueError:
            _worth_data = None
    return _worth_data


def _worth_run() -> None:
    global _worth_data
    try:
        reads.whales.scorer(cache_dir=str(STORE_DIR / "price-cache"))

        def progress(done: int, total: int, _s: Any) -> None:
            _worth.update(done=done, total=total)

        d = reads.whales.discover(wallets=WORTH_WALLETS, on_progress=progress)
        data = d.to_dict(sample=True) | {"format": WORTH_FORMAT}
        for s in data["scores"]:
            settled = [b for b in s["sample"] if b.get("payout") is not None]  # before the sample is cut
            s["won"], s["settled"] = sum(1 for b in settled if b["payout"] == 1), len(settled)
            # the price paid is the market's chance they'd win, so this is the win rate the odds expected
            s["odds"] = sum(b["price"] for b in settled) / len(settled) if settled else None
            s["sample"] = s["sample"][:30]
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        worth_file().write_text(json.dumps(data))
        _worth_data = data
        _worth_one.clear()
        _worth["error"] = None
    except Exception as e:  # show it on the page; the last good result stays
        _worth["error"] = str(e)
    finally:
        _worth["running"] = False


def worth_refresh() -> None:
    if _worth["running"]:
        return
    _worth.update(running=True, done=0, total=0, error=None)
    threading.Thread(target=_worth_run, daemon=True).start()


def _worth_age_s() -> float:
    data = _worth_load()
    if data is None or data.get("format") != WORTH_FORMAT:
        return float("inf")
    return time.time() - _iso_ts(data["finished"])


def _worth_loop() -> None:
    """Keeps the result at most WORTH_MAX_AGE_S old while the app runs."""
    while not _worth_stop.is_set():
        if not _worth["running"]:
            if _worth["error"] is not None:
                _worth_stop.wait(WORTH_RETRY_S)
                _worth["error"] = None
                continue
            if _worth_age_s() >= WORTH_MAX_AGE_S:
                worth_refresh()
        _worth_stop.wait(60)


@app.get("/api/worth")
def worth() -> dict[str, Any]:
    """Every scored wallet without its bets, plus whether a refresh is running."""
    data = _worth_load()
    out: dict[str, Any] = {"status": dict(_worth), "result": None}
    if data is not None:
        out["result"] = {k: v for k, v in data.items() if k != "scores"} | {
            "scores": [{k: v for k, v in s.items() if k != "sample"} for s in data["scores"]]
        }
    return out


@app.post("/api/worth/refresh")
def worth_refresh_route() -> dict[str, Any]:
    worth_refresh()
    return {"status": dict(_worth)}


@app.get("/api/worth/{wallet}")
def worth_wallet(wallet: str) -> dict[str, Any]:
    """One wallet's score with its checked bets: from the last refresh, or scored now (5–30 s)."""
    w = wallet.lower()
    data = _worth_load()
    for s in (data or {}).get("scores", []):
        if s["wallet"] == w:
            return s
    if w not in _worth_one:
        try:
            _worth_one[w] = reads.whales.score(w).to_dict()
        except VenueError as e:
            raise venue_error(e) from e
    return _worth_one[w]


# ---- live bets from traders worth following ----
# Their buys over the last day, one row per bet (fills on the same outcome merged), with the price now:
# the current price of their open position. Polymarket's public trade feed runs a few minutes behind.

FEED_SEGMENTS = ("proven", "quiet", "rising")  # the Follow and Watch verdicts
FEED_HOURS = 24


@app.get("/api/live")
def live() -> dict[str, Any]:
    data = _worth_load()
    follow = [s for s in (data or {}).get("scores", []) if s["segment"] in FEED_SEGMENTS]
    since = datetime.now(UTC) - timedelta(hours=FEED_HOURS)
    bets: list[dict[str, Any]] = []
    failed = 0
    for s in follow:
        try:
            d = cached(
                f"live:{s['wallet']}", 25, lambda w=s["wallet"]: reads.whales.trader("polymarket", w, trades=100)
            )
        except VenueError:
            failed += 1
            continue
        now = {p.market: p.current_price for p in d.positions}
        one: dict[str, dict[str, Any]] = {}
        for t in d.trades:
            if t.action != "buy" or t.at < since or not t.size:
                continue
            b = one.setdefault(
                t.market,
                {"wallet": s["wallet"], "name": s["name"], "segment": s["segment"], "label": s["label"],
                 "title": t.title, "outcome": t.outcome, "url": t.url, "at": t.at, "usd": 0.0, "size": 0.0,
                 "price_now": now.get(t.market)},
            )  # fmt: skip
            b["at"], b["usd"], b["size"] = max(b["at"], t.at), b["usd"] + t.usd, b["size"] + t.size
        bets += [b | {"price": b["usd"] / b["size"], "at": b["at"].isoformat()} for b in one.values()]
    bets.sort(key=lambda b: b["at"], reverse=True)
    return {"traders": [{"wallet": s["wallet"], "name": s["name"], "label": s["label"]} for s in follow],
            "failed": failed, "hours": FEED_HOURS, "bets": bets}  # fmt: skip


def _iso_ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


# ---- copying (paper) ----
# Copy rules are set once and saved in the store dir: which traders, contracts a trade, how far above their
# price to pay, and where to copy to. One background loop polls a copier per trader and copies each new buy
# with paper money; every copied trade is appended to copied.jsonl, so "My copied trades" survives restarts.


class CopyRules(BaseModel):
    size: float = 5
    max_above: float = 0.03  # dollars a contract above their price
    copy_to: str = "polymarket_us"


class CopyTrader(BaseModel):
    venue: str
    id: str
    name: str | None = None
    categories: list[str] = []


def rules_file() -> Path:
    return STORE_DIR / "copy-rules.json"


def copied_file() -> Path:
    return STORE_DIR / "copied.jsonl"


_copiers: dict[str, Copier] = {}
_status: dict[str, dict[str, Any]] = {}  # per trader: checked_at, error
_recent: list[dict[str, Any]] = []  # the last copied and skipped events, newest last
_loop_thread: threading.Thread | None = None
_loop_stop = threading.Event()


def load_rules() -> dict[str, Any]:
    if rules_file().exists():
        try:
            return json.loads(rules_file().read_text())
        except ValueError:
            pass
    return CopyRules().model_dump() | {"traders": []}


def save_rules(rules: dict[str, Any]) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    rules_file().write_text(json.dumps(rules, indent=2))


def _key(venue: str, id: str) -> str:
    return f"{venue}:{id}"


def _make_copier(rules: dict[str, Any], t: dict[str, Any]) -> Copier:
    trader = Trader(
        venue=t["venue"],
        id=t["id"],
        name=t.get("name") or t["id"],
        volume_unit="contracts" if t["venue"] == "kalshi" else "usd",
    )
    return paper().whales.follow(
        trader,
        size=float(rules["size"]),
        venue=rules["copy_to"],
        max_slippage=float(rules["max_above"]),
        categories=tuple(t.get("categories") or ()),
        copy_sells=False,  # buys only, held until the market settles
    )


def apply_rules() -> None:
    """Make the running copiers match the saved rules, and start the loop if anyone is copied."""
    global _loop_thread
    rules = load_rules()
    with _lock:
        _copiers.clear()
        for t in rules["traders"]:
            _copiers[_key(t["venue"], t["id"])] = _make_copier(rules, t)
    if _copiers and (_loop_thread is None or not _loop_thread.is_alive()):
        _loop_stop.clear()
        _loop_thread = threading.Thread(target=_copy_loop, daemon=True)
        _loop_thread.start()


def _record(cp: Copier, events: list[Any]) -> None:
    for e in events:
        d = e.to_dict() | {"trader": cp.trader.to_dict()}
        _recent.append(d)
        if e.status == "copied":
            with copied_file().open("a") as f:
                f.write(json.dumps(d) + "\n")
            _copied_cache["at"] = 0.0  # a new row: work it out again on the next read
    del _recent[:-200]


def _copy_loop() -> None:
    while not _loop_stop.is_set():
        for key, cp in list(_copiers.items()):
            st = _status.setdefault(key, {})
            if _copiers.get(key) is not cp:
                continue  # the rules changed since this pass began
            try:
                # Not under _lock: a poll can take seconds (Layer matching is paced), and the paper store
                # locks itself. Only this thread polls, so copiers never run at once.
                events = cp.poll()
                with _lock:
                    _record(cp, events)
                st.update(checked_at=time.time(), error=None)
            except VenueError as e:
                st["error"] = e.message
            except Exception as e:  # keep copying through a bad read; show it on the page
                st["error"] = str(e)
        _loop_stop.wait(5)


@app.get("/api/copy")
def copy_state() -> dict[str, Any]:
    rules = load_rules()
    now = time.time()
    for t in rules["traders"]:
        st = _status.get(_key(t["venue"], t["id"]), {})
        t["checked_s_ago"] = None if st.get("checked_at") is None else round(now - st["checked_at"], 1)
        t["error"] = st.get("error")
    return {"rules": rules, "recent": list(reversed(_recent[-50:]))}


@app.put("/api/copy/rules")
def set_rules(req: CopyRules) -> dict[str, Any]:
    if req.copy_to not in ("polymarket_us", "kalshi"):
        raise HTTPException(400, detail="copy_to must be polymarket_us or kalshi")
    rules = load_rules() | req.model_dump()
    save_rules(rules)
    apply_rules()
    return copy_state()


@app.post("/api/copy/traders")
def add_trader(req: CopyTrader) -> dict[str, Any]:
    rules = load_rules()
    rules["traders"] = [t for t in rules["traders"] if _key(t["venue"], t["id"]) != _key(req.venue, req.id)]
    rules["traders"].append(req.model_dump())
    save_rules(rules)
    apply_rules()
    return copy_state()


@app.delete("/api/copy/traders/{venue}/{trader_id}")
def remove_trader(venue: str, trader_id: str) -> dict[str, Any]:
    rules = load_rules()
    rules["traders"] = [t for t in rules["traders"] if _key(t["venue"], t["id"]) != _key(venue, trader_id)]
    save_rules(rules)
    apply_rules()
    return copy_state()


COPIED_TTL_S = 30
_copied_cache: dict[str, Any] = {"at": 0.0, "data": None}
_copied_lock = threading.Lock()  # one valuation at a time


@app.get("/api/copied")
def copied() -> dict[str, Any]:
    """One row per copied trade with its status (open / won / lost) and profit after fees, newest first.

    Valuing open bets reads each market's book, and Polymarket US can ask us to wait 10 s, so the answer
    is kept for 30 s and, while a new one is worked out, the last one is served.
    """
    hit = _copied_cache["data"]
    if hit is None:  # the first answer is worth waiting for
        with _copied_lock:
            return _copied_cache["data"] or _copied_refresh()
    if time.time() - _copied_cache["at"] >= COPIED_TTL_S:
        threading.Thread(target=_copied_background, daemon=True).start()
    return hit


def _copied_refresh() -> dict[str, Any]:
    data = _copied_now()
    _copied_cache.update(at=time.time(), data=data)
    return data


def _copied_background() -> None:
    if not _copied_lock.acquire(blocking=False):
        return  # already being worked out
    try:
        _copied_refresh()
    except Exception:  # keep serving the last answer; the next read tries again
        pass
    finally:
        _copied_lock.release()


def _copied_now() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if copied_file().exists():
        for line in copied_file().read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    ids = [r["order_id"] for r in rows if r.get("order_id")]
    try:
        # No engine lock: the paper store locks itself, and the copy loop can hold _lock for seconds.
        results = paper().whales.copy_results(ids) if ids else {}
    except VenueError as e:
        raise venue_error(e) from e
    out = []
    for r in reversed(rows):
        res = results.get(r.get("order_id") or "")
        out.append(
            {
                "at": r["at"],
                "trader": r["trader"],
                "source": r["source"],
                "venue": r["venue"],
                "market": r["market"],
                "side": r["side"],
                "result": res.to_dict() if res else None,
            }
        )
    done = [o for o in out if o["result"] and o["result"]["status"] != "unfilled"]
    net = sum(o["result"]["pnl"] or 0 for o in done)
    count = {k: sum(1 for o in done if o["result"]["status"] == k) for k in ("open", "won", "lost", "void")}
    return {
        "rows": out,
        "total": {"net": round(net, 2), "trades": len(done), **count},
        "unvalued": sum(1 for o in done if o["result"]["pnl"] is None),
    }


@app.get("/api/paper")
def paper_account() -> dict[str, Any]:
    p = paper().pnl()
    return {
        "net": p.net,
        "realized": p.realized,
        "unrealized": p.unrealized,
        "fees": p.fees,
        "rows": [
            {
                "venue": r.venue,
                "market": r.market,
                "side": r.side,
                "contracts": r.contracts,
                "cost": r.cost,
                "unrealized": r.unrealized,
                "mark": r.mark,
            }
            for r in p.rows
            if r.contracts
        ],
    }


@app.post("/api/paper/reset")
def paper_reset() -> dict[str, Any]:
    """Start the paper account over: no positions and no copied trades. The copy rules stay."""
    global _paper
    with _lock:
        _copiers.clear()
        if _paper is not None:
            _paper.close()
            _paper = None
        (STORE_DIR / "paper.db").unlink(missing_ok=True)
        copied_file().unlink(missing_ok=True)
        _recent.clear()
        _copied_cache.update(at=0.0, data=None)
    apply_rules()
    return paper_account()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


def main() -> None:
    port = int(os.environ.get("WHALES_PORT", "8790"))
    print(f"Layer Whales on http://127.0.0.1:{port}  (paper store: {STORE_DIR})")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
