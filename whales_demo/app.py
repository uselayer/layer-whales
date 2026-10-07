"""The demo's local engine: a small HTTP service on your machine that runs the uselayer SDK.

Everything runs here, with your own keys. Nothing is hosted. Copies are paper trades (real books,
fake money) in their own store, ~/.uselayer/whales-demo/paper.db unless WHALES_STORE_DIR says otherwise.

    LAYER_API_KEY=lyr_... uv run whales-demo        # then open http://127.0.0.1:8790
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from uselayer import Client, Copier, Trader, VenueError

HERE = Path(__file__).parent
STORE_DIR = Path(os.environ.get("WHALES_STORE_DIR", Path.home() / ".uselayer" / "whales-demo"))

app = FastAPI(title="Layer Whales")
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


# ---- following (paper) ----


class FollowRequest(BaseModel):
    venue: str
    id: str
    name: str | None = None
    size: float = 5
    copy_to: str = "polymarket_us"
    max_slippage: float = 0.03


_copier: Copier | None = None
_stop = threading.Event()
_thread: threading.Thread | None = None
_error: str | None = None


_checked_at: float | None = None


def _loop(cp: Copier) -> None:
    global _error, _checked_at
    while not _stop.is_set():
        try:
            with _lock:
                cp.poll()
            _error = None
            _checked_at = time.time()
        except VenueError as e:
            _error = e.message
        except Exception as e:  # keep following through a bad read; show it on the page
            _error = str(e)
        _stop.wait(5)


@app.post("/api/follow")
def follow(req: FollowRequest) -> dict[str, Any]:
    global _copier, _thread
    unfollow()
    t = Trader(
        venue=req.venue,  # type: ignore[arg-type]
        id=req.id,
        name=req.name or req.id,
        volume_unit="contracts" if req.venue == "kalshi" else "usd",
    )
    with _lock:
        _copier = paper().whales.follow(
            t,
            size=req.size,
            venue=req.copy_to,  # type: ignore[arg-type]
            max_slippage=req.max_slippage,
        )
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(_copier,), daemon=True)
    _thread.start()
    return following()


@app.delete("/api/follow")
def unfollow() -> dict[str, Any]:
    global _copier, _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=10)
    _thread = None
    _copier = None
    return {"following": None}


@app.get("/api/follow")
def following() -> dict[str, Any]:
    cp = _copier
    if cp is None:
        return {"following": None, "events": []}
    return {
        "following": cp.trader.to_dict(),
        "copy_to": cp.venue,
        "size": cp.size,
        "started": cp._started,
        "checked_s_ago": None if _checked_at is None else round(time.time() - _checked_at, 1),
        "error": _error,
        "events": [e.to_dict() for e in reversed(cp.events[-50:])],
    }


@app.get("/api/paper")
def paper_account() -> dict[str, Any]:
    with _lock:
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
    global _paper
    unfollow()
    with _lock:
        if _paper is not None:
            _paper.close()
            _paper = None
        (STORE_DIR / "paper.db").unlink(missing_ok=True)
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
