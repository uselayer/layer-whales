"""The trader directory: every Polymarket wallet Signals has found, and where, kept across runs.

Phase 1 is finding only. Nothing here is scored, ranked or shown on the page, and the Worth following check
still picks its own 200 wallets: a wallet in the directory is *discovered*, not evaluated or recommended.

Where a wallet can be found (``source``):

- ``leaderboard``: on one of Polymarket's leaderboards (``place`` is the board, ``"PNL MONTH POLITICS"``;
  ``rank`` their latest place on it). From the SDK's ``find_wallets`` sweep.
- ``market``: trading in an open market picked from every level of activity, from the same sweep.
- ``feed``: a trade of theirs in the Feed's big trades.
- ``search``: a wallet opened on Signals that the last check didn't have (pasted into search, mostly).
- ``follow``: someone follows them. Who follows whom stays in follows.json, never here.

A market or Feed sighting is kept per category (``place`` is the category, ``example`` the last market), so the
directory stays the size of its wallets however many markets a sweep reads. Each wallet also keeps the first
place it was ever found (``first_source``, ``first_place``).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCES = ("leaderboard", "market", "feed", "search", "follow")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (wallet TEXT PRIMARY KEY, name TEXT, first_seen REAL NOT NULL,
  last_seen REAL NOT NULL, first_source TEXT NOT NULL, first_place TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS wallets_by_first_seen ON wallets (first_seen);
CREATE TABLE IF NOT EXISTS sightings (wallet TEXT NOT NULL, source TEXT NOT NULL, place TEXT NOT NULL,
  category TEXT, rank INTEGER, example TEXT, example_title TEXT, times INTEGER NOT NULL, trades INTEGER NOT NULL,
  usd REAL NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY (wallet, source, place));
CREATE INDEX IF NOT EXISTS sightings_by_source ON sightings (source, last_seen);
CREATE INDEX IF NOT EXISTS sightings_by_category ON sightings (category);
CREATE TABLE IF NOT EXISTS sweeps (started REAL PRIMARY KEY, finished REAL, sightings INTEGER NOT NULL DEFAULT 0,
  new INTEGER NOT NULL DEFAULT 0, error TEXT);
"""


@dataclass(frozen=True)
class Seen:
    """One sighting to keep. ``place`` names where within the source (a board, a category; "" for search and
    follow). ``example`` / ``example_title``: the market they were seen in, for market and Feed sightings."""

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


def from_sighting(s: Any) -> Seen:
    """The SDK's ``Sighting`` (uselayer 0.9) as kept here: a board as it is, a market by its category."""
    if s.source == "leaderboard":
        return Seen(s.wallet, "leaderboard", s.where, s.name, s.category, rank=s.rank)
    return Seen(s.wallet, "market", s.category or "Other", s.name, s.category, example=s.where,
                example_title=s.title, trades=s.trades, usd=s.usd)  # fmt: skip


_UPSERT_WALLET = """
INSERT INTO wallets (wallet, name, first_seen, last_seen, first_source, first_place) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (wallet) DO UPDATE SET last_seen = excluded.last_seen, name = COALESCE(excluded.name, wallets.name)
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

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.path)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(SCHEMA)
                yield con
                con.commit()
            finally:
                con.close()

    def add(self, seen: Iterable[Seen], at: float | None = None) -> int:
        """Keeps each sighting: a new wallet is added, a known one gets its last-seen time. Returns how many
        wallets were new."""
        now = time.time() if at is None else at
        rows = [s for s in seen if s.wallet]
        if not rows:
            return 0
        with self._db() as con:
            before = con.total_changes
            con.executemany(
                "INSERT OR IGNORE INTO wallets (wallet, name, first_seen, last_seen, first_source, first_place) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(s.wallet.lower(), s.name, now, now, s.source, s.place) for s in rows],
            )
            new = con.total_changes - before
            con.executemany(_UPSERT_WALLET, [(s.wallet.lower(), s.name, now, now, s.source, s.place) for s in rows])
            con.executemany(
                _UPSERT_SIGHTING,
                [(s.wallet.lower(), s.source, s.place, s.category, s.rank, s.example, s.example_title, s.trades,
                  s.usd, now, now) for s in rows],
            )  # fmt: skip
        return new

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
