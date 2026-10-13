"""The trader directory: every Polymarket wallet Signals has found, and where, kept across runs, with what scoring
said about each.

Phase 1 is finding; phase 2 keeps every score worked out; phase 3 evaluates every wallet in the background
(``due``: what to look at next). Nothing here is ranked or shown on the page, and the Worth following check still
picks its own 200 wallets: a wallet in the directory is *discovered*, and a score kept here is the record of a
check, never a recommendation.

Where a wallet can be found (``source``):

- ``leaderboard``: on one of Polymarket's leaderboards (``place`` is the board, ``"PNL MONTH POLITICS"``;
  ``rank`` their latest place on it). From the SDK's ``find_wallets`` sweep.
- ``market``: trading in an open market picked from every level of activity, from the same sweep.
- ``feed``: a trade of theirs in the Feed's big trades.
- ``search``: a wallet opened on Signals that the last check didn't have (pasted into search, mostly).
- ``follow``: someone follows them. Who follows whom stays in follows.json, never here.
- ``check``: the Worth following check scored them (``place``: ``leaderboard`` or ``market``, where its own reads
  found them), including the scores it kept before the directory existed (imported).

A market or Feed sighting is kept per category (``place`` is the category, ``example`` the last market), so the
directory stays the size of its wallets however many markets a sweep reads. Each wallet also keeps the first
place it was ever found (``first_source``, ``first_place``).

Each wallet's evaluation (phases 2 and 3), on its row in ``wallets``:

- ``status``: ``found`` (never looked at), ``scored``, ``checked`` (a quick check's evidence only, no score: see
  ``quick_look``), or ``failed`` (the last try failed). An earlier score stays whatever comes after it.
- ``not_recommended``: why they aren't recommended now, one of REASONS (None: they are, or nothing's known yet).
  Every wallet is kept, whatever it says: it's about the evidence to recommend them, never whether they're worth
  keeping or looking at again. ``reason_label`` says it in a sentence.
- ``buys_30d``, ``buys_90d``, ``last_buy``: the evidence, as the last quick check read it: markets bought in the
  last 30 and 90 days (at least: one read is at most QUICK_TRADES trades) and their latest buy.
- ``scored_at``: when its latest score was worked out; ``tried_at``: the last try, whatever came of it.
- ``next_check``: when it's due to be looked at again (``next_check_at``); ``failures``: tries failed in a row.
- ``seen_category``: the category it was first seen in, so the evaluation can take each category in turn before
  a wallet has a score (``category``) of its own.
- ``last_active``: their latest trade whose time we know: a Feed trade, the last buy a score saw, or the latest
  trade a quick check read. A leaderboard or market sighting has no trade time, so it never sets this.
- ``segment`` and ``category`` (where most of their checked bets were), from the latest score.

``scores`` keeps each wallet's latest score whole (the engine's dict, zlib-compressed JSON, its checked bets cut
to ``SAMPLE_KEEP``) with its headline figures in columns, ``score_history`` the headline figures of every score
kept, and ``score_categories`` the latest score's price test in each category. A score never replaces a newer one.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from uselayer.whale_scores import ACTIVE_DAYS, RECOMMENDED, RISING_BETS

SOURCES = ("leaderboard", "market", "feed", "search", "follow", "check")
STATUSES = ("found", "scored", "checked", "failed")
# Why a wallet isn't recommended now. The first three are about the evidence (what a quick check or a score saw of
# their recent trading); the rest are what a score found.
REASONS = ("not_enough_recent_evidence", "not_trading_lately", "dormant", "no_edge", "lucky", "no_view")
REASON_LABELS = {
    "not_enough_recent_evidence": "Not enough recent evidence: {buys_30d} bets in the last 30 days, and {need} are "
                                  "needed to judge a trader",
    "not_trading_lately": "Not trading lately: no buy in the last {active} days",
    "dormant": "Dormant: no buy in the last {evidence} days",
    "no_edge": "Scored: no clear edge",
    "lucky": "Scored: a big profit, but the evidence says luck more than skill",
    "no_view": "Scored: a market maker or arbitrage, not views on the outcome",
}  # fmt: skip
# what kept a score: the Worth following check, a trader page, the import, the background evaluation
VIA = ("check", "page", "import", "background")
KINDS = ("recommended", "new", "recheck")  # what ``due`` hands out, in that order
SAMPLE_KEEP = 30  # checked bets kept with a score, as the Worth following check keeps them

SCORE_DAYS = 30  # the window a score reads, as the Worth following check and a trader page score
EVIDENCE_DAYS = 90  # how far back a quick check counts buys, so a selective trader's record is kept too
QUICK_TRADES = 500  # the latest trades a quick check reads: one read (Polymarket's most a page), so a few markets traded
#                     heavily still reach back past the 30 days instead of cutting the read short

# When a wallet is looked at again, by why it isn't recommended (``next_check_at``).
RESCORE_S = 6 * 3600  # a recommended trader (one of uselayer's RECOMMENDED segments): scored again, as the check is
RECHECK_EVIDENCE_S = 86400  # not enough recent evidence, or not trading lately: one read a day, so they're scored as
#                             soon as they have the bets (or buy again)
RECHECK_SCORED_S = 2 * 86400  # any other score
RECHECK_QUIET_S = 7 * 86400  # dormant, or a score of market making or arbitrage (which rarely changes)
RETRY_S = (900, 3600, 4 * 3600, 12 * 3600, 86400)  # after 1, 2, 3, 4, 5 or more failed tries in a row
# A leaderboard place counts for on_leaderboard while a sweep saw it this recently (a sweep is daily, and takes a
# few hours). When the sweep is off or behind, the evaluation reads the boards itself (app.listed_now).
LISTED_FRESH_S = 26 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (wallet TEXT PRIMARY KEY, name TEXT, first_seen REAL NOT NULL,
  last_seen REAL NOT NULL, first_source TEXT NOT NULL, first_place TEXT NOT NULL, last_active REAL,
  status TEXT NOT NULL DEFAULT 'found', scored_at REAL, tried_at REAL, score_error TEXT, segment TEXT, category TEXT);
CREATE INDEX IF NOT EXISTS wallets_by_first_seen ON wallets (first_seen);
CREATE TABLE IF NOT EXISTS sightings (wallet TEXT NOT NULL, source TEXT NOT NULL, place TEXT NOT NULL,
  category TEXT, rank INTEGER, example TEXT, example_title TEXT, times INTEGER NOT NULL, trades INTEGER NOT NULL,
  usd REAL NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY (wallet, source, place));
CREATE INDEX IF NOT EXISTS sightings_by_source ON sightings (source, last_seen);
CREATE INDEX IF NOT EXISTS sightings_by_category ON sightings (category);
CREATE TABLE IF NOT EXISTS sweeps (started REAL PRIMARY KEY, finished REAL, sightings INTEGER NOT NULL DEFAULT 0,
  new INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS scores (wallet TEXT PRIMARY KEY, scored_at REAL NOT NULL, via TEXT NOT NULL,
  format INTEGER, segment TEXT, skill TEXT, copyable TEXT, style TEXT, confidence TEXT, active INTEGER, bets INTEGER,
  decisions INTEGER, edge REAL, edge_z REAL, copy REAL, copy_low REAL, trading_pnl_window REAL, pnl REAL, volume REAL,
  last_buy REAL, window_start REAL, window_end REAL, result BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS score_history (wallet TEXT NOT NULL, scored_at REAL NOT NULL, via TEXT NOT NULL,
  segment TEXT, skill TEXT, copyable TEXT, bets INTEGER, edge REAL, edge_z REAL, copy REAL, copy_low REAL,
  trading_pnl_window REAL, PRIMARY KEY (wallet, scored_at));
CREATE TABLE IF NOT EXISTS score_categories (wallet TEXT NOT NULL, category TEXT NOT NULL, bets INTEGER NOT NULL,
  edge REAL, edge_z REAL, copy REAL, copy_low REAL, strong INTEGER NOT NULL, PRIMARY KEY (wallet, category));
CREATE INDEX IF NOT EXISTS score_categories_by_category ON score_categories (category, strong);
"""
# A phase 1 directory.db has wallets without these: added when it's first opened, empty (status "found").
_PHASE_2_COLUMNS = (("last_active", "REAL"), ("status", "TEXT NOT NULL DEFAULT 'found'"), ("scored_at", "REAL"),
                    ("tried_at", "REAL"), ("score_error", "TEXT"), ("segment", "TEXT"), ("category", "TEXT"))  # fmt: skip
_PHASE_2_INDEXES = """
CREATE INDEX IF NOT EXISTS wallets_by_status ON wallets (status, scored_at);
CREATE INDEX IF NOT EXISTS wallets_by_last_active ON wallets (last_active);
"""
# Phase 3, the evaluation's schedule: added to an older directory.db when it's first opened (``_upgrade``).
_PHASE_3_COLUMNS = (("next_check", "REAL"), ("failures", "INTEGER NOT NULL DEFAULT 0"), ("seen_category", "TEXT"),
                    ("not_recommended", "TEXT"), ("buys_30d", "INTEGER"), ("buys_90d", "INTEGER"),
                    ("last_buy", "REAL"))  # fmt: skip
_PHASE_3_INDEXES = "CREATE INDEX IF NOT EXISTS wallets_by_next_check ON wallets (next_check);"

_RECOMMENDED_SQL = ", ".join(f"'{s}'" for s in RECOMMENDED)
# Which of KINDS a due wallet is: a recommended trader (scored, or failed since), one never looked at, any other.
_KIND = f"""CASE WHEN segment IN ({_RECOMMENDED_SQL}) AND status IN ('scored', 'failed') THEN 0
  WHEN status = 'found' THEN 1 ELSE 2 END"""
# On the boards the Worth following check reads to call a wallet "on the leaderboard" (uselayer's find_candidates,
# passed to its score as on_leaderboard): the monthly profit board's top 1,000; the weekly and all-time profit and
# monthly volume boards' top 50; each category's monthly profit top 50.
CHECK_CATEGORIES = ("SPORTS", "CRYPTO", "POLITICS", "ECONOMICS", "CULTURE", "WEATHER", "ESPORTS", "TECH")
# (order, period, category, places): the same boards, to read directly
CHECK_BOARDS = (("PNL", "MONTH", None, 1000), ("PNL", "WEEK", None, 50), ("PNL", "ALL", None, 50),
                ("VOL", "MONTH", None, 50), *(("PNL", "MONTH", c, 50) for c in CHECK_CATEGORIES))  # fmt: skip
_CHECK_BOARDS = ("PNL WEEK OVERALL", "PNL ALL OVERALL", "VOL MONTH OVERALL", *(f"PNL MONTH {c}" for c in CHECK_CATEGORIES))
_LISTED = f"""EXISTS (SELECT 1 FROM sightings s WHERE s.wallet = q.wallet AND s.source = 'leaderboard'
  AND s.last_seen >= ? AND ((s.place = 'PNL MONTH OVERALL' AND s.rank <= 1000)
  OR (s.place IN ({", ".join(f"'{b}'" for b in _CHECK_BOARDS)}) AND s.rank <= 50)))"""


def reason_for(segment: str | None) -> str | None:
    """Why a score's segment isn't recommended (one of REASONS), or None when it is."""
    if segment is None or segment in RECOMMENDED:
        return None
    return "not_trading_lately" if segment == "inactive" else segment


def reason_label(reason: str | None, buys_30d: int | None = None) -> str | None:
    """A reason in a sentence, for a page (None when they're recommended)."""
    if reason is None:
        return None
    return REASON_LABELS.get(reason, reason).format(buys_30d=buys_30d if buys_30d is not None else "Fewer than",
                                                    need=RISING_BETS, active=ACTIVE_DAYS, evidence=EVIDENCE_DAYS)  # fmt: skip


def next_check_at(status: str, reason: str | None, segment: str | None, at: float, failures: int = 0) -> float:
    """When a wallet is due again after a look at ``at`` that left it ``status``, not recommended for ``reason``
    (its latest score's ``segment``): a recommended trader in RESCORE_S; one short of evidence or not trading
    lately each day; dormant ones and market makers weekly; a failed try later each time it fails again in a row."""
    if status == "failed":
        return at + RETRY_S[min(max(failures, 1), len(RETRY_S)) - 1]
    if status == "found":
        return at
    if reason in ("not_enough_recent_evidence", "not_trading_lately"):
        return at + RECHECK_EVIDENCE_S
    if reason in ("dormant", "no_view"):
        return at + RECHECK_QUIET_S
    return at + (RESCORE_S if segment in RECOMMENDED else RECHECK_SCORED_S)


@dataclass(frozen=True)
class QuickLook:
    """What one read of a wallet's latest trades says: whether to ``score`` them now, else why they aren't
    recommended (``reason``), and the evidence: markets bought in the last 30 and 90 days, their latest buy and
    their latest trade."""

    score: bool
    reason: str | None
    buys_30d: int
    buys_90d: int
    last_buy: float | None
    last_trade: float | None


def quick_look(trades: list[tuple[float, bool, str]], now: float, read_all: bool,
               scored_at: float | None = None) -> QuickLook:  # fmt: skip
    """From a wallet's latest trades (``(time, bought, market)``, newest first, as one read gives them), whether a
    score is worth its reads now. Not when there's no buy in the last EVIDENCE_DAYS (``dormant``), or fewer than
    RISING_BETS markets bought in the last SCORE_DAYS (``not_enough_recent_evidence``: a score needs that many bets
    to say anything about skill), or no buy in the last ACTIVE_DAYS and none since their last score
    (``not_trading_lately``: a new score would say the same). Both thresholds are the SDK's own: they decide whether
    there's enough recent evidence to recommend someone, and the wallet is kept and looked at again either way.
    When the read was cut short (``read_all`` False), only the time it covers counts."""
    covered = float("-inf") if read_all or not trades else min(t for t, _, _ in trades)
    week, window, evidence = (now - d * 86400 for d in (ACTIVE_DAYS, SCORE_DAYS, EVIDENCE_DAYS))
    bought = [(t, m) for t, b, m in trades if b]
    buys_30d = len({m for t, m in bought if t >= window})
    buys_90d = len({m for t, m in bought if t >= evidence})
    last_buy = max((t for t, _ in bought), default=None)
    reason = None
    if covered <= evidence and buys_90d == 0:
        reason = "dormant"
    elif covered <= window and buys_30d < RISING_BETS:
        reason = "not_enough_recent_evidence"
    elif covered <= week and (last_buy is None or last_buy < week) and scored_at is not None and (
            last_buy is None or last_buy <= scored_at):  # fmt: skip
        reason = "not_trading_lately"
    return QuickLook(reason is None, reason, buys_30d, buys_90d, last_buy,
                     max((t for t, _, _ in trades), default=None))  # fmt: skip


@dataclass(frozen=True)
class Seen:
    """One sighting to keep. ``place`` names where within the source (a board, a category; "" for search and
    follow). ``example`` / ``example_title``: the market they were seen in, for market and Feed sightings.
    ``traded_at``: when the trade they were seen in was made, only when that's known (a Feed trade)."""

    wallet: str
    source: str
    place: str = ""
    name: str | None = None
    category: str | None = None
    rank: int | None = None
    example: str | None = None
    example_title: str | None = None
    trades: int = 0
    usd: float = 0.0
    traded_at: float | None = None


def from_sighting(s: Any) -> Seen:
    """The SDK's ``Sighting`` (uselayer 0.9) as kept here: a board as it is, a market by its category."""
    if s.source == "leaderboard":
        return Seen(s.wallet, "leaderboard", s.where, s.name, s.category, rank=s.rank)
    return Seen(s.wallet, "market", s.category or "Other", s.name, s.category, example=s.where,
                example_title=s.title, trades=s.trades, usd=s.usd)  # fmt: skip


def _ts(v: Any) -> float | None:
    """A time as a score keeps it (ISO text) in seconds; None when it's missing or unreadable."""
    if isinstance(v, int | float):
        return float(v)
    try:
        return datetime.fromisoformat(v).timestamp() if isinstance(v, str) else None
    except ValueError:
        return None


def _stat(d: Any) -> tuple[float | None, float | None, float | None]:
    """A kept ``Stat``'s mean, z and the bottom of its range (as kept: one from before uselayer 0.8 has no ``low``,
    and its ``mean - margin`` is what that check said)."""
    if not isinstance(d, dict) or d.get("mean") is None:
        return None, None, None
    low = d.get("low")
    if low is None and d.get("margin") is not None:
        low = round(d["mean"] - d["margin"], 5)
    return d["mean"], d.get("z"), low


def last_trade(score: dict[str, Any]) -> float | None:
    """The latest buy a score saw: its ``last_buy``, or (kept before uselayer 0.6) its latest checked bet."""
    if (at := _ts(score.get("last_buy"))) is not None:
        return at
    ats = [t for b in (score.get("recent") or []) + (score.get("sample") or []) if (t := _ts(b.get("at"))) is not None]
    return max(ats) if ats else None


def main_category(score: dict[str, Any]) -> str | None:
    """Where most of a score's checked bets were; None without any."""
    cats = [c for c in score.get("categories") or [] if c.get("category") and c.get("bets")]
    return max(cats, key=lambda c: c["bets"])["category"] if cats else None


def _username(score: dict[str, Any]) -> str | None:
    """Their Polymarket username; None for one without (the SDK names them by their wallet)."""
    name = score.get("name")
    return None if not name or str(name).startswith("0x") else str(name)


def _found_by(score: dict[str, Any], via: str) -> Seen:
    """How a wallet first gets into the directory when a score is its first sighting: a check's (or an imported
    one's) as ``check``, where its reads found them; one scored on a page as a search."""
    name = _username(score)
    if via == "page":
        return Seen(score["wallet"], "search", name=name)
    return Seen(score["wallet"], "check", "leaderboard" if score.get("on_leaderboard") else "market", name=name)


# the later of the two trade times, either of which may be missing (SQLite's MAX of a NULL is NULL)
_LAST_ACTIVE = (
    "MAX(COALESCE(excluded.last_active, wallets.last_active), COALESCE(wallets.last_active, excluded.last_active))"
)
_UPSERT_WALLET = f"""
INSERT INTO wallets (wallet, name, first_seen, last_seen, first_source, first_place, last_active, seen_category)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (wallet) DO UPDATE SET last_seen = excluded.last_seen, name = COALESCE(excluded.name, wallets.name),
  last_active = {_LAST_ACTIVE}, seen_category = COALESCE(wallets.seen_category, excluded.seen_category)
"""
_UPSERT_SIGHTING = """
INSERT INTO sightings (wallet, source, place, category, rank, example, example_title, times, trades, usd,
  first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
ON CONFLICT (wallet, source, place) DO UPDATE SET last_seen = excluded.last_seen, times = sightings.times + 1,
  rank = COALESCE(excluded.rank, sightings.rank), example = COALESCE(excluded.example, sightings.example),
  example_title = COALESCE(excluded.example_title, sightings.example_title),
  trades = sightings.trades + excluded.trades, usd = sightings.usd + excluded.usd
"""


class Directory:
    """The directory in one SQLite file. Safe to share between threads: one connection at a time."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._upgraded = False

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.path)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(SCHEMA)
                if not self._upgraded:
                    self._upgrade(con)
                    self._upgraded = True
                yield con
                con.commit()
            finally:
                con.close()

    @staticmethod
    def _upgrade(con: sqlite3.Connection) -> None:
        """A directory.db from an earlier phase gets the columns it lacks; then each wallet without a schedule
        (only ever one from before phase 3) gets one from its last look, and the category it was first seen in."""
        have = {r[1] for r in con.execute("PRAGMA table_info(wallets)")}
        for col, kind in _PHASE_2_COLUMNS + _PHASE_3_COLUMNS:
            if col not in have:
                con.execute(f"ALTER TABLE wallets ADD COLUMN {col} {kind}")
        con.executescript(_PHASE_2_INDEXES + _PHASE_3_INDEXES)
        rows = con.execute(
            "SELECT wallet, status, segment, first_seen, scored_at, tried_at, failures FROM wallets "
            "WHERE next_check IS NULL"
        ).fetchall()
        if not rows:
            return
        con.executemany(
            "UPDATE wallets SET next_check = ?, not_recommended = ? WHERE wallet = ?",
            [(next_check_at(st, reason_for(seg), seg, (tried if st == "failed" else scored) or first, fails),
              reason_for(seg), w) for w, st, seg, first, scored, tried, fails in rows],
        )  # fmt: skip
        con.execute(
            "UPDATE wallets SET seen_category = (SELECT category FROM sightings s WHERE s.wallet = wallets.wallet "
            "AND s.category IS NOT NULL ORDER BY s.first_seen, s.times DESC LIMIT 1) WHERE seen_category IS NULL"
        )

    def add(self, seen: Iterable[Seen], at: float | None = None) -> int:
        """Keeps each sighting: a new wallet is added, a known one gets its last-seen time. Returns how many
        wallets were new."""
        now = time.time() if at is None else at
        rows = [s for s in seen if s.wallet]
        if not rows:
            return 0
        with self._db() as con:
            return self._add(con, rows, now)

    @staticmethod
    def _add(con: sqlite3.Connection, rows: list[Seen], now: float) -> int:
        before = con.total_changes
        con.executemany(
            "INSERT OR IGNORE INTO wallets (wallet, name, first_seen, last_seen, first_source, first_place, "
            "next_check, seen_category) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(s.wallet.lower(), s.name, now, now, s.source, s.place, now, s.category) for s in rows],
        )
        new = con.total_changes - before
        con.executemany(
            _UPSERT_WALLET,
            [(s.wallet.lower(), s.name, now, now, s.source, s.place, s.traded_at, s.category) for s in rows],
        )
        con.executemany(
            _UPSERT_SIGHTING,
            [(s.wallet.lower(), s.source, s.place, s.category, s.rank, s.example, s.example_title, s.trades,
              s.usd, now, now) for s in rows],
        )  # fmt: skip
        return new

    def keep_scores(self, scores: Iterable[dict[str, Any]], via: str, at: float | None = None) -> int:
        """Keeps each wallet's score (the engine's dict), worked out at ``at`` (now by default), ``via`` the check,
        a trader page or the import. A wallet the directory hasn't found yet is added (``_found_by``). The score
        becomes the wallet's latest unless a newer one is kept; its headline figures go into the history either
        way. Returns how many became the latest."""
        now = time.time() if at is None else at
        rows = [s for s in scores if s.get("wallet")]
        if not rows:
            return 0
        kept = 0
        with self._db() as con:
            self._add(con, [_found_by(s, via) for s in rows if not self._has(con, s["wallet"])], now)
            for s in rows:
                w = s["wallet"].lower()
                edge, edge_z, _ = _stat(s.get("edge"))
                copy, _, copy_low = _stat(s.get("copy"))
                head = (s.get("segment"), s.get("skill"), s.get("copyable"), s.get("bets"), edge, edge_z, copy, copy_low,
                        s.get("trading_pnl_window"))  # fmt: skip
                con.execute("INSERT OR IGNORE INTO score_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (w, now, via, *head))  # fmt: skip
                active = last_trade(s)
                con.execute(
                    "UPDATE wallets SET last_active = MAX(COALESCE(?, last_active), COALESCE(last_active, ?)) "
                    "WHERE wallet = ?",
                    (active, active, w),
                )
                # the same score again (the import, on each launch) or an older one: the latest stays as it is
                if con.execute("SELECT 1 FROM scores WHERE wallet = ? AND scored_at >= ?", (w, now)).fetchone():
                    continue
                window = s.get("window") or [None, None]
                result = zlib.compress(json.dumps(s | {"sample": (s.get("sample") or [])[:SAMPLE_KEEP]}).encode())
                con.execute(
                    "INSERT OR REPLACE INTO scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (w, now, via, s.get("format"), s.get("segment"), s.get("skill"), s.get("copyable"), s.get("style"),
                     s.get("confidence"), None if s.get("active") is None else int(bool(s["active"])), s.get("bets"),
                     s.get("decisions"), edge, edge_z, copy, copy_low, s.get("trading_pnl_window"), s.get("pnl"),
                     s.get("volume"), active, _ts(window[0]), _ts(window[-1]), result),
                )  # fmt: skip
                con.execute("DELETE FROM score_categories WHERE wallet = ?", (w,))
                con.executemany(
                    "INSERT OR REPLACE INTO score_categories VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(w, c["category"], c.get("bets") or 0, *_stat(c.get("edge"))[:2], _stat(c.get("copy"))[0],
                      _stat(c.get("copy"))[2], int(bool(c.get("strong")))) for c in s.get("categories") or []
                     if c.get("category")],
                )  # fmt: skip
                # a try since this score (one that failed, or a quick check) keeps its status, why, and its schedule
                con.execute(
                    "UPDATE wallets SET status = CASE WHEN tried_at > ? THEN status ELSE 'scored' END, "
                    "score_error = CASE WHEN tried_at > ? THEN score_error END, scored_at = ?, "
                    "next_check = CASE WHEN tried_at > ? THEN next_check ELSE ? END, "
                    "not_recommended = CASE WHEN tried_at > ? THEN not_recommended ELSE ? END, "
                    "failures = CASE WHEN tried_at > ? THEN failures ELSE 0 END, "
                    "tried_at = MAX(COALESCE(tried_at, ?), ?), segment = ?, category = ?, name = COALESCE(name, ?) "
                    "WHERE wallet = ?",
                    (now, now, now, now, next_check_at("scored", reason_for(s.get("segment")), s.get("segment"), now),
                     now, reason_for(s.get("segment")), now, now, now, s.get("segment"), main_category(s),
                     _username(s), w),
                )  # fmt: skip
                kept += 1
        return kept

    def score_failed(self, wallet: str, error: str, at: float | None = None) -> None:
        """Scoring a wallet failed: ``failed``, with why and when, tried again later each time it fails again in
        a row (RETRY_S). An earlier score stays, with its time."""
        now = time.time() if at is None else at
        w = wallet.lower()
        with self._db() as con:
            if not self._has(con, w):
                self._add(con, [Seen(w, "search")], now)
            failures, segment = con.execute("SELECT failures, segment FROM wallets WHERE wallet = ?", (w,)).fetchone()
            con.execute(
                "UPDATE wallets SET status = 'failed', tried_at = ?, score_error = ?, failures = ?, next_check = ? "
                "WHERE wallet = ?",
                (now, error[:500], failures + 1, next_check_at("failed", None, segment, now, failures + 1), w),
            )

    def quick_checked(self, wallet: str, look: QuickLook, at: float | None = None) -> None:
        """What a quick check found (``quick_look``): the evidence (``buys_30d``, ``buys_90d``, ``last_buy``, and
        their latest trade as ``last_active``) always. When it isn't worth a score now, the wallet is ``checked``,
        not recommended for the look's reason, until it's due again; otherwise a score follows and says the rest.
        An earlier score stays either way."""
        now = time.time() if at is None else at
        w = wallet.lower()
        with self._db() as con:
            if not self._has(con, w):
                self._add(con, [Seen(w, "search")], now)
            con.execute(
                "UPDATE wallets SET last_active = MAX(COALESCE(?, last_active), COALESCE(last_active, ?)), "
                "buys_30d = ?, buys_90d = ?, last_buy = MAX(COALESCE(?, last_buy), COALESCE(last_buy, ?)) "
                "WHERE wallet = ?",
                (look.last_trade, look.last_trade, look.buys_30d, look.buys_90d, look.last_buy, look.last_buy, w),
            )
            if not look.score:
                segment = con.execute("SELECT segment FROM wallets WHERE wallet = ?", (w,)).fetchone()[0]
                con.execute(
                    "UPDATE wallets SET status = 'checked', not_recommended = ?, tried_at = ?, score_error = NULL, "
                    "failures = 0, next_check = ? WHERE wallet = ?",
                    (look.reason, now, next_check_at("checked", look.reason, segment, now), w),
                )

    def due(self, limit: int, now: float | None = None) -> list[dict[str, Any]]:
        """Up to ``limit`` wallets due a look (``next_check`` passed), in the order to take them: recommended
        traders first (most overdue first), then wallets never looked at (newest found first), then the rest
        (most overdue first). Within each, the categories take turns (a wallet's from its score, else where it
        was first seen), so no category waits behind a bigger one. Each with its ``kind`` (one of KINDS),
        ``status``, ``scored_at``, ``category``, ``name``, and ``on_leaderboard`` as the Worth following check
        would pass it."""
        now = time.time() if now is None else now
        with self._db() as con:
            rows = con.execute(
                f"""SELECT wallet, kind, cat, status, scored_at, name, {_LISTED} FROM (
                  SELECT *, ROW_NUMBER() OVER (PARTITION BY kind, cat
                    ORDER BY CASE WHEN kind = 1 THEN -first_seen ELSE next_check END, wallet) AS turn
                  FROM (SELECT wallet, status, scored_at, name, first_seen, next_check, {_KIND} AS kind,
                    COALESCE(category, seen_category, 'Other') AS cat FROM wallets WHERE next_check <= ?)) q
                ORDER BY kind, turn, cat LIMIT ?""",
                (now - LISTED_FRESH_S, now, limit),
            ).fetchall()
        return [{"wallet": w, "kind": KINDS[k], "category": c, "status": st, "scored_at": at, "name": n,
                 "on_leaderboard": bool(b)} for w, k, c, st, at, n, b in rows]  # fmt: skip

    def boards_seen_at(self) -> float | None:
        """When a sweep last saw the monthly profit board (the one on_leaderboard leans on most), or None."""
        with self._db() as con:
            return con.execute(  # type: ignore[no-any-return]
                "SELECT MAX(last_seen) FROM sightings WHERE source = 'leaderboard' AND place = 'PNL MONTH OVERALL'"
            ).fetchone()[0]

    def due_counts(self, now: float | None = None) -> dict[str, int]:
        """How many wallets are due a look now, by kind."""
        now = time.time() if now is None else now
        with self._db() as con:
            got = dict(con.execute(f"SELECT {_KIND}, COUNT(*) FROM wallets WHERE next_check <= ? GROUP BY 1", (now,)))
        return {k: got.get(i, 0) for i, k in enumerate(KINDS)}

    @staticmethod
    def _has(con: sqlite3.Connection, wallet: str) -> bool:
        return con.execute("SELECT 1 FROM wallets WHERE wallet = ?", (wallet.lower(),)).fetchone() is not None

    def score(self, wallet: str) -> dict[str, Any] | None:
        """A wallet's latest score as it was kept, with ``scored_at`` and ``via``; None if it was never scored."""
        with self._db() as con:
            r = con.execute("SELECT result, scored_at, via FROM scores WHERE wallet = ?", (wallet.lower(),)).fetchone()
        return None if r is None else json.loads(zlib.decompress(r[0])) | {"scored_at": r[1], "via": r[2]}

    def history(self, wallet: str) -> list[dict[str, Any]]:
        """The headline figures of every score kept for a wallet, oldest first."""
        with self._db() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM score_history WHERE wallet = ? ORDER BY scored_at", (wallet.lower(),))
            return [dict(r) for r in rows]

    def categories_of(self, wallet: str) -> list[dict[str, Any]]:
        """The latest score's price test in each category, most bets first."""
        with self._db() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM score_categories WHERE wallet = ? ORDER BY bets DESC, category", (wallet.lower(),)
            )
            return [dict(r) for r in rows]

    def sweep_started(self, at: float | None = None) -> float:
        started = time.time() if at is None else at
        with self._db() as con:
            con.execute("INSERT OR REPLACE INTO sweeps (started) VALUES (?)", (started,))
        return started

    def sweep_progress(self, started: float, sightings: int, new: int) -> None:
        with self._db() as con:
            con.execute(
                "UPDATE sweeps SET sightings = sightings + ?, new = new + ? WHERE started = ?", (sightings, new, started)
            )

    def sweep_finished(self, started: float, error: str | None = None, at: float | None = None) -> None:
        with self._db() as con:
            con.execute(
                "UPDATE sweeps SET finished = ?, error = ? WHERE started = ?",
                (time.time() if at is None else at, error, started),
            )

    def last_sweep(self) -> dict[str, Any] | None:
        """The latest sweep that finished without an error, or None."""
        with self._db() as con:
            r = con.execute(
                "SELECT started, finished, sightings, new FROM sweeps WHERE finished IS NOT NULL AND error IS NULL "
                "ORDER BY started DESC LIMIT 1"
            ).fetchone()
        return None if r is None else {"started": r[0], "finished": r[1], "sightings": r[2], "new": r[3]}

    def last_error(self) -> dict[str, Any] | None:
        """The latest sweep, when it failed: when, and why."""
        with self._db() as con:
            r = con.execute("SELECT started, finished, error FROM sweeps ORDER BY started DESC LIMIT 1").fetchone()
        return None if r is None or r[2] is None else {"started": r[0], "finished": r[1], "error": r[2]}

    def stats(self, now: float | None = None) -> dict[str, Any]:
        """Counts only, never a wallet: how many found, how many new in the last day, by source (and seen
        only there), by category, and the sweeps."""
        now = time.time() if now is None else now
        with self._db() as con:
            total = con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
            new_day = con.execute("SELECT COUNT(*) FROM wallets WHERE first_seen >= ?", (now - 86400,)).fetchone()[0]
            by_source = dict(con.execute("SELECT source, COUNT(DISTINCT wallet) FROM sightings GROUP BY source"))
            only = dict(con.execute(
                "SELECT source, COUNT(*) FROM (SELECT wallet, MIN(source) AS source FROM sightings GROUP BY wallet "
                "HAVING COUNT(DISTINCT source) = 1) GROUP BY source"
            ))  # fmt: skip
            first = dict(con.execute("SELECT first_source, COUNT(*) FROM wallets GROUP BY first_source"))
            by_cat = dict(con.execute(
                "SELECT category, COUNT(DISTINCT wallet) FROM sightings WHERE category IS NOT NULL GROUP BY category"
            ))  # fmt: skip
            boards = con.execute("SELECT COUNT(DISTINCT place) FROM sightings WHERE source = 'leaderboard'").fetchone()[0]
            sweeps = con.execute("SELECT COUNT(*) FROM sweeps WHERE finished IS NOT NULL AND error IS NULL").fetchone()[0]
            status = dict(con.execute("SELECT status, COUNT(*) FROM wallets GROUP BY status"))
            scored_day = con.execute("SELECT COUNT(*) FROM wallets WHERE scored_at >= ?", (now - 86400,)).fetchone()[0]
            segments = dict(con.execute(
                "SELECT segment, COUNT(*) FROM wallets WHERE segment IS NOT NULL GROUP BY segment ORDER BY 2 DESC"
            ))  # fmt: skip
            scored_cat = dict(con.execute(
                "SELECT category, COUNT(*) FROM wallets WHERE category IS NOT NULL GROUP BY category ORDER BY 2 DESC"
            ))  # fmt: skip
            strong = dict(con.execute(
                "SELECT category, COUNT(*) FROM score_categories WHERE strong = 1 GROUP BY category ORDER BY 2 DESC"
            ))  # fmt: skip
            via = dict(con.execute("SELECT via, COUNT(*) FROM scores GROUP BY via"))
            active_week = con.execute("SELECT COUNT(*) FROM wallets WHERE last_active >= ?", (now - 7 * 86400,)).fetchone()[0]
            known_active = con.execute("SELECT COUNT(*) FROM wallets WHERE last_active IS NOT NULL").fetchone()[0]
            history = con.execute("SELECT COUNT(*) FROM score_history").fetchone()[0]
            tried_day = dict(con.execute(
                "SELECT status, COUNT(*) FROM wallets WHERE tried_at >= ? GROUP BY status", (now - 86400,)
            ))  # fmt: skip
            reasons = dict(con.execute(
                "SELECT not_recommended, COUNT(*) FROM wallets WHERE not_recommended IS NOT NULL GROUP BY 1"
            ))  # fmt: skip
            recommended = con.execute(
                f"SELECT COUNT(*) FROM wallets WHERE status = 'scored' AND segment IN ({_RECOMMENDED_SQL})"
            ).fetchone()[0]
            selective = con.execute(
                "SELECT COUNT(*) FROM wallets WHERE buys_30d < ? AND buys_90d >= ?", (RISING_BETS, RISING_BETS)
            ).fetchone()[0]
        return {
            "wallets": total,
            "new_last_day": new_day,
            "by_source": {s: by_source.get(s, 0) for s in SOURCES},
            "only_from": {s: only.get(s, 0) for s in SOURCES},
            "first_found_by": {s: first.get(s, 0) for s in SOURCES},
            "by_category": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
            "leaderboards_seen": boards,
            "sweeps_finished": sweeps,
            "last_sweep": self.last_sweep(),
            "evaluation": {
                "by_status": {s: status.get(s, 0) for s in STATUSES},
                "scored_last_day": scored_day,
                "latest_score_by": {v: via.get(v, 0) for v in VIA},
                "scores_kept": history,
                "by_segment": segments,
                "by_category": scored_cat,
                "strong_by_category": strong,
                "last_active_known": known_active,
                "active_last_week": active_week,
                "looked_at_last_day": {s: tried_day.get(s, 0) for s in STATUSES if s != "found"},
                "recommended_now": recommended,
                "not_recommended": {r: reasons.get(r, 0) for r in REASONS},
                # enough bets over 90 days, too few in the last 30: the traders investigation B is about
                "selective": selective,
                "due_now": self.due_counts(now),
            },
        }

    def sources_of(self, wallet: str) -> list[dict[str, Any]]:
        """Every place one wallet was seen, most recent first (for tests and checks; not served)."""
        with self._db() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM sightings WHERE wallet = ? ORDER BY last_seen DESC, source, place", (wallet.lower(),)
            ).fetchall()
        return [dict(r) for r in rows]

    def wallet(self, wallet: str) -> dict[str, Any] | None:
        with self._db() as con:
            con.row_factory = sqlite3.Row
            r = con.execute("SELECT * FROM wallets WHERE wallet = ?", (wallet.lower(),)).fetchone()
        return None if r is None else dict(r)
