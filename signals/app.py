"""The engine: a small HTTP service that runs the uselayer SDK, on your machine or hosted.

Locally, everything runs on your machine with your own keys, and there is one sandbox (real books, fake
money) in ~/.uselayer/signals unless SIGNALS_STORE_DIR says otherwise.

    LAYER_API_KEY=lyr_... uv run signals            # then open http://127.0.0.1:8790

Hosted (SIGNALS_HOSTED=1, see the Dockerfile), each browser gets its own sandbox, found by an anonymous
cookie: no signup. Keys stay on the server. Sandbox only: nothing here trades with real money.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from uselayer import Client, Copier, Trader, VenueError, WhaleTrade
from uselayer.whale_scores import category_of

HERE = Path(__file__).parent
STORE_DIR = Path(os.environ.get("SIGNALS_STORE_DIR", Path.home() / ".uselayer" / "signals"))
_OLD_STORE_DIR = Path.home() / ".uselayer" / "whales-demo"  # before the rename to Signals: main() moves it once
HOSTED = os.environ.get("SIGNALS_HOSTED") == "1"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    resume_copying()  # with the saved rules
    threading.Thread(target=_worth_loop, daemon=True).start()  # last result shows at once; refresh behind it
    yield
    _loop_stop.set()
    _worth_stop.set()


app = FastAPI(title="Signals", lifespan=lifespan)
reads = Client(store=":memory:")  # leaderboards, traders, links: reads only
_lock = threading.Lock()  # one copier pass and one sandbox change at a time


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

WORTH_WALLETS = int(os.environ.get("SIGNALS_WORTH_WALLETS", "200"))
WORTH_MAX_AGE_S = 6 * 3600
WORTH_RETRY_S = 30 * 60  # after a failed check
WORTH_FORMAT = 4  # bump when the kept result gains fields; an older one is re-checked on launch (4: uselayer 0.6.0 scores)
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
    if len(window) == 2 and s.get("decisions") is not None:
        days = max(1.0, (_iso_ts(window[1]) - _iso_ts(window[0])) / 86400)
        out["trades_per_day"] = round(s["decisions"] / days, 2)
    price = s.get("avg_price") or _median([b.get("price") for b in bets])
    copy = s.get("copy") or {}
    # what a copier a few minutes late kept after the fee, per dollar they paid
    out["copy_return"] = copy["mean"] / price if copy.get("mean") is not None and price else None
    if _mock() and s.get("pnl_weeks") is None:
        out |= _mock_history(s)
    return out


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
    if w not in _worth_one:
        try:
            _worth_one[w] = reads.whales.score(w).to_dict()
        except VenueError as e:
            raise venue_error(e) from e
        if len(_worth_one) > WORTH_ONE_KEEP:
            del _worth_one[next(iter(_worth_one))]  # the oldest
    return with_links(_worth_one[w]) | _board(_worth_one[w])


def with_links(s: dict[str, Any]) -> dict[str, Any]:
    """A score whose checked bets each link to their event on polymarket.com."""
    return s | {k: [b | {"url": polymarket_url(b.get("event"))} for b in s.get(k) or []] for k in ("recent", "sample")}


# ---- live bets from traders worth following ----
# Their buys over the last day, one row per bet (fills on the same outcome merged), with the price now:
# the current price of their open position. Polymarket's public trade feed runs a few minutes behind.

FEED_SEGMENTS = ("proven", "quiet", "rising")  # the Follow and Watch verdicts; "inactive" traders aren't followed
FEED_HOURS = 24
GET_IN = 0.03  # at most this far above their price you can still get in: the same limit copying uses
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
                 "market": t.market, "title": t.title, "outcome": t.outcome, "url": t.url, "at": t.at,
                 "usd": 0.0, "size": 0.0, "price_now": now.get(t.market), "token": t.token},
            )  # fmt: skip
            b["at"], b["usd"], b["size"] = max(b["at"], t.at), b["usd"] + t.usd, b["size"] + t.size
        bets += [b | {"price": b["usd"] / b["size"], "at": b["at"].isoformat()} for b in one.values()]
    bets.sort(key=lambda b: b["at"], reverse=True)
    cats = _categories([b["market"] for b in bets]) if bets else {}
    for b in bets:
        b["status"], b["category"] = bet_status(b["price"], b["price_now"]), cats.get(b["market"])
    return {"traders": [{"wallet": s["wallet"], "name": s["name"], "segment": s["segment"], "label": s["label"]}
                        for s in follow],
            "failed": failed, "hours": FEED_HOURS, "bets": bets}  # fmt: skip


def _iso_ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


# ---- sandboxes ----
# Locally there is one sandbox, in STORE_DIR. Hosted, each browser gets its own, found by an anonymous cookie
# (no signup) and kept in STORE_DIR/visitors/<id>: its paper account, copy rules and copied trades. The folder
# is made on the first copy or preview, not on a visit.

LOCAL = "local"
SID_COOKIE = "signals_sid"
SID_MAX_AGE_S = 365 * 86400  # from the last visit
_SID = re.compile(r"[0-9a-f]{32}")
MAX_TRADERS = int(os.environ.get("SIGNALS_MAX_TRADERS", "5"))  # hosted: traders one browser copies at once
MAX_COPYING = int(os.environ.get("SIGNALS_MAX_COPYING", "300"))  # hosted: browsers copying at once
IDLE_S = 14 * 86400  # hosted: a browser away this long stops being copied for, until it's back (its rules stay)
FORGET_S = 3600  # hosted: an open sandbox nobody is copying for is closed after this long without a visit


class CopyRules(BaseModel):
    size: float = 5
    max_above: float = 0.03  # dollars a contract above their price


class CopyTrader(BaseModel):
    """Copy all of a trader's next trades, in every market. There is no "only some categories": a rule saved
    with ``categories`` before keeps working as saved (see ``_make_copier``), but no new one gets them."""

    venue: str
    id: str
    name: str | None = None


def _key(venue: str, id: str) -> str:
    return f"{venue}:{id}"


def _shared_trades(venue: Any, id: str, *, limit: int = 50) -> list[WhaleTrade]:
    """A trader's recent trades, read once for every sandbox copying them: each copier asks every 5 s."""
    rows: list[WhaleTrade] = cached(f"trades:{venue}:{id}:{limit}", 4, lambda: reads.whales.trades(venue, id, limit=limit))
    return rows


class Sandbox:
    """One sandbox: a paper account, copy rules, a copier per copied trader, and the copied trades."""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.dir = STORE_DIR if sid == LOCAL else STORE_DIR / "visitors" / sid
        self.client: Client | None = None
        self.copiers: dict[str, Copier] = {}
        self.status: dict[str, dict[str, Any]] = {}  # per trader: checked_at, error
        self.recent: list[dict[str, Any]] = []  # the last copied and skipped events, newest last
        self.copied_cache: dict[str, Any] = {"at": 0.0, "data": None, "copies": 0}  # copies: as of this answer
        self.copies = 0  # copied trades recorded since it opened: a newer one makes the cached answer stale
        self.copied_lock = threading.Lock()  # one valuation at a time
        self.seen = time.time()
        self.touched = 0.0

    def paper(self) -> Client:
        if self.client is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.client = Client(store=str(self.dir / "paper.db"))
            self.client.whales.trades = _shared_trades  # type: ignore[method-assign]
        return self.client

    def has_account(self) -> bool:
        return self.client is not None or (self.dir / "paper.db").exists()

    def rules_file(self) -> Path:
        return self.dir / "copy-rules.json"

    def copied_file(self) -> Path:
        """Every copied trade, one per line, so My trades survives restarts."""
        return self.dir / "copied.jsonl"

    def load_rules(self) -> dict[str, Any]:
        if self.rules_file().exists():
            try:
                rules: dict[str, Any] = json.loads(self.rules_file().read_text())
                rules.pop("copy_to", None)  # retired: each trader is copied on their own venue
                return rules
            except ValueError:
                pass
        return CopyRules().model_dump() | {"traders": []}

    def save_rules(self, rules: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rules_file().write_text(json.dumps(rules, indent=2))

    def visit(self) -> None:
        """Note a visit. Hosted, the rules file's time says when they were last here, for after a restart."""
        self.seen = time.time()
        if HOSTED and self.seen - self.touched > 3600 and self.rules_file().exists():
            self.rules_file().touch()
            self.touched = self.seen

    def apply_rules(self) -> None:
        """Make the running copiers match the saved rules, and start the loop if anyone is copied."""
        rules = self.load_rules()
        with _lock:
            self.copiers.clear()
            for t in rules["traders"]:
                self.copiers[_key(t["venue"], t["id"])] = self._make_copier(rules, t)
        if self.copiers:
            _start_loop()

    def _make_copier(self, rules: dict[str, Any], t: dict[str, Any]) -> Copier:
        trader = Trader(
            venue=t["venue"],
            id=t["id"],
            name=t.get("name") or t["id"],
            volume_unit="contracts" if t["venue"] == "kalshi" else "usd",
        )
        return self.paper().whales.follow(
            trader,
            size=float(rules["size"]),
            venue="kalshi" if t["venue"] == "kalshi" else "polymarket",  # where they bet: same market, same price
            max_slippage=float(rules["max_above"]),
            categories=tuple(t.get("categories") or ()),
            copy_sells=False,  # buys only, held until the market settles
        )

    def record(self, e: Any, trader: dict[str, Any]) -> dict[str, Any]:
        """Keep a copy event for the page, and every copied trade in copied.jsonl for My trades."""
        d: dict[str, Any] = e.to_dict() | {"trader": trader}
        self.recent.append(d)
        if e.status == "copied":
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.copied_file().open("a") as f:
                f.write(json.dumps(d) + "\n")
            self.copies += 1  # a new row: the next read waits for an answer that has it
        del self.recent[:-200]
        return d

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def reset(self) -> None:
        """Start over: no positions, no copied trades and nobody copied. The size and price limits stay."""
        with _lock:
            self.copiers.clear()
            self.save_rules(self.load_rules() | {"traders": []})
            self.close()
            for name in ("paper.db", "paper.db-wal", "paper.db-shm"):
                (self.dir / name).unlink(missing_ok=True)
            self.copied_file().unlink(missing_ok=True)
            self.recent.clear()
            self.copied_cache.update(at=0.0, data=None)


_boxes: dict[str, Sandbox] = {}
_boxes_lock = threading.Lock()


def sandbox(sid: str) -> Sandbox:
    """The sandbox for ``sid``, opened on first use; copying resumes if its saved rules copy anyone."""
    with _boxes_lock:
        box = _boxes.get(sid)
        new = box is None
        if box is None:
            box = _boxes[sid] = Sandbox(sid)
    box.visit()
    if new and box.load_rules()["traders"]:
        box.apply_rules()
    return box


def _set_sid(response: Response, sid: str) -> None:
    response.set_cookie(SID_COOKIE, sid, max_age=SID_MAX_AGE_S, httponly=True, samesite="lax", secure=True)


def me(request: Request, response: Response) -> Sandbox:
    """The visitor's sandbox: the one local sandbox, or (hosted) their browser's, made on their first visit."""
    if not HOSTED:
        return sandbox(LOCAL)
    sid = request.cookies.get(SID_COOKIE, "")
    if not _SID.fullmatch(sid):
        sid = secrets.token_hex(16)
        _set_sid(response, sid)
    return sandbox(sid)


def resume_copying() -> None:
    """On start: the local sandbox, or every browser that was copying and was here in the last two weeks."""
    if not HOSTED:
        sandbox(LOCAL)
        return
    now = time.time()
    for f in (STORE_DIR / "visitors").glob("*/copy-rules.json"):
        sid, last = f.parent.name, f.stat().st_mtime
        if _SID.fullmatch(sid) and now - last < IDLE_S:
            sandbox(sid).seen = last


def _tidy(now: float) -> None:
    """Hosted: close sandboxes nobody is using, and stop copying for browsers away two weeks."""
    if not HOSTED:
        return
    with _boxes_lock:
        gone = [b for b in _boxes.values() if now - b.seen > (IDLE_S if b.copiers else FORGET_S)]
        for b in gone:
            del _boxes[b.sid]
    for b in gone:
        with _lock:
            b.copiers.clear()
            b.close()


# ---- copying ----
# Copy rules are set once per sandbox: which traders, contracts a trade and how far above their price to pay.
# Each trader is copied where they bet: a Polymarket trader on the same Polymarket market, a Kalshi trader on
# the same Kalshi market (needs a Kalshi key). One background loop polls every sandbox's copiers and copies
# each new buy with fake money.

_loop_thread: threading.Thread | None = None
_loop_stop = threading.Event()


def _start_loop() -> None:
    global _loop_thread
    if _loop_thread is None or not _loop_thread.is_alive():
        _loop_stop.clear()
        _loop_thread = threading.Thread(target=_copy_loop, daemon=True)
        _loop_thread.start()


def _copy_loop() -> None:
    while not _loop_stop.is_set():
        for box in list(_boxes.values()):
            for key, cp in list(box.copiers.items()):
                st = box.status.setdefault(key, {})
                if box.copiers.get(key) is not cp:
                    continue  # the rules changed since this pass began
                try:
                    # Not under _lock: a poll can take seconds (Layer matching is paced), and the paper store
                    # locks itself. Only this thread polls, so copiers never run at once.
                    events = cp.poll()
                    with _lock:
                        for e in events:
                            box.record(e, cp.trader.to_dict())
                    st.update(checked_at=time.time(), error=None)
                except VenueError as e:
                    st["error"] = e.message
                except Exception as e:  # keep copying through a bad read; show it on the page
                    st["error"] = str(e)
        _tidy(time.time())
        _loop_stop.wait(5)


@app.get("/api/copy")
def copy_state(box: Sandbox = Depends(me)) -> dict[str, Any]:
    rules = box.load_rules()
    now = time.time()
    for t in rules["traders"]:
        st = box.status.get(_key(t["venue"], t["id"]), {})
        t["checked_s_ago"] = None if st.get("checked_at") is None else round(now - st["checked_at"], 1)
        t["error"] = st.get("error")
    return {"rules": rules, "recent": list(reversed(box.recent[-50:]))}


@app.put("/api/copy/rules")
def set_rules(req: CopyRules, box: Sandbox = Depends(me)) -> dict[str, Any]:
    box.save_rules(box.load_rules() | req.model_dump())
    box.apply_rules()
    return copy_state(box)


@app.post("/api/copy/traders")
def add_trader(req: CopyTrader, box: Sandbox = Depends(me)) -> dict[str, Any]:
    rules = box.load_rules()
    others = [t for t in rules["traders"] if _key(t["venue"], t["id"]) != _key(req.venue, req.id)]
    if HOSTED and len(others) >= MAX_TRADERS:
        raise HTTPException(400, detail=f"You can copy up to {MAX_TRADERS} traders at a time. Stop one to add another.")
    if HOSTED and not box.copiers and sum(1 for b in list(_boxes.values()) if b.copiers) >= MAX_COPYING:
        raise HTTPException(503, detail="Copying is full right now. Try again later.")
    rules["traders"] = [*others, req.model_dump()]
    box.save_rules(rules)
    box.apply_rules()
    return copy_state(box)


@app.delete("/api/copy/traders/{venue}/{trader_id}")
def remove_trader(venue: str, trader_id: str, box: Sandbox = Depends(me)) -> dict[str, Any]:
    rules = box.load_rules()
    rules["traders"] = [t for t in rules["traders"] if _key(t["venue"], t["id"]) != _key(venue, trader_id)]
    box.save_rules(rules)
    box.apply_rules()
    return copy_state(box)


# ---- copy one trade now (a Feed trade the trader already made), with a payout preview ----


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
    token: str | None = None  # the exact outcome token they bought: the copy matches it first
    spend: float | None = None  # dollars to put in, fee included; None: the copy rules' contracts


def _sized(req: OneTrade, rules: dict[str, Any]) -> dict[str, Any]:
    """How much to copy: ``spend`` dollars when the page sends an amount, else the copy rules' contracts."""
    size: dict[str, Any] = {"spend": req.spend} if req.spend else {"size": float(rules["size"])}
    return size | {"max_slippage": float(rules["max_above"])}


def _their_trade(req: OneTrade) -> WhaleTrade:
    at = datetime.fromisoformat(req.at) if req.at else datetime.now(UTC)
    return WhaleTrade(
        venue="polymarket", trader=req.wallet, name=req.name, market=req.market, side="yes", action="buy",
        price=req.price, size=req.size or 0.0, usd=req.usd or 0.0, at=at, trade_id=f"feed:{req.wallet}:{req.market}:{req.at}",
        title=req.title, outcome=req.outcome, url=req.url, token=req.token,
    )  # fmt: skip


@app.post("/api/trade/preview")
def trade_preview(req: OneTrade, box: Sandbox = Depends(me)) -> dict[str, Any]:
    """What copying this trade now would cost and pay, on the same Polymarket market. Sends nothing."""
    if req.spend is not None and req.spend <= 0:
        raise HTTPException(400, detail="Enter an amount above $0.")
    rules = box.load_rules()
    try:
        p = box.paper().whales.preview_copy(_their_trade(req), **_sized(req, rules))
    except VenueError as e:
        raise venue_error(e) from e
    return p.to_dict()


@app.post("/api/trade/copy")
def trade_copy(req: OneTrade, box: Sandbox = Depends(me)) -> dict[str, Any]:
    """Buy this trade now in the sandbox, if the price is still at most 3¢ above theirs."""
    if req.spend is not None and req.spend <= 0:
        raise HTTPException(400, detail="Enter an amount above $0.")
    rules = box.load_rules()
    try:
        e = box.paper().whales.copy_trade(_their_trade(req), **_sized(req, rules))
    except VenueError as err:
        raise venue_error(err) from err
    with _lock:
        return box.record(e, {"venue": "polymarket", "id": req.wallet, "name": req.name or req.wallet})


COPIED_TTL_S = 30


@app.get("/api/copied")
def copied(box: Sandbox = Depends(me)) -> dict[str, Any]:
    """One row per copied trade with its status (open / won / lost) and profit after fees, newest first.

    Valuing open bets reads each market's book, and Polymarket US can ask us to wait 10 s, so the answer
    is kept for 30 s and, while a new one is worked out, the last one is served. Not after a new copy: then
    the last answer doesn't have it, so this waits for one that does.
    """
    hit = box.copied_cache["data"]
    if hit is None or box.copied_cache["copies"] != box.copies:  # the first answer, or one after a copy
        with box.copied_lock:
            if box.copied_cache["data"] is not None and box.copied_cache["copies"] == box.copies:
                return box.copied_cache["data"]  # type: ignore[no-any-return]  # worked out while we waited
            return _copied_refresh(box)
    if time.time() - box.copied_cache["at"] >= COPIED_TTL_S:
        threading.Thread(target=_copied_background, args=(box,), daemon=True).start()
    return hit  # type: ignore[no-any-return]


def _copied_refresh(box: Sandbox) -> dict[str, Any]:
    copies = box.copies  # before reading: a copy recorded meanwhile leaves this answer stale
    data = _copied_now(box)
    box.copied_cache.update(at=time.time(), data=data, copies=copies)
    return data


def _copied_background(box: Sandbox) -> None:
    if not box.copied_lock.acquire(blocking=False):
        return  # already being worked out
    try:
        _copied_refresh(box)
    except Exception:  # keep serving the last answer; the next read tries again
        pass
    finally:
        box.copied_lock.release()


def _copied_now(box: Sandbox) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if box.copied_file().exists():
        for line in box.copied_file().read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    ids = [r["order_id"] for r in rows if r.get("order_id")]
    try:
        # No engine lock: the paper store locks itself, and the copy loop can hold _lock for seconds.
        results = box.paper().whales.copy_results(ids) if ids else {}
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
    groups = _compare(out)
    same = [g for g in groups if g["theirs"]["at_their_price"] is not None and g["you"]["pnl"] is not None]
    return {
        "rows": out,
        "groups": groups,
        "total": {"net": round(net, 2), "trades": len(done), **count},
        "by_trade": {  # the same, one per row of the page: repeat copies of one trade count once
            "trades": sum(1 for g in groups if g["you"]["contracts"]),
            "copies": len(done),
            **{k: sum(1 for g in groups if g["you"]["contracts"] and g["you"]["status"] == k)
               for k in ("open", "won", "lost", "void")},
        },
        "vs": {  # the same trades, valued the same way, at the prices the traders paid (before their fees)
            "you": round(sum(g["you"]["pnl"] for g in same), 2),
            "at_their_price": round(sum(g["theirs"]["at_their_price"] for g in same), 2),
            "put_in": round(sum(g["you"]["put_in"] for g in same), 2),
            "trades": len(same),
        },
        "unvalued": sum(1 for o in done if o["result"]["pnl"] is None),
    }


def _compare(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One group per trader and outcome (copies of their repeat buys together), newest first: your result
    next to theirs. Both are valued the same way: at the payout once the market settled, else at the price
    you could sell at now. Theirs is what their buys made held to the end, before their fees (Polymarket
    doesn't show their fees or when they sold); ``at_their_price`` is your contracts at their price."""
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for o in rows:  # newest first
        k = (o["trader"].get("id"), o["venue"], o["market"], o["side"])
        g = groups.setdefault(k, {"at": o["at"], "trader": o["trader"], "source": o["source"], "venue": o["venue"],
                                  "market": o["market"], "side": o["side"], "copies": []})  # fmt: skip
        g["copies"].append(o)
    out = []
    for g in groups.values():
        res = [c["result"] for c in g["copies"] if c["result"] and c["result"]["status"] != "unfilled"]
        n = sum(r.get("contracts") or 0 for r in res)
        cost = sum(r.get("cost") or 0 for r in res)
        fees = sum(r.get("fees") or 0 for r in res)
        pnls = [r.get("pnl") for r in res]
        first = res[0] if res else {}
        status = first.get("status") or "unfilled"
        value = first.get("payout") if status in ("won", "lost", "void") else first.get("mark")  # one contract
        you_pnl = None if not res or any(x is None for x in pnls) else round(sum(pnls), 4)
        put_in = round(cost + fees, 4)
        src = [c["source"] for c in g["copies"]]
        sized = [x for x in src if (x.get("size") or 0) > 0]
        their_n = sum(x["size"] for x in sized)
        their_usd = sum(x.get("usd") or 0 for x in sized)
        if their_n:
            their_price: float | None = their_usd / their_n
        else:  # a trade copied from the Feed before sizes were kept: their average price only
            prices = [x["price"] for x in src if x.get("price")]
            their_price = sum(prices) / len(prices) if prices else None
        theirs = {"price": their_price, "contracts": their_n or None, "put_in": round(their_usd, 2) or None,
                  "pnl": None, "return": None, "at_their_price": None}  # fmt: skip
        if their_price and value is not None:
            theirs["return"] = round(value / their_price - 1, 4)
            theirs["at_their_price"] = round((value - their_price) * n, 4) if n else None
            if their_n:
                theirs["pnl"] = round(value * their_n - their_usd, 2)
        out.append(g | {
            "you": {"contracts": n, "avg_price": cost / n if n else None, "fees": round(fees, 4), "put_in": put_in,
                    "pnl": you_pnl, "return": round(you_pnl / put_in, 4) if you_pnl is not None and put_in else None,
                    "status": status, "value": value},
            "theirs": theirs,
        })  # fmt: skip
    return out


@app.get("/api/paper")
def paper_account(box: Sandbox = Depends(me)) -> dict[str, Any]:
    if not box.has_account():  # nothing copied yet: don't make a sandbox just to say it's empty
        return {"net": 0.0, "realized": 0.0, "unrealized": 0.0, "fees": 0.0, "rows": []}
    p = box.paper().pnl()
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
def paper_reset(box: Sandbox = Depends(me)) -> dict[str, Any]:
    """Start the sandbox over: no positions, no copied trades and nobody copied. The size and price limits stay."""
    box.reset()
    box.apply_rules()
    return paper_account(box)


@app.get("/")
def index(request: Request) -> FileResponse:
    page = FileResponse(HERE / "static" / "index.html")
    if HOSTED:  # the page's API calls then all carry the same browser id
        sid = request.cookies.get(SID_COOKIE, "")
        _set_sid(page, sid if _SID.fullmatch(sid) else secrets.token_hex(16))
        page.headers["Cache-Control"] = "private, no-store"  # never cached in front of us: the cookie is theirs
    return page


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    """The in-app logo, for browsers that ask for /favicon.ico instead of reading the page's icon link."""
    return FileResponse(HERE / "static" / "icon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")  # the font and the icon


def main() -> None:
    if "SIGNALS_STORE_DIR" not in os.environ and not STORE_DIR.exists() and _OLD_STORE_DIR.exists():
        _OLD_STORE_DIR.rename(STORE_DIR)
    port = int(os.environ.get("SIGNALS_PORT", "8790"))
    print(f"Signals on http://127.0.0.1:{port}  (paper store: {STORE_DIR})")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
