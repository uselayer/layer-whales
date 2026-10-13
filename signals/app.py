"""The engine: a small HTTP service that runs the uselayer SDK, on your machine or hosted.

Locally, everything runs on your machine with your own keys; what it keeps (the Worth following check, price
history) is in ~/.uselayer/signals unless SIGNALS_STORE_DIR says otherwise.

    LAYER_API_KEY=lyr_... uv run signals            # then open http://127.0.0.1:8790

Hosted (SIGNALS_HOSTED=1, see the Dockerfile), it's the same reads: no signup, and keys stay on the server.
Signals places no orders: it shows what copying a trade would cost, and you buy on Polymarket yourself.
"""

from __future__ import annotations

import json
import os
import re
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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from uselayer import Client, VenueError, WhaleTrade
from uselayer.whale_scores import COPY_BETS, SEGMENTS, Stat, category_of, copyable_verdict

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
    yield
    _worth_stop.set()


app = FastAPI(title="Signals", lifespan=lifespan)
reads = Client(store=":memory:")  # leaderboards, traders, links, cost previews: reads only


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


@app.get("/api/config")
def config() -> dict[str, Any]:
    """What the page needs to know before it draws: which venues Signals reads (see VENUES)."""
    return {"venues": list(VENUES)}


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
WORTH_FORMAT = 6  # bump when the kept result gains fields; an older one is re-checked on launch (6: uselayer 0.8.0 scores)
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
    price = s.get("avg_price") or _median([b.get("price") for b in bets])
    copy = Stat.from_dict(s["copy"]) if s.get("copy") else None
    # What a copier a few minutes late kept after the fee, per dollar they paid: an estimate with its 95% range.
    # Only the range's bottom ranks a trader ("Copy estimate" sort), and only over COPY_BETS bets or more.
    per = (lambda x: x / price) if copy and price else None
    out["copy_return"] = per(copy.mean) if per else None
    out["copy_return_low"] = per(copy.low) if per else None
    out["copy_return_high"] = per(copy.high) if per else None
    out["copy_rank"] = out["copy_return_low"] if copy and copy.n >= COPY_BETS else None
    out["copy_ok"], out["copy_why"] = copy_offer(s)
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


def copy_offer(s: dict[str, Any]) -> tuple[bool, str | None]:
    """Whether Signals offers copying this trader, and if not, why, in a plain sentence. One rule for Traders, a
    trader's page, the Feed and the copy routes: a recommended tier (Proven sharp, Quiet sharp, Rising), and
    copies a few minutes late reliably above zero after fees (the SDK's ``copyable``: the bottom of the 95%
    range above zero). Never Sharp but too fast, Lucky, No clear edge, market makers or traders away."""
    seg = s.get("segment")
    if seg == "too_fast":
        return False, "No proven copy advantage: what copies kept after fees isn't reliably above zero, so copying isn't offered."
    if seg == "inactive":
        return False, "They haven't traded in the last 7 days, so copying isn't offered while they're away."
    if seg not in COPY_TIERS:
        return False, "Not a trader we recommend, so copying isn't offered."
    if s.get("copyable") == "unknown":
        return False, "Too few of their trades had prices after the buy to tell whether copies gain, so copying isn't offered yet."
    if s.get("copyable") != "yes":
        return False, "What copies a few minutes late kept after fees isn't reliably above zero, so copying isn't offered."
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
    for s in (data or {}).get("scores", []):
        if s["wallet"] == w:
            return with_links(s) | _board(s)
    s = _score_of(w)
    return with_links(s) | _board(s)


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
            raise venue_error(e) from e
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

FEED_SEGMENTS = ("proven", "quiet", "rising")  # the Follow and Watch verdicts; "inactive" traders aren't followed
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


@app.get("/api/live")
def live() -> dict[str, Any]:
    data = _worth_load()
    follow = [s for s in (data or {}).get("scores", []) if s["segment"] in FEED_SEGMENTS]
    since = datetime.now(UTC) - timedelta(hours=FEED_HOURS)
    bets: list[dict[str, Any]] = []
    failed = 0
    offers = {s["wallet"]: copy_offer(s)[0] for s in follow}
    for s in follow:
        try:
            d = cached(
                f"live:{s['wallet']}", 25, lambda w=s["wallet"]: reads.whales.trader("polymarket", w, trades=100, closed=0)
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
                 "copy_ok": offers[s["wallet"]], "market": t.market, "title": t.title, "outcome": t.outcome, "url": t.url, "at": t.at,
                 "usd": 0.0, "size": 0.0, "price_now": now.get(t.market), "token": t.token},
            )  # fmt: skip
            b["at"], b["usd"], b["size"] = max(b["at"], t.at), b["usd"] + t.usd, b["size"] + t.size
        bets += [b | {"price": b["usd"] / b["size"], "at": b["at"].isoformat()} for b in one.values()]
    bets.sort(key=lambda b: b["at"], reverse=True)
    cats = _categories([b["market"] for b in bets]) if bets else {}
    for b in bets:
        b["status"], b["category"] = bet_status(b["price"], b["price_now"]), cats.get(b["market"])
    return {"traders": [{"wallet": s["wallet"], "name": s["name"], "segment": s["segment"], "label": s["label"],
                         "copy_ok": offers[s["wallet"]]} for s in follow],
            "failed": failed, "hours": FEED_HOURS, "bets": bets}  # fmt: skip


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
    return p.to_dict()  # type: ignore[no-any-return]


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
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
