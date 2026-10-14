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
from collections import Counter
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
from uselayer import Client, Trader, TraderDetail, VenueError, Whales, WhaleTrade
from uselayer.whale_finder import all_boards
from uselayer.whale_scores import (
    CATEGORY_TAGS,
    COPY_BETS,
    RECOMMENDED,
    SEGMENTS,
    Stat,
    category_of,
    copyable_verdict,
    segment_of,
)

from signals import us_match
from signals.budget import BackgroundHttp, Budget
from signals.directory import (
    CHECK_BOARDS,
    EXPLORE_GROUPS,
    LISTED_FRESH_S,
    QUICK_TRADES,
    SORTS,
    Directory,
    QuickLook,
    Seen,
    fresh_for,
    from_sighting,
    quick_look,
)  # fmt: skip

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
    if EVALUATE_ON:  # looks at every wallet in the directory, in turn, on the same background budget as the sweep
        threading.Thread(target=_evaluate_loop, daemon=True).start()
    if TG_TOKEN:  # alerts: the bot's messages (connecting a chat, /list, /stop), and new buys of followed traders
        threading.Thread(target=_bot_loop, daemon=True).start()
        threading.Thread(target=_alert_loop, daemon=True).start()
    if LAYER_KEY:  # Polymarket US twins of the International markets, from Layer's matches (us_match)
        threading.Thread(target=us_match.loop, args=(reads.matches, _us_stop), daemon=True).start()
    yield
    _us_stop.set()
    _worth_stop.set()
    _follow_stop.set()
    _alert_wake.set()
    _live_stop.set()
    _directory_stop.set()


app = FastAPI(title="Signals", lifespan=lifespan)
reads = Client(store=":memory:")  # leaderboards, traders, links, cost previews: reads only
LAYER_KEY = os.environ.get("LAYER_API_KEY", "")
_us_stop = threading.Event()


def us_of(market: str | None, token: str | None = None, outcome: str | None = None) -> dict[str, Any] | None:
    """A Polymarket International outcome's verified twin on Polymarket US (``found``), a checked "no verified
    US match" (``found`` False), or None when Layer's matches can't be read right now: see ``signals.us_match``."""
    return us_match.twins.lookup(market, token, outcome)


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


def feed_reader(request: Request) -> None:
    """The Feed (Recommended, Big trades, an alert's trade) is for signed-in people; Traders and trader pages need
    no account. Without signing in switched on (running locally without Clerk's key) it's open like the rest."""
    if clerk_frontend_api(CLERK_KEY):
        _session(request, "Sign in to see the Feed.")


@app.get("/api/config")
def config() -> dict[str, Any]:
    """What the page needs to know before it draws: which venues Signals reads (see VENUES), and Clerk's
    publishable key when signing in (to follow traders) is switched on. The key is public by design."""
    return {"venues": list(VENUES), "clerk_key": CLERK_KEY or None, "alerts": bool(TG_TOKEN), "explore": EXPLORE_ON}


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
def trading_now(response: Response, min_usd: float = 250, _: None = Depends(feed_reader)) -> list[dict[str, Any]]:
    """Recent large trades on the venues Signals reads, newest first (the Feed's Big trades). Named Kalshi
    traders here usually show their trades. A Polymarket trade carries its market's category, so the page can
    filter by your markets; the SDK has no categories for Kalshi markets, so theirs is None."""
    _private(response)
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
    return [t | ({"category": cats.get(t["market"]), "us": us_of(t["market"], t.get("token"), t.get("outcome"))}
                 if t["venue"] == "polymarket" else {"category": None}) for t in out]


@app.get("/api/trader/{venue}/{trader_id}")
def trader(venue: str, trader_id: str) -> dict[str, Any]:
    venue_on(venue)
    try:
        d = cached(f"trader:{venue}:{trader_id}", 15, lambda: reads.whales.trader(venue, trader_id, trades=40, closed=50))  # type: ignore[arg-type]
    except VenueError as e:
        if e.code == "not_found":
            count("no_profile", target="trader")
        raise venue_error(e) from e
    out = d.to_dict()
    links = position_links(d.trader.id) if venue == "polymarket" and d.positions else {}
    out["positions"] = [
        p | {"status": bet_status(p["avg_price"], p["current_price"]), "url": links.get(p["market"])}
        | ({"us": us_of(p["market"], None, p.get("outcome"))} if venue == "polymarket" else {})
        for p in out["positions"]
    ]
    if venue == "polymarket":
        out["trades"] = [t | {"us": us_of(t["market"], t.get("token"), t.get("outcome"))} for t in out["trades"]]
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
# result meanwhile. A wallet that isn't in it is scored when its page is opened. Its reads come out of the
# background's budget (BACKGROUND_RATE), shared with the directory's sweep and evaluation. With
# SIGNALS_DISCOVERY=directory the page shows the directory's latest scores instead (_worth_load), and the check
# keeps running beside it, so going back is instant.

WORTH_WALLETS = int(os.environ.get("SIGNALS_WORTH_WALLETS", "200"))
WORTH_MAX_AGE_S = 6 * 3600
WORTH_RETRY_S = 30 * 60  # after a failed check
WORTH_FORMAT = 9  # bump when the kept result gains fields; an older one is re-checked on launch (8: uselayer 0.10, tiers; 9: 0.11, edge_verdict, parlays and edge_losing)
WORTH_MIN_AGE_HOSTED_S = 3600  # hosted: "Check again" only starts a new check once the last is this old
WORTH_ONE_KEEP = 500  # wallets scored on demand, kept until the next check
_worth_stop = threading.Event()
_worth: dict[str, Any] = {"running": False, "done": 0, "total": 0, "error": None}
_worth_data: dict[str, Any] | None = None
_worth_one: dict[str, dict[str, Any]] = {}


def worth_file() -> Path:
    return STORE_DIR / "worth-following.json"


def _worth_load(interests: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """What Traders, the Feed's Recommended, alerts and trader pages read as the traders checked: the directory's
    latest scores with SIGNALS_DISCOVERY=directory (``discovery_result``, the picked ``interests`` first), else,
    or while the directory has none, the Worth following check's (``_check_load``). Same shape either way."""
    if DISCOVERY == "directory" and (got := discovery_result(interests)) is not None:
        return got
    return _check_load()


def _check_load() -> dict[str, Any] | None:
    """The Worth following check's last result, as it kept it in worth-following.json."""
    global _worth_data
    if _worth_data is None and worth_file().exists():
        try:
            _worth_data = _worth_upgrade(json.loads(worth_file().read_text()))
        except ValueError:
            _worth_data = None
    return _worth_data


def _worth_upgrade(data: dict[str, Any]) -> dict[str, Any]:
    """A result kept by an older uselayer, shown while the new check runs, judged by today's rules. Before
    0.8.0: its copy range widened to Student's t, ``copyable`` only when that range's bottom is above zero.
    Before 0.10: its tier from skill and track record (the SDK's ``segment_of``), never from copying, so a
    sharp kept as "too fast" is a sharp again and big profit without a clear edge isn't called luck. Before 0.11:
    a measured edge with a losing window isn't "edge not proven", and a parlay trader's skill is unmeasured."""
    if (data.get("format") or 0) >= 9:
        return data
    for s in data.get("scores", []):
        if (data.get("format") or 0) < 6:
            copy = Stat.from_dict(s["copy"]) if s.get("copy") else None
            s["copy"] = copy.to_dict() if copy else None
            if s.get("copyable") == "yes" and copyable_verdict(copy) != "yes":
                s["copyable"] = copyable_verdict(copy)
        s["segment"] = segment_of(s)
        s["label"], s["action"] = SEGMENTS[s["segment"]]
    return data


def _worth_run() -> None:
    global _worth_data
    try:
        reads.whales.scorer(cache_dir=str(STORE_DIR / "price-cache"))  # a trader page's scores

        def progress(done: int, total: int, _s: Any) -> None:
            _worth.update(done=done, total=total)

        d = background().discover(wallets=WORTH_WALLETS, on_progress=progress)
        data = d.to_dict(sample=True) | {"format": WORTH_FORMAT}
        for s in data["scores"]:
            _with_odds(s)
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


def _with_odds(s: dict[str, Any]) -> dict[str, Any]:
    """A score as the check keeps it: of its checked bets (all of them: before the sample is cut), how many
    settled and won, and the win rate their prices expected."""
    settled = [b for b in s.get("sample") or [] if b.get("payout") is not None]
    s["won"], s["settled"] = sum(1 for b in settled if b["payout"] == 1), len(settled)
    # the price paid is the market's chance they'd win, so this is the win rate the odds expected
    s["odds"] = sum(b["price"] for b in settled) / len(settled) if settled else None
    return s


def worth_refresh() -> None:
    if _worth["running"]:
        return
    _worth.update(running=True, done=0, total=0, error=None)
    threading.Thread(target=_worth_run, daemon=True).start()


def _worth_age_s() -> float:
    data = _check_load()
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


# ---- the trader directory (1: finding wallets; 2: keeping their scores; 3: evaluating them; see directory.py) ----
# Every wallet Signals finds, and where, kept across runs: the SDK's sweep of every leaderboard and of markets
# at every level of activity, once a day, plus traders in the Feed's big trades, wallets opened that the last
# check didn't have, and followed wallets. Every score worked out is kept there too (the Worth following check's,
# a trader page's, a failed try, and on launch the last check's from worth-following.json), at no extra reads.
# Traders shows the check's own wallets unless SIGNALS_DISCOVERY=directory (phase 5, below the evaluation).
# /api/directory gives counts only. SIGNALS_DIRECTORY=0 turns the sweep off; scores are kept either way.
# What the check, the sweep and the evaluation (below) read from Polymarket comes out of one budget, BACKGROUND_RATE.

DIRECTORY_ON = os.environ.get("SIGNALS_DIRECTORY", "1") != "0"
DIRECTORY_SWEEP_S = 24 * 3600  # a new sweep once the last finished one is this old
DIRECTORY_RETRY_S = 3600  # after a sweep that failed
# The background's Polymarket reads a second at most: the Worth following check's, the sweep's and the evaluation's
# together, retries included. They share it rather than take turns: one running alone gets all of it. The SDK
# allows 8 a second per Polymarket host, for everything the engine reads, and the background's reads come out of
# those 8, so the Feed and pages always have the other 4 (the Feed's poller reads one trader at a time, 4 reads in
# ~1.2 s, so it never needs more than ~3.3 a second), and all 8 while the background is idle.
BACKGROUND_RATE = float(os.environ.get("SIGNALS_BACKGROUND_RATE", "4"))
# Scoring a wallet on the spot ("Check now", a trader page nobody has scored lately): this many Polymarket reads a
# second, so with the background's (4 + 3 of the SDK's 8 a host) the Feed's poller always keeps a share of its own.
CHECK_RATE = float(os.environ.get("SIGNALS_CHECK_RATE", "3"))
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


_background: dict[str, Any] = {}
_background_lock = threading.Lock()


def background() -> Whales:
    """The SDK's reads for the background (the check, the sweep, the evaluation): the engine's own client, so the
    per-host limits are shared with every other read, with each Polymarket read also taken from one budget of
    BACKGROUND_RATE a second (signals/budget.py). Its scorer keeps price histories where the check keeps them."""
    with _background_lock:
        if "whales" not in _background:
            budget = Budget(BACKGROUND_RATE, yield_to=_reading_followed)  # half rate while alerts' reads run
            whales = Whales(BackgroundHttp(reads._http, budget), reads._layer, reads)  # type: ignore[arg-type]
            whales.scorer(cache_dir=str(STORE_DIR / "price-cache"))
            _background.update(budget=budget, whales=whales)
        return _background["whales"]  # type: ignore[no-any-return]


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
    data = _check_load()
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


MARKETS_PART = "markets"  # the sweep's market part, as sweep_parts names it (a leaderboard is "PNL MONTH POLITICS")


def sweep_left(d: Directory, now: float | None = None) -> tuple[list[tuple[str, str, str]], bool]:
    """What a sweep still has to read: the leaderboards, and whether the markets, not read to their end in the last
    DIRECTORY_SWEEP_S. After a restart (a deploy) a sweep picks up here instead of starting over."""
    done = d.parts_done_since((time.time() if now is None else now) - DIRECTORY_SWEEP_S)
    return [b for b in all_boards() if " ".join(b) not in done], MARKETS_PART not in done


def directory_sweep(stop: threading.Event | None = None) -> dict[str, Any]:
    """One sweep: followed wallets, then the SDK's ``find_wallets`` over what's left (``sweep_left``): the
    leaderboards, then markets of every size, kept as they come, at the background's rate. Each leaderboard is
    marked done once the sweep has moved past it and kept everything it found, and the markets once they're all
    read, so a restart loses at most the board it was on. Returns what it kept and the error if it stopped early."""
    d = directory()
    halt = stop or threading.Event()
    # a sweep cut off before sweeps kept their progress: the boards it got past count as read (once)
    d.backfill_parts([" ".join(b) for b in all_boards()], time.time() - DIRECTORY_SWEEP_S)
    boards, markets = sweep_left(d)
    started = d.sweep_started()
    print(f"directory sweep started: {len(boards)} of {len(all_boards())} leaderboards"
          f"{' and the markets' if markets else ''} to read ({BACKGROUND_RATE:g} reads a second at most, with the "
          f"rest of the background)", flush=True)  # fmt: skip
    sightings = new = 0
    error: str | None = None
    batch: list[Seen] = []

    def keep() -> None:
        nonlocal sightings, new
        if batch:
            n = d.add(batch)
            d.sweep_progress(started, len(batch), n)
            sightings, new = sightings + len(batch), new + n
            batch.clear()

    board: str | None = None  # the leaderboard being read: done once the sweep has moved past it

    def board_done() -> None:
        if board is not None:
            keep()  # everything found on it is kept before it's marked done
            d.part_done(board)

    try:
        batch += [Seen(w, "follow") for w in sorted({w for u in _load_follows()["users"].values() for w in u.get("follows") or []})]
        found = background().find_wallets(leaderboards=bool(boards), boards=boards, markets=markets)  # the budget paces it
        for s in found:
            if halt.is_set():
                error = "Stopped before it finished (the app was shutting down)."
                break
            where = s.where if s.source == "leaderboard" else None
            if where != board:  # on to the next board, or to the markets
                board_done()
                board = where
            batch.append(from_sighting(s))
            if len(batch) >= DIRECTORY_BATCH:
                keep()
        else:  # read to the end: the last board, and the markets if they were read
            board_done()
            if markets:
                keep()
                d.part_done(MARKETS_PART)
    except Exception as e:  # what it found stays; the next sweep picks up from the board it was on
        error = f"{type(e).__name__}: {e}"
    try:
        keep()  # the last batch, however the sweep ended
    except (sqlite3.Error, OSError) as e:
        error = error or f"{type(e).__name__}: {e}"
    d.sweep_finished(started, error)
    # one line for the logs (hosted: Railway's), so a sweep can be watched without the owner's /api/directory
    print(f"directory sweep {'stopped' if error else 'done'} in {(time.time() - started) / 60:.0f} min: "
          f"{sightings} sightings, {new} new wallets{f', error: {error}' if error else ''}",
          flush=True)  # fmt: skip
    return {"sightings": sightings, "new": new, "error": error}


def _directory_loop() -> None:
    """A sweep whenever a leaderboard or the markets weren't read in the last DIRECTORY_SWEEP_S (``sweep_left``),
    a minute after launch (the page's reads first); after a failed one, again in DIRECTORY_RETRY_S."""
    _directory_stop.wait(60)
    while not _directory_stop.is_set():
        try:
            boards, markets = sweep_left(directory())
            if boards or markets:
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


# ---- the directory, phase 3: every wallet evaluated in the background ----
# Each wallet due a look (Directory.due: recommended traders people are shown every 6 hours, any other one daily,
# wallets never looked at, newest found first, then the rest as they come due; the categories take turns, and at
# least NEW_SHARE of the reads go to wallets never looked at while any are left) gets a quick check first: one read of its
# latest trades, kept as the evidence (markets bought in 30 and 90 days, the last buy). A wallet with too few bets in
# the last 30 days for a score to say anything, no buy in 90, or no buy this week and none since its last score, is
# labelled why it isn't recommended and looked at again (daily; weekly when dormant). Every wallet is kept. The rest
# are scored as a trader page scores them (5-30 s, 10-80 reads) and kept. A recommended trader is scored again
# without the check: their score is what's shown.
# The schedule is kept in directory.db, so a restart carries on where it was. SIGNALS_EVALUATE=1 turns it on.

EVALUATE_ON = os.environ.get("SIGNALS_EVALUATE") == "1"
EVALUATE_WORKERS = 3  # wallets looked at side by side: a score mostly waits for answers, and the budget holds the rate
EVALUATE_BATCH = 30  # wallets taken from the queue at a time
EVALUATE_START_S = 120  # after launch, so the page's first reads come first
EVALUATE_IDLE_S = 60  # with nothing due, it looks again after this
EVALUATE_FAILS_IN_A_ROW = 5  # this many failed tries in a row and Polymarket is likely down...
EVALUATE_PAUSE_S = 600  # ...so it waits this long before the next
EVALUATE_LOG_S = 3600  # a line in the logs this often
LISTED_KEEP_S = 6 * 3600  # the boards read directly, when the sweep's are stale, are kept this long (31 reads)
OUTCOMES = ("scored", "not_enough_recent_evidence", "not_trading_lately", "dormant", "failed")
# Rescoring recommended traders comes first, but it mustn't take everything: while any wallet has never been looked
# at, at least NEW_SHARE of the evaluation's reads go to those (a round takes them before other recommended traders
# whenever their share has fallen below it). Reads are estimated: a quick look is 1, a full score SCORE_READS.
NEW_SHARE = 0.5
SCORE_READS = 50  # a full score's reads, about (measured 49 over a random 80, 2026-10-14)
READS_DECAY = 0.98  # each round, older reads count a little less, so the share follows what's due lately
_evaluate_state: dict[str, Any] = {"running": False, "error": None, "paused_until": None, "leaderboards": None,
                                   "since_launch": dict.fromkeys(OUTCOMES, 0), "reads": {"new": 0.0, "other": 0.0}}  # fmt: skip


def presented_wallets() -> frozenset[str]:
    """The recommended traders people are shown, whose scores are kept PRESENTED_RESCORE_S fresh: those on the
    Recommended list (the check's, or the directory's capped at its limit) and every wallet someone follows (its
    alerts and page). Bounded however big the directory grows, so rescoring them never takes the whole budget;
    Explore's "Passed our checks" lists the rest, rescored daily."""

    def read() -> frozenset[str]:
        rec = {s["wallet"] for s in (_check_result() or {}).get("scores", []) if s.get("segment") in RECOMMENDED}
        return frozenset(rec | {w for u in _load_follows()["users"].values() for w in u.get("follows") or []})

    return cached("presented", 60, read)  # type: ignore[no-any-return]


def _check_result() -> dict[str, Any] | None:
    """The list the Recommended tab shows (``_worth_load``: the check's result, or with SIGNALS_DISCOVERY the
    directory's, capped)."""
    return _worth_load()


def _reserve_new() -> bool:
    """Whether wallets never looked at get the next round first: their share of the recent reads is under NEW_SHARE."""
    r = _evaluate_state["reads"]
    total = r["new"] + r["other"]
    return total > 0 and r["new"] / total < NEW_SHARE


def _count_reads(kind: str, outcome: str) -> None:
    quick = 0 if kind in ("presented", "recommended") else 1
    reads = quick + (SCORE_READS if outcome == "scored" else 0)
    _evaluate_state["reads"]["new" if kind == "new" else "other"] += reads


def quick_check(wallet: str, scored_at: float | None = None, now: float | None = None) -> QuickLook:
    """One read of a wallet's latest trades: the evidence, and whether a score is worth its reads (``quick_look``)."""
    now = time.time() if now is None else now
    got = background().trades("polymarket", wallet, limit=QUICK_TRADES)
    trades = [(t.at.timestamp(), t.action == "buy", t.market.partition(":")[0], t.usd) for t in got]
    return quick_look(trades, now, read_all=len(got) < QUICK_TRADES, scored_at=scored_at)


def listed_now() -> frozenset[str]:
    """The wallets the Worth following check would call "on the leaderboard" (uselayer's find_candidates: the
    monthly profit board's top 1,000, the weekly and all-time profit and monthly volume boards' top 50, each
    category's monthly top 50), read now on the background's budget and kept LISTED_KEEP_S. For when the sweep's
    sightings are stale or the sweep is off, so a score's on_leaderboard (and so its tier) doesn't change with them."""

    def read() -> frozenset[str]:
        poly = background()._poly  # the SDK's leaderboard reader, as its own finder uses it
        out: set[str] = set()
        for order, period, category, places in CHECK_BOARDS:
            for offset in range(0, places, 50):
                rows = poly.leaderboard(order, period, 50, category=category, offset=offset)
                out |= {str(r.get("proxyWallet") or "").lower() for r in rows} - {""}
                if len(rows) < 50:
                    break
        return frozenset(out)

    return cached("listed", LISTED_KEEP_S, read)  # type: ignore[no-any-return]


def _on_leaderboard(due: list[dict[str, Any]], now: float | None = None) -> None:
    """Each due wallet's on_leaderboard: from the sweep's sightings while they're fresh (``Directory.due``), else
    from the boards read now. If those can't be read either, the sightings stand, and /api/directory says so."""
    now = time.time() if now is None else now
    seen = directory().boards_seen_at()
    if seen is not None and now - seen < LISTED_FRESH_S:
        _evaluate_state["leaderboards"] = "sweep"
        return
    try:
        listed = listed_now()
    except VenueError as e:
        _evaluate_state["leaderboards"] = f"stale, and couldn't be read: {e}"
        return
    _evaluate_state["leaderboards"] = "read"
    for item in due:
        item["on_leaderboard"] = item["wallet"] in listed


def evaluate_one(item: dict[str, Any]) -> str:
    """One due wallet (from ``Directory.due``): the quick check, then, if it's worth a score, its score, kept in
    the directory. Returns what came of it, one of OUTCOMES. Any error is kept as a failed try, so one wallet
    never holds up the rest: it's tried again later each time it fails again in a row."""
    d, w = directory(), item["wallet"]
    try:
        if item["kind"] not in ("presented", "recommended"):
            look = quick_check(w, item.get("scored_at"))
            d.quick_checked(w, look)
            if not look.score:
                return look.reason  # type: ignore[return-value]
        scored = background().score(w, on_leaderboard=item["on_leaderboard"]).to_dict()
    except Exception as e:  # VenueError mostly (the read's retries already spent)
        d.score_failed(w, f"{type(e).__name__}: {e}")
        return "failed"
    d.keep_scores([_with_odds(scored) | {"format": WORTH_FORMAT}], "background")
    return "scored"


def evaluate_round(stop: threading.Event | None = None) -> dict[str, Any]:
    """The next EVALUATE_BATCH due wallets, EVALUATE_WORKERS at a time. Stops early after EVALUATE_FAILS_IN_A_ROW
    failed tries in a row. Returns how many were due, what came of those looked at (by outcome), and how many
    failed in a row at the end."""
    halt = stop or threading.Event()
    reads = _evaluate_state["reads"]
    for k in reads:
        reads[k] *= READS_DECAY
    due = directory().due(EVALUATE_BATCH, presented=presented_wallets(), new_first=_reserve_new())
    if due:
        _on_leaderboard(due)
    done = dict.fromkeys(OUTCOMES, 0)
    lock = threading.Lock()
    in_a_row = 0

    def one(item: dict[str, Any]) -> None:
        nonlocal in_a_row
        if halt.is_set() or in_a_row >= EVALUATE_FAILS_IN_A_ROW:
            return
        got = evaluate_one(item)
        with lock:
            _count_reads(item["kind"], got)
            done[got] += 1
            _evaluate_state["since_launch"][got] += 1
            in_a_row = in_a_row + 1 if got == "failed" else 0

    with ThreadPoolExecutor(max_workers=EVALUATE_WORKERS, thread_name_prefix="evaluate") as pool:
        list(pool.map(one, due))
    return {"due": len(due), "done": done, "failed_in_a_row": in_a_row}


def _evaluate_loop() -> None:
    """Rounds of ``evaluate_round`` while the app runs: a minute apart when nothing is due, EVALUATE_PAUSE_S after
    a run of failures or an error, and a line in the logs each EVALUATE_LOG_S with what it did."""
    _directory_stop.wait(EVALUATE_START_S)
    print(f"directory evaluation started ({BACKGROUND_RATE:g} reads a second at most, with the sweep)", flush=True)
    _evaluate_state["running"] = True
    logged, hour = time.time(), dict.fromkeys(OUTCOMES, 0)
    while not _directory_stop.is_set():
        pause = 0.0
        try:
            r = evaluate_round(_directory_stop)
            hour = {k: hour[k] + r["done"][k] for k in OUTCOMES}
            _evaluate_state["error"] = None
            if r["failed_in_a_row"] >= EVALUATE_FAILS_IN_A_ROW:
                pause = EVALUATE_PAUSE_S
                print(f"directory evaluation: {r['failed_in_a_row']} failed in a row, waiting "
                      f"{EVALUATE_PAUSE_S // 60} min", flush=True)  # fmt: skip
            elif not r["due"]:
                pause = EVALUATE_IDLE_S
        except Exception as e:  # the directory couldn't be read or written: try again later
            _evaluate_state["error"] = f"{type(e).__name__}: {e}"
            print(f"directory evaluation error: {_evaluate_state['error']}", flush=True)
            pause = EVALUATE_PAUSE_S
        if time.time() - logged >= EVALUATE_LOG_S:
            budget = _background["budget"].state() if "budget" in _background else {}
            try:
                due = directory().due_counts(presented=presented_wallets())
            except (sqlite3.Error, OSError):
                due = {}
            print(f"directory evaluation, last {(time.time() - logged) / 60:.0f} min: "
                  f"{', '.join(f'{n} {k}' for k, n in hour.items())}; due now: "
                  f"{', '.join(f'{n} {k}' for k, n in due.items())}; {budget.get('per_second')} reads a second, "
                  f"{budget.get('rate_limited')} rate limits so far", flush=True)  # fmt: skip
            logged, hour = time.time(), dict.fromkeys(OUTCOMES, 0)
        if pause:
            _evaluate_state["paused_until"] = time.time() + pause
            _directory_stop.wait(pause)
            _evaluate_state["paused_until"] = None
    _evaluate_state["running"] = False


# ---- the directory, phase 5: discovery from the directory ----
# With SIGNALS_DISCOVERY=directory, Traders, the Feed's Recommended, alerts and trader pages read the traders
# checked from the directory: each wallet's latest score from the last DISCOVERY_FRESH_S, whoever worked it out (the
# Worth following check, the background evaluation, a trader page), in the check's order (tier, then the strength
# of their edge), at most DISCOVERY_LIMIT, in the same shape as the check's result: those in the picked markets
# first (/api/worth?markets=…, onboarding's interests, matched as the page's matchesInterests), so a pick gets the
# best of the whole directory, not of a list cut for everyone. The limit keeps the page and the Feed's poller the
# size they are; Explore browses the whole directory, never limited by it. A score is the same score wherever it's
# read, so a wallet gets the same tier, section and copy status from either; the directory picks no one itself and
# its scoring and thresholds are the SDK's, unchanged.
# For now the check keeps running either way, so going back to it (SIGNALS_DISCOVERY=check, the default) shows a
# fresh result at once, and with no fresh score in the directory its result is shown anyway. /api/discovery
# compares the two, for the owner. Once the directory has carried Signals for a while, the check can go.

DISCOVERY = os.environ.get("SIGNALS_DISCOVERY", "check")  # "directory": discovery from the directory
# A score older than this isn't picked: twice the background evaluation's daily rescore of a recommended trader
# nobody is shown or follows. Once shown, they're rescored every RESCORE_S, so a shown trader's score stays well
# inside it (/api/discovery counts any shown older than 2 × RESCORE_S). One window for every candidate, so nobody
# drops out and back in as their score ages.
DISCOVERY_FRESH_S = 2 * 86400
# traders served: as many as the check scores, so the page weighs what it did (~5 KB a trader) and the Feed's
# poller reads as many Recommended traders as it did
DISCOVERY_LIMIT = int(os.environ.get("SIGNALS_DISCOVERY_LIMIT", str(WORTH_WALLETS)))
DISCOVERY_TTL_S = 60
DISCOVERY_APART_S = WORTH_MAX_AGE_S  # validation: a score worked out apart this close to the check's is compared
_discovery_state: dict[str, Any] = {"error": None}


def _interests_of(markets: str) -> tuple[str, ...]:
    """The picked markets from ``?markets=Politics,Sports``, as onboarding offers them, in its order."""
    picked = {m.strip() for m in markets.split(",")}
    return tuple(c for c in INTEREST_CHOICES if c in picked)


def _upgraded(s: dict[str, Any]) -> dict[str, Any]:
    """A kept score judged by today's rules, as the check's own file is (``_worth_upgrade``)."""
    return _worth_upgrade({"format": s.get("format") or 0, "scores": [s]})["scores"][0]  # type: ignore[no-any-return]


def _in_markets(row: dict[str, Any], interests: tuple[str, ...]) -> bool:
    """The page's matchesInterests: an interest is their most-traded category or one they're strong in."""
    return not interests or row["main"] in interests or any(c in interests for c in row["strong"])


def discovery_result(interests: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """The directory's traders checked, in the shape of the check's result (scores, counts, coverage, sources,
    failed, started and finished: the oldest and newest score shown), plus ``source`` and how many fresh scores
    there were to pick from (``considered``). None when it has no fresh score or can't be read: the check's then.
    Kept DISCOVERY_TTL_S per pick."""

    def build() -> dict[str, Any] | None:
        d, since = directory(), time.time() - DISCOVERY_FRESH_S
        rows = d.latest_scores(since)
        if not rows:
            return None
        by = {r["wallet"]: r for r in rows}
        old = [w for w, r in by.items() if (r["format"] or 0) < WORTH_FORMAT]
        for w, s in d.scores_of(old).items():  # kept by an older uselayer: its tier by today's rules
            by[w]["segment"] = _upgraded(s)["segment"]
        order = list(SEGMENTS)

        def rank(r: dict[str, Any]) -> tuple[int, float]:  # as the check's: by tier, then the strength of their edge
            return order.index(r["segment"]) if r["segment"] in order else len(order), -(r["edge_z"] or 0)

        # those in the picked markets first, then by tier (recommended first) and edge; Explore lists the rest
        picked = sorted(rows, key=lambda r: (not _in_markets(r, interests), *rank(r)))[:DISCOVERY_LIMIT]
        kept = d.scores_of(r["wallet"] for r in picked)
        scores = [_upgraded(kept[r["wallet"]]) for r in sorted(picked, key=rank) if r["wallet"] in kept]
        at = [s["scored_at"] for s in scores]
        return {
            "scores": scores,
            "counts": {k: sum(1 for s in scores if s.get("segment") == k) for k in SEGMENTS},
            "coverage": {k: sum((s.get("coverage") or {}).get(k, 0) for s in scores)
                         for k in ("bets", "priced_1m", "priced_5m", "priced_1h", "combos")},  # fmt: skip
            "sources": {"leaderboard": sum(1 for s in scores if s.get("on_leaderboard")),
                        "beyond_leaderboards": sum(1 for s in scores if not s.get("on_leaderboard"))},  # fmt: skip
            "failed": d.failed_since(since),
            "started": datetime.fromtimestamp(min(at), UTC).isoformat(),
            "finished": datetime.fromtimestamp(max(at), UTC).isoformat(),
            "format": WORTH_FORMAT,
            "source": "directory",
            "considered": len(rows),
            "interests": list(interests),
        }

    try:
        got = cached(f"discovery:{','.join(interests)}", DISCOVERY_TTL_S, build)
    except (sqlite3.Error, OSError) as e:  # the check's result is shown meanwhile
        _discovery_state["error"] = f"{type(e).__name__}: {e}"
        return None
    _discovery_state["error"] = None
    return got  # type: ignore[no-any-return]


def _recommended(data: dict[str, Any] | None) -> set[str]:
    return {s["wallet"] for s in (data or {}).get("scores", []) if s.get("segment") in RECOMMENDED}


def discovery_compare() -> dict[str, Any]:
    """The check and the directory side by side, for the owner while the directory is validated: which one is
    shown; each one's traders and recommended traders by section; who is recommended by only one, and why; and
    every wallet the check scored that was also scored apart from it (a trader page, the background evaluation)
    within DISCOVERY_APART_S, compared: the same tier, section and copy verdict, or not, with the differences."""
    from signals.directory import RESCORE_S  # how often a trader shown is rescored

    check, served = _check_load(), _worth_load()
    view = discovery_result()
    rows = {r["wallet"]: r for r in directory().latest_scores(time.time() - DISCOVERY_FRESH_S)}

    now = time.time()

    def summary(data: dict[str, Any] | None) -> dict[str, Any] | None:
        if data is None:
            return None
        scores = data.get("scores", [])
        rec = [s for s in scores if s.get("segment") in RECOMMENDED]
        boards = directory().on_boards(s["wallet"] for s in scores)
        done = _iso_ts(data["finished"]) if data.get("finished") else now
        ages = sorted((now - s.get("scored_at", done)) / 3600 for s in scores)
        return {"traders": len(scores), "recommended": len(rec),
                "by_section": {k: sum(1 for s in scores if section_of(s.get("segment")) == k) for k in SECTIONS},
                "started": data.get("started"), "finished": data.get("finished"),
                # where they come from: the check's own "on the leaderboard" (the top 1,000 a month and so on),
                # any board a sweep has seen them on, or none of the 88 at all
                "on_leaderboard": sum(1 for s in scores if s.get("on_leaderboard")),
                "seen_on_any_board": len(boards),
                "recommended_on_leaderboard": sum(1 for s in rec if s.get("on_leaderboard")),
                "recommended_on_no_board": sum(1 for s in rec if s["wallet"] not in boards),
                "scored_by": dict(Counter(s.get("via", "check") for s in scores)),
                "recommended_by_category": dict(Counter(_main(s) or "None" for s in rec).most_common()),
                "age_hours": None if not ages else {"newest": round(ages[0], 1), "median": round(ages[len(ages) // 2], 1),
                                                    "oldest": round(ages[-1], 1)},  # fmt: skip
                # shown recommended traders are rescored every RESCORE_S: older than twice that, rescoring is failing
                "recommended_older_than_12h": sum(1 for s in rec if now - s.get("scored_at", done) > 2 * RESCORE_S)}  # fmt: skip

    in_check, in_view = _recommended(check), _recommended(view)
    pool_rec = {w for w, r in rows.items() if r["segment"] in RECOMMENDED}
    checked = {s["wallet"]: s for s in (check or {}).get("scores", [])}

    def why_not_in_view(w: str) -> str:
        r = rows.get(w)
        if r is None:
            return "no_fresh_score"
        return "over_limit" if r["segment"] in RECOMMENDED else "scored_since_another_tier"

    def why_not_in_check(w: str) -> str:
        return "check_scored_another_tier" if w in checked else "not_in_checks_list"

    apart: dict[str, Any] = {"pairs": 0, "same_tier": 0, "same_section": 0, "same_copyable": 0, "differ": []}
    if check and check.get("finished"):
        at = _iso_ts(check["finished"])
        for w, h in directory().scored_apart(checked, at, DISCOVERY_APART_S).items():
            s = checked[w]
            same = {"same_tier": s.get("segment") == h["segment"],
                    "same_section": section_of(s.get("segment")) == section_of(h["segment"]),
                    "same_copyable": s.get("copyable") == h["copyable"]}  # fmt: skip
            apart["pairs"] += 1
            for k, v in same.items():
                apart[k] += v
            if not all(same.values()):
                apart["differ"].append({"wallet": w, "check": s.get("segment"), "apart": h["segment"], "via": h["via"],
                                        "check_copyable": s.get("copyable"), "apart_copyable": h["copyable"],
                                        "check_bets": s.get("bets"), "apart_bets": h["bets"],
                                        "hours_apart": round((h["scored_at"] - at) / 3600, 1)})  # fmt: skip
        apart["differ"] = sorted(apart["differ"], key=lambda x: abs(x["hours_apart"]))[:25]
    return {
        "flag": DISCOVERY,
        "serving": None if served is None else served.get("source", "check"),
        "fresh_s": DISCOVERY_FRESH_S,
        "limit": DISCOVERY_LIMIT,
        "error": _discovery_state["error"],
        "check": summary(check),
        "directory": None if view is None else summary(view) | {"considered": view["considered"]},
        "recommended": {
            "both": len(in_check & in_view),
            "only_check": dict(Counter(why_not_in_view(w) for w in in_check - in_view)),
            "only_directory": dict(Counter(why_not_in_check(w) for w in in_view - in_check)),
        },
        "scored_apart": apart,
        # everything the directory picks from: every fresh score, before the limit
        # (recommended_over_limit: recommended, but past the limit with no pick: on Explore, not on Traders' list)
        "pool": {"scores": len(rows), "recommended": len(pool_rec), "recommended_over_limit": len(pool_rec - in_view),
                 "recommended_seen_on_any_board": len(directory().on_boards(pool_rec)),
                 "scored_by": dict(Counter(r["via"] for r in rows.values())),
                 "recommended_by_category": dict(Counter(r["main"] or "None" for w, r in rows.items()
                                                         if w in pool_rec).most_common())},  # fmt: skip
        # each onboarding pick: the recommended traders in those markets the check's list has, and the directory's
        # list for that pick has (matched as the page matches them)
        "by_interest": {c: {"check": sum(1 for s in (check or {}).get("scores", []) if s.get("segment") in RECOMMENDED
                                         and _in_markets(_row(s), (c,))),
                            "directory": sum(1 for s in (discovery_result((c,)) or {}).get("scores", [])
                                             if s.get("segment") in RECOMMENDED and _in_markets(_row(s), (c,)))}
                        for c in INTEREST_CHOICES},  # fmt: skip
    }


def _main(s: dict[str, Any]) -> str | None:
    """A score's most-traded category, as the page reads it (its first)."""
    return ((s.get("categories") or [{}])[0] or {}).get("category")


def _row(s: dict[str, Any]) -> dict[str, Any]:
    """A whole score as ``_in_markets`` reads a directory row."""
    return {"main": _main(s), "strong": s.get("strong_categories") or []}


@app.get("/api/discovery")
def discovery_route(request: Request, response: Response) -> dict[str, Any]:
    """The check and the directory compared (``discovery_compare``): the owner's, as /api/directory is."""
    _owner(request)
    response.headers["Cache-Control"] = "private, no-store"
    return discovery_compare()


@app.get("/api/directory")
def directory_stats(request: Request, response: Response) -> dict[str, Any]:
    """How many wallets the directory has found, and where, and how its evaluation is going: counts only, never a
    wallet. The owner's, as /api/metrics is: locally, or with SIGNALS_METRICS_KEY."""
    _owner(request)
    response.headers["Cache-Control"] = "private, no-store"
    stats = cached("directory", DIRECTORY_STATS_TTL_S, lambda: directory().stats())
    boards, markets = sweep_left(directory())
    evaluating = {k: v for k, v in _evaluate_state.items()} | {"on": EVALUATE_ON}
    if "budget" in _background:
        evaluating["budget"] = _background["budget"].state()
    return stats | {"sweep_left": {"leaderboards": len(boards), "of": len(all_boards()), "markets": markets},
                    "sweeping": _directory_state["running"], "last_error": directory().last_error(),
                    "evaluating": evaluating}


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
    out["copy_label"], out["copy_why"] = copy_status(s)
    out["copy_ok"] = out["copy_label"] == LABEL_COPYABLE
    out["section"] = section_of(s.get("segment"))
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
SECTIONS = ("sharp", "rising")  # Traders' sections, in order: Sharps (Proven and Quiet), Rising


def section_of(segment: str | None) -> str | None:
    """Which section of Traders a recommended trader is listed in, by their tier alone: ``"sharp"`` (Proven
    and Quiet sharps) or ``"rising"``. Whether copying pays never moves a trader between sections: that's their
    copy status (``copy_status``), shown in every row. None for a trader we don't recommend, who is only in
    "See all checked traders"."""
    if segment not in RECOMMENDED:
        return None
    return "rising" if segment == "rising" else "sharp"  # "too_fast", kept before uselayer 0.10, was a sharp


# A trader's copy status, a separate question from their tier, in the same words everywhere (Traders, a trader's
# page, the Feed, the copy routes): Copyable; Costly to copy (the whole 95% range of what copies kept is below
# zero); Copy not proven (it isn't reliably above zero); or Not offered (copies kept money, but the trader's
# own record doesn't pass the checks).
LABEL_COPYABLE, LABEL_NOT_PROVEN, LABEL_COSTLY, LABEL_NOT_OFFERED = (
    "Copyable", "Copy not proven", "Costly to copy", "Not offered",
)  # fmt: skip


def copy_status(s: dict[str, Any]) -> tuple[str, str | None]:
    """A trader's copy status and, unless Copyable, why: one plain sentence that starts with it ("Costly to
    copy: …"). Copyable takes a recommended tier (Proven sharp, Quiet sharp, Rising) and copies a few minutes
    late reliably above zero after fees (the SDK's ``copyable``: the bottom of the 95% range above zero). The
    rest is said from the copy result alone, whatever the tier, as a reading of the past."""
    if s.get("segment") in COPY_TIERS and s.get("copyable") == "yes":
        return LABEL_COPYABLE, None
    copy = Stat.from_dict(s["copy"]) if s.get("copy") else None
    if copy is not None and copy.n >= COPY_BETS and copy.high < 0:
        return LABEL_COSTLY, (
            f"{LABEL_COSTLY}: buying after them lost money in the trades we checked, even at the best end of our"
            " range. Usually the price had already moved by the time you could buy. This is about the past, not a promise."
        )
    if s.get("copyable") == "unknown":
        return LABEL_NOT_PROVEN, f"{LABEL_NOT_PROVEN}: we had too few of their trades to check whether buying after them makes money."
    if s.get("copyable") != "yes":
        return LABEL_NOT_PROVEN, f"{LABEL_NOT_PROVEN}: buying after them sometimes made money and sometimes lost it, so we can't tell."
    if s.get("segment") == "inactive":
        return LABEL_NOT_OFFERED, f"{LABEL_NOT_OFFERED}: they haven't traded in the last 7 days."
    return LABEL_NOT_OFFERED, f"{LABEL_NOT_OFFERED}: buying after them made money, but the trader doesn't pass our skill checks."


def copy_offer(s: dict[str, Any]) -> tuple[bool, str | None]:
    """Whether Signals offers copying this trader (Copyable), and if not, why: ``copy_status`` as a yes or no.
    One rule for Traders, a trader's page, the Feed and the copy routes."""
    label, why = copy_status(s)
    return label == LABEL_COPYABLE, why


def copy_fields(s: dict[str, Any] | None) -> dict[str, Any]:
    """A trader's copy status as a Feed card or a follow carries it: ``copy_ok`` and ``copy_label``. No score,
    no status: copying isn't offered for a trader the last check didn't score."""
    label = copy_status(s)[0] if s else None
    return {"copy_ok": label == LABEL_COPYABLE, "copy_label": label}


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
def worth(markets: str = "") -> dict[str, Any]:
    """Every scored wallet without its bets, plus whether a refresh is running. ``markets``: the picked
    interests (``Politics,Sports``), whose traders come first when Traders reads the directory."""
    data = _worth_load(_interests_of(markets))
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
def worth_wallet(wallet: str, request: Request, score: int = 1) -> dict[str, Any]:
    """One wallet's score with its checked bets: from the last refresh, else (Explore on) a fresh one kept in the
    directory, else scored now (5–30 s, ``check_now``). With ``score=0`` (opened from Explore) a wallet without a
    fresh score isn't scored: its page shows what's known and a "Check now" button instead. With Explore on, the
    wallet's directory row comes too (``directory``: the evidence, where it was found)."""
    w = wallet.lower()
    data = _worth_load()
    s = next((x for x in (data or {}).get("scores", []) if x["wallet"] == w), None)
    row = _explore_row(w) if EXPLORE_ON and _WALLET.fullmatch(w) else None
    if s is None and EXPLORE_ON and _WALLET.fullmatch(w):
        s = _stored_score(w)
    if s is None and not score:
        return {"wallet": w, "unchecked": True, "name": (row or {}).get("name"), "directory": row}
    if s is None:
        if _WALLET.fullmatch(w):  # a wallet the last check didn't have, opened here: pasted into search, mostly
            _directory_note_later([Seen(w, "search")])
        s = check_now(w, request)
    b = _board(s)
    return with_links(s) | b | {"why": why_lines(s | b)} | ({"directory": row} if row is not None else {})


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
    # Skill and track record only: whether copying pays is the separate "copying" line below.
    section = s.get("section")
    if section == "rising":  # hedged: few trades yet
        listed = f"Early signs: the price has tended to move their way after they buy{f', over {edge_n} trades so far' if edge_n else ''}, too few to be sure yet."
    elif section == "sharp":
        listed = f"The price tends to move their way within minutes after they buy{over}, and it holds without their best trades."
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
    copying = (f"{LABEL_COPYABLE}: buying a few minutes after them made money in the trades we checked, often enough that it isn't just luck. This is about the past, not a promise."
               if ok else why or f"{LABEL_NOT_PROVEN}.")  # fmt: skip
    return {"trades": trades, "how": how_line[:1].upper() + how_line[1:] + "." if how_line else None,
            "listed": listed, "copying": copying}  # fmt: skip


# ---- Explore (SIGNALS_EXPLORE=1): the directory on the Traders page ----
# Every wallet the directory found somewhere public, in three groups: limited evidence (too few bets to judge, from a
# quick check's count or a score), checked (a score's verdict, or a quick check's: dormant, paused), and, only when
# asked for, not checked yet. Sorted and filtered by facts only; nothing is ranked or newly recommended. Browsing
# reads only directory.db, never Polymarket; a trader page reuses a score the directory keeps while it's fresh
# (STORED_FRESH_S), and "Check now" scores one on the spot, a few at a time (CHECKS_AT_ONCE, CHECKS_PER_CLIENT).

EXPLORE_ON = os.environ.get("SIGNALS_EXPLORE") == "1"
EXPLORE_PAGE = 50
EXPLORE_MAX_OFFSET = 2000
EXPLORE_TTL_S = 60  # a page of Explore, kept this long (the directory changes a few wallets a minute)
EXPLORE_COUNTS_TTL_S = 300
STORED_FRESH_S = 2 * 86400  # a stored score this new is shown as it is (a recommended tier's: ``fresh_for``)
CHECKS_AT_ONCE = 3  # "Check now" (a trader page scoring a wallet on the spot): this many at once, site-wide...
CHECK_WAIT_S = 20  # ...waiting this long for a turn before saying "try again"
CHECKS_PER_CLIENT = (6, 600)  # hosted: at most 6 checks per 10 minutes from one address
_checks = threading.BoundedSemaphore(CHECKS_AT_ONCE)
_check_locks: dict[str, threading.Lock] = {}
_check_times: dict[str, list[float]] = {}


def _explore_row(wallet: str) -> dict[str, Any] | None:
    try:
        return directory().explore_row(wallet)
    except (sqlite3.Error, OSError):
        return None


def _stored_score(wallet: str, now: float | None = None) -> dict[str, Any] | None:
    """The directory's score for a wallet while it's fresh and of today's format: a recommended tier's for
    ``fresh_for`` (12 hours for one people are shown, 48 for any other: twice how often each is scored again), any
    other's for STORED_FRESH_S. Else None."""
    now = time.time() if now is None else now
    try:
        s = directory().score(wallet)
    except (sqlite3.Error, OSError):
        return None
    if s is None or s.get("format") != WORTH_FORMAT:
        return None
    fresh = fresh_for(wallet.lower() in presented_wallets()) if s.get("segment") in RECOMMENDED else STORED_FRESH_S
    return s | {"stored_at": s["scored_at"]} if now - s["scored_at"] < fresh else None


def _client(request: Request | None) -> str:
    if request is None:
        return "local"
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def check_now(wallet: str, request: Request | None = None) -> dict[str, Any]:
    """A wallet scored on the spot (``_score_of``), as a trader page does for one nobody has scored lately. One
    score per wallet at a time (others asking wait for it), CHECKS_AT_ONCE site-wide, and hosted, CHECKS_PER_CLIENT
    from one address: past those, a 429 with a plain "try again", so pages never queue up the site's reads."""
    w = wallet.lower()
    if w in _worth_one:
        return _worth_one[w]
    if HOSTED:
        who, (most, per) = _client(request), CHECKS_PER_CLIENT
        now = time.time()
        recent = [t for t in _check_times.get(who, []) if now - t < per]
        if len(recent) >= most:
            raise HTTPException(429, detail="You've checked a lot of traders in the last few minutes. Try again in a few minutes.")
        _check_times[who] = [*recent, now]
        if len(_check_times) > 5000:
            _check_times.clear()
    lock = _check_locks.setdefault(w, threading.Lock())
    with lock:
        if w in _worth_one:  # someone else just checked it
            return _worth_one[w]
        if not _checks.acquire(timeout=CHECK_WAIT_S):
            raise HTTPException(429, detail="Lots of traders are being checked right now. Try again in a minute.")
        try:
            return _score_of(w)
        finally:
            _checks.release()
            if len(_check_locks) > 5000:
                _check_locks.clear()


@app.get("/api/traders")
def explore(group: str = "passed", q: str = "", category: str = "", active: int = 0, min_usd: float = 0,
            board: str = "", makers: int = 0, unchecked: int = 0, sort: str = "recent",
            offset: int = 0) -> dict[str, Any]:  # fmt: skip
    """A page of Explore (see Directory.browse), with how many each group lists. From directory.db only, never
    Polymarket. A search (``q``) looks through the limited-evidence and checked groups, and the not-checked-yet one
    too with ``unchecked=1``; otherwise ``unchecked_matches`` says how many of those it would find (up to 20)."""
    if not EXPLORE_ON:
        raise HTTPException(404, detail="Not found")
    if group not in EXPLORE_GROUPS or sort not in SORTS or not 0 <= offset <= EXPLORE_MAX_OFFSET:
        raise HTTPException(400, detail="Unknown group, sort or page.")
    q = q.strip()[:64]
    if q and len(q) < 2:
        raise HTTPException(400, detail="Type at least 2 letters to search.")
    search_in = ("limited", "checked", "recommended", *(("unchecked",) if unchecked else ()))
    args = (group, q.lower(), category, active, min_usd, board, makers, unchecked, sort, offset)
    page = cached(f"explore:{args}", EXPLORE_TTL_S, lambda: directory().browse(
        group, q=q, category=category or None, active_days=active or None, min_usd=min_usd or None,
        board=board or None, makers=bool(makers), sort=sort, limit=EXPLORE_PAGE, offset=offset,
        search_in=search_in))  # fmt: skip
    counts = cached("explore:counts", EXPLORE_COUNTS_TTL_S, lambda: directory().explore_counts())
    others = None
    if q and not unchecked:
        others = cached(f"explore:unchecked:{q.lower()}", EXPLORE_TTL_S, lambda: len(directory().browse(
            "unchecked", q=q, limit=20, search_in=("unchecked",))["rows"]))  # fmt: skip
    return page | {"rows": [_passed_row(r) for r in page["rows"]], "counts": counts, "offset": offset,
                   "unchecked_matches": others}


def _passed_row(r: dict[str, Any]) -> dict[str, Any]:
    """A row of a recommended tier, with its three answers kept apart, each from the stored score: profitability
    (trading profit over the score's 30 days, in the row), skill (the tier, edge and bets, in the row) and
    copyability (``copy_status``, the Recommended list's own rule). And whether it's on the Recommended list."""
    if r["group"] not in ("passed", "recommended"):
        return r
    s = _stored_or_none(r["wallet"])
    rec = {x["wallet"] for x in (_check_result() or {}).get("scores", []) if x.get("segment") in RECOMMENDED}
    return r | {"copy_label": copy_status(s)[0] if s else None, "on_recommended": r["wallet"] in rec}


def _stored_or_none(wallet: str) -> dict[str, Any] | None:
    try:
        return directory().score(wallet)
    except (sqlite3.Error, OSError):
        return None


def _score_of(wallet: str) -> dict[str, Any]:
    """A wallet's score: from the traders checked (with discovery from the directory, any fresh score it keeps),
    or scored now (5–30 s) and kept until the next check."""
    w = wallet.lower()
    for s in (_worth_load() or {}).get("scores", []):
        if s["wallet"] == w:
            return s  # type: ignore[no-any-return]
    if DISCOVERY == "directory" and _WALLET.fullmatch(w) and (kept := _stored_score(w)) is not None:
        return kept  # the directory's own, as Explore's pages reuse it
    if w not in _worth_one:
        try:
            _worth_one[w] = checker().score(w).to_dict()  # its own share of Polymarket's reads, never the Feed's
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

FEED_SEGMENTS = RECOMMENDED  # Traders' two sections: Sharps and Rising ("inactive" isn't in it)
RECOMMENDED_MIN_USD = 1000.0  # the Feed's Recommended shows only trades this big: fewer, bigger trades
FEED_HOURS = 24
GET_IN = 0.03  # at most this far above their price you can still get in: an alert's "$10 gets you" limit too
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


LIVE_POLL_S = 30  # a round of the poller: everyone followed is read each round (alerts want it fresh)...
LIVE_REC_S = 60  # ...and a Recommended trader nobody follows this often, as the round's slot allows
LIVE_PRICE_S = 90  # open positions (the price now) read again once this old, or as soon as their trades change
LIVE_STALE_S = 120  # a trader's page check (one_bet) reads Polymarket itself when what's kept is older
LIVE_ALERT_S = 60  # alerts want it fresher
LIVE_SHOW_S = 1800  # the Feed shows what the poller kept up to this old (saying how old); older, "couldn't be read"
LIVE_PAGE_READS = 3  # a Feed view reads at most this many traders the poller hasn't read yet; the rest are pending
LIVE_KEEP_S = 300  # a trader nobody recommends or follows any more drops out of the cache after this
LIVE_REC_FLOOR = 4  # Recommended reads a round even when everyone followed fills the slot, so it never stops
LIVE_TRADES, LIVE_GAP_TRADES = 100, 500  # a trader's latest trades per read; further back when there's a gap
_live_priced: dict[str, float] = {}  # wallet -> when (monotonic) its open positions were last read
_live_failed: dict[str, float] = {}  # wallet -> when (monotonic) the poller last couldn't read it
_live_hinted: set[str] = set()  # the shared big-trades feed's trades that already moved their trader up
_live_round: dict[str, float] = {"ended": float("-inf")}  # when (monotonic) the poller's last round ended
_followed_reading = threading.Event()  # set while the poller reads followed traders: the background and checks give way


def _reading_followed() -> bool:
    return _followed_reading.is_set()


_check_budget = Budget(CHECK_RATE, yield_to=_reading_followed)
_checker: dict[str, Whales] = {}


def checker() -> Whales:
    """The SDK's reads for scoring a wallet on the spot ("Check now", a trader page): the engine's own client, with
    each Polymarket read also taken from _check_budget (CHECK_RATE a second, half while followed traders are read),
    whichever of the SDK's threads makes it, as ``background`` does for the background."""
    with _background_lock:
        if "whales" not in _checker:
            _checker["whales"] = Whales(BackgroundHttp(reads._http, _check_budget), reads._layer, reads)  # type: ignore[arg-type]
        return _checker["whales"]


def _open_positions(wallet: str) -> list[Any]:
    """A trader's open positions: the SDK's TraderDetail.positions in one read, without its profile and stats."""
    return reads.whales.positions("polymarket", wallet, limit=50)


def _live_fetch(wallet: str, priced: bool) -> TraderDetail:
    """A trader's last 100 trades, and their open positions only when there's a trade to price: one from the last
    FEED_HOURS (the Feed's and alerts' window), or ``priced``. Positions are reused while their trades haven't
    changed and they're under LIVE_PRICE_S old. 1–2 reads, never the profile or stats (the name is on the trades);
    a third, further back, when none of the last 100 were kept before (so a gap between reads never drops a trade)."""
    hit = _cache.get(f"live:{wallet}")
    old = hit[1] if hit else None
    trades = reads.whales.trades("polymarket", wallet, limit=LIVE_TRADES)
    if old is not None and old.trades and len(trades) >= LIVE_TRADES and not (
        {t.trade_id for t in trades} & {t.trade_id for t in old.trades}
    ):  # more trades since the last read than one read holds: read further back, so none is missed
        trades = reads.whales.trades("polymarket", wallet, limit=LIVE_GAP_TRADES)
    since = time.time() - FEED_HOURS * 3600
    positions: list[Any] = []
    if priced or any(t.at.timestamp() >= since for t in trades):
        at = _live_priced.get(wallet)
        same = old is not None and [t.trade_id for t in old.trades] == [t.trade_id for t in trades]
        if same and at is not None and time.monotonic() - at < LIVE_PRICE_S:
            positions = old.positions
        else:
            positions, _live_priced[wallet] = _open_positions(wallet), time.monotonic()
    else:
        _live_priced.pop(wallet, None)
    name = next((t.name for t in trades if t.name), None)
    return TraderDetail(Trader(venue="polymarket", id=wallet, name=name), "visible", {}, positions, trades)


def _live_read(wallet: str, max_age: float = LIVE_STALE_S, priced: bool = False) -> Any:
    """A trader's last 100 trades and open positions: what the poller keeps (_live_loop), read from Polymarket
    only when it's missing or older than ``max_age`` (and then once, however many ask at the same time).
    ``priced``: their positions too, even with no trade in the last FEED_HOURS (a trade's own price check)."""
    if priced and wallet not in _live_priced:
        max_age = 0  # kept without positions: read again, with them
    return cached(f"live:{wallet}", max_age, lambda: _live_fetch(wallet, priced))


def _live_kept(wallet: str) -> tuple[float, Any] | None:
    """What the poller keeps for a trader: (when it was read, monotonic; their trades and positions), or None."""
    return _cache.get(f"live:{wallet}")


def _live_wanted() -> tuple[set[str], set[str]]:
    """The traders whose trades the Feed and alerts read: everyone someone follows, and Recommended's."""
    rec = {s["wallet"] for s in (_worth_load() or {}).get("scores", []) if s["segment"] in FEED_SEGMENTS}
    return {w for u in _load_follows()["users"].values() for w in u.get("follows") or []}, rec


def _live_hints(wallets: set[str]) -> set[str]:
    """Of ``wallets``, those with a trade in Polymarket's shared feed of RECOMMENDED_MIN_USD-or-more fills that
    isn't among the trades kept for them: one read for everyone (the one Big trades and big-trade alerts read),
    so a Recommended trader who just made a big trade is read now rather than on their turn. A hint only: most of
    Recommended's trades (resting orders, trades in pieces) never show there, so everyone still gets their turn."""
    try:
        rows = cached(f"now:{RECOMMENDED_MIN_USD}", 20,
                      lambda: reads.whales.big_trades(RECOMMENDED_MIN_USD, venues=VENUES, kalshi_pages=5))  # type: ignore[arg-type]  # fmt: skip
    except VenueError:
        return set()
    out = set()
    for t in rows:
        w = (t.trader or "").lower()
        if w in wallets and t.trade_id not in _live_hinted:
            _live_hinted.add(t.trade_id)  # once: their own trades can show it a little later than this feed
            hit = _live_kept(w)
            if hit is None or t.trade_id not in {x.trade_id for x in hit[1].trades}:
                out.add(w)
    if len(_live_hinted) > 5000:
        _live_hinted.intersection_update(t.trade_id for t in rows)
    return out


def live_poll_once(stop: threading.Event | None = None) -> int:
    """One round of the poller, one read at a time (never in a burst: the SDK's Polymarket budget is shared with
    pages and the background): everyone followed (each round), then Recommended traders the shared big-trades feed
    says just traded, then the other Recommended traders (each LIVE_REC_S), oldest (or never read) first, while
    the round's LIVE_POLL_S lasts (LIVE_REC_FLOOR at least); whoever doesn't fit goes first next round, and a rate
    limit ends the round. Half a round's slack on each, so a read a little later in one round than the last is
    still due in the next. Then traders nobody wants any more are dropped. Returns how many were read."""
    followed, rec = _live_wanted()
    rec -= followed
    started = time.monotonic()
    every_round, rec_after = LIVE_POLL_S / 2, LIVE_REC_S - LIVE_POLL_S / 2

    def age(w: str) -> float:
        hit = _live_kept(w)
        return float("inf") if hit is None else started - hit[0]

    hinted = _live_hints(rec) if rec else set()
    def due_now(w: str) -> bool:  # once a round, however long the last one took; a page's read since then counts
        hit = _live_kept(w)
        return hit is None or hit[0] <= _live_round["ended"] or age(w) >= every_round

    due = sorted((w for w in followed if due_now(w)), key=lambda w: (-age(w), w))  # oldest first; ties by wallet
    late = sorted((w for w in rec if w in hinted or age(w) >= rec_after), key=lambda w: (w not in hinted, -age(w), w))
    done = 0
    def followed_read() -> None:  # alerts' reads are done: the others' full rate back, and the alert pass now
        if _followed_reading.is_set():
            _followed_reading.clear()
            _alert_wake.set()

    if due:
        _followed_reading.set()
    try:
        for i, w in enumerate(due + late):
            if i == len(due):
                followed_read()
            if stop is not None and stop.is_set():
                break
            if i >= len(due) + LIVE_REC_FLOOR and time.monotonic() - started >= LIVE_POLL_S:
                break  # the rest of Recommended goes first next round
            try:
                _live_read(w, 0 if w in hinted or i < len(due) else rec_after)
                _live_failed.pop(w, None)
                done += 1
            except VenueError as e:
                _live_failed[w] = time.monotonic()
                if e.code == "rate_limited":  # the SDK's retries are spent: more reads now would only add to it
                    _worth["live_error"] = "Polymarket rate limited the Feed's reads; the rest wait for the next round"
                    break
    finally:
        followed_read()
    _live_round["ended"] = time.monotonic()
    wanted, mono = followed | rec, time.monotonic()
    for key in [k for k in list(_cache) if k.startswith("live:")]:
        hit = _cache.get(key)
        if key[5:] not in wanted and hit and mono - hit[0] > LIVE_KEEP_S:
            _cache.pop(key, None)
            _live_priced.pop(key[5:], None)
    for w in [w for w in _live_failed if w not in wanted]:
        del _live_failed[w]
    since = time.time() - FEED_HOURS * 3600
    shown = {t.market for w in wanted if (hit := _live_kept(w)) for t in hit[1].trades
             if t.action == "buy" and t.at.timestamp() >= since}  # fmt: skip
    try:
        _categories(list(shown))  # the SDK keeps each market's category: no Feed view waits on a new market's
    except VenueError:
        pass
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
             "copy_ok": who.get("copy_ok", False), "copy_label": who.get("copy_label"), "market": t.market, "title": t.title, "outcome": t.outcome, "url": t.url,
             "at": t.at, "usd": 0.0, "size": 0.0, "price_now": now.get(t.market), "token": t.token},
        )  # fmt: skip
        b["at"], b["usd"], b["size"] = max(b["at"], t.at), b["usd"] + t.usd, b["size"] + t.size
    return [b | {"price": b["usd"] / b["size"], "at": b["at"].isoformat(), "us": us_of(b["market"], b["token"], b["outcome"])}
            for b in one.values()]  # fmt: skip


@app.get("/api/bet/{wallet}/{market}")
def one_bet(wallet: str, market: str, response: Response, _: None = Depends(feed_reader)) -> dict[str, Any]:
    """One trader's buy of one outcome, as a Feed card has it (status, price now, copy_ok): what an alert's
    "Check current price" opens. From their last 100 trades; 404 when it isn't among them."""
    _private(response)
    w = wallet.lower()
    if not _WALLET.fullmatch(w):
        raise HTTPException(400, detail="That isn't a Polymarket wallet address.")
    s = next((x for x in (_worth_load() or {}).get("scores", []) if x["wallet"] == w), None)
    try:
        d = _live_read(w, priced=True)  # an older trade too: its price now is still wanted
    except VenueError as e:
        if e.code == "not_found":  # a wallet with no Polymarket profile: counted, so we know how often it happens
            count("no_profile", target="check")
        raise venue_error(e) from e
    who = {"wallet": w, "name": (s or {}).get("name") or d.trader.name, "segment": (s or {}).get("segment"),
           "label": (s or {}).get("label")} | copy_fields(s)  # fmt: skip
    b = next((x for x in _bets_of(d, who) if x["market"] == market), None)
    if b is None:
        raise HTTPException(404, detail="That trade isn't among their recent trades any more.")
    try:
        cat = _categories([market]).get(market)
    except VenueError:
        cat = None
    return b | {"status": bet_status(b["price"], b["price_now"]), "category": cat}


@app.get("/api/live")
def live(response: Response, _: None = Depends(feed_reader)) -> dict[str, Any]:
    """The Feed's Recommended: trades of RECOMMENDED_MIN_USD or more over the last FEED_HOURS from the traders in
    Traders' two sections (Sharps, Rising). Quiet on purpose: fewer, bigger trades."""
    _private(response)
    data = _worth_load()
    people = [s | copy_fields(s) for s in (data or {}).get("scores", []) if s["segment"] in FEED_SEGMENTS]
    got = _feed(people, RECOMMENDED_MIN_USD)
    return {"traders": [{k: s[k] for k in ("wallet", "name", "segment", "label", "copy_ok", "copy_label")} for s in people],
            "hours": FEED_HOURS, "min_usd": RECOMMENDED_MIN_USD} | got  # fmt: skip


def _feed(people: list[dict[str, Any]], min_usd: float = 0.0) -> dict[str, Any]:
    """These traders' buys over the last FEED_HOURS (one per outcome, ``min_usd`` or more), newest first, with
    status and category, from what the poller keeps: a view never waits on Polymarket for dozens of traders. Only
    a trader the poller hasn't read yet (just followed, or just after a restart) is read here, at most
    LIVE_PAGE_READS a view; the rest are ``pending`` until its next round. ``failed``: traders that couldn't be read
    (or only longer ago than LIVE_SHOW_S). ``as_of``: when the oldest of what's shown was read (epoch ms)."""
    since = datetime.now(UTC) - timedelta(hours=FEED_HOURS)
    bets: list[dict[str, Any]] = []
    failed = pending = 0
    reads_left, mono = LIVE_PAGE_READS, time.monotonic()
    oldest = mono
    for s in people:
        w = s["wallet"]
        hit = _live_kept(w)
        if hit is None and reads_left > 0 and w not in _live_failed:
            reads_left -= 1
            try:
                _live_read(w)
            except VenueError:
                _live_failed[w] = time.monotonic()
            hit = _live_kept(w)
        if hit is None or mono - hit[0] > LIVE_SHOW_S:
            if hit is None and w not in _live_failed:
                pending += 1
            else:
                failed += 1
            continue
        oldest, d = min(oldest, hit[0]), hit[1]
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
    as_of = round((time.time() - (time.monotonic() - oldest)) * 1000)
    return {"bets": bets, "failed": failed, "pending": pending, "as_of": as_of}


def _iso_ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


# ---- what buying a trade's outcome now would cost (a Feed trade the trader already made) ----
# A quote only, for every trade whoever made it: it reads Polymarket's market and order book and sends nothing.
# Whether copying a trader is offered (copy_offer) is a separate record, never a gate on this. Signals places no
# orders and keeps no sandbox: you buy on Polymarket yourself.

PREVIEW_SIZE = 5  # contracts, when the page sends no amount
BUY_LIMIT = 0.03  # a quote walks the book up to this far above the cheapest price for sale now, and no further


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


def _their_trade(req: OneTrade, price: float | None = None) -> WhaleTrade:
    """Their trade as the SDK takes it; ``price`` stands in for theirs to set where the book walk stops."""
    at = datetime.fromisoformat(req.at) if req.at else datetime.now(UTC)
    return WhaleTrade(
        venue="polymarket", trader=req.wallet, name=req.name, market=req.market, side="yes", action="buy",
        price=req.price if price is None else price, size=req.size or 0.0, usd=req.usd or 0.0, at=at,
        trade_id=f"feed:{req.wallet}:{req.market}:{req.at}", title=req.title, outcome=req.outcome, url=req.url,
        token=req.token,
    )  # fmt: skip


@app.post("/api/trade/preview")
def trade_preview(req: OneTrade) -> dict[str, Any]:
    """What buying this outcome now would cost and pay on Polymarket, for anyone's trade: an estimate, never an
    order or a promised fill. Read here, never taken from the page: the market's status (the SDK says, in
    ``reason``, when Polymarket lists it closed or isn't taking orders) and the order book, priced up to
    ``BUY_LIMIT`` above the cheapest price for sale now (``best_price``; ``limit`` is the most a contract is
    priced at). ``price_changed``: the book moved between the reads, so there's no quote, only "refresh". Their
    price (``their_price``) is context only. ``priced_at``: when it was read. Sends nothing; each quote is counted
    (``quotes``: outcome, reads, time taken), with nothing about who asked or which trade."""
    if req.spend is not None and req.spend <= 0:
        raise HTTPException(400, detail="Enter an amount above $0.")
    size: dict[str, Any] = {"spend": req.spend} if req.spend else {"size": float(PREVIEW_SIZE)}
    t0, reads_made = time.monotonic(), [0]
    try:
        p = _buy_now(req, size, reads_made)
    except VenueError as e:
        count_quote("rate_limited" if e.code == "rate_limited" else "error", reads_made[0], t0)
        raise venue_error(e) from e
    changed = _price_changed(p)
    count_quote(_quote_outcome(p, changed), reads_made[0], t0)
    return p.to_dict() | {"their_price": req.price, "priced_at": datetime.now(UTC).isoformat(),  # type: ignore[no-any-return]
                          "price_changed": changed}  # fmt: skip


def _buy_now(req: OneTrade, size: dict[str, Any], reads_made: list[int]) -> Any:
    """The SDK prices a buy up to a trade's price plus ``max_slippage``. The first read gets the market's status
    and the cheapest price for sale now (``best_price``, set whether or not this amount fills); when that isn't
    their price, a second read prices the amount from it. A market that's closed, not taking orders or has no
    book to read comes back from the first read as it is. ``reads_made`` counts the reads, for ``quotes``."""
    reads_made[0] += 1
    p = reads.whales.preview_copy(_their_trade(req), **size, max_slippage=BUY_LIMIT)
    best = p.best_price if p.depth_checked else p.price  # no book read: Gamma's best price for sale
    if best is None or abs(best - req.price) < 1e-9:
        return p
    reads_made[0] += 1
    return reads.whales.preview_copy(_their_trade(req, price=best), **size, max_slippage=BUY_LIMIT)


def _price_changed(p: Any) -> bool:
    """Nothing for sale within the limit because the cheapest price is now above it: the book moved between the
    two reads. Not a lack of liquidity, so the page says "Price changed" and offers a fresh quote."""
    return bool(not p.ok and p.depth_checked and p.best_price is not None and p.limit is not None
                and p.best_price > p.limit + 1e-9)  # fmt: skip


def _quote_outcome(p: Any, changed: bool) -> str:
    """How a quote ended, for ``quotes``: ok, no_book (priced from Gamma's best price, the book unread),
    price_changed, short (the book holds less than the amount), closed, rate_limited or other."""
    if p.ok:
        return "ok" if p.depth_checked else "no_book"
    if changed:
        return "price_changed"
    why = (p.reason or "").lower()
    if "too many requests" in why:
        return "rate_limited"
    if "taking orders" in why or "doesn't list" in why:
        return "closed"
    return "short" if p.depth_checked and p.max_spend else "other"


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
    return _session(request, "Sign in to follow traders.")


def _session(request: Request, why: str) -> str:
    """``signed_in``'s check; ``why`` is what a missing or foreign sign-in is told."""
    fapi = clerk_frontend_api(CLERK_KEY)
    if not fapi:
        raise HTTPException(404, detail="Signing in isn't switched on here.")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not token:
        raise HTTPException(401, detail=why)
    try:
        claims = jwt.decode(token, _signing_key(token), algorithms=["RS256"], issuer=f"https://{fapi}",
                            options={"require": ["exp", "iat", "sub"]}, leeway=5)  # fmt: skip
    except jwt.PyJWKClientConnectionError as e:
        raise HTTPException(503, detail="Couldn't check your sign-in just now. Try again in a minute.") from e
    except jwt.PyJWTError as e:
        raise HTTPException(401, detail="Your sign-in has expired. Sign in again.") from e
    if claims.get("azp") and claims["azp"] not in _origins():
        raise HTTPException(401, detail=f"{why[:-1]} on this site.")
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
    a trader followed from Explore shows the verdict their page shows (the directory's fresh stored score, as
    ``worth_wallet`` reuses it; no read), and one nobody has scored lately has no tier and copying isn't offered for
    them (copy_ok False)."""
    _private(response)
    follows = list((_load_follows()["users"].get(uid) or {}).get("follows") or [])
    scores = {s["wallet"]: s for s in (_worth_load() or {}).get("scores", [])}
    people = []
    for w in follows:
        s = scores.get(w) or _stored_score(w) or {}
        label = s.get("label") or (SEGMENTS[s["segment"]][0] if s.get("segment") in SEGMENTS else None)
        people.append({"wallet": w, "name": s.get("name"), "segment": s.get("segment"), "label": label}
                      | copy_fields(s or None))  # fmt: skip
    return {"traders": people, "hours": FEED_HOURS} | _feed(people)


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
    "Connected. You'll get a message here a few minutes after a trader you follow buys or sells, whatever the amount: "
    "what they traded, at what price, the price now and when they traded. Polymarket's public data runs a few minutes "
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
            _say(chat, f"Follow traders on {SITE}, then tap Connect Telegram there to get an alert after they trade.")
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
CREATE TABLE IF NOT EXISTS quotes (at REAL NOT NULL, outcome TEXT NOT NULL, reads INTEGER NOT NULL, ms REAL NOT NULL);
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


COUNTED = ("buy", "sell", "group", "whale")  # what the daily cap counts: a group once, its edits never


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


LINK_TARGETS = {"Buy on Polymarket International": "market", "Open on Polymarket International": "market",
                "View on Polymarket US": "market_us", "Their record": "record", "Check current price": "check"}  # fmt: skip

# Where the trade happened, said on every alert: the traders Signals follows trade on Polymarket International.
# With a verified Polymarket US twin (us_match), that it's the same market, never the same price.
TRADED_INTL = "Traded on Polymarket International."
US_TWIN_LINE = (f"{TRADED_INTL} The same market is on Polymarket US (matched through Kalshi), where prices and"
                " liquidity may differ.")  # fmt: skip
US_DIFFERS = " Some settlement rules differ."
NO_US_TWIN_LINE = f"{TRADED_INTL} No verified US match found."


def _venue_line(t: WhaleTrade) -> tuple[str, str | None]:
    """The alert's line on where the trade happened, and the Polymarket US link when there's a verified twin."""
    us = us_of(t.market, t.token, t.outcome)
    if us is None:
        return TRADED_INTL, None
    if not us["found"]:
        return NO_US_TWIN_LINE, None
    return US_TWIN_LINE + (US_DIFFERS if us["differences"] else ""), us["url"]


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
COPY_LINE = "<i>Buying after this trader has made money before, and you can still get in near their price. That's the past, not a promise.</i>"


def _lag(at: float, seen: float) -> str:
    """How long before this alert the trade was made, measured: "6 min", or "under a minute"."""
    mins = round((seen - at) / 60)
    return "under a minute" if mins < 1 else f"{mins} min"


def _traded(at: float, seen: float) -> str:
    """When they traded, and how long before this alert that was: "Traded 14:02 UTC, 6 min before this alert."."""
    return f"Traded {datetime.fromtimestamp(at, UTC):%H:%M} UTC, {_lag(at, seen)} before this alert."


def _trade_text(head: str, wallet: str, t: WhaleTrade, paid: float, now: float, at: float, seen: float,
                record: bool, aid: str | None) -> str:  # fmt: skip
    """What happened, the same for every trader: ``head`` (who bought which outcome, at what price, for how much),
    the market, the price now (", price already moved" outside the "can still get in" range, ``bet_status``), and
    when they traded, measured against this alert (Polymarket shows trades minutes late), and where (``_venue_line``:
    Polymarket International, and its Polymarket US twin when there's a verified one). Then COPY_LINE, only when
    copying is offered for them and the price is still in range. Links: "Check current price" (the trade's own page
    on Signals, neutral for every trader), their record, "Buy on Polymarket International" (it only opens the market:
    Signals places no orders), "View on Polymarket US" with a twin; then the delay line."""
    e = html.escape
    status = bet_status(paid, now)
    where, us_url = _venue_line(t)
    lines = [head, e(t.title or "A Polymarket market"), f"Now {_cents(now)}{', price already moved' if status != 'in' else ''}.",
             _traded(at, seen), where]  # fmt: skip
    if status == "in" and _copy_offered(wallet):
        lines.append(COPY_LINE)
    links = _links(("Check current price", f"{SITE}/#/check/{wallet}/{t.market}"),
                   ("Their record", f"{SITE}/#/worth/{wallet}" if record else None),
                   ("Buy on Polymarket International", t.url), ("View on Polymarket US", us_url), aid=aid)  # fmt: skip
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
    where, us_url = _venue_line(t)
    lines += [f"Now {_cents(now)}{', price already moved' if moved else ''}.", f"Latest buy {_lag(last_at, seen)} before this alert.",
              where, _links(("Check current price", f"{SITE}/#/check/{last}/{t.market}"), ("Buy on Polymarket International", t.url),
                            ("View on Polymarket US", us_url), aid=aid),
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


def _sell_text(wallet: str, t: WhaleTrade, price: float, usd: float, held: bool, now: float | None, at: float,
               seen: float, aid: str | None = None) -> str:  # fmt: skip
    """The Telegram message for a sell by a trader you follow, whoever they are: information only, never a
    suggestion to sell and nothing about copying. Who sold which outcome at what price (their average across the
    fills) and for how much, the market, and when they traded, measured against this alert. While they still hold
    some of it: the price now and "Check current price"; once they hold none, "They no longer hold it." and no price
    (there's no position to read it from, and no number is made up)."""
    e = html.escape
    still = ("They still hold some." if now is None else f"Now {_cents(now)}. They still hold some.") if held else "They no longer hold it."
    lines = [f"<b>{e(_who(wallet))}</b> sold <b>{e(t.outcome or 'Yes')}</b> at {_cents(price)} (${usd:,.0f})",
             e(t.title or "A Polymarket market"), still, _traded(at, seen)]  # fmt: skip
    where, us_url = _venue_line(t)
    lines.append(where)
    links = _links(("Check current price", f"{SITE}/#/check/{wallet}/{t.market}" if held else None),
                   ("Their record", f"{SITE}/#/worth/{wallet}"), ("Open on Polymarket International", t.url),
                   ("View on Polymarket US", us_url), aid=aid)  # fmt: skip
    return "\n".join([*lines, links, DATA_DELAY])


class NewSell:
    """One trader's new sell of one outcome in a round, fills merged: the price they got on average, whether they
    still hold some (an open position), and the price now from it. Its text is built once for every follower."""

    def __init__(self, wallet: str, fills: list[WhaleTrade], held: bool, now: float | None, seen: float) -> None:
        self.wallet, self.t, self.held, self.now, self.seen = wallet, max(fills, key=lambda f: f.at), held, now, seen
        self.size, self.usd = sum(f.size for f in fills), sum(f.usd for f in fills)
        self.at = min(f.at for f in fills).timestamp()
        self.price = self.usd / self.size
        self.outcome = self.t.token or self.t.market
        self._text: str | None = None

    def settled(self) -> bool:
        """Sold at a settled price, or the price now is (2¢ or less, 98¢ or more): closing out a decided market."""
        return _settled(self.price) or (self.now is not None and _settled(self.now))

    def text(self, aid: str) -> str:
        if self._text is None:
            self._text = _sell_text(self.wallet, self.t, self.price, self.usd, self.held, self.now, self.at, self.seen,
                                    ALERT_ID_MARK)  # fmt: skip
        return self._text.replace(ALERT_ID_MARK, aid)


def _sold_recently(trader: str, outcome: str, now: float) -> bool:
    """Whether this trader's sell of this outcome was already alerted (to anyone) in the last ALERT_AGAIN_S, from
    alerts.db, so a restart doesn't alert it again."""
    with _db() as con:
        q = "SELECT 1 FROM alerts WHERE kind = 'sell' AND trader = ? AND outcome = ? AND sent_at >= ? LIMIT 1"
        return con.execute(q, (trader, outcome, now - ALERT_AGAIN_S)).fetchone() is not None


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
    buy or sell, whatever the amount and whoever the trader, a message to every connected follower (grouped when 2+ traders they follow
    bought the same outcome; sells never are), then big trades in people's chosen markets. A trader's trades from before their
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
    new_sells: list[NewSell] = []
    for w in to:
        try:  # what the poller keeps (it reads everyone followed first, each round); read here only if it hasn't yet
            d = hit[1] if (hit := _live_kept(w)) else _live_read(w, LIVE_ALERT_S)
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
        sold: dict[str, list[WhaleTrade]] = {}
        for t in new:
            if t.action in ("buy", "sell") and t.size and now - t.at.timestamp() <= ALERT_MAX_AGE_S:
                (fills if t.action == "buy" else sold).setdefault(t.market, []).append(t)
        for market, ts in sold.items():
            sell = NewSell(w, ts, market in prices, prices.get(market), now)
            if now - _alerted.get((w, f"sell:{market}"), 0) < ALERT_AGAIN_S or sell.settled() or _sold_recently(w, sell.outcome, now):
                continue
            _alerted[(w, f"sell:{market}")] = now
            new_sells.append(sell)
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
    for sell in new_sells:  # never grouped
        for uid in to[sell.wallet]:
            if _send(uid, users[uid]["chat"], "sell", sell.text, trader=sell.wallet, outcome=sell.outcome,
                     trade_at=sell.at, stop=sell.wallet):  # fmt: skip
                sent += 1
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
            price_now = next((p.current_price for p in _live_read(w, LIVE_ALERT_S, priced=True).positions
                              if p.market == t.market), None)  # fmt: skip
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
        _alert_wake.clear()
        try:
            alert_pass()
        except Exception as e:  # keep alerting through a bad round
            _tg["error"] = f"{type(e).__name__}: {e}"
        _alert_wake.wait(ALERT_EVERY_S)  # or sooner: the poller wakes it once it has read everyone followed


_alert_wake = threading.Event()


# ---- counting: is any of this useful? ----
# Per alert and per follow, kept in the store's alerts.db with nothing personal beyond the signed-in account (its
# Clerk id): no IPs, no user agents. Minutes from their trade to the alert (alerts: trade_at, sent_at), taps on an
# alert's links (/go), "Stop alerts" taps and unfollows, votes (votes), follows, interests picked and Telegram
# connected (events). Also anonymous, with no id, wallet or trade: each "What buying now costs" quote (quotes: how
# it ended, Polymarket reads, milliseconds) and each page that couldn't open because the wallet has no Polymarket
# profile (events: no_profile). /api/metrics sums them up for the owner: locally, or with SIGNALS_METRICS_KEY.

METRICS_KEY = os.environ.get("SIGNALS_METRICS_KEY", "")
EVENTS = ("tap", "stop", "unfollow", "follow", "telegram", "picked", "no_profile")
_ALERT_ID = re.compile(r"[A-Za-z0-9_-]{4,16}")


def count(kind: str, *, uid: str | None = None, alert_id: str | None = None, target: str | None = None,
          trader: str | None = None) -> None:  # fmt: skip
    """Append one event. Never in the way: counting that fails is dropped, whatever was being done goes ahead."""
    try:
        with _db() as con:
            con.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", (time.time(), kind, uid, alert_id, target, trader))
    except (sqlite3.Error, OSError):
        pass


def count_quote(outcome: str, reads_made: int, t0: float) -> None:
    """One quote: how it ended, how many Polymarket reads it took and how long (from ``t0``, monotonic). Never in the way."""
    try:
        with _db() as con:
            con.execute("INSERT INTO quotes VALUES (?, ?, ?, ?)", (time.time(), outcome, reads_made, (time.monotonic() - t0) * 1000))
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
    """Where a /go link may send someone: Polymarket (International or US), or Signals itself. Nowhere else
    (no open redirect)."""
    try:
        u = urllib.parse.urlsplit(url)
        site = urllib.parse.urlsplit(SITE)
        host = u.hostname
    except ValueError:  # a malformed link (a bad port, a broken IPv6 host): Signals itself, never a 500
        return False
    return u.scheme == "https" and (host in ("polymarket.com", "polymarket.us") or (u.scheme, u.netloc) == (site.scheme, site.netloc))


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
        quotes = con.execute("SELECT outcome, reads, ms FROM quotes WHERE at >= ?", (since,)).fetchall()
        no_profile = dict(con.execute("SELECT target, COUNT(*) FROM events WHERE kind = 'no_profile' AND at >= ?"
                                      " GROUP BY target", (since,)).fetchall())  # fmt: skip
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
        "quotes": {"n": len(quotes), "by_outcome": {o: sum(1 for q in quotes if q[0] == o) for o in sorted({q[0] for q in quotes})},
                   "polymarket_reads": sum(q[1] for q in quotes), "reads_per_quote": ratio(sum(q[1] for q in quotes), len(quotes)),
                   "ms": {"median": _pct([q[2] for q in quotes], 0.5), "p90": _pct([q[2] for q in quotes], 0.9)},
                   "share_rate_limited": ratio(sum(1 for q in quotes if q[0] == "rate_limited"), len(quotes))},
        "no_profile": {"by_page": no_profile, "check_link_taps": taps.get("check", 0)},
        "us_twins": {"fresh": us_match.twins.fresh(), "counts": us_match.twins.counts, "error": us_match.twins.error},
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
