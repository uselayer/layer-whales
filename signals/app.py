"""The engine: a small HTTP service that runs the uselayer SDK, on your machine or hosted.

Locally, everything runs on your machine with your own keys; what it keeps (the Worth following check, price
history) is in ~/.uselayer/signals unless SIGNALS_STORE_DIR says otherwise.

    LAYER_API_KEY=lyr_... uv run signals            # then open http://127.0.0.1:8790

Hosted (SIGNALS_HOSTED=1, see the Dockerfile), it's the same reads: no signup, and keys stay on the server.
Signals places no orders: it shows what copying a trade would cost, and you buy on Polymarket yourself.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from uselayer import Client, VenueError, WhaleTrade
from uselayer.whale_scores import CATEGORY_TAGS, COPY_BETS, RECOMMENDED, SEGMENTS, Stat, category_of, copyable_verdict

from signals.directory import Directory, Seen, from_sighting

HERE = Path(__file__).parent
STORE_DIR = Path(os.environ.get("SIGNALS_STORE_DIR", Path.home() / ".uselayer" / "signals"))
_OLD_STORE_DIR = Path.home() / ".uselayer" / "whales-demo"  # before the rename to Signals: main() moves it once
HOSTED = os.environ.get("SIGNALS_HOSTED") == "1"
# The venues Signals reads. The SDK covers Kalshi and Polymarket; Signals is Polymarket-only for now, and adds
# Kalshi back with SIGNALS_VENUES=polymarket,kalshi. Every read passes this to the SDK, so a venue that's off
# is never called at all (not read and then hidden). Polymarket is always on: the Feed and Traders are built on it.
VENUES: tuple[str, ...] = ("polymarket", *(("kalshi",) if "kalshi" in os.environ.get("SIGNALS_VENUES", "").split(",") else ()))
VENUE_NAMES = {"polymarket": "Polymarket", "kalshi": "Kalshi"}


def venue_on(venue: str) -> None:
    """Refuse (404, a plain sentence) a venue Signals doesn't read right now."""
    if venue not in VENUES:
        raise HTTPException(404, detail=f"Signals covers {' and '.join(VENUE_NAMES[v] for v in VENUES)} only right now.")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    threading.Thread(target=_worth_loop, daemon=True).start()  # last result shows at once; refresh behind it
    threading.Thread(target=_live_loop, daemon=True).start()  # keeps the Feed's and alerts' trader reads filled
    _directory_later(_directory_import)  # the scores kept before the directory had them (once: then a no-op)
    if DIRECTORY_ON:  # finds wallets for the trader directory, once a day, at a rate that leaves the site its reads
        threading.Thread(target=_directory_loop, daemon=True).start()
    if TG_TOKEN:  # alerts: the bot's messages (connecting a chat, /list, /stop), and new buys of followed traders
        threading.Thread(target=_bot_loop, daemon=True).start()
        threading.Thread(target=_alert_loop, daemon=True).start()
    yield
    _worth_stop.set()
    _follow_stop.set()
    _live_stop.set()
    _directory_stop.set()


app = FastAPI(title="Signals", lifespan=lifespan)
reads = Client(store=":memory:")  # leaderboards, traders, links, cost previews: reads only


# ---- a small cache, so clicking around doesn't re-read the venues ----

_cache: dict[str, tuple[float, Any]] = {}
_flight: dict[str, threading.Lock] = {}  # one read at a time per key: concurrent misses share it
_flight_lock = threading.Lock()


def cached(key: str, ttl_s: float, fn: Callable[[], Any]) -> Any:
    """``fn()``, kept ``ttl_s`` seconds under ``key``. Concurrent misses on one key share a single read."""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    with _flight_lock:
        lock = _flight.setdefault(key, threading.Lock())
    with lock:
        hit = _cache.get(key)  # someone else may have just read it
        if hit and time.monotonic() - hit[0] < ttl_s:
            return hit[1]
        value = fn()
        _cache[key] = (time.monotonic(), value)
        return value


def venue_error(e: VenueError) -> HTTPException:
    return HTTPException(502, detail={"message": e.message, "hint": e.hint})


# ---- reads ----


@app.get("/api/config")
def config() -> dict[str, Any]:
    """What the page needs to know before it draws: which venues Signals reads (see VENUES), and Clerk's
    publishable key when signing in (to follow traders) is switched on. The key is public by design."""
    return {"venues": list(VENUES), "clerk_key": CLERK_KEY or None, "alerts": bool(TG_TOKEN)}


@app.get("/api/top")
def top(venue: str = "all", by: str = "pnl", limit: int = 25) -> list[dict[str, Any]]:
    if venue == "all" and len(VENUES) == 1:
        venue = VENUES[0]  # "all" is only the venues Signals reads: the SDK's "all" would read Kalshi too
    elif venue != "all":
        venue_on(venue)
    try:
        rows = cached(
            f"top:{venue}:{by}:{limit}", 120, lambda: reads.whales.top(venue, by=by, limit=limit)  # type: ignore[arg-type]
        )
    except VenueError as e:
        raise venue_error(e) from e
    return [t.to_dict() for t in rows]


@app.get("/api/trading-now")
def trading_now(min_usd: float = 250) -> list[dict[str, Any]]:
    """Recent large trades on the venues Signals reads, newest first (the Feed's Big trades). Named Kalshi
    traders here usually show their trades. A Polymarket trade carries its market's category, so the page can
    filter by your markets; the SDK has no categories for Kalshi markets, so theirs is None."""
    try:
        rows = cached(f"now:{min_usd}", 20, lambda: reads.whales.big_trades(min_usd, venues=VENUES, kalshi_pages=5))  # type: ignore[arg-type]
    except VenueError as e:
        raise venue_error(e) from e
    out = [t.to_dict() for t in rows[:100]]
    poly = [t["market"] for t in out if t["venue"] == "polymarket"]
    try:
        cats = _categories(poly) if poly else {}
    except VenueError:
        cats = {}  # the trades still show, under All
    _feed_note(rows[:100], cats)
    return [t | {"category": cats.get(t["market"]) if t["venue"] == "polymarket" else None} for t in out]


@app.get("/api/trader/{venue}/{trader_id}")
def trader(venue: str, trader_id: str) -> dict[str, Any]:
    venue_on(venue)
    try:
        d = cached(f"trader:{venue}:{trader_id}", 15, lambda: reads.whales.trader(venue, trader_id, trades=40, closed=50))  # type: ignore[arg-type]
    except VenueError as e:
        raise venue_error(e) from e
    out = d.to_dict()
    links = position_links(d.trader.id) if venue == "polymarket" and d.positions else {}
    out["positions"] = [
        p | {"status": bet_status(p["avg_price"], p["current_price"]), "url": links.get(p["market"])}
        for p in out["positions"]
    ]
    return out


POLYMARKET_EVENT = "https://polymarket.com/event/"


def polymarket_url(event: str | None, market: str | None = None) -> str | None:
    """The market's page on polymarket.com: its event, opened on the market when it's one of several."""
    if not event or event.startswith("0x"):  # a condition id stands in when the event is unknown
        return None
    return POLYMARKET_EVENT + event + (f"/{market}" if market and market != event else "")


def position_links(wallet: str) -> dict[str, str | None]:
    """Each open position's page on polymarket.com, by market id. uselayer's positions leave the slugs out,
    so this reads Polymarket's positions again; drop it once ``WhalePosition`` carries a url."""
    try:
        rows = cached(f"poslinks:{wallet}", 15, lambda: reads.whales._poly.positions(wallet, 50))
    except VenueError:
        return {}  # the trades still show, just without links
    return {
        f"{p.get('conditionId')}:{int(p.get('outcomeIndex') or 0)}": polymarket_url(p.get("eventSlug"), p.get("slug"))
        for p in rows
    }


@app.get("/api/links/{venue}/{trader_id}")
def links(venue: str, trader_id: str) -> dict[str, Any]:
    """Accounts on the other venue that may be the same person. Takes 5–30 s: it reads both venues, so it's
    off (404) unless Signals reads both."""
    venue_on(venue)
    if len(VENUES) < 2:
        raise HTTPException(404, detail="Signals covers one venue right now, so there's no other venue to check.")
    try:
        t = cached(f"trader:{venue}:{trader_id}", 15, lambda: reads.whales.trader(venue, trader_id, trades=40, closed=50))  # type: ignore[arg-type]
        found = cached(f"links:{venue}:{trader_id}", 600, lambda: reads.whales.links(t.trader))
    except VenueError as e:
        raise venue_error(e) from e
    return {"needs_layer_key": not os.environ.get("LAYER_API_KEY"), "links": [x.to_dict() for x in found]}


# ---- worth following (Polymarket) ----
# Finding and scoring ~200 wallets takes 20-30 minutes, so it runs in the background: on launch when the
# kept result is older than WORTH_MAX_AGE_S, then again each time it gets that old. The page shows the last
# result meanwhile. A wallet that isn't in it is scored when its page is opened.

WORTH_WALLETS = int(os.environ.get("SIGNALS_WORTH_WALLETS", "200"))
WORTH_MAX_AGE_S = 6 * 3600
WORTH_RETRY_S = 30 * 60  # after a failed check
WORTH_FORMAT = 7  # bump when the kept result gains fields; an older one is re-checked on launch (7: uselayer 0.8.1, copy_entry)
WORTH_MIN_AGE_HOSTED_S = 3600  # hosted: "Check again" only starts a new check once the last is this old
WORTH_ONE_KEEP = 500  # wallets scored on demand, kept until the next check
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
            _worth_data = _worth_upgrade(json.loads(worth_file().read_text()))
        except ValueError:
            _worth_data = None
    return _worth_data


def _worth_upgrade(data: dict[str, Any]) -> dict[str, Any]:
    """A result kept before uselayer 0.8.0, shown while the new check runs, judged by today's copy rule: its
    copy range widened to Student's t, ``copyable`` only when that range's bottom is above zero, and a proven or
    quiet sharp whose copies aren't reliably positive shown as the SDK now would (too fast to copy). Never
    looser than the kept verdicts: nothing is promoted."""
    if (data.get("format") or 0) >= 6:
        return data
    for s in data.get("scores", []):
        copy = Stat.from_dict(s["copy"]) if s.get("copy") else None
        s["copy"] = copy.to_dict() if copy else None
        if s.get("copyable") == "yes" and copyable_verdict(copy) != "yes":
            s["copyable"] = copyable_verdict(copy)
        if s.get("segment") in ("proven", "quiet") and s.get("copyable") != "yes":
            s["segment"], s["label"], s["action"] = "too_fast", SEGMENTS["too_fast"][0], SEGMENTS["too_fast"][1]
    return data


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
        _directory_later(lambda d: d.keep_scores(_with_format(data), "check", _iso_ts(data["finished"])))
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


# ---- the trader directory (phase 1: finding wallets; phase 2: keeping their scores; see directory.py) ----
# Every wallet Signals finds, and where, kept across runs: the SDK's sweep of every leaderboard and of markets
# at every level of activity, once a day, plus traders in the Feed's big trades, wallets opened that the last
# check didn't have, and followed wallets. Every score worked out is kept there too (the Worth following check's,
# a trader page's, a failed try, and on launch the last check's from worth-following.json), at no extra reads.
# Nothing is shown from it yet: the Worth following check still picks its own wallets and the page reads its file.
# /api/directory gives counts only. SIGNALS_DIRECTORY=0 turns the sweep off; scores are kept either way.

DIRECTORY_ON = os.environ.get("SIGNALS_DIRECTORY", "1") != "0"
DIRECTORY_SWEEP_S = 24 * 3600  # a new sweep once the last finished one is this old
DIRECTORY_RETRY_S = 3600  # after a sweep that failed
# The sweep's reads a second at most. The SDK allows 8 a second per Polymarket host, for everything the engine reads.
# The Feed's poller reads one trader at a time (4 reads, ~1.2 s), so it never needs more than ~3.3 a second; 3 here
# leaves room for pages too. The sweep runs at ~2 a second anyway: each read waits for its answer.
DIRECTORY_RATE = 3.0
DIRECTORY_HOLD_S = 30  # while the Worth following check runs (it can use the whole allowance), the sweep waits
DIRECTORY_BATCH = 2000  # sightings kept per write
DIRECTORY_STATS_TTL_S = 300
_directory_stop = threading.Event()
_directory_state: dict[str, Any] = {"running": False}
_directories: dict[Path, Directory] = {}
_feed_noted: set[str] = set()  # the Feed's trades already kept, so a cached read isn't kept again
# Pages hand their sightings to one writer thread: a sweep's batch holds the directory for up to ~0.3 s at full
# size, and no page should wait for that.
_directory_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="directory")


def directory() -> Directory:
    path = STORE_DIR / "directory.db"
    if path not in _directories:
        _directories[path] = Directory(path)
    return _directories[path]


def _directory_note(seen: list[Seen]) -> None:
    """Keeps sightings without ever failing a page: a directory that can't be written is left for the next sweep."""
    try:
        directory().add(seen)
    except (sqlite3.Error, OSError) as e:
        _directory_state["note_error"] = f"{type(e).__name__}: {e}"


def _directory_note_later(seen: list[Seen]) -> None:
    """``_directory_note`` on the writer thread, so the page answers without waiting for the directory."""
    _directory_writer.submit(_directory_note, seen)


def _directory_later(write: Callable[[Directory], Any]) -> None:
    """Any other write to the directory (a score, a failed try, the import) on the writer thread, in order with
    the sightings. It never fails a page or the check: what went wrong is kept for /api/directory."""

    def run() -> None:
        try:
            write(directory())
        except Exception as e:  # the page, the check and the next write go on
            _directory_state["note_error"] = f"{type(e).__name__}: {e}"

    _directory_writer.submit(run)


def _with_format(data: dict[str, Any]) -> list[dict[str, Any]]:
    """A kept check's scores, each with the check's format (which version of the score it is)."""
    return [s | {"format": data.get("format")} for s in data.get("scores", [])]


def _directory_import(d: Directory) -> None:
    """The scores the last check kept in worth-following.json, into the directory as of when that check finished.
    Each launch: the first time it adds them; after that it's the same score again, and the directory keeps the
    one it has (a newer check, or a trader page's, is never replaced)."""
    data = _worth_load()
    if data and data.get("scores") and data.get("finished"):
        d.keep_scores(_with_format(data), "import", _iso_ts(data["finished"]))


def _feed_note(trades: list[WhaleTrade], cats: dict[str, str]) -> None:
    """The Polymarket traders in the Feed's big trades, each trade kept once, by its market's category."""
    fresh = [t for t in trades if t.venue == "polymarket" and t.trader and t.trade_id not in _feed_noted]
    if not fresh:
        return
    if len(_feed_noted) > 5000:
        _feed_noted.clear()
    _feed_noted.update(t.trade_id for t in fresh)
    _directory_note_later([
        Seen(str(t.trader).lower(), "feed", cats.get(t.market) or "Other",
             name=None if not t.name or t.name.startswith("0x") else t.name, category=cats.get(t.market),
             example=t.market.partition(":")[0], example_title=t.title, trades=1, usd=t.usd,
             traded_at=t.at.timestamp())
        for t in fresh
    ])  # fmt: skip


def directory_sweep(stop: threading.Event | None = None) -> dict[str, Any]:
    """One sweep: followed wallets, then the SDK's ``find_wallets`` (every leaderboard, then markets of every
    size), kept as they come. While the Worth following check runs it waits, between reads (the SDK reads only as
    it's asked for the next wallet). Returns what it kept, how long it waited, and the error if it stopped early."""
    d = directory()
    halt = stop or threading.Event()
    started = d.sweep_started()
    print(f"directory sweep started ({DIRECTORY_RATE:g} reads a second at most)", flush=True)
    sightings = new = 0
    held = 0.0
    error: str | None = None
    batch: list[Seen] = []

    def keep() -> None:
        nonlocal sightings, new
        if batch:
            n = d.add(batch)
            d.sweep_progress(started, len(batch), n)
            sightings, new = sightings + len(batch), new + n
            batch.clear()

    try:
        batch += [Seen(w, "follow") for w in sorted({w for u in _load_follows()["users"].values() for w in u.get("follows") or []})]
        for s in reads.whales.find_wallets(per_second=DIRECTORY_RATE):
            if halt.is_set():
                error = "Stopped before it finished (the app was shutting down)."
                break
            batch.append(from_sighting(s))
            if len(batch) >= DIRECTORY_BATCH:
                keep()
            while _worth["running"] and not halt.is_set():  # before the next read: the check has the allowance
                _directory_state["held"] = "the Worth following check is running"
                halt.wait(DIRECTORY_HOLD_S)
                held += DIRECTORY_HOLD_S
            _directory_state["held"] = None
    except Exception as e:  # what it found stays; the next sweep starts over
        error = f"{type(e).__name__}: {e}"
    try:
        keep()  # the last batch, however the sweep ended
    except (sqlite3.Error, OSError) as e:
        error = error or f"{type(e).__name__}: {e}"
    d.sweep_finished(started, error)
    # one line for the logs (hosted: Railway's), so a sweep can be watched without the owner's /api/directory
    print(f"directory sweep {'stopped' if error else 'done'} in {(time.time() - started) / 60:.0f} min: "
          f"{sightings} sightings, {new} new wallets, held {held / 60:.0f} min{f', error: {error}' if error else ''}",
          flush=True)  # fmt: skip
    return {"sightings": sightings, "new": new, "held_s": held, "error": error}


def _directory_loop() -> None:
    """A sweep whenever the last finished one is DIRECTORY_SWEEP_S old, a minute after launch (the page's reads
    first); after a failed one, again in DIRECTORY_RETRY_S."""
    _directory_stop.wait(60)
    while not _directory_stop.is_set():
        try:
            last = directory().last_sweep()
            if not _worth["running"] and (last is None or time.time() - last["finished"] >= DIRECTORY_SWEEP_S):
                _directory_state["running"] = True
                try:
                    result = directory_sweep(_directory_stop)
                finally:
                    _directory_state["running"] = False
                if result["error"]:
                    _directory_stop.wait(DIRECTORY_RETRY_S)
                    continue
        except (sqlite3.Error, OSError) as e:
            _directory_state["error"] = f"{type(e).__name__}: {e}"
            _directory_stop.wait(DIRECTORY_RETRY_S)
            continue
        _directory_stop.wait(600)


@app.get("/api/directory")
def directory_stats(request: Request, response: Response) -> dict[str, Any]:
    """How many wallets the directory has found, and where: counts only, never a wallet. The owner's, as
    /api/metrics is: locally, or with SIGNALS_METRICS_KEY."""
    _owner(request)
    response.headers["Cache-Control"] = "private, no-store"
    stats = cached("directory", DIRECTORY_STATS_TTL_S, lambda: directory().stats())
    return stats | {"sweeping": _directory_state["running"], "held": _directory_state.get("held"),
                    "last_error": directory().last_error()}


# ---- the Traders leaderboard's columns ----
# How each trader trades, from what the last check kept: no new reads. A field the SDK provides itself
# (typical_usd, last_buy, from uselayer 0.6) wins; until then it's worked out here from the checked bets.
# Profit history (pnl_weeks, pnl_daily, periods) only comes from the SDK: the page shows "—" without it.

def _mock() -> bool:
    """Sample profit history to review the page: SIGNALS_MOCK=1, and only ever on your machine, never hosted."""
    return os.environ.get("SIGNALS_MOCK") == "1" and not HOSTED


def _median(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def _board(s: dict[str, Any]) -> dict[str, Any]:
    """The leaderboard's fields for one scored trader, added to (never replacing) what the score has."""
    bets = s.get("sample") or []
    out: dict[str, Any] = {}
    if s.get("settled") is None and bets:  # a wallet scored on demand: the check works these out, here we do
        settled = [b for b in bets if b.get("payout") is not None]
        out["won"], out["settled"] = sum(1 for b in settled if b["payout"] == 1), len(settled)
        out["odds"] = sum(b["price"] for b in settled) / len(settled) if settled else None
    if s.get("typical_usd") is None:
        out["typical_usd"] = _median([b.get("usd") for b in bets])
    if s.get("last_buy") is None:
        ats = [b["at"] for b in (s.get("recent") or []) + bets if b.get("at")]
        out["last_buy"] = max(ats) if ats else None
    window = s.get("window") or []
    if len(window) == 2:
        # The SDK reads at most 5,000 fills, so a busy trader's read can start after the window does: their
        # trades a day, usual trade and win record then cover only the days read (fills_from, uselayer 0.8).
        read_from = s.get("fills_from")
        if isinstance(read_from, str):
            read_from = _iso_ts(read_from)
        start = max(_iso_ts(window[0]), float(read_from or 0))
        days = max(1.0, (_iso_ts(window[1]) - start) / 86400)
        out["covered_days"] = round(days)
        if s.get("decisions") is not None:
            out["trades_per_day"] = round(s["decisions"] / days, 2)
    # What a copier paid a contract on the checked bets (uselayer 0.8.1: at the ask, the same bets as the copy
    # result). Before 0.8.1 there's only their own average price, over every buy in the window.
    price = s.get("copy_entry") or s.get("avg_price") or _median([b.get("price") for b in bets])
    copy = Stat.from_dict(s["copy"]) if s.get("copy") else None
    # What a copier a few minutes late kept after the fee, per dollar they paid: an estimate with its 95% range.
    # Only the range's bottom ranks a trader ("Copy estimate" sort), and only over COPY_BETS bets or more.
    per = (lambda x: x / price) if copy and price else None
    out["copy_return"] = per(copy.mean) if per else None
    out["copy_return_low"] = per(copy.low) if per else None
    out["copy_return_high"] = per(copy.high) if per else None
    out["copy_rank"] = out["copy_return_low"] if copy and copy.n >= COPY_BETS else None
    out["copy_ok"], out["copy_why"] = copy_offer(s)
    out["section"] = section_of(s.get("segment"), out["copy_ok"])
    if _mock() and s.get("pnl_weeks") is None:
        out |= _mock_history(s)
    out |= _consistency(out.get("pnl_weeks", s.get("pnl_weeks")))
    won, settled = out.get("won", s.get("won")), out.get("settled", s.get("settled"))
    # Win rate ranks by the bottom of its 95% range, and only from WIN_MIN settled trades: 1 of 1 isn't 100%.
    out["win_low"] = wilson_low(won, settled) if won is not None and settled and settled >= WIN_MIN else None
    return out


WIN_MIN = 10  # settled trades before a win rate can rank a trader
WEEKS_MIN = 4  # weeks of history before consistency can rank a trader


def wilson_low(k: int, n: int, z: float = 1.96) -> float:
    """The bottom of the 95% range of a rate of ``k`` in ``n`` (Wilson's): small samples rank low, not first."""
    p = k / n
    centre, spread = p + z * z / (2 * n), z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5
    return max(0.0, (centre - spread) / (1 + z * z / n))  # never below 0 by rounding


def _consistency(weeks: list[float | None] | None) -> dict[str, Any]:
    """Weeks in profit out of the weeks they have history for: a week before their first trade (None, from
    uselayer 0.8) is left out, never counted as a losing week. Ranks by the bottom of the 95% range, from
    WEEKS_MIN weeks."""
    if not isinstance(weeks, list):
        return {"weeks_up": None, "weeks_known": None, "consistency_low": None}
    known = [w for w in weeks if w is not None]
    up = sum(1 for w in known if w > 0)
    low = wilson_low(up, len(known)) if len(known) >= WEEKS_MIN else None
    return {"weeks_up": up, "weeks_known": len(known), "consistency_low": low}


COPY_TIERS = ("proven", "quiet", "rising")  # the recommended tiers copying can be offered for
SECTIONS = ("copy", "rising", "watch")  # Traders' sections, in order: Worth copying, Rising, Worth watching


def section_of(segment: str | None, copy_ok: bool) -> str | None:
    """Which section of Traders a recommended trader is listed in: ``"copy"`` (Worth copying) when copying is
    offered (``copy_offer``), else ``"rising"`` for Rising and ``"watch"`` (Worth watching) for No proven copy
    advantage: skilled traders worth keeping an eye on, whose copies don't reliably pay. None for a trader we
    don't recommend, who is only in "See all checked traders"."""
    if segment not in RECOMMENDED:
        return None
    if copy_ok:
        return "copy"
    return "rising" if segment == "rising" else "watch"


# A trader's copy status, in the same words everywhere (Traders, a trader's page, the Feed, the copy routes):
# Copyable; Copy not proven (a tier we recommend whose copies aren't reliably above zero); or Not recommended.
LABEL_COPYABLE, LABEL_NOT_PROVEN, LABEL_NOT_RECOMMENDED = "Copyable", "Copy not proven", "Not recommended"


def copy_offer(s: dict[str, Any]) -> tuple[bool, str | None]:
    """Whether Signals offers copying this trader, and if not, why: one plain sentence that starts with their
    status ("Copy not proven: …"). One rule for Traders, a trader's page, the Feed and the copy routes: a
    recommended tier (Proven sharp, Quiet sharp, Rising), and copies a few minutes late reliably above zero
    after fees (the SDK's ``copyable``: the bottom of the 95% range above zero). Never Sharp but too fast,
    Lucky, No clear edge, market makers or traders away."""
    seg = s.get("segment")
    if seg == "too_fast":
        return False, f"{LABEL_NOT_PROVEN}: the price moves before a copy gets in, so what copies kept after fees isn't reliably above zero."
    if seg == "inactive":
        return False, f"{LABEL_NOT_RECOMMENDED}: they haven't traded in the last 7 days."
    if seg not in COPY_TIERS:
        return False, f"{LABEL_NOT_RECOMMENDED}: only the traders we recommend can be copyable."
    if s.get("copyable") == "unknown":
        return False, f"{LABEL_NOT_PROVEN}: too few of their trades had prices after the buy to tell whether copies gain."
    if s.get("copyable") != "yes":
        return False, f"{LABEL_NOT_PROVEN}: what copies a few minutes late kept after fees isn't reliably above zero."
    return True, None


def _mock_history(s: dict[str, Any]) -> dict[str, Any]:
    """Made-up profit history in the SDK's shape, marked ``sample_data``. Never served hosted (see _mock)."""
    import random

    r = random.Random(s.get("wallet"))
    scale = abs(s.get("pnl_window") or 1000) / 4
    days = [scale / 7 * (r.random() * 2 - 0.7) for _ in range(90)]
    start = datetime.now(UTC) - timedelta(days=90)
    cum, daily = 0.0, [[start.date().isoformat(), 0.0]]  # as the SDK: 91 points, day 0 = 0, through today
    for i, d in enumerate(days, 1):
        cum += d
        daily.append([(start + timedelta(days=i)).date().isoformat(), round(cum, 2)])
    weeks = [round(sum(days[len(days) - 84 + 7 * i :][:7]), 2) for i in range(12)]  # the last 12 weeks, oldest first
    volume = (s.get("volume_window") or abs(s.get("pnl_window") or 1000) * 20) / 30
    periods = {f"{n}d": {"trading_pnl": round(sum(days[-n:]), 2), "volume": round(volume * n, 2),
                         "return_per_dollar": sum(days[-n:]) / (volume * n)} for n in (7, 30, 90)}  # fmt: skip
    return {"pnl_weeks": weeks, "pnl_daily": daily, "periods": periods, "sample_data": True}


@app.get("/api/worth")
def worth() -> dict[str, Any]:
    """Every scored wallet without its bets, plus whether a refresh is running."""
    data = _worth_load()
    out: dict[str, Any] = {"status": dict(_worth), "result": None}
    if data is not None:
        out["result"] = {k: v for k, v in data.items() if k != "scores"} | {
            "scores": [{k: v for k, v in s.items() if k != "sample"} | _board(s) for s in data["scores"]]
        }
    return out


@app.post("/api/worth/refresh")
def worth_refresh_route() -> dict[str, Any]:
    if not HOSTED or _worth_age_s() >= WORTH_MIN_AGE_HOSTED_S:  # hosted, anyone can press it: not too often
        worth_refresh()
    return {"status": dict(_worth)}


@app.get("/api/worth/{wallet}")
def worth_wallet(wallet: str) -> dict[str, Any]:
    """One wallet's score with its checked bets: from the last refresh, or scored now (5–30 s)."""
    w = wallet.lower()
    data = _worth_load()
    s = next((x for x in (data or {}).get("scores", []) if x["wallet"] == w), None)
    if s is None:
        if _WALLET.fullmatch(w):  # a wallet the last check didn't have, opened here: pasted into search, mostly
            _directory_note_later([Seen(w, "search")])
        s = _score_of(w)
    b = _board(s)
    return with_links(s) | b | {"why": why_lines(s | b)}


# ---- "Why they're here" on a trader's page: plain lines, from what the check kept ----
# The page adds "Lately" (their last buys, read fresh); the numbers and method stay in "How we measure this".


def _usd(x: float, signed: bool = False) -> str:
    """Dollars as the page shows them: $855, $1,000, $1.2K, $3.4M; never "$1000K" (that's $1M)."""
    a = abs(x)
    if a >= 999_500:  # would round to 1000K
        s = f"{a / 1e6:.{0 if a >= 1e8 else 1}f}M"
    elif a >= 1e3:
        s = f"{a / 1e3:.{0 if a >= 1e5 else 1}f}K"
    else:
        s = f"{round(a):,}" if a >= 10 else f"{a:.2f}"
    s = re.sub(r"\.0([KM])$", r"\1", s)
    return f"{'−' if x < 0 else '+' if signed and x > 0 else ''}${s}"


def _per_day(n: float) -> str:
    """New markets a day, or a week when it's under one a day (the page's perDayText); never a made-up one."""
    k, unit = (round(n), "day") if n >= 1 else (round(n * 7), "week")
    if not k:
        return "fewer than one new market a week"
    return f"{k} new market{'' if k == 1 else 's'} a {unit}"


def _trade_words(t: str) -> str:
    """Sentences the SDK writes say "bet"; Signals says "trade"."""
    return re.sub(r"\b([Bb])et(s?)\b", lambda m: ("Trade" if m.group(1) == "B" else "trade") + m.group(2), t)


def why_lines(s: dict[str, Any]) -> dict[str, str | None]:
    """Four of the five lines of "Why they're here" (the page adds Lately), each a short plain sentence, or
    None when the data doesn't say. ``s`` is a score with its leaderboard fields (``_board``)."""
    cats = [c for c in s.get("categories") or [] if c.get("bets")]
    total = sum(c["bets"] for c in cats)
    shares = [(c["category"], round(100 * c["bets"] / total)) for c in cats[:3]] if total else []
    shares = [x for i, x in enumerate(shares) if i == 0 or x[1] >= 5]  # a 2% sideline isn't what they trade
    trades = ", ".join(f"{c} {p}%" for c, p in shares) or None

    band = s.get("price_band")
    how = [f"buys around {_cents(band[0]).rstrip('¢')}–{_cents(band[1])}" if isinstance(band, list) and len(band) == 2
           else f"pays {_cents(s['avg_price'])} on average" if s.get("avg_price") is not None else None,
           f"usually {_usd(s['typical_usd'])}" if s.get("typical_usd") is not None else None,
           _per_day(s["trades_per_day"]) if s.get("trades_per_day") is not None else None,
           f"holds {round(s['held_share'] * 100)}% of closed positions until the market settles" if s.get("held_share") is not None else None]  # fmt: skip
    how_line = ", ".join(x for x in how if x)

    edge_n = (s.get("edge") or {}).get("n")
    over = f" (over {edge_n} trades)" if edge_n else ""
    section = s.get("section")
    if s.get("segment") == "rising" or section == "rising":  # hedged, even when copying is offered: few trades yet
        listed = f"Early signs: the price has tended to move their way after they buy{f', over {edge_n} trades so far' if edge_n else ''}, too few to be sure yet."
    elif section == "copy":
        listed = (f"The price tended to move their way within minutes after they bought{over}, and in the trades we"
                  " checked it was still moving when a copy a few minutes late got in.")  # fmt: skip
    elif section == "watch":
        listed = f"The price tends to move their way within minutes after they buy{over}, though usually before a copy could get in."
    else:
        listed = f"Not on our list: {_trade_words(s['reason'])}" if s.get("reason") else "Not on our list."
    record = []
    profit = ((s.get("periods") or {}).get("30d") or {}).get("trading_pnl")
    if profit is None:
        profit = s.get("trading_pnl_window")  # as the page's profitOf: trading profit only, never Polymarket's figure
    if profit is not None:
        record.append(f"trading profit {_usd(profit, True)} in the last 30 days")
    if s.get("weeks_known"):
        weeks = "the last 12 weeks" if s["weeks_known"] >= 12 else f"the {s['weeks_known']} weeks since their first trade"
        record.append(f"up in {s['weeks_up']} of {weeks}")
    if record:
        listed += " " + "; ".join(record)[0].upper() + "; ".join(record)[1:] + "."

    ok, why = s.get("copy_ok"), s.get("copy_why")
    copying = (f"{LABEL_COPYABLE}: copies a few minutes late were reliably above zero after fees in the trades we checked; a reading of the past, not a promise."
               if ok else why or f"{LABEL_NOT_RECOMMENDED}.")  # fmt: skip
    return {"trades": trades, "how": how_line[:1].upper() + how_line[1:] + "." if how_line else None,
            "listed": listed, "copying": copying}  # fmt: skip


def _score_of(wallet: str) -> dict[str, Any]:
    """A wallet's score: from the last check, or scored now (5–30 s) and kept until the next check."""
    w = wallet.lower()
    for s in (_worth_load() or {}).get("scores", []):
        if s["wallet"] == w:
            return s  # type: ignore[no-any-return]
    if w not in _worth_one:
        try:
            _worth_one[w] = reads.whales.score(w).to_dict()
        except VenueError as e:
            if _WALLET.fullmatch(w):
                why = f"{type(e).__name__}: {e}"
                _directory_later(lambda d: d.score_failed(w, why))
            raise venue_error(e) from e
        if _WALLET.fullmatch(w):
            scored = _worth_one[w] | {"format": WORTH_FORMAT}
            _directory_later(lambda d: d.keep_scores([scored], "page"))
        if len(_worth_one) > WORTH_ONE_KEEP:
            del _worth_one[next(iter(_worth_one))]  # the oldest
    return _worth_one[w]


def refuse_unless_copy_offered(venue: str, wallet: str) -> None:
    """The cost preview's check (400, a plain sentence): a Polymarket trader's trade is priced for copying only
    when ``copy_offer`` says so, the same rule the page shows. Kalshi traders have no score, so nothing to check them against."""
    if venue != "polymarket":
        return
    ok, why = copy_offer(_score_of(wallet))
    if not ok:
        raise HTTPException(400, detail=why)


ACCOUNT_TTL_S = 60
_WALLET = re.compile(r"0x[0-9a-f]{40}")


@app.get("/api/account/{wallet}")
def account(wallet: str) -> dict[str, Any]:
    """A Polymarket wallet's account now: open positions and cash, with when they were read. ``total_usd`` only
    when both parts were read (``complete``); ``missing`` names a part that couldn't be. Kept for a minute."""
    w = wallet.lower()
    if not _WALLET.fullmatch(w):
        raise HTTPException(400, detail="That isn't a Polymarket wallet address.")
    return cached(f"account:{w}", ACCOUNT_TTL_S, lambda: reads.whales.account(w).to_dict())  # type: ignore[no-any-return]


def with_links(s: dict[str, Any]) -> dict[str, Any]:
    """A score whose checked bets each link to their event on polymarket.com."""
    return s | {k: [b | {"url": polymarket_url(b.get("event"))} for b in s.get(k) or []] for k in ("recent", "sample")}


# ---- live bets from traders worth following ----
# Their buys over the last day, one row per bet (fills on the same outcome merged), with the price now:
# the current price of their open position. Polymarket's public trade feed runs a few minutes behind.

FEED_SEGMENTS = RECOMMENDED  # Traders' three sections: Worth copying, Rising, Worth watching ("inactive" isn't in it)
RECOMMENDED_MIN_USD = 1000.0  # the Feed's Recommended shows only trades this big: fewer, bigger trades
FEED_HOURS = 24
GET_IN = 0.03  # at most this far above their price you can still get in: the cost preview's limit too
FELL_BY = 0.05  # at most this far below: a bigger drop means the market turned against them, a different trade
SETTLED_LOW, SETTLED_HIGH = 0.02, 0.98  # at or past these the market has settled, or as good as


def bet_status(paid: float | None, now: float | None) -> str | None:
    """Can you still get in near what they paid?

    ``in`` (from 5¢ below to 3¢ above), ``moved`` (further either way), ``won`` / ``lost`` (the price is at
    98¢+ or 2¢-, so the market has settled or as good as: not a bargain), ``closed`` (their position is gone:
    sold, or settled and paid out), or None when their price isn't known.
    """
    if now is None:
        return "closed"
    if now <= SETTLED_LOW:
        return "lost"
    if now >= SETTLED_HIGH:
        return "won"
    if paid is None:
        return None
    return "in" if -FELL_BY - 1e-9 <= now - paid <= GET_IN + 1e-9 else "moved"


def why_not_copy(paid: float, now: float | None) -> str | None:
    """Why buying now at ``now`` isn't copying their trade at ``paid``, in a plain sentence, or None when you
    can still get in. The same rule as the Feed's status line (``bet_status``), so the engine never prices a
    trade the page wouldn't offer: settled (2¢ or less, 98¢ or more), or moved away either way."""
    status = bet_status(paid, now)
    if status == "in":
        return None
    if status == "won":
        return "The price is 98¢ or more: this market has settled, or as good as. There's nothing left to copy."
    if status == "lost":
        return "The price is 2¢ or less: this market has settled against them, or as good as. Not a bargain."
    if now is None:  # "closed": nothing to price it at
        return "There's no price for this market right now."
    gap = f"{max(1, round(abs(now - paid) * 100))}¢"
    if now < paid:
        return (f"The price is {gap} below what they paid: the market has turned against them since. Buying now "
                "would be a different trade from theirs.")  # fmt: skip
    return f"The price is {gap} above what they paid, more than 3¢: most of their edge is already in the price."


def _categories(markets: list[str]) -> dict[str, str]:
    """Each Polymarket market's category (Sports, Politics…), read in one batch and kept by the SDK."""
    book = reads.whales.scorer().markets.get(m.partition(":")[0] for m in markets)
    return {m: category_of(book[c]) for m in markets if (c := m.partition(":")[0]) in book}


LIVE_POLL_S = 30  # the poller refreshes every Recommended and followed trader this often, one at a time
LIVE_STALE_S = 120  # pages read what the poller keeps up to this old; only a miss reads Polymarket itself
LIVE_ALERT_S = 60  # alerts want it fresher
LIVE_KEEP_S = 300  # a trader nobody recommends or follows any more drops out of the cache after this


def _live_read(wallet: str, max_age: float = LIVE_STALE_S) -> Any:
    """A trader's last 100 trades and open positions: what the poller keeps (_live_loop), read from Polymarket
    only when it's missing or older than ``max_age`` (and then once, however many ask at the same time)."""
    return cached(f"live:{wallet}", max_age, lambda: reads.whales.trader("polymarket", wallet, trades=100, closed=0))


def _live_wanted() -> set[str]:
    """The traders whose trades the Feed and alerts read: Recommended's, and everyone someone follows."""
    rec = {s["wallet"] for s in (_worth_load() or {}).get("scores", []) if s["segment"] in FEED_SEGMENTS}
    return rec | {w for u in _load_follows()["users"].values() for w in u.get("follows") or []}


def live_poll_once(stop: threading.Event | None = None) -> int:
    """One round of the poller: each wanted trader read in turn (never in a burst: the SDK's Polymarket budget is
    shared), skipping one read in the last LIVE_POLL_S; then traders nobody wants any more are dropped."""
    wanted, done = _live_wanted(), 0
    for w in sorted(wanted):
        if stop is not None and stop.is_set():
            break
        try:
            _live_read(w, LIVE_POLL_S - 5)
            done += 1
        except VenueError:
            continue
    now = time.monotonic()
    for key in [k for k in list(_cache) if k.startswith("live:")]:
        hit = _cache.get(key)
        if key[5:] not in wanted and hit and now - hit[0] > LIVE_KEEP_S:
            _cache.pop(key, None)
    return done


def _live_loop() -> None:
    while not _live_stop.is_set():
        started = time.monotonic()
        try:
            live_poll_once(_live_stop)
        except Exception as e:  # keep the cache filled through a bad round
            _worth["live_error"] = f"{type(e).__name__}: {e}"
        _live_stop.wait(max(5.0, LIVE_POLL_S - (time.monotonic() - started)))


_live_stop = threading.Event()


def _bets_of(d: Any, who: dict[str, Any], since: datetime | None = None) -> list[dict[str, Any]]:
    """A trader's buys since ``since``, one per outcome (fills merged: ``price`` is what they paid on average),
    with the price now: their open position's. ``who`` gives the wallet, name, segment, label and copy_ok."""
    now = {p.market: p.current_price for p in d.positions}
    one: dict[str, dict[str, Any]] = {}
    for t in d.trades:
        if t.action != "buy" or (since and t.at < since) or not t.size:
            continue
        b = one.setdefault(
            t.market,
            {"wallet": who["wallet"], "name": who.get("name"), "segment": who.get("segment"), "label": who.get("label"),
             "copy_ok": who.get("copy_ok", False), "market": t.market, "title": t.title, "outcome": t.outcome, "url": t.url,
             "at": t.at, "usd": 0.0, "size": 0.0, "price_now": now.get(t.market), "token": t.token},
        )  # fmt: skip
        b["at"], b["usd"], b["size"] = max(b["at"], t.at), b["usd"] + t.usd, b["size"] + t.size
    return [b | {"price": b["usd"] / b["size"], "at": b["at"].isoformat()} for b in one.values()]


@app.get("/api/bet/{wallet}/{market}")
def one_bet(wallet: str, market: str) -> dict[str, Any]:
    """One trader's buy of one outcome, as a Feed card has it (status, price now, copy_ok): what an alert's
    "Check current price" opens. From their last 100 trades; 404 when it isn't among them."""
    w = wallet.lower()
    if not _WALLET.fullmatch(w):
        raise HTTPException(400, detail="That isn't a Polymarket wallet address.")
    s = next((x for x in (_worth_load() or {}).get("scores", []) if x["wallet"] == w), None)
    try:
        d = _live_read(w)
    except VenueError as e:
        raise venue_error(e) from e
    who = {"wallet": w, "name": (s or {}).get("name") or d.trader.name, "segment": (s or {}).get("segment"),
           "label": (s or {}).get("label"), "copy_ok": s is not None and copy_offer(s)[0]}  # fmt: skip
    b = next((x for x in _bets_of(d, who) if x["market"] == market), None)
    if b is None:
        raise HTTPException(404, detail="That trade isn't among their recent trades any more.")
    try:
        cat = _categories([market]).get(market)
    except VenueError:
        cat = None
    return b | {"status": bet_status(b["price"], b["price_now"]), "category": cat}


@app.get("/api/live")
def live() -> dict[str, Any]:
    """The Feed's Recommended: trades of RECOMMENDED_MIN_USD or more over the last FEED_HOURS from the traders in
    Traders' three sections (Worth copying, Rising, Worth watching). Quiet on purpose: fewer, bigger trades."""
    data = _worth_load()
    people = [s | {"copy_ok": copy_offer(s)[0]} for s in (data or {}).get("scores", []) if s["segment"] in FEED_SEGMENTS]
    bets, failed = _feed(people, RECOMMENDED_MIN_USD)
    return {"traders": [{k: s[k] for k in ("wallet", "name", "segment", "label", "copy_ok")} for s in people],
            "failed": failed, "hours": FEED_HOURS, "min_usd": RECOMMENDED_MIN_USD, "bets": bets}  # fmt: skip


def _feed(people: list[dict[str, Any]], min_usd: float = 0.0) -> tuple[list[dict[str, Any]], int]:
    """These traders' buys over the last FEED_HOURS (one per outcome, ``min_usd`` or more), newest first, with
    status and category; and how many traders couldn't be read."""
    since = datetime.now(UTC) - timedelta(hours=FEED_HOURS)
    bets: list[dict[str, Any]] = []
    failed = 0

    for s in people:  # from what the poller keeps: a page view never reads Polymarket for dozens of traders
        try:
            d = _live_read(s["wallet"])
        except VenueError:
            failed += 1
            continue
        if not s.get("name"):
            s["name"] = d.trader.name  # a followed wallet we haven't scored: their Polymarket name, as on their cards
        bets += [b for b in _bets_of(d, s, since) if b["usd"] >= min_usd]
    bets.sort(key=lambda b: b["at"], reverse=True)
    try:
        cats = _categories([b["market"] for b in bets]) if bets else {}
    except VenueError:
        cats = {}  # the trades still show, under All
    for b in bets:
        b["status"], b["category"] = bet_status(b["price"], b["price_now"]), cats.get(b["market"])
    return bets, failed


def _iso_ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


# ---- what copying one trade would cost now (a Feed trade the trader already made) ----
# A preview only: it reads Polymarket's order book and sends nothing. Signals places no orders and keeps no
# sandbox: you buy on Polymarket yourself.

PREVIEW_SIZE = 5  # contracts, when the page sends no amount


class OneTrade(BaseModel):
    """A Feed trade: their buys of one outcome, merged (``price`` is what they paid on average)."""

    wallet: str
    name: str | None = None
    market: str  # <conditionId>:<outcome>
    price: float
    title: str | None = None
    outcome: str | None = None
    url: str | None = None
    at: str | None = None
    size: float | None = None  # their contracts and dollars on this outcome (the Feed card's numbers)
    usd: float | None = None
    token: str | None = None  # the exact outcome token they bought: the preview matches it first
    spend: float | None = None  # dollars to put in, fee included; None: PREVIEW_SIZE contracts


def _their_trade(req: OneTrade) -> WhaleTrade:
    at = datetime.fromisoformat(req.at) if req.at else datetime.now(UTC)
    return WhaleTrade(
        venue="polymarket", trader=req.wallet, name=req.name, market=req.market, side="yes", action="buy",
        price=req.price, size=req.size or 0.0, usd=req.usd or 0.0, at=at, trade_id=f"feed:{req.wallet}:{req.market}:{req.at}",
        title=req.title, outcome=req.outcome, url=req.url, token=req.token,
    )  # fmt: skip


@app.post("/api/trade/preview")
def trade_preview(req: OneTrade) -> dict[str, Any]:
    """What buying this trade now would cost and pay, on the same Polymarket market, priced from the order
    book up to 3¢ above their price. Refused (400, a plain sentence) when the price is one you can't still get
    in at: settled, more than 5¢ below theirs or more than 3¢ above (``why_not_copy``); the SDK itself only
    refuses a price above theirs plus ``max_slippage``. Sends nothing."""
    if req.spend is not None and req.spend <= 0:
        raise HTTPException(400, detail="Enter an amount above $0.")
    size: dict[str, Any] = {"spend": req.spend} if req.spend else {"size": float(PREVIEW_SIZE)}
    refuse_unless_copy_offered("polymarket", req.wallet)
    try:
        p = reads.whales.preview_copy(_their_trade(req), **size, max_slippage=GET_IN)
    except VenueError as e:
        raise venue_error(e) from e
    if p.ok and (why := why_not_copy(req.price, p.price)):
        raise HTTPException(400, detail=why)
    return p.to_dict() | {"best_price": _best_price(req, p)}  # type: ignore[no-any-return]


def _best_price(req: OneTrade, p: Any) -> float | None:
    """The cheapest price for sale now, to name the slippage (your average fill against it). A uselayer release
    whose CopyPreview has ``best_price`` (the best ask, before the fee) gives it; uselayer 0.8.1's doesn't, so then:
    when every contract filled at one level, the average is the best price; when the walk went deeper, a
    one-contract preview's price is the best ask (the SDK returns it even when one contract is under the market's
    smallest order). None when the order book couldn't be read."""
    if not p.ok or not getattr(p, "depth_checked", False) or p.price is None:
        return None
    if getattr(p, "best_price", None) is not None:  # the SDK says it: no second read
        return float(p.best_price)
    if getattr(p, "worst_price", None) is None or p.worst_price <= p.price + 1e-9:
        return float(p.price)
    try:
        q = reads.whales.preview_copy(_their_trade(req), size=1.0, max_slippage=GET_IN)
    except VenueError:
        return None
    return float(q.price) if q.depth_checked and q.price is not None and q.price <= p.price + 1e-9 else None


# ---- following traders: sign in with Clerk (Google or Telegram), alerts on Telegram ----
# Browsing needs no account. Following a trader is the one thing that asks you to sign in, through Clerk:
# Google, or Telegram as a custom OpenID Connect provider, both set up in Clerk's dashboard. The page sends
# Clerk's short-lived session token as a Bearer header and the engine checks it against Clerk's public keys,
# so no Clerk secret lives here. Alerts go to Telegram: "Connect Telegram" gives a one-time link to our bot,
# Start there connects that chat, and it gets a message when someone you follow buys, whatever the amount and
# whoever they are (alert_pass: what they bought, the price now and when they traded, grouped when several buy the
# same outcome, at most DAILY_CAP a day), and for big trades in the markets you saved (whale_pass). Sign-in is off unless CLERK_PUBLISHABLE_KEY is set, alerts unless
# TELEGRAM_BOT_TOKEN is. Nothing here places an order.

CLERK_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SITE = os.environ.get("SIGNALS_SITE", "https://usesignals.app")
MAX_FOLLOWS = 20
CODE_TTL_S = 15 * 60  # a "Connect Telegram" link works this long, once
ALERT_EVERY_S = 30
ALERT_MAX_AGE_S = 30 * 60  # a buy first seen later than this isn't alerted (Polymarket shows trades minutes late)
ALERT_AGAIN_S = 6 * 3600  # one alert per trader and market in this long, however many fills
DATA_DELAY = "Polymarket's public data runs 3–5 minutes behind."  # said on every alert, and on the Feed
DAILY_CAP = 20  # alerts per person per UTC day (a group counts once); then one "limit reached" message
CAP_TEXT = "You've reached today's alert limit; more tomorrow."
GROUP_S = 6 * 3600  # 2+ traders you follow buying the same outcome within this long make one grouped alert
GROUP_FAST_S = 3600  # the same, for markets that settle fast
FAST_CATEGORIES = ("Sports", "Esports")
WHALE_USD = 10_000.0  # a buy this big in your chosen markets alerts you, whoever the trader
WHALE_DAILY = 5  # of the DAILY_CAP, at most this many a day are big trades, so they can't crowd out your follows
# The markets a person can pick (onboarding offers the first four): the SDK's categories.
INTEREST_CHOICES = ("Politics", "Sports", "Crypto", "Esports", *(c for c, _ in CATEGORY_TAGS if c not in
                    ("Politics", "Sports", "Crypto", "Esports")), "Other")  # fmt: skip
_follow_lock = threading.Lock()  # one change to follows.json at a time
_follow_stop = threading.Event()
_codes: dict[str, tuple[str, float]] = {}  # one-time "Connect Telegram" codes: code -> (Clerk user id, made at)
_tg: dict[str, Any] = {"bot": None, "offset": 0, "error": None}
_seen: dict[str, set[str]] = {}  # per followed trader: trade ids already looked at
_alerted: dict[tuple[str, str], float] = {}  # (trader, market) -> when it was last alerted


def clerk_frontend_api(key: str) -> str | None:
    """The Frontend API host a publishable key names: pk_live_ + base64 of "clerk.example.com$"."""
    m = re.fullmatch(r"pk_(?:test|live)_([A-Za-z0-9+/=_-]+)", key)
    if not m:
        return None
    raw = m.group(1)
    try:
        host = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode()
    except ValueError:
        return None
    return host[:-1] if host.endswith("$") and len(host) > 1 else None


_jwks: jwt.PyJWKClient | None = None


def _signing_key(token: str) -> Any:
    """Clerk's public key for this token, from the Frontend API's JWKS (fetched once, then cached)."""
    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(f"https://{clerk_frontend_api(CLERK_KEY)}/.well-known/jwks.json", cache_keys=True)
    return _jwks.get_signing_key_from_jwt(token).key


def _origins() -> set[str]:
    """Where a session token may come from (its ``azp``): the site, and this machine when running locally."""
    port = os.environ.get("SIGNALS_PORT", "8790")
    return {SITE} | (set() if HOSTED else {f"http://127.0.0.1:{port}", f"http://localhost:{port}"})


def signed_in(request: Request) -> str:
    """The signed-in user's Clerk id, from the page's ``Authorization: Bearer`` session token, or 401 with a
    plain sentence. Checked here: the signature (Clerk's public keys, RS256), expiry, issuer, origin."""
    fapi = clerk_frontend_api(CLERK_KEY)
    if not fapi:
        raise HTTPException(404, detail="Signing in isn't switched on here.")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not token:
        raise HTTPException(401, detail="Sign in to follow traders.")
    try:
        claims = jwt.decode(token, _signing_key(token), algorithms=["RS256"], issuer=f"https://{fapi}",
                            options={"require": ["exp", "iat", "sub"]}, leeway=5)  # fmt: skip
    except jwt.PyJWKClientConnectionError as e:
        raise HTTPException(503, detail="Couldn't check your sign-in just now. Try again in a minute.") from e
    except jwt.PyJWTError as e:
        raise HTTPException(401, detail="Your sign-in has expired. Sign in again.") from e
    if claims.get("azp") and claims["azp"] not in _origins():
        raise HTTPException(401, detail="Sign in on this site to follow traders.")
    return str(claims["sub"])


def follows_file() -> Path:
    """Who follows whom: {"users": {Clerk user id: {"follows": [wallet, ...], "chat": Telegram chat id}}}."""
    return STORE_DIR / "follows.json"


def _load_follows() -> dict[str, Any]:
    try:
        d = json.loads(follows_file().read_text())
    except (OSError, ValueError):
        d = {}
    return {"users": d.get("users") or {}}


def _save_follows(d: dict[str, Any]) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = follows_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(follows_file())  # whole or not at all


def _user(d: dict[str, Any], uid: str) -> dict[str, Any]:
    u: dict[str, Any] = d["users"].setdefault(uid, {"follows": [], "chat": None})
    return u


def _private(response: Response) -> None:
    """An answer about one signed-in person: never cached in front of us."""
    response.headers["Cache-Control"] = "private, no-store"


def _me_view(u: dict[str, Any]) -> dict[str, Any]:
    """``interests``: the markets you picked, saved to your account (None until you've saved any; [] is all
    markets). Big-trade alerts go only to people who have saved them."""
    return {"follows": list(u.get("follows") or []), "telegram": u.get("chat") is not None, "alerts": bool(TG_TOKEN),
            "interests": u.get("interests")}  # fmt: skip


class Follow(BaseModel):
    wallet: str


class Interests(BaseModel):
    interests: list[str]


@app.put("/api/me/interests")
def save_interests(req: Interests, response: Response, uid: str = Depends(signed_in)) -> dict[str, Any]:
    """The markets you picked (onboarding, Traders' chips, the Feed's Change), kept on your account so big
    trades there can alert you. [] means all markets."""
    _private(response)
    bad = [c for c in req.interests if c not in INTEREST_CHOICES]
    if bad:
        raise HTTPException(400, detail=f"{bad[0][:40]} isn't one of the markets you can pick.")
    picked = [c for c in INTEREST_CHOICES if c in req.interests]
    with _follow_lock:
        d = _load_follows()
        u = _user(d, uid)
        u["interests"] = picked
        _save_follows(d)
    return _me_view(u)


@app.get("/api/me")
def me(response: Response, uid: str = Depends(signed_in)) -> dict[str, Any]:
    """Who you follow, and whether Telegram alerts are connected."""
    _private(response)
    return _me_view(_load_follows()["users"].get(uid) or {})


@app.post("/api/me/follows")
def follow(req: Follow, response: Response, uid: str = Depends(signed_in)) -> dict[str, Any]:
    _private(response)
    w = req.wallet.lower()
    if not _WALLET.fullmatch(w):
        raise HTTPException(400, detail="That isn't a Polymarket wallet address.")
    with _follow_lock:
        d = _load_follows()
        u = _user(d, uid)
        if w not in u["follows"]:
            if len(u["follows"]) >= MAX_FOLLOWS:
                raise HTTPException(400, detail=f"You can follow up to {MAX_FOLLOWS} traders. Unfollow one to add another.")
            u["follows"].append(w)
            _save_follows(d)
            count("follow", uid=uid, trader=w)
            _directory_note_later([Seen(w, "follow")])  # the wallet only: who follows them stays in follows.json
    return _me_view(u)


@app.delete("/api/me/follows/{wallet}")
def unfollow(wallet: str, response: Response, uid: str = Depends(signed_in)) -> dict[str, Any]:
    _private(response)
    with _follow_lock:
        d = _load_follows()
        u = _user(d, uid)
        if wallet.lower() in u["follows"]:
            u["follows"].remove(wallet.lower())
            _save_follows(d)
            count("unfollow", uid=uid, trader=wallet.lower())
    return _me_view(u)


@app.get("/api/me/feed")
def my_feed(response: Response, uid: str = Depends(signed_in)) -> dict[str, Any]:
    """The Feed's Following: your followed traders' buys over the last FEED_HOURS, every size. Whoever they are:
    a trader the last check didn't score has no tier, and copying isn't offered for them (copy_ok False)."""
    _private(response)
    follows = list((_load_follows()["users"].get(uid) or {}).get("follows") or [])
    scores = {s["wallet"]: s for s in (_worth_load() or {}).get("scores", [])}
    people = [{"wallet": w, "name": (s := scores.get(w) or {}).get("name"), "segment": s.get("segment"),
               "label": s.get("label"), "copy_ok": bool(s) and copy_offer(s)[0]} for w in follows]  # fmt: skip
    bets, failed = _feed(people)
    return {"traders": people, "failed": failed, "hours": FEED_HOURS, "bets": bets}


@app.post("/api/me/telegram")
def connect_telegram(response: Response, uid: str = Depends(signed_in)) -> dict[str, Any]:
    """A one-time link to our Telegram bot: Start there connects that chat to your alerts (CODE_TTL_S)."""
    _private(response)
    if not TG_TOKEN:
        raise HTTPException(404, detail="Alerts aren't switched on here yet.")
    bot = _bot_name()
    if not bot:
        raise HTTPException(503, detail="Telegram isn't answering right now. Try again in a minute.")
    now = time.time()
    for c in [c for c, (_, at) in _codes.items() if now - at > CODE_TTL_S]:
        _codes.pop(c, None)
    code = secrets.token_urlsafe(12)  # 16 characters, all allowed in a Telegram start link
    _codes[code] = (uid, now)
    return {"link": f"https://t.me/{bot}?start={code}"}


def tg(method: str, **params: Any) -> Any:
    """One Telegram Bot API call. A failure raises RuntimeError with Telegram's own words, never the token."""
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=json.dumps(params).encode(),
                                 headers={"content-type": "application/json"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=float(params.get("timeout", 0)) + 15) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except ValueError:
            body = {"description": f"HTTP {e.code}"}
    except OSError as e:  # no answer: URLError, a timeout, a dropped connection
        raise RuntimeError(f"Telegram didn't answer ({type(e).__name__})") from None
    if not body.get("ok"):
        raise RuntimeError(f"Telegram: {body.get('description') or 'refused'}")
    return body.get("result")


def _bot_name() -> str | None:
    if _tg["bot"] is None:
        try:
            _tg["bot"] = tg("getMe")["username"]
        except (RuntimeError, KeyError, TypeError):
            return None
    return _tg["bot"]  # type: ignore[no-any-return]


def _say(chat: str, text: str, **kw: Any) -> Any:
    """Send one message; Telegram's answer (the message, with its ``message_id``)."""
    return tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True, **kw)


def _who(wallet: str) -> str:
    """A trader's name as the page shows it: their name, or "Trader 0x91…76", never a whole address."""
    for s in (_worth_load() or {}).get("scores", []):
        if s["wallet"] == wallet and s.get("name") and not str(s["name"]).startswith("0x"):
            return str(s["name"])
    return f"Trader {wallet[:4]}…{wallet[-2:]}"


CONNECTED = (
    "Connected. You'll get a message here a few minutes after a trader you follow buys, whatever the amount: what "
    "they bought, what they paid, the price now and when they traded. Polymarket's public data runs a few minutes "
    "behind, so alerts are never instant. When two or more of them buy the same thing, one message lists them all, and it's "
    f"updated as more buy. If you've picked markets on Signals, trades of ${WHALE_USD:,.0f} or more there come too. "
    f"At most {DAILY_CAP} alerts a day.\n\nFollowing: {{names}}\n/list: who you follow · /stop: turn alerts off"
)


def _vote(chat: str, data: str) -> str | None:
    """"Useful" / "Not useful" under an alert (``v:<alert id>:1`` or ``:0``): one vote per alert and account, a
    second tap changes it. Only the account the alert went to can vote on it. The note to show on the tap."""
    _, aid, useful = (data.split(":") + ["", ""])[:3]
    uid = _chat_owner(chat) if chat else None
    if not uid or useful not in ("0", "1"):
        return None
    with _db() as con:
        row = con.execute("SELECT uid FROM alerts WHERE id = ?", (aid,)).fetchone()
        if not row or row[0] != uid:
            return None
        con.execute("INSERT INTO votes VALUES (?, ?, ?, ?) ON CONFLICT (alert_id, uid) DO UPDATE SET useful = "
                    "excluded.useful, at = excluded.at", (aid, uid, int(useful), time.time()))  # fmt: skip
    return "Noted: useful." if useful == "1" else "Noted: not useful."


def _on_update(u: dict[str, Any]) -> None:
    """One message or button tap sent to the bot."""
    cb = u.get("callback_query")
    if cb:  # a button under an alert: "Useful" / "Not useful", or "Stop alerts from …"
        chat = str(((cb.get("message") or {}).get("chat") or {}).get("id") or "")
        data = str(cb.get("data") or "")
        ack = _vote(chat, data) if data.startswith("v:") else None
        try:  # quietly: a small note on the tap, no message in the chat
            tg("answerCallbackQuery", callback_query_id=cb.get("id"), **({"text": ack} if ack else {}))
        except RuntimeError:
            pass
        if chat and data.startswith("unf:"):
            w = data[4:]
            mid = (cb.get("message") or {}).get("message_id")
            owner = _chat_owner(chat)
            if owner and w in ((_load_follows()["users"].get(owner) or {}).get("follows") or []):  # a real stop
                count("stop", uid=owner, trader=w, alert_id=_alert_of(chat, mid))
            with _follow_lock:
                d = _load_follows()
                for x in d["users"].values():
                    if x.get("chat") == chat and w in x.get("follows", []):
                        x["follows"].remove(w)
                _save_follows(d)
            _say(chat, f"Done: no more alerts from {html.escape(_who(w))}.")
        return
    msg = u.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id") or "")
    text = str(msg.get("text") or "").strip()
    if not chat or not text.startswith("/"):
        return
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.split("@")[0], arg.strip()
    if cmd == "/start":
        hit = _codes.pop(arg, None) if arg else None
        if hit and time.time() - hit[1] <= CODE_TTL_S:
            with _follow_lock:
                d = _load_follows()
                for x in d["users"].values():  # one chat belongs to one account
                    if x.get("chat") == chat:
                        x["chat"] = None
                mine = _user(d, hit[0])
                mine["chat"] = chat
                _save_follows(d)
            count("telegram", uid=hit[0])
            names = ", ".join(_who(w) for w in mine["follows"]) or "nobody yet"
            _say(chat, CONNECTED.format(names=html.escape(names)))
        elif arg:
            _say(chat, f"That link has expired. Tap Connect Telegram on {SITE} again.")
        else:
            _say(chat, f"Follow traders on {SITE}, then tap Connect Telegram there to get an alert after they buy.")
    elif cmd == "/list":
        mine = next((x for x in _load_follows()["users"].values() if x.get("chat") == chat), None)
        names = [_who(w) for w in (mine or {}).get("follows", [])]
        _say(chat, html.escape("You follow: " + ", ".join(names)) if names else f"You don't follow anyone yet. Find traders on {SITE}.")
    elif cmd == "/stop":
        with _follow_lock:
            d = _load_follows()
            for x in d["users"].values():
                if x.get("chat") == chat:
                    x["chat"] = None
            _save_follows(d)
        _say(chat, f"Alerts are off. Tap Connect Telegram on {SITE} to turn them back on.")


def _bot_loop() -> None:
    """Reads what people send the bot (long polling: no webhook to set up)."""
    while not _follow_stop.is_set():
        try:
            updates = tg("getUpdates", offset=_tg["offset"], timeout=25, allowed_updates=["message", "callback_query"])
            _tg["error"] = None
        except RuntimeError as e:
            _tg["error"] = str(e)
            _follow_stop.wait(10)
            continue
        for u in updates or []:
            _tg["offset"] = max(_tg["offset"], int(u["update_id"]) + 1)
            try:
                _on_update(u)
            except Exception as e:  # one bad message doesn't stop the bot
                _tg["error"] = f"{type(e).__name__}: {e}"


def _scored(wallet: str) -> bool:
    """Whether the last check scored this wallet: then Signals has a page with their record."""
    return any(s["wallet"] == wallet for s in (_worth_load() or {}).get("scores", []))


def _chat_owner(chat: str) -> str | None:
    """The account a Telegram chat is connected to (one chat belongs to one account)."""
    return next((uid for uid, x in _load_follows()["users"].items() if x.get("chat") == chat), None)


# ---- what alerts keep: alerts.db in the store ----
# alerts: every alert sent (and the day's "limit reached" message), per account, with its Telegram message, so
# the daily cap can count them, a grouped alert can be edited in place and a vote names its alert.
# buys: the buys each person was told about, per outcome, so the second trader's buy can make a grouped alert.
# pending: a trader's fills not alerted yet, over the last ALERT_MAX_AGE_S, so split buys add up into one alert
# across rounds and restarts.
# votes: "Useful" / "Not useful", one per alert and account, changeable.

_db_lock = threading.RLock()  # re-entrant: a nested read never hangs the alert loop
_db_ready: set[str] = set()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, uid TEXT NOT NULL, chat TEXT, kind TEXT NOT NULL,
  trader TEXT, outcome TEXT, message_id INTEGER, sent_at REAL NOT NULL, trade_at REAL, day TEXT NOT NULL,
  trigger_at REAL);
CREATE INDEX IF NOT EXISTS alerts_by_day ON alerts (uid, day);
CREATE TABLE IF NOT EXISTS buys (uid TEXT NOT NULL, outcome TEXT NOT NULL, trader TEXT NOT NULL, price REAL,
  usd REAL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS buys_by_outcome ON buys (uid, outcome, at);
CREATE TABLE IF NOT EXISTS pending (trade_id TEXT PRIMARY KEY, trader TEXT NOT NULL, market TEXT NOT NULL,
  price REAL, size REAL NOT NULL, usd REAL NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS votes (alert_id TEXT NOT NULL, uid TEXT NOT NULL, useful INTEGER NOT NULL,
  at REAL NOT NULL, PRIMARY KEY (alert_id, uid));
CREATE TABLE IF NOT EXISTS events (at REAL NOT NULL, kind TEXT NOT NULL, uid TEXT, alert_id TEXT, target TEXT,
  trader TEXT);
"""


def alerts_db() -> Path:
    return STORE_DIR / "alerts.db"


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """One connection, one thread at a time (the alert loop, the bot and the API all write)."""
    with _db_lock:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(alerts_db())
        try:
            con.executescript(_SCHEMA)
            if str(alerts_db()) not in _db_ready:  # a db from before trigger_at: add the column, once
                if "trigger_at" not in {r[1] for r in con.execute("PRAGMA table_info(alerts)")}:
                    con.execute("ALTER TABLE alerts ADD COLUMN trigger_at REAL")
                _db_ready.add(str(alerts_db()))
            yield con
            con.commit()
        finally:
            con.close()


def _today(now: float | None = None) -> str:
    """The UTC day the daily cap counts in."""
    return datetime.fromtimestamp(time.time() if now is None else now, UTC).date().isoformat()


COUNTED = ("buy", "group", "whale")  # what the daily cap counts: a group once, its edits never


def _sent_today(uid: str, kinds: tuple[str, ...] = COUNTED) -> int:
    with _db() as con:
        q = f"SELECT COUNT(*) FROM alerts WHERE uid = ? AND day = ? AND kind IN ({','.join('?' * len(kinds))})"
        return int(con.execute(q, (uid, _today(), *kinds)).fetchone()[0])


def _keyboard(alert_id: str, stop: str | None = None) -> dict[str, Any]:
    """Under every alert: Useful / Not useful, and "Stop alerts from <name>" when it's about one trader you follow."""
    rows = [[{"text": "Useful", "callback_data": f"v:{alert_id}:1"}, {"text": "Not useful", "callback_data": f"v:{alert_id}:0"}]]
    if stop:
        rows.append([{"text": f"Stop alerts from {_who(stop)}", "callback_data": f"unf:{stop}"}])
    return {"inline_keyboard": rows}


def _send(uid: str, chat: str, kind: str, text_for: Callable[[str], str], *, trader: str | None = None,
          outcome: str | None = None, trade_at: float | None = None, stop: str | None = None,
          trigger_at: float | None = None) -> str | None:  # fmt: skip
    """Send one alert if today's cap allows it; its id, or None when it wasn't sent. At the cap, the first alert
    that doesn't fit sends CAP_TEXT instead, once a day (a failed send tries again with the next one), and the
    rest are dropped until the next UTC day. ``text_for`` gets the alert's id (its links can name it).
    ``trade_at``: when the alert's buys began (a group's first buy); ``trigger_at``: when the buy that sent it was
    made (defaults to ``trade_at``)."""
    if _sent_today(uid) >= DAILY_CAP:
        if not _sent_today(uid, ("cap",)):
            try:
                _say(chat, CAP_TEXT)
            except RuntimeError:
                return None
            with _db() as con:
                con.execute("INSERT INTO alerts (id, uid, chat, kind, sent_at, day) VALUES (?, ?, ?, 'cap', ?, ?)",
                            (secrets.token_urlsafe(6), uid, chat, time.time(), _today()))  # fmt: skip
        return None
    aid = secrets.token_urlsafe(6)  # 8 characters: fits Telegram's 64-byte button data with room to spare
    try:
        msg = _say(chat, text_for(aid), reply_markup=_keyboard(aid, stop))
    except RuntimeError:
        return None  # blocked the bot, or Telegram is down: the next buy tries again
    with _db() as con:
        con.execute("INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (aid, uid, chat, kind, trader, outcome, (msg or {}).get("message_id") if isinstance(msg, dict) else None,
                     time.time(), trade_at, _today(), trade_at if trigger_at is None else trigger_at))  # fmt: skip
    return aid


def _cents(p: float) -> str:
    c = p * 100
    return f"{c:.1f}¢" if c < 1 or 99 < c < 100 else f"{round(c)}¢"


def _copy_offered(wallet: str) -> bool:
    """Whether the page offers copying this trader (``copy_offer`` on the last check)."""
    s = next((x for x in (_worth_load() or {}).get("scores", []) if x["wallet"] == wallet), None)
    return s is not None and copy_offer(s)[0]


LINK_TARGETS = {"Buy on Polymarket": "market", "Their record": "record", "Check current price": "check"}


def _go(aid: str, target: str, url: str) -> str:
    """A link in an alert, routed through Signals so a tap is counted (``/go``): it redirects to ``url``."""
    return f"{SITE}/go/{aid}/{target}?{urllib.parse.urlencode({'to': url})}"


def _links(*pairs: tuple[str, str | None], aid: str | None = None) -> str:
    """The links under an alert; with the alert's id, each goes through ``/go`` so taps can be counted."""
    e = html.escape
    return " · ".join(f'<a href="{e(_go(aid, LINK_TARGETS[label], url) if aid else url)}">{label}</a>'
                      for label, url in pairs if url)  # fmt: skip


# Said only for a trader copying is offered for, while the price is still near theirs: secondary to what happened,
# and no cost (that's "Check current price"'s, with the fee, the fill and what's for sale).
COPY_LINE = "<i>Copying is offered for this trader, and you can still get in near their price. Based on past copies, not a promise.</i>"


def _lag(at: float, seen: float) -> str:
    """How long before this alert the trade was made, measured: "6 min", or "under a minute"."""
    mins = round((seen - at) / 60)
    return "under a minute" if mins < 1 else f"{mins} min"


def _trade_text(head: str, wallet: str, t: WhaleTrade, paid: float, now: float, at: float, seen: float,
                record: bool, aid: str | None) -> str:  # fmt: skip
    """What happened, the same for every trader: ``head`` (who bought which outcome, at what price, for how much),
    the market, the price now (", price already moved" outside the "can still get in" range, ``bet_status``), and
    when they traded, measured against this alert (Polymarket shows trades minutes late). Then COPY_LINE, only when
    copying is offered for them and the price is still in range. Links: "Check current price" (the trade's own page
    on Signals, neutral for every trader), their record, "Buy on Polymarket" (it only opens the market: Signals
    places no orders); then the delay line."""
    e = html.escape
    status = bet_status(paid, now)
    lines = [head, e(t.title or "A Polymarket market"), f"Now {_cents(now)}{', price already moved' if status != 'in' else ''}.",
             f"Traded {datetime.fromtimestamp(at, UTC):%H:%M} UTC, {_lag(at, seen)} before this alert."]  # fmt: skip
    if status == "in" and _copy_offered(wallet):
        lines.append(COPY_LINE)
    links = _links(("Check current price", f"{SITE}/#/check/{wallet}/{t.market}"),
                   ("Their record", f"{SITE}/#/worth/{wallet}" if record else None), ("Buy on Polymarket", t.url), aid=aid)  # fmt: skip
    return "\n".join([*lines, links, DATA_DELAY])


def _alert_text(wallet: str, t: WhaleTrade, paid: float, now: float, usd: float, at: float, seen: float,
                aid: str | None = None) -> str:  # fmt: skip
    """The Telegram message for one new buy by a trader you follow, whoever they are: following is research
    first, so every trader's buy gets the same facts (``_trade_text``). ``paid`` is their average across the fills,
    ``at`` their first fill, ``seen`` when this alert was made."""
    e = html.escape
    head = f"<b>{e(_who(wallet))}</b> bought <b>{e(t.outcome or 'Yes')}</b> at {_cents(paid)} (${usd:,.0f})"
    return _trade_text(head, wallet, t, paid, now, at, seen, True, aid)


def _group_text(members: list[tuple[str, float, float]], t: WhaleTrade, now: float, seen: float,
                aid: str | None = None) -> str:  # fmt: skip
    """One message for 2+ traders you follow buying the same outcome: each one's price and buy time, in the
    order they bought (so you see who went first), the price now, how long before this alert the latest buy was
    made, and "Check current price" for the latest buy. Neutral for everyone: no "get in"."""
    e = html.escape
    lines = [f"<b>{len(members)} traders you follow bought {e(t.outcome or 'Yes')}</b> on {e(t.title or 'a Polymarket market')}"]
    for w, price, at in members:
        lines.append(f"· {e(_who(w))} paid {_cents(price)} at {datetime.fromtimestamp(at, UTC):%H:%M} UTC")
    last, _, last_at = members[-1]  # the latest buyer: their price is the closest to now
    moved = bet_status(members[-1][1], now) != "in"
    lines += [f"Now {_cents(now)}{', price already moved' if moved else ''}.", f"Latest buy {_lag(last_at, seen)} before this alert.",
              _links(("Check current price", f"{SITE}/#/check/{last}/{t.market}"), ("Buy on Polymarket", t.url), aid=aid),
              DATA_DELAY]  # fmt: skip
    return "\n".join(lines)


ALERT_ID_MARK = "\x00alert\x00"  # stands in for the alert id in a text built once for every follower


def _settled(now: float) -> bool:
    """2¢ or less, 98¢ or more: the market has settled, or as good as (AGENTS.md's rule). Nothing to alert."""
    return now <= SETTLED_LOW or now >= SETTLED_HIGH


class NewBuy:
    """One trader's new buy of one outcome, fills merged: what they paid on average and the price now. Its alert
    text is built once, however many people follow the trader. ``seen``: when the round that found it ran."""

    def __init__(self, wallet: str, fills: list[WhaleTrade], now: float,
                 totals: tuple[float, float, float] | None = None, seen: float | None = None) -> None:  # fmt: skip
        self.wallet, self.t = wallet, max(fills, key=lambda f: f.at)
        self.size, self.usd = sum(f.size for f in fills), sum(f.usd for f in fills)
        self.at = min(f.at for f in fills).timestamp()  # when they bought: their first fill
        if totals:  # (contracts, dollars, first fill) over every fill not alerted yet, earlier rounds' included
            self.size, self.usd, self.at = totals
        self.paid, self.now = self.usd / self.size, now  # what they paid on average, across those buys
        self.outcome = self.t.token or self.t.market  # the exact outcome token; the outcome index is sometimes junk
        self.seen = time.time() if seen is None else seen
        self._text: str | None = None

    def text(self, aid: str) -> str:
        if self._text is None:
            self._text = _alert_text(self.wallet, self.t, self.paid, self.now, self.usd, self.at, self.seen, ALERT_ID_MARK)
        return self._text.replace(ALERT_ID_MARK, aid)


def _recently(uid: str | None, trader: str, outcome: str, now: float) -> bool:
    """Whether this trader's buy of this outcome was already alerted in the last ALERT_AGAIN_S, from alerts.db,
    so a restart doesn't alert it again (to anyone, or to ``uid`` for a big trade)."""
    with _db() as con:
        if uid is None:
            q = "SELECT 1 FROM buys WHERE trader = ? AND outcome = ? AND at >= ? LIMIT 1"
            return con.execute(q, (trader, outcome, now - ALERT_AGAIN_S)).fetchone() is not None
        q = "SELECT 1 FROM alerts WHERE uid = ? AND kind = 'whale' AND trader = ? AND outcome = ? AND sent_at >= ? LIMIT 1"
        return con.execute(q, (uid, trader, outcome, now - ALERT_AGAIN_S)).fetchone() is not None


def _cluster(rows: list[tuple[str, float, float]], at: float, window: float) -> list[tuple[str, float, float]]:
    """The run of buys around ``at`` in which each is within ``window`` of the one before (a sliding window), so a
    late-arriving buy with an earlier time still joins, and a buy within the window of the latest joins too."""
    runs: list[list[tuple[str, float, float]]] = []
    for r in sorted(rows, key=lambda r: r[2]):
        if runs and r[2] - runs[-1][-1][2] <= window:
            runs[-1].append(r)
        else:
            runs.append([r])
    return next((run for run in runs if run[0][2] <= at <= run[-1][2]), [])


def _deliver(uid: str, chat: str, b: NewBuy, category: str | None) -> int:
    """Tell one person about one buy by a trader they follow: alone, or grouped with other traders they follow
    who bought the same outcome, each within GROUP_S of the one before (GROUP_FAST_S in Sports and Esports; GROUP_S
    when the category isn't known). The second buyer sends the grouped alert; later ones edit it in place (Telegram
    editMessageText), never a new ping. Returns messages sent."""
    window = GROUP_FAST_S if category in FAST_CATEGORIES else GROUP_S
    with _db() as con:
        con.execute("INSERT INTO buys VALUES (?, ?, ?, ?, ?, ?)", (uid, b.outcome, b.wallet, b.paid, b.usd, b.at))
        rows = con.execute("SELECT trader, price, at FROM buys WHERE uid = ? AND outcome = ? AND at >= ? ORDER BY at",
                           (uid, b.outcome, b.at - 7 * 86400)).fetchall()  # fmt: skip
        run = _cluster(rows, b.at, window)
        group = con.execute("SELECT id, chat, message_id FROM alerts WHERE uid = ? AND kind = 'group' AND outcome = ?"
                            " AND trade_at BETWEEN ? AND ? ORDER BY sent_at DESC LIMIT 1",
                            (uid, b.outcome, run[0][2], run[-1][2])).fetchone() if run else None  # fmt: skip
    members: dict[str, tuple[str, float, float]] = {}
    for w, price, at in run:
        members.setdefault(w, (w, price, at))  # each trader's first buy in the run
    first = sorted(members.values(), key=lambda m: m[2])
    if group:
        if group[2] is not None:
            try:
                tg("editMessageText", chat_id=group[1], message_id=group[2], text=_group_text(first, b.t, b.now, b.seen, group[0]),
                   parse_mode="HTML", disable_web_page_preview=True, reply_markup=_keyboard(group[0]))  # fmt: skip
            except RuntimeError:
                pass  # deleted, or Telegram is down: nothing new to ping about either way
        return 0
    if len(first) >= 2:
        aid = _send(uid, chat, "group", lambda aid: _group_text(first, b.t, b.now, b.seen, aid), outcome=b.outcome,
                    trade_at=first[0][2], trigger_at=b.at)  # fmt: skip
        return 1 if aid else 0
    aid = _send(uid, chat, "buy", b.text, trader=b.wallet, outcome=b.outcome, trade_at=b.at, stop=b.wallet)
    return 1 if aid else 0


def _add_up(w: str, fills: list[WhaleTrade], now: float) -> dict[str, tuple[float, float, float]]:
    """Keep these new fills (alerts.db: pending) and return, per market, the running total of this trader's fills
    not alerted yet over the last ALERT_MAX_AGE_S, this round's and earlier rounds' (and before a restart):
    (contracts, dollars, first fill's time). So a $60 buy and another $60 a minute later add up to $120."""
    with _db() as con:
        con.execute("DELETE FROM pending WHERE at < ?", (now - ALERT_MAX_AGE_S,))
        con.executemany("INSERT OR IGNORE INTO pending VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(f.trade_id, w, f.market, f.price, f.size, f.usd, f.at.timestamp()) for f in fills])  # fmt: skip
        rows = con.execute("SELECT market, SUM(size), SUM(usd), MIN(at) FROM pending WHERE trader = ? AND at >= ?"
                           " GROUP BY market", (w, now - ALERT_MAX_AGE_S)).fetchall()  # fmt: skip
    return {m: (size, usd, at) for m, size, usd, at in rows if size}


def _added_up(w: str, market: str) -> None:
    """These fills have been alerted: they start no new total."""
    with _db() as con:
        con.execute("DELETE FROM pending WHERE trader = ? AND market = ?", (w, market))


def alert_pass(now: float | None = None) -> int:
    """One round: each followed trader's trades, read once however many people follow them, and for each new
    buy, whatever the amount and whoever the trader, a message to every connected follower (grouped when 2+ traders they follow
    bought the same outcome), then big trades in people's chosen markets. A trader's trades from before their
    first round are only noted, never alerted. No alert for a buy with no price now (no open position to read it
    from: there's no saying how far the price moved), nor when the price now is settled (2¢ or less, 98¢ or more).
    Returns how many messages went out."""
    users = {uid: u for uid, u in _load_follows()["users"].items() if u.get("chat")}
    to: dict[str, list[str]] = {}
    for uid, u in users.items():
        for w in u.get("follows") or []:
            to.setdefault(w, []).append(uid)
    for w in [w for w in _seen if w not in to]:
        del _seen[w]
    now = time.time() if now is None else now
    new_buys: list[NewBuy] = []
    for w in to:
        try:
            d = _live_read(w, LIVE_ALERT_S)
        except VenueError:
            continue
        first, seen = w not in _seen, _seen.setdefault(w, set())
        new = [t for t in d.trades if t.trade_id not in seen]
        seen.update(t.trade_id for t in d.trades)
        if len(seen) > 2000:
            _seen[w] = {t.trade_id for t in d.trades}
        if first:
            continue
        prices = {p.market: p.current_price for p in d.positions}
        fills: dict[str, list[WhaleTrade]] = {}
        for t in new:
            if t.action == "buy" and t.size and now - t.at.timestamp() <= ALERT_MAX_AGE_S:
                fills.setdefault(t.market, []).append(t)
        totals = _add_up(w, [f for ts in fills.values() for f in ts], now) if fills else {}
        for market, ts in fills.items():
            price = prices.get(market)
            if now - _alerted.get((w, market), 0) < ALERT_AGAIN_S or price is None or _settled(price):
                continue
            b = NewBuy(w, ts, price, totals.get(market), seen=now)
            if _recently(None, w, b.outcome, now):
                continue
            _alerted[(w, market)] = now
            _added_up(w, market)
            new_buys.append(b)
    try:
        cats = _categories([b.t.market for b in new_buys]) if new_buys else {}
    except VenueError:
        cats = {}  # grouped over the longer window
    sent = 0
    for b in new_buys:
        for uid in to[b.wallet]:
            sent += _deliver(uid, users[uid]["chat"], b, cats.get(b.t.market))
    return sent + whale_pass(users, now)


# ---- big trades in your markets, whoever the trader ----

_whale: dict[str, Any] = {"started": False, "seen": set()}
_whale_alerted: dict[tuple[str, str, str], float] = {}  # (account, trader, market) -> when


def _whale_text(t: WhaleTrade, now: float, category: str | None, seen: float, aid: str | None = None) -> str:
    """A big trade in your markets, with the same facts as a followed trader's buy (``_trade_text``): who (linked
    to their Signals page when we've scored them, else "Trader 0x91…76"), what they bought at what price, the
    market, the price now and when they traded. COPY_LINE only when copying is offered for them and the price is
    still in range."""
    e = html.escape
    w = (t.trader or "").lower()
    who = f'<a href="{e(SITE)}/#/worth/{w}">{e(_who(w))}</a>' if _scored(w) else e(_who(w))
    where = f" in {e(category)}" if category else ""
    head = f"<b>Big trade{where}:</b> {who} bought <b>{e(t.outcome or 'Yes')}</b> at {_cents(t.price)} (${t.usd:,.0f})"
    return _trade_text(head, w, t, t.price, now, t.at.timestamp(), seen, False, aid)


def whale_pass(users: dict[str, dict[str, Any]], now: float) -> int:
    """Buys of WHALE_USD or more (the SDK's big trades, as the Feed's) in each person's chosen markets, from
    traders they don't already follow. Only for people who picked markets and saved them (onboarding or the Feed's
    Change, signed in): none for nobody saved, and none for [] (Skip, or all markets: no choice made). At most
    WHALE_DAILY a day each, within the daily cap; never at a settled price. The first round only notes what's there."""
    wants = {uid: u for uid, u in users.items() if u.get("interests")}
    if not wants:
        return 0
    try:
        rows = cached(f"now:{WHALE_USD:.0f}", 20, lambda: reads.whales.big_trades(WHALE_USD, venues=VENUES))  # type: ignore[arg-type]
    except VenueError:
        return 0
    rows = [t for t in rows if t.venue == "polymarket" and t.action == "buy" and t.trader and t.usd >= WHALE_USD]
    seen: set[str] = _whale["seen"]
    new = [t for t in rows if t.trade_id not in seen and now - t.at.timestamp() <= ALERT_MAX_AGE_S]
    seen.update(t.trade_id for t in rows)
    if len(seen) > 5000:
        _whale["seen"] = {t.trade_id for t in rows}
    if not _whale["started"]:
        _whale["started"] = True
        return 0
    try:
        cats = _categories([t.market for t in new]) if new else {}
    except VenueError:
        cats = {}
    sent = 0
    for t in new:
        w, cat, outcome = (t.trader or "").lower(), cats.get(t.market), t.token or t.market
        to = [uid for uid, u in wants.items() if cat in u["interests"] and w not in (u.get("follows") or [])
              and now - _whale_alerted.get((uid, w, t.market), 0) >= ALERT_AGAIN_S]  # fmt: skip
        if not to:
            continue
        try:
            price_now = next((p.current_price for p in _live_read(w, LIVE_ALERT_S).positions if p.market == t.market), None)
        except VenueError:
            continue
        if price_now is None or _settled(price_now):
            continue
        text = _whale_text(t, price_now, cat, now, ALERT_ID_MARK)  # once for everyone it goes to
        for uid in to:
            if _sent_today(uid, ("whale",)) >= WHALE_DAILY or _recently(uid, w, outcome, now):
                continue
            _whale_alerted[(uid, w, t.market)] = now
            if _send(uid, wants[uid]["chat"], "whale", lambda aid, x=text: x.replace(ALERT_ID_MARK, aid), trader=w,
                     outcome=outcome, trade_at=t.at.timestamp()):  # fmt: skip
                sent += 1
    return sent


def _alert_loop() -> None:
    while not _follow_stop.is_set():
        try:
            alert_pass()
        except Exception as e:  # keep alerting through a bad round
            _tg["error"] = f"{type(e).__name__}: {e}"
        _follow_stop.wait(ALERT_EVERY_S)


# ---- counting: is any of this useful? ----
# Per alert and per follow, kept in the store's alerts.db with nothing personal beyond the signed-in account (its
# Clerk id): no IPs, no user agents. Minutes from their trade to the alert (alerts: trade_at, sent_at), taps on an
# alert's links (/go), "Stop alerts" taps and unfollows, votes (votes), follows, interests picked and Telegram
# connected (events). /api/metrics sums them up for the owner: locally, or with SIGNALS_METRICS_KEY.

METRICS_KEY = os.environ.get("SIGNALS_METRICS_KEY", "")
EVENTS = ("tap", "stop", "unfollow", "follow", "telegram", "picked")
_ALERT_ID = re.compile(r"[A-Za-z0-9_-]{4,16}")


def count(kind: str, *, uid: str | None = None, alert_id: str | None = None, target: str | None = None,
          trader: str | None = None) -> None:  # fmt: skip
    """Append one event. Never in the way: counting that fails is dropped, whatever was being done goes ahead."""
    try:
        with _db() as con:
            con.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", (time.time(), kind, uid, alert_id, target, trader))
    except (sqlite3.Error, OSError):
        pass


def _alert_of(chat: str, message_id: Any) -> str | None:
    """The alert a Telegram message is (its id), from the chat and message id."""
    if message_id is None:
        return None
    try:
        with _db() as con:
            row = con.execute("SELECT id FROM alerts WHERE chat = ? AND message_id = ?", (chat, message_id)).fetchone()
    except (sqlite3.Error, OSError):
        return None
    return row[0] if row else None


def _alert_known(alert_id: str) -> bool:
    """Only taps on alerts we sent are counted: a made-up /go link redirects, but isn't a tap."""
    try:
        with _db() as con:
            return con.execute("SELECT 1 FROM alerts WHERE id = ?", (alert_id,)).fetchone() is not None
    except (sqlite3.Error, OSError):
        return False


def _allowed(url: str) -> bool:
    """Where a /go link may send someone: Polymarket, or Signals itself. Nowhere else (no open redirect)."""
    try:
        u = urllib.parse.urlsplit(url)
        site = urllib.parse.urlsplit(SITE)
        host = u.hostname
    except ValueError:  # a malformed link (a bad port, a broken IPv6 host): Signals itself, never a 500
        return False
    return u.scheme == "https" and (host == "polymarket.com" or (u.scheme, u.netloc) == (site.scheme, site.netloc))


@app.get("/go/{alert_id}/{target}")
def go(alert_id: str, target: str, to: str = "") -> RedirectResponse:
    """A tap on an alert's link: counted (alert id, target, time), then on to the link. The redirect happens
    whether or not the count is kept, so the market link always works."""
    dest = to if _allowed(to) else SITE
    if _ALERT_ID.fullmatch(alert_id) and target in set(LINK_TARGETS.values()) and _alert_known(alert_id):
        count("tap", alert_id=alert_id, target=target)
    return RedirectResponse(dest, status_code=302, headers={"Cache-Control": "no-store"})


@app.post("/api/count/picked")
def picked() -> dict[str, bool]:
    """Someone answered onboarding's one question (which markets). Anonymous: no id of any kind, a time only,
    so follows per visitor who picks interests can be worked out."""
    count("picked")
    return {"ok": True}


def _owner(request: Request) -> None:
    """The metrics are the owner's: this machine when running locally, else the owner's key (SIGNALS_METRICS_KEY,
    from the environment) as a Bearer token. Anyone else gets a 404, as if there were nothing here."""
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if METRICS_KEY and key and secrets.compare_digest(key.encode(), METRICS_KEY.encode()):  # bytes: any text is a 404
        return
    local = request.client is not None and request.client.host in ("127.0.0.1", "::1")
    if not HOSTED and local and not request.headers.get("x-forwarded-for"):
        return
    raise HTTPException(404, detail="Not Found")


def _pct(xs: list[float], q: float) -> float | None:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


@app.get("/api/metrics")
def metrics(request: Request, response: Response, days: int = 30) -> dict[str, Any]:
    """A read-only summary of the counts over the last ``days`` (see "Counting" in the README)."""
    _owner(request)
    response.headers["Cache-Control"] = "private, no-store"
    since = time.time() - max(1, days) * 86400
    week = 7 * 86400
    with _db() as con:
        sent = dict(con.execute("SELECT kind, COUNT(*) FROM alerts WHERE sent_at >= ? GROUP BY kind", (since,)).fetchall())
        # from the buy that sent the alert (trigger_at; a group's is its latest buyer's, not its first)
        lags = [r[0] / 60 for r in con.execute("SELECT sent_at - COALESCE(trigger_at, trade_at) FROM alerts WHERE sent_at >= ?"
                                                " AND COALESCE(trigger_at, trade_at) IS NOT NULL AND kind != 'cap'", (since,))]  # fmt: skip
        taps = dict(con.execute("SELECT target, COUNT(*) FROM events WHERE kind = 'tap' AND at >= ? GROUP BY target", (since,)).fetchall())
        tapped = con.execute("SELECT COUNT(DISTINCT e.alert_id) FROM events e JOIN alerts a ON a.id = e.alert_id"
                             " WHERE e.kind = 'tap' AND e.at >= ?", (since,)).fetchone()[0]  # fmt: skip
        n = dict(con.execute("SELECT kind, COUNT(*) FROM events WHERE at >= ? GROUP BY kind", (since,)).fetchall())
        # A stop or unfollow "after an alert": that account had an alert about that trader in the 7 days before.
        after = dict(con.execute(
            "SELECT e.kind, COUNT(DISTINCT e.rowid) FROM events e JOIN alerts a ON a.uid = e.uid AND a.trader = e.trader"
            " AND a.sent_at <= e.at AND a.sent_at >= e.at - ? WHERE e.kind IN ('stop', 'unfollow') AND e.at >= ?"
            " GROUP BY e.kind", (week, since)).fetchall())  # fmt: skip
        votes = dict(con.execute("SELECT useful, COUNT(*) FROM votes WHERE at >= ? GROUP BY useful", (since,)).fetchall())
        voted = con.execute("SELECT COUNT(DISTINCT alert_id) FROM votes WHERE at >= ?", (since,)).fetchone()[0]
    users = _load_follows()["users"].values()
    followers = [u for u in users if u.get("follows")]
    with_interests = [u for u in users if u.get("interests") is not None]
    counted = sum(v for k, v in sent.items() if k in COUNTED)
    def ratio(a: float, b: float) -> float | None:
        return round(a / b, 3) if b else None

    return {
        "days": days,
        "alerts": {"sent": counted, "by_kind": sent, "minutes_from_trade_to_alert": {
            "median": _pct(lags, 0.5), "p90": _pct(lags, 0.9), "n": len(lags)}},
        "taps": {"by_target": taps, "alerts_tapped": tapped, "share_of_alerts_tapped": ratio(tapped, counted)},
        "stops": {"stop_taps": n.get("stop", 0), "stop_taps_within_7_days_of_an_alert": after.get("stop", 0),
                  "unfollows": n.get("unfollow", 0), "unfollows_within_7_days_of_an_alert": after.get("unfollow", 0)},
        "votes": {"useful": votes.get(1, 0), "not_useful": votes.get(0, 0), "alerts_voted_on": voted},
        "follows": {"interest_picks": n.get("picked", 0), "follows": n.get("follow", 0),
                    "follows_per_interest_pick": ratio(n.get("follow", 0), n.get("picked", 0)),
                    "accounts_with_saved_interests": len(with_interests),
                    "follows_per_account_with_saved_interests": ratio(sum(len(u.get("follows") or []) for u in with_interests), len(with_interests))},
        "telegram": {"followers": len(followers), "followers_with_telegram": sum(1 for u in followers if u.get("chat")),
                     "share": ratio(sum(1 for u in followers if u.get("chat")), len(followers)),
                     "connected_in_period": n.get("telegram", 0)},
    }  # fmt: skip


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    """The in-app logo, for browsers that ask for /favicon.ico instead of reading the page's icon link."""
    return FileResponse(HERE / "static" / "icon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")  # the font and the icon


def main() -> None:
    if "SIGNALS_STORE_DIR" not in os.environ and not STORE_DIR.exists() and _OLD_STORE_DIR.exists():
        _OLD_STORE_DIR.rename(STORE_DIR)
    port = int(os.environ.get("SIGNALS_PORT", "8790"))
    print(f"Signals on http://127.0.0.1:{port}  (store: {STORE_DIR})")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)  # no IPs kept


if __name__ == "__main__":
    main()
