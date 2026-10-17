"""The trader directory: every Polymarket wallet Signals has found, and where, kept across runs, with what scoring
said about each.

Phase 1 is finding; phase 2 keeps every score worked out; phase 3 evaluates every wallet in the background
(``due``: what to look at next); Explore (``browse``) lets people look through it, all of it. A wallet in the
directory is *discovered*, and a score kept here is the record of a check, never a recommendation by itself. Phase
5 (``latest_scores``, ``scores_of``) lets the engine serve Traders' recommendations from the latest scores kept
here instead of the Worth following check's own 200 (SIGNALS_DISCOVERY, in app.py), by the same tiers: the
directory picks no one itself, and Explore is never limited to what Traders recommends.

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
- ``buys_30d``, ``buys_90d``, ``last_buy``, ``market_usd``: the evidence, as the last quick check read it: markets
  bought in the last 30 and 90 days, their latest buy, and the middle of the dollars they put in a market over the 90
  days. ``evidence_cut``: the read stopped at QUICK_TRADES trades before reaching back 90 days, so the counts are
  "at least". Never looked at: all None, which a page says is unknown, never zero.
- ``typical_usd``: their usual trade, from their latest score (the SDK's ``typical_usd``).
- ``scored_at``: when its latest score was worked out; ``tried_at``: the last try, whatever came of it.
- ``next_check``: when it's due to be looked at again (``next_check_at``); ``failures``: tries failed in a row.
- ``seen_category``: the category it was first seen in, so the evaluation can take each category in turn before
  a wallet has a score (``category``) of its own. Where a wallet was found says little about what it trades (a
  Tech board lists people who mostly trade Sports), so a page names it only as where they were found.
- ``last_active``: their latest trade whose time we know: a Feed trade, the last buy a score saw, or the latest
  trade a quick check read. A leaderboard or market sighting has no trade time, so it never sets this.
- ``segment`` and ``category`` (where most of their checked bets were), from the latest score.

``scores`` keeps each wallet's latest score whole (the engine's dict, zlib-compressed JSON, its checked bets cut
to ``SAMPLE_KEEP``) with its headline figures in columns, ``score_history`` the headline figures of every score
kept, and ``score_categories`` the latest score's price test in each category. A score never replaces a newer one.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import zlib
from collections import Counter
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
REASONS = ("not_enough_recent_evidence", "not_trading_lately", "dormant", "too_few", "parlays", "edge_losing",
           "no_edge", "big_profit", "lucky", "no_view")  # fmt: skip
REASON_LABELS = {
    "not_enough_recent_evidence": "Not enough recent evidence: {buys_30d} bets in the last 30 days, and {need} are "
                                  "needed to judge a trader",
    "not_trading_lately": "Not trading lately: no buy in the last {active} days",
    "dormant": "Dormant: no buy in the last {evidence} days",
    "too_few": "Scored: limited evidence, too few of their trades could be checked to measure skill either way",
    "parlays": "Scored: skill unmeasured, they trade mostly parlays, which have no price history",
    "edge_losing": "Scored: the price moves their way, but they lost money from trading over 30 days",
    "no_edge": "Scored: edge not proven",
    "big_profit": "Scored: a big profit, but the price test doesn't show skill (and nothing shows luck)",
    "lucky": "Scored: a big profit that rests on one win",
    "no_view": "Scored: a market maker or arbitrage, not views on the outcome",
}  # fmt: skip
# uselayer 0.10's tiers (``segment``) are skill and track record only: "too_few" and "big_profit" are new, and a
# "lucky" or "no_edge" kept before then may be either of them until the wallet is scored again. uselayer 0.11 adds
# "parlays" (skill unmeasured, not absent) and "edge_losing" (an edge, kept from sharp by a losing window), both
# once "too_few" or "no_edge".
# what kept a score: the Worth following check, a trader page, the import, the background evaluation
VIA = ("check", "page", "import", "background")
# what ``due`` hands out, in that order: a recommended trader people are shown (``presented``), any other
# recommended trader, a wallet never looked at, the rest (``new_first`` puts new ones before other recommended)
KINDS = ("presented", "recommended", "new", "recheck")
SAMPLE_KEEP = 30  # checked bets kept with a score, as the Worth following check keeps them

SCORE_DAYS = 30  # the window a score reads, as the Worth following check and a trader page score
EVIDENCE_DAYS = 90  # how far back a quick check counts buys, so a selective trader's record is kept too
QUICK_TRADES = 500  # the latest trades a quick check reads: one read (Polymarket's most a page), so a few markets traded
#                     heavily still reach back past the 30 days instead of cutting the read short

# When a wallet is looked at again, by why it isn't recommended (``next_check_at``).
# Freshness, one rule for every place a score is shown (Explore, a trader page, the Recommended list): a recommended
# trader people are shown (``presented``: on the Recommended list, or followed) is scored again every
# PRESENTED_RESCORE_S, as the check scores its own; any other recommended trader the directory found, once a day.
# A score counts as fresh for twice its cadence (``fresh_for``): one missed rescore never drops a trader.
PRESENTED_RESCORE_S = 6 * 3600
BACKGROUND_RESCORE_S = 24 * 3600
RESCORE_S = PRESENTED_RESCORE_S  # (the name phase 3 gave the 6 hours)
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
CREATE TABLE IF NOT EXISTS sweep_parts (part TEXT PRIMARY KEY, done_at REAL NOT NULL);
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
CREATE TABLE IF NOT EXISTS daily (day TEXT NOT NULL, metric TEXT NOT NULL, key TEXT NOT NULL, n REAL NOT NULL,
  PRIMARY KEY (day, metric, key));
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
# Explore: what a list row shows and filters on, and search by name.
_EXPLORE_COLUMNS = (("market_usd", "REAL"), ("evidence_cut", "INTEGER"), ("typical_usd", "REAL"),
                    ("listed", "INTEGER NOT NULL DEFAULT 0"))  # fmt: skip
PUBLIC_SOURCES = ("leaderboard", "market", "feed", "check")
_EXPLORE_INDEXES = """
CREATE INDEX IF NOT EXISTS wallets_by_name ON wallets (name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS wallets_by_reason ON wallets (status, not_recommended);
"""

_RECOMMENDED_SQL = ", ".join(f"'{s}'" for s in RECOMMENDED)
# Which of KINDS a due wallet is: a recommended trader (scored, or failed since), one never looked at, any other.
# A trader people are shown (``presented``) is that whatever tier their stored score says: the Recommended list can
# serve an older-format score re-tiered to a recommended tier the stored segment doesn't have (seen 2026-10-14: a
# format-7 "no_edge" served as Rising, its score going stale because it was only rechecked every 2 days).
_KIND = f"""CASE WHEN status IN ('scored', 'failed') AND wallet IN (SELECT wallet FROM temp.presented_now) THEN 0
  WHEN segment IN ({_RECOMMENDED_SQL}) AND status IN ('scored', 'failed') THEN 1
  WHEN status = 'found' THEN 2 ELSE 3 END"""
# A trader people are shown is due once its score is PRESENTED_RESCORE_S old, whatever next_check says.
_DUE = f"""(next_check <= :now OR (status = 'scored' AND scored_at <= :now - {PRESENTED_RESCORE_S}
  AND wallet IN (SELECT wallet FROM temp.presented_now)))"""


def fresh_for(presented: bool) -> float:
    """How long a recommended trader's score counts as fresh: twice its cadence, so one missed rescore never drops
    them (12 hours for one people are shown, 48 for any other)."""
    return 2 * (PRESENTED_RESCORE_S if presented else BACKGROUND_RESCORE_S)


PASSED_FRESH_S = fresh_for(False)  # Explore's "Passed our checks": a recommended tier from the last 48 hours
# On the boards the Worth following check reads to call a wallet "on the leaderboard" (uselayer's find_candidates,
# passed to its score as on_leaderboard): the monthly profit board's top 1,000; the weekly and all-time profit and
# monthly volume boards' top 50; each category's monthly profit top 50.
CHECK_CATEGORIES = ("SPORTS", "CRYPTO", "POLITICS", "ECONOMICS", "CULTURE", "WEATHER", "ESPORTS", "TECH")
# (order, period, category, places): the same boards, to read directly
CHECK_BOARDS = (("PNL", "MONTH", None, 1000), ("PNL", "WEEK", None, 50), ("PNL", "ALL", None, 50),
                ("VOL", "MONTH", None, 50), *(("PNL", "MONTH", c, 50) for c in CHECK_CATEGORIES))  # fmt: skip
_CHECK_BOARDS = ("PNL WEEK OVERALL", "PNL ALL OVERALL", "VOL MONTH OVERALL", *(f"PNL MONTH {c}" for c in CHECK_CATEGORIES))
# The unary + keeps SQLite off sightings_by_source (source, last_seen) for this check, so it looks up the wallet's own
# sightings by the primary key. Without it, production's SQLite (3.46, no ANALYZE) walked every recent leaderboard
# sighting (~765K) for each wallet in the queue: 13 s a round under the directory's lock (0.7 s with it, 2026-10-16).
_LISTED = f"""EXISTS (SELECT 1 FROM sightings s WHERE s.wallet = q.wallet AND s.source = 'leaderboard'
  AND +s.last_seen >= ? AND ((s.place = 'PNL MONTH OVERALL' AND s.rank <= 1000)
  OR (s.place IN ({", ".join(f"'{b}'" for b in _CHECK_BOARDS)}) AND s.rank <= 50)))"""


def _typical_usd(result: bytes) -> float | None:
    """A kept score's usual trade (the SDK's ``typical_usd``), from its stored result; None when it has none or
    the result can't be read."""
    try:
        usd = json.loads(zlib.decompress(result)).get("typical_usd")
    except (zlib.error, ValueError, TypeError, AttributeError):
        return None
    return float(usd) if isinstance(usd, int | float) and not isinstance(usd, bool) and usd >= 0 else None


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
    (its latest score's ``segment``): a recommended trader in BACKGROUND_RESCORE_S (one people are shown sooner:
    ``due``'s ``presented``); one short of evidence or not trading
    lately each day; dormant ones and market makers weekly; a failed try later each time it fails again in a row."""
    if status == "failed":
        return at + RETRY_S[min(max(failures, 1), len(RETRY_S)) - 1]
    if status == "found":
        return at
    if reason in ("not_enough_recent_evidence", "not_trading_lately"):
        return at + RECHECK_EVIDENCE_S
    if reason in ("dormant", "no_view"):
        return at + RECHECK_QUIET_S
    return at + (BACKGROUND_RESCORE_S if segment in RECOMMENDED else RECHECK_SCORED_S)


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
    market_usd: float | None = None  # the middle of the dollars they bought a market with, over EVIDENCE_DAYS
    cut: bool = False  # the read stopped before reaching back EVIDENCE_DAYS: the counts are "at least"


def quick_look(trades: list[tuple[Any, ...]], now: float, read_all: bool,
               scored_at: float | None = None) -> QuickLook:  # fmt: skip
    """From a wallet's latest trades (``(time, bought, market[, dollars])``, newest first, as one read gives them), whether a
    score is worth its reads now. Not when there's no buy in the last EVIDENCE_DAYS (``dormant``), or fewer than
    RISING_BETS markets bought in the last SCORE_DAYS (``not_enough_recent_evidence``: a score needs that many bets
    to say anything about skill), or no buy in the last ACTIVE_DAYS and none since their last score
    (``not_trading_lately``: a new score would say the same). Both thresholds are the SDK's own: they decide whether
    there's enough recent evidence to recommend someone, and the wallet is kept and looked at again either way.
    When the read was cut short (``read_all`` False), only the time it covers counts."""
    covered = float("-inf") if read_all or not trades else min(t[0] for t in trades)
    week, window, evidence = (now - d * 86400 for d in (ACTIVE_DAYS, SCORE_DAYS, EVIDENCE_DAYS))
    bought = [(t[0], t[2]) for t in trades if t[1]]
    per_market: dict[str, float] = {}
    for t in trades:
        if t[1] and t[0] >= evidence and len(t) > 3 and t[3] is not None:
            per_market[t[2]] = per_market.get(t[2], 0.0) + float(t[3])
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
    usds = sorted(per_market.values())
    middle = (usds[len(usds) // 2] if len(usds) % 2 else (usds[len(usds) // 2 - 1] + usds[len(usds) // 2]) / 2) if usds else None
    return QuickLook(reason is None, reason, buys_30d, buys_90d, last_buy, max((t[0] for t in trades), default=None),
                     None if middle is None else round(middle, 2), covered > evidence)  # fmt: skip


CHECK_GROUPS = ("passed", "limited", "checked", "unchecked")  # what our checks found: each listed wallet is in one
WHALE_USD = 1000  # Explore's Whales: a usual bet (a trade, or a quick look's market) of this much or more
EXPLORE_GROUPS = (*CHECK_GROUPS, "whales")  # Whales cuts across the others: a fact about size, never a verdict
# Listed only when found somewhere public (PUBLIC_SOURCES: the sweep's leaderboards and markets, the Feed's big
# trades, the check's own reads), which ``listed`` keeps. A wallet known only because someone searched for or
# followed it stays out of Explore and its search.
_PUBLIC = "w.listed = 1"
_LIMITED = ("((w.status = 'checked' AND w.not_recommended = 'not_enough_recent_evidence') "
            "OR (w.status != 'checked' AND w.scored_at IS NOT NULL AND w.segment = 'too_few'))")  # fmt: skip
_TIERED = f"(w.status != 'checked' AND w.scored_at IS NOT NULL AND w.segment IN ({_RECOMMENDED_SQL}))"
_GROUPS = {
    # too few bets to judge: a quick check's count (``evidence`` "quick"), or a score that found too few (``scored``)
    "limited": _LIMITED,
    # looked at, with something to say, short of a recommended tier: a score's verdict, or a quick check's (dormant,
    # paused). Recommended tiers are the Recommended tab's: Explore's search finds them, its lists leave them out.
    "checked": f"((w.scored_at IS NOT NULL OR w.status = 'checked') AND NOT {_LIMITED} AND NOT {_TIERED})",
    # a recommended tier from the last PASSED_FRESH_S ("Passed our checks"; ``:passed_after``), and any (search)
    "passed": f"({_TIERED} AND w.scored_at >= {{passed_after}})",
    "recommended": _TIERED,
    # found, never looked at: nothing about them has been measured
    "unchecked": "(w.scored_at IS NULL AND w.status IN ('found', 'failed'))",
    # big bettors, whatever our checks found (each row keeps its own group): their size, not their skill
    "whales": f"(COALESCE(w.typical_usd, w.market_usd) >= {WHALE_USD})",
}
# Facts to sort by, never a judgement: unknowns last.
SORTS = {
    "recent": "w.last_active DESC NULLS LAST",
    "size": "COALESCE(w.typical_usd, w.market_usd) DESC NULLS LAST",
    "new": "w.first_seen DESC",
    # depth of record: markets bought in the last 90 days, as the last quick check read them (a count, not a grade)
    "record": "w.buys_90d DESC NULLS LAST, w.buys_30d DESC NULLS LAST",
}
# Explore's "what we checked" filter: ``full``, wallets whose latest look is a full check (a score), not a quick look
EVIDENCE = {"full": "(w.scored_at IS NOT NULL AND w.status != 'checked')"}
_WALLETISH = re.compile(r"0x[0-9a-f]{2,40}")
# A wallet's category: its score's (what they trade), else where it was first seen; ``category_from`` says which
# ("score", "seen"), so a page names a "seen" one as where they were found, never as what they trade.
_CATEGORY_FROM = "CASE WHEN w.category IS NOT NULL THEN 'score' WHEN w.seen_category IS NOT NULL THEN 'seen' END"
_ROW = f"w.wallet, w.name, COALESCE(w.category, w.seen_category) AS category, {_CATEGORY_FROM} AS category_from, " + """
  w.last_active, w.status, w.not_recommended, w.segment, w.scored_at, w.tried_at, w.buys_30d, w.buys_90d, w.last_buy, w.market_usd, w.evidence_cut, w.typical_usd,
  w.first_seen, w.first_source, sc.bets AS bets_checked, sc.format AS score_format, sc.skill, sc.copyable,
  sc.edge, sc.edge_z, sc.copy AS copy_mean, sc.copy_low, sc.trading_pnl_window, sc.window_end,
  (SELECT b.place || '|' || b.rank FROM sightings b WHERE b.wallet = w.wallet AND b.source = 'leaderboard'
    AND b.rank IS NOT NULL ORDER BY b.rank LIMIT 1) AS best_board,
  (SELECT m.example_title FROM sightings m WHERE m.wallet = w.wallet AND m.source IN ('market', 'feed')
    AND m.example_title IS NOT NULL ORDER BY m.last_seen DESC LIMIT 1) AS seen_in,
  """ + _PUBLIC + " AS public"


def _group(g: str, now: float) -> str:
    """A group's SQL as of ``now`` (the passed group's cutoff is a number worked out here, never anyone's input)."""
    return _GROUPS[g].replace("{passed_after}", f"{now - PASSED_FRESH_S:.3f}")


def _like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _explore(r: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """A wallet as Explore shows it. ``group`` and ``evidence`` (``quick``: a quick check's counts only; ``scored``:
    a score; None: never looked at). A figure that wasn't read stays None (unknown), never 0. A recommended tier is
    ``passed`` while its score is fresh (PASSED_FRESH_S), ``recommended`` once it isn't (found by a search only)."""
    now = time.time() if now is None else now
    limited = (r["status"] == "checked" and r["not_recommended"] == "not_enough_recent_evidence") or (
        r["status"] != "checked" and r["scored_at"] is not None and r["segment"] == "too_few")  # fmt: skip
    looked = r["scored_at"] is not None or r["status"] == "checked"
    tiered = r["status"] != "checked" and r["scored_at"] is not None and r["segment"] in RECOMMENDED
    board, _, rank = (r.pop("best_board") or "").partition("|")
    return r | {
        "group": "limited" if limited else ("passed" if r["scored_at"] >= now - PASSED_FRESH_S else "recommended")
        if tiered else "checked" if looked else "unchecked",
        "evidence": None if not looked else "quick" if r["status"] == "checked" else "scored",
        "label": reason_label(r["not_recommended"], r["buys_30d"]) if r["status"] == "checked" else None,
        "board": {"place": board, "rank": int(rank)} if board else None,
        "evidence_cut": None if r["evidence_cut"] is None else bool(r["evidence_cut"]),
        "public": bool(r["public"]),
    }


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
        for col, kind in _PHASE_2_COLUMNS + _PHASE_3_COLUMNS + _EXPLORE_COLUMNS:
            if col not in have:
                con.execute(f"ALTER TABLE wallets ADD COLUMN {col} {kind}")
        con.executescript(_PHASE_2_INDEXES + _PHASE_3_INDEXES + _EXPLORE_INDEXES)
        if "listed" not in have:  # once: which wallets were already found somewhere public
            con.execute(
                "UPDATE wallets SET listed = 1 WHERE wallet IN (SELECT DISTINCT wallet FROM sightings WHERE source IN "
                f"({', '.join(repr(x) for x in PUBLIC_SOURCES)}))"
            )
        # Explore's "usual trade" for wallets scored before it was kept on the wallet: from their latest kept score
        # (its stored result), never a new read and never a new score. A score without one leaves it unknown.
        missing = con.execute(
            "SELECT w.wallet, s.result FROM wallets w JOIN scores s ON s.wallet = w.wallet WHERE w.typical_usd IS NULL"
        ).fetchall()
        sizes = [(usd, w) for w, blob in missing if (usd := _typical_usd(blob)) is not None]
        if sizes:
            con.executemany("UPDATE wallets SET typical_usd = ? WHERE wallet = ?", sizes)
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
        con.executemany("UPDATE wallets SET listed = 1 WHERE wallet = ? AND listed = 0",
                        [(w,) for w in {s.wallet.lower() for s in rows if s.source in PUBLIC_SOURCES}])  # fmt: skip
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
                    "tried_at = MAX(COALESCE(tried_at, ?), ?), segment = ?, category = ?, name = COALESCE(name, ?), "
                    "typical_usd = ? WHERE wallet = ?",
                    (now, now, now, now, next_check_at("scored", reason_for(s.get("segment")), s.get("segment"), now),
                     now, reason_for(s.get("segment")), now, now, now, s.get("segment"), main_category(s),
                     _username(s), s.get("typical_usd"), w),
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
                "buys_30d = ?, buys_90d = ?, last_buy = MAX(COALESCE(?, last_buy), COALESCE(last_buy, ?)), "
                "market_usd = ?, evidence_cut = ? WHERE wallet = ?",
                (look.last_trade, look.last_trade, look.buys_30d, look.buys_90d, look.last_buy, look.last_buy,
                 look.market_usd, int(look.cut), w),
            )
            if not look.score:
                segment = con.execute("SELECT segment FROM wallets WHERE wallet = ?", (w,)).fetchone()[0]
                con.execute(
                    "UPDATE wallets SET status = 'checked', not_recommended = ?, tried_at = ?, score_error = NULL, "
                    "failures = 0, next_check = ? WHERE wallet = ?",
                    (look.reason, now, next_check_at("checked", look.reason, segment, now), w),
                )

    @staticmethod
    def _presented(con: sqlite3.Connection, presented: Iterable[str]) -> None:
        con.execute("CREATE TEMP TABLE IF NOT EXISTS presented_now (wallet TEXT PRIMARY KEY)")
        con.execute("DELETE FROM temp.presented_now")
        con.executemany("INSERT OR IGNORE INTO temp.presented_now VALUES (?)", [(w.lower(),) for w in presented])

    def due(self, limit: int, now: float | None = None, *, presented: Iterable[str] = (),
            new_first: bool = False) -> list[dict[str, Any]]:  # fmt: skip
        """Up to ``limit`` wallets due a look, in the order to take them: recommended traders people are shown
        (``presented``: due once their score is PRESENTED_RESCORE_S old), any other recommended trader (once a day),
        wallets never looked at (newest found first), then the rest (as each comes due); most overdue first.
        ``new_first``: wallets never looked at before other recommended traders (the evaluation's reserve, so the
        rest of the directory keeps being checked). Within each, the categories take turns (a wallet's from its
        score, else where it was first seen), so no category waits behind a bigger one. Each with its ``kind`` (one
        of KINDS), ``status``, ``scored_at``, ``category``, ``name``, and ``on_leaderboard`` as the Worth following
        check would pass it."""
        now = time.time() if now is None else now
        rank = "CASE kind WHEN 0 THEN 0 WHEN 2 THEN 1 WHEN 1 THEN 2 ELSE 3 END" if new_first else "kind"
        with self._db() as con:
            self._presented(con, presented)
            rows = con.execute(
                f"""SELECT wallet, kind, cat, status, scored_at, name, {_LISTED.replace("?", ":listed_after")} FROM (
                  SELECT *, ROW_NUMBER() OVER (PARTITION BY kind, cat
                    ORDER BY CASE WHEN kind = 2 THEN -first_seen ELSE next_check END, wallet) AS turn
                  FROM (SELECT wallet, status, scored_at, name, first_seen, next_check, {_KIND} AS kind,
                    COALESCE(category, seen_category, 'Other') AS cat FROM wallets WHERE {_DUE})) q
                ORDER BY {rank}, turn, cat LIMIT :limit""",
                {"listed_after": now - LISTED_FRESH_S, "now": now, "limit": limit},
            ).fetchall()
        return [{"wallet": w, "kind": KINDS[k], "category": c, "status": st, "scored_at": at, "name": n,
                 "on_leaderboard": bool(b)} for w, k, c, st, at, n, b in rows]  # fmt: skip

    def boards_seen_at(self) -> float | None:
        """When a sweep last saw the monthly profit board (the one on_leaderboard leans on most), or None."""
        with self._db() as con:
            return con.execute(  # type: ignore[no-any-return]
                "SELECT MAX(last_seen) FROM sightings WHERE source = 'leaderboard' AND place = 'PNL MONTH OVERALL'"
            ).fetchone()[0]

    def due_counts(self, now: float | None = None, presented: Iterable[str] = ()) -> dict[str, int]:
        """How many wallets are due a look now, by kind."""
        now = time.time() if now is None else now
        with self._db() as con:
            self._presented(con, presented)
            got = dict(con.execute(f"SELECT {_KIND}, COUNT(*) FROM wallets WHERE {_DUE} GROUP BY 1", {"now": now}))
        return {k: got.get(i, 0) for i, k in enumerate(KINDS)}

    @staticmethod
    def _has(con: sqlite3.Connection, wallet: str) -> bool:
        return con.execute("SELECT 1 FROM wallets WHERE wallet = ?", (wallet.lower(),)).fetchone() is not None

    def score(self, wallet: str) -> dict[str, Any] | None:
        """A wallet's latest score as it was kept, with ``scored_at`` and ``via``; None if it was never scored."""
        with self._db() as con:
            r = con.execute("SELECT result, scored_at, via FROM scores WHERE wallet = ?", (wallet.lower(),)).fetchone()
        return None if r is None else json.loads(zlib.decompress(r[0])) | {"scored_at": r[1], "via": r[2]}

    def latest_scores(self, since: float) -> list[dict[str, Any]]:
        """Every wallet whose latest score was worked out since ``since``, without reading any score whole: its
        ``segment``, ``edge_z``, ``format``, ``scored_at`` and ``via``, the category most of its checked bets were in
        (``main``: the SDK's first, most bets then name) and the categories it's ``strong`` in. What discovery
        picks from (phase 5, ``app.discovery_result``)."""
        with self._db() as con:
            rows = con.execute(
                "SELECT wallet, segment, edge_z, format, scored_at, via FROM scores WHERE scored_at >= ?", (since,)
            ).fetchall()
            cats: dict[str, list[tuple[str, int]]] = {}
            for w, c, strong in con.execute(
                "SELECT c.wallet, c.category, c.strong FROM score_categories c JOIN scores s ON s.wallet = c.wallet "
                "WHERE s.scored_at >= ? ORDER BY c.wallet, c.bets DESC, c.category", (since,)
            ):  # fmt: skip
                cats.setdefault(w, []).append((c, strong))
        return [{"wallet": w, "segment": seg, "edge_z": z, "format": f, "scored_at": at, "via": via,
                 "main": cats[w][0][0] if cats.get(w) else None, "strong": [c for c, s in cats.get(w, []) if s]}
                for w, seg, z, f, at, via in rows]  # fmt: skip

    def scores_of(self, wallets: Iterable[str]) -> dict[str, dict[str, Any]]:
        """The latest score of each of these wallets, as ``score`` gives it, by wallet. A score without the trader's
        Polymarket username takes the one the directory has seen them under."""
        ws = sorted({w.lower() for w in wallets})
        out: dict[str, dict[str, Any]] = {}
        with self._db() as con:
            for i in range(0, len(ws), 500):  # SQLite's limit on ? in one statement
                part = ws[i : i + 500]
                for w, result, at, via, name in con.execute(
                    "SELECT s.wallet, s.result, s.scored_at, s.via, w.name FROM scores s JOIN wallets w "
                    f"ON w.wallet = s.wallet WHERE s.wallet IN ({', '.join('?' * len(part))})", part
                ):  # fmt: skip
                    s = json.loads(zlib.decompress(result)) | {"scored_at": at, "via": via}
                    if name and not _username(s):
                        s["name"] = name
                    out[w] = s
        return out

    def on_boards(self, wallets: Iterable[str]) -> set[str]:
        """Which of these wallets the directory has ever seen on any of Polymarket's leaderboards (a sweep's)."""
        ws = sorted({w.lower() for w in wallets})
        out: set[str] = set()
        with self._db() as con:
            for i in range(0, len(ws), 500):
                part = ws[i : i + 500]
                out |= {r[0] for r in con.execute(
                    f"SELECT DISTINCT wallet FROM sightings WHERE source = 'leaderboard' AND wallet IN "
                    f"({', '.join('?' * len(part))})", part
                )}  # fmt: skip
        return out

    def failed_since(self, since: float) -> int:
        """How many wallets' last try since ``since`` failed."""
        with self._db() as con:
            return con.execute(  # type: ignore[no-any-return]
                "SELECT COUNT(*) FROM wallets WHERE status = 'failed' AND tried_at >= ?", (since,)
            ).fetchone()[0]

    def scored_apart(self, wallets: Iterable[str], at: float, within: float) -> dict[str, dict[str, Any]]:
        """For each wallet, the headline figures of its score worked out apart from the Worth following check (on
        a trader page, or by the background evaluation) closest in time to ``at``, within ``within`` seconds either
        way, with ``via``: what validation compares the check's own score with. A wallet without one is left out."""
        ws = sorted({w.lower() for w in wallets})
        out: dict[str, dict[str, Any]] = {}
        with self._db() as con:
            con.row_factory = sqlite3.Row
            for i in range(0, len(ws), 500):
                part = ws[i : i + 500]
                for r in con.execute(
                    f"SELECT * FROM score_history WHERE wallet IN ({', '.join('?' * len(part))}) "
                    "AND via IN ('page', 'background') AND scored_at BETWEEN ? AND ?",
                    (*part, at - within, at + within),
                ):  # fmt: skip
                    best = out.get(r["wallet"])
                    if best is None or abs(r["scored_at"] - at) < abs(best["scored_at"] - at):
                        out[r["wallet"]] = dict(r)
        return out

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

    def part_done(self, part: str, at: float | None = None) -> None:
        """One part of a sweep read to its end, with everything it found kept: a leaderboard (``"PNL MONTH
        POLITICS"``) or ``"markets"``. A sweep after a restart skips parts done since the last cutoff."""
        with self._db() as con:
            con.execute("INSERT OR REPLACE INTO sweep_parts VALUES (?, ?)", (part, time.time() if at is None else at))

    def backfill_parts(self, order: list[str], since: float) -> int:
        """For a directory from before ``sweep_parts`` (a sweep was cut off by a deploy before it kept its progress):
        a sweep reads the leaderboards in ``order``, so every board before the last one with sightings since
        ``since`` was read to its end. Marks those done, as of their last sighting; the last one, maybe cut off
        partway, is read again. Only when no part was ever marked. Returns how many it marked."""
        with self._db() as con:
            if con.execute("SELECT 1 FROM sweep_parts LIMIT 1").fetchone():
                return 0
            seen = dict(con.execute(
                "SELECT place, MAX(last_seen) FROM sightings WHERE source = 'leaderboard' AND last_seen >= ? "
                "GROUP BY place", (since,)
            ))  # fmt: skip
            reached = max((i for i, b in enumerate(order) if b in seen), default=-1)
            done = [(b, seen[b]) for b in order[:reached] if b in seen]
            con.executemany("INSERT OR REPLACE INTO sweep_parts VALUES (?, ?)", done)
        return len(done)

    def parts_done_since(self, since: float) -> set[str]:
        with self._db() as con:
            return {r[0] for r in con.execute("SELECT part FROM sweep_parts WHERE done_at >= ?", (since,))}

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

    def browse(self, group: str = "limited", *, q: str = "", category: str | None = None,
               active_days: int | None = None, min_usd: float | None = None, board: str | None = None,
               makers: bool = False, evidence: str | None = None, sort: str = "recent", limit: int = 50,
               offset: int = 0, search_in: tuple[str, ...] = ("limited", "checked", "recommended"),
               now: float | None = None) -> dict[str, Any]:  # fmt: skip
        """A page of Explore: the wallets in ``group`` (one of EXPLORE_GROUPS), or with ``q``, the wallets in the
        ``search_in`` groups whose name has it in it (or whose wallet starts with it), filtered and sorted by facts
        only (SORTS), never by a ranking of how good they are. Only wallets found in public places (``_PUBLIC``):
        one known here only because someone searched for or followed it is never listed or found. Returns ``rows``
        (``explore_row``) and whether there are ``more``."""
        now = time.time() if now is None else now
        where, args = [_PUBLIC], []
        q = q.strip().lower()
        if q:
            if _WALLETISH.fullmatch(q):  # a wallet's start: a range of the key, so the index finds it
                where.append("w.wallet >= ? AND w.wallet < ?")
                args += [q, q + "g"]  # wallets are lowercase hex: "g" sorts after every one that starts with q
            else:
                where.append("w.name LIKE ? ESCAPE '\\'")
                args.append("%" + _like(q) + "%")
            where.append("(" + " OR ".join(_group(g, now) for g in search_in) + ")")
        else:
            where.append(_group(group, now))
        if category:
            where.append("COALESCE(w.category, w.seen_category) = ?")
            args.append(category)
        if active_days:
            where.append("w.last_active >= ?")
            args.append(now - active_days * 86400)
        if min_usd:
            where.append("COALESCE(w.typical_usd, w.market_usd) >= ?")
            args.append(min_usd)
        if board in ("on", "off"):
            where.append(f"{'' if board == 'on' else 'NOT '}EXISTS (SELECT 1 FROM sightings b WHERE b.wallet = w.wallet "
                         "AND b.source = 'leaderboard')")  # fmt: skip
        if evidence in EVIDENCE:
            where.append(EVIDENCE[evidence])
        if not makers:
            where.append("COALESCE(w.segment, '') != 'no_view'")
        order = SORTS.get(sort, SORTS["recent"])
        with self._db() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                f"SELECT {_ROW} FROM wallets w LEFT JOIN scores sc ON sc.wallet = w.wallet "
                f"WHERE {' AND '.join(where)} ORDER BY {order}, w.wallet LIMIT ? OFFSET ?",
                (*args, limit + 1, offset),
            ).fetchall()
        return {"rows": [_explore(dict(r), now) for r in rows[:limit]], "more": len(rows) > limit}

    def explore_counts(self, now: float | None = None) -> dict[str, int]:
        """How many wallets each Explore group lists (public ones only). Every whale is in one of the other groups
        too; they're counted as listed, market makers left out (``browse``'s ``makers``)."""
        now = time.time() if now is None else now
        case = " ".join(f"WHEN {_group(g, now)} THEN '{g}'" for g in CHECK_GROUPS)
        with self._db() as con:
            got = dict(con.execute(f"SELECT CASE {case} END, COUNT(*) FROM wallets w WHERE {_PUBLIC} GROUP BY 1"))
            got["whales"] = con.execute(f"SELECT COUNT(*) FROM wallets w WHERE {_PUBLIC} AND {_group('whales', now)} "
                                        "AND COALESCE(w.segment, '') != 'no_view'").fetchone()[0]  # fmt: skip
        return {g: got.get(g, 0) for g in EXPLORE_GROUPS}

    def explore_row(self, wallet: str) -> dict[str, Any] | None:
        """One wallet as an Explore row (for its page: the evidence, where it was found), or None if unknown."""
        with self._db() as con:
            con.row_factory = sqlite3.Row
            r = con.execute(
                f"SELECT {_ROW} FROM wallets w LEFT JOIN scores sc ON sc.wallet = w.wallet WHERE w.wallet = ?",
                (wallet.lower(),),
            ).fetchone()
        return None if r is None else _explore(dict(r))  # as of now

    def wallet(self, wallet: str) -> dict[str, Any] | None:
        with self._db() as con:
            con.row_factory = sqlite3.Row
            r = con.execute("SELECT * FROM wallets WHERE wallet = ?", (wallet.lower(),)).fetchone()
        return None if r is None else dict(r)

    # ---- phase 6: monitoring (the owner's /api/monitoring: counts only, never a wallet) ----

    def tally(self, rows: Iterable[tuple[str, str, str, float]]) -> None:
        """Adds ``(day, metric, key, n)`` counts to ``daily`` (signals/monitoring.py): summed, except a metric
        ending in ``_max``, which keeps the day's largest."""
        rows = list(rows)
        if not rows:
            return
        with self._db() as con:
            con.executemany(
                "INSERT INTO daily VALUES (?, ?, ?, ?) ON CONFLICT (day, metric, key) DO UPDATE SET "
                "n = CASE WHEN metric LIKE '%\\_max' ESCAPE '\\' THEN MAX(n, excluded.n) ELSE n + excluded.n END",
                rows,
            )

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """A read-only connection outside the lock (WAL lets it read while the evaluation writes), so the seconds of
        counting the owner's report takes never hold up a score or a page's sightings."""
        if not self._upgraded or not self.path.exists():
            with self._db():
                pass
        con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            con.create_function("family", 1, _family, deterministic=True)
            yield con
        finally:
            con.close()

    def monitoring(self, presented: Iterable[str] = (), days: int = 14, now: float | None = None) -> dict[str, Any]:
        """Everything the directory can say about how discovery is going, over the last ``days`` UTC days (today
        included): the funnel (FUNNEL) overall, by category, by where wallets were found (and first found, and
        found only there; by leaderboard and best rank); each day's new wallets, scores and ``daily`` tallies; how
        fresh scores are, what's due and how long the queue of wallets never looked at is; why tries fail; the
        sweeps. ``presented``: the traders people are shown, whose scores are kept fresher (``fresh_for``)."""
        now = time.time() if now is None else now
        first_day = _day(now - (max(1, days) - 1) * 86400)
        shown = {w.lower() for w in presented}
        args = {"fresh": now - PASSED_FRESH_S, "now": now, "first_day": first_day, "presented": json.dumps(sorted(shown)),
                "since": datetime.fromisoformat(first_day + "T00:00:00+00:00").timestamp()}  # fmt: skip
        kind = _KIND.replace("SELECT wallet FROM temp.presented_now", "SELECT value FROM json_each(:presented)")
        due = _DUE.replace("SELECT wallet FROM temp.presented_now", "SELECT value FROM json_each(:presented)")
        with self._read() as con:

            def funnels(sql: str) -> dict[str, dict[str, Any]]:
                return {k or "None": _funnel(r) for k, *r in con.execute(sql, args)}

            total = _funnel(con.execute(f"SELECT {_FUNNEL} FROM wallets w", args).fetchone())
            by_cat = funnels(f"SELECT {_CAT}, {_FUNNEL} FROM wallets w GROUP BY 1 ORDER BY 2 DESC")
            strong = dict(con.execute(
                f"SELECT c.category, COUNT(*) FROM score_categories c JOIN wallets w ON w.wallet = c.wallet "
                f"WHERE c.strong = 1 AND {_QUALIFIED} GROUP BY 1", args
            ))  # fmt: skip
            seen = funnels(f"SELECT s.source, {_FUNNEL} FROM (SELECT DISTINCT wallet, source FROM sightings) s "
                           "JOIN wallets w ON w.wallet = s.wallet GROUP BY 1")  # fmt: skip
            only = funnels(f"SELECT s.source, {_FUNNEL} FROM (SELECT wallet, MIN(source) AS source FROM sightings "
                           "GROUP BY wallet HAVING COUNT(DISTINCT source) = 1) s JOIN wallets w ON w.wallet = s.wallet "
                           "GROUP BY 1")  # fmt: skip
            first = funnels(f"SELECT w.first_source, {_FUNNEL} FROM wallets w GROUP BY 1")
            boards = funnels(f"SELECT b.board, {_FUNNEL} FROM (SELECT DISTINCT wallet, family(place) AS board "
                             "FROM sightings WHERE source = 'leaderboard') b JOIN wallets w ON w.wallet = b.wallet "
                             "GROUP BY 1 ORDER BY 2 DESC")  # fmt: skip
            bands = funnels(f"SELECT CASE WHEN r.best IS NULL THEN 'no_board' WHEN r.best <= 100 THEN 'top_100' "
                            f"WHEN r.best <= 1000 THEN '101_1000' ELSE 'beyond_1000' END, {_FUNNEL} FROM wallets w "
                            "LEFT JOIN (SELECT wallet, MIN(rank) AS best FROM sightings WHERE source = 'leaderboard' "
                            "GROUP BY wallet) r ON r.wallet = w.wallet GROUP BY 1")  # fmt: skip
            new = con.execute("SELECT date(first_seen, 'unixepoch'), first_source, COUNT(*) FROM wallets "
                              "WHERE first_seen >= :since GROUP BY 1, 2", args).fetchall()  # fmt: skip
            scored = con.execute("SELECT date(scored_at, 'unixepoch'), via, COUNT(*) FROM score_history "
                                 "WHERE scored_at >= :since GROUP BY 1, 2", args).fetchall()  # fmt: skip
            scored_wallets = dict(con.execute("SELECT date(scored_at, 'unixepoch'), COUNT(DISTINCT wallet) "
                                              "FROM score_history WHERE scored_at >= :since GROUP BY 1", args))  # fmt: skip
            tallies = con.execute("SELECT day, metric, key, n FROM daily WHERE day >= :first_day", args).fetchall()
            counted_from = con.execute("SELECT MIN(day) FROM daily").fetchone()[0]
            tiered = con.execute(f"SELECT w.wallet, w.scored_at FROM wallets w WHERE {_TIERED}").fetchall()
            score_ages = dict(con.execute(f"SELECT {_age('w.scored_at')}, COUNT(*) FROM wallets w "
                                          "WHERE w.scored_at IS NOT NULL GROUP BY 1", args))  # fmt: skip
            quick_ages = dict(con.execute(f"SELECT {_age('w.tried_at')}, COUNT(*) FROM wallets w "
                                          "WHERE w.status = 'checked' GROUP BY 1", args))  # fmt: skip
            backlog = con.execute(f"SELECT {kind}, COUNT(*), MIN(next_check) FROM wallets WHERE {due} GROUP BY 1",
                                  args).fetchall()  # fmt: skip
            unlooked, oldest_found = con.execute("SELECT COUNT(*), MIN(first_seen) FROM wallets WHERE status = 'found'").fetchone()
            errors = [e for (e,) in con.execute("SELECT score_error FROM wallets WHERE status = 'failed'")]
            failing = con.execute("SELECT COUNT(*), SUM(tried_at >= :now - 86400), SUM(failures >= 3) FROM wallets "
                                  "WHERE status = 'failed'", args).fetchone()  # fmt: skip
            sweeps = con.execute("SELECT started, finished, sightings, new, error FROM sweeps WHERE started >= :since "
                                 "ORDER BY started DESC", args).fetchall()  # fmt: skip
            # each score of a wallet ever scored a recommended tier and scored in the window, with the score before it
            # (LAG over all of its history, so the window's first score still has the one before) and whether any
            # earlier one was; wallets with no score in the window are left out, so the cost follows the window
            pairs = con.execute(
                f"""SELECT date(scored_at, 'unixepoch'), segment, via, prev, prev_via, was FROM (
                  SELECT scored_at, segment, via,
                    LAG(segment) OVER w AS prev, LAG(via) OVER w AS prev_via,
                    MAX(segment IN ({_RECOMMENDED_SQL})) OVER (w ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS was
                  FROM score_history WHERE wallet IN (SELECT wallet FROM score_history WHERE segment IN ({_RECOMMENDED_SQL})
                    INTERSECT SELECT wallet FROM score_history WHERE scored_at >= :since)
                  WINDOW w AS (PARTITION BY wallet ORDER BY scored_at))
                WHERE scored_at >= :since""",
                args,
            ).fetchall()  # fmt: skip
            ever = con.execute(f"SELECT COUNT(DISTINCT wallet) FROM score_history WHERE segment IN ({_RECOMMENDED_SQL})").fetchone()[0]
            recommended_now = con.execute(f"SELECT COUNT(*) FROM wallets w WHERE {_TIERED}").fetchone()[0]
        for f in by_cat.values():
            f["strong_in"] = 0
        for c, n in strong.items():
            by_cat.setdefault(c, _funnel((0, 0, 0, 0, 0)) | {"strong_in": 0})["strong_in"] = n
        due_now = {KINDS[i]: {"wallets": n, "most_overdue_h": round(max(0.0, now - at) / 3600, 1)} for i, n, at in backlog}
        return {
            "funnel": total,
            "qualification": _qualification(first_day, now, pairs, recommended_now, ever),
            "by_category": by_cat,
            "sources": {"seen_in": seen, "only_seen_in": only, "first_found_by": first, "leaderboards": boards,
                        "best_rank": bands},  # fmt: skip
            "by_day": _by_day(first_day, now, new, scored, scored_wallets, tallies),
            "counted_from": counted_from,
            "freshness": {
                "recommended": {"shown": _ages([at for w, at in tiered if w in shown], now, fresh_for(True)),
                                "others": _ages([at for w, at in tiered if w not in shown], now, fresh_for(False))},
                "score_age": {b: score_ages.get(b, 0) for _, b in AGE_BANDS},
                "quick_check_age": {b: quick_ages.get(b, 0) for _, b in AGE_BANDS},
                "due_now": {k: due_now.get(k, {"wallets": 0, "most_overdue_h": None}) for k in KINDS},
                "never_looked_at": {"wallets": unlooked, "oldest_waiting_days": None if oldest_found is None
                                    else round((now - oldest_found) / 86400, 1)},  # fmt: skip
            },
            "failures": {"failed_now": failing[0], "failed_last_day": failing[1] or 0,
                         "failing_3_or_more_in_a_row": failing[2] or 0,
                         "why": dict(Counter(_error_kind(e) for e in errors).most_common(8))},  # fmt: skip
            "sweeps": [{"started": s, "finished": f, "minutes": None if f is None else round((f - s) / 60),
                        "sightings": n, "new": nw, "error": e} for s, f, n, nw, e in sweeps],  # fmt: skip
        }


# ---- phase 6: monitoring's definitions ----
# The lines Explore draws: screened is any look at a wallet (a quick check's evidence, or a score); fully evaluated,
# a score; recommended, a recommended tier (Explore's _TIERED); qualified, one whose score is fresh (PASSED_FRESH_S,
# "Passed our checks": what Traders can serve). A wallet's category: its score's, else where it was first seen.
FUNNEL = ("found", "screened", "fully_evaluated", "recommended", "qualified")
_CAT = "COALESCE(w.category, w.seen_category, 'Other')"
_SCREENED = "(w.status = 'checked' OR w.scored_at IS NOT NULL OR w.buys_30d IS NOT NULL)"
_QUALIFIED = f"({_TIERED} AND w.scored_at >= :fresh)"
_FUNNEL = f"COUNT(*), SUM({_SCREENED}), SUM(w.scored_at IS NOT NULL), SUM({_TIERED}), SUM({_QUALIFIED})"
AGE_BANDS = ((86400, "under_1d"), (2 * 86400, "1_2d"), (7 * 86400, "2_7d"), (30 * 86400, "7_30d"),
             (float("inf"), "over_30d"))  # fmt: skip


QUALIFICATION = ("rescored", "kept", "dropped", "entered", "re_entered", "net")


def _qualification(first_day: str, now: float, pairs: list[tuple[str, str | None, str, str | None, str | None, int | None]],
                   recommended_now: int, ever: int) -> dict[str, Any]:  # fmt: skip
    """How the recommended set changes as traders are scored again, each UTC day from ``first_day`` (newest first)
    and over them all. Per score: one after a recommended score is ``rescored`` and either ``kept`` a recommended tier
    (a move between them, rising to proven, is kept) or ``dropped`` (``dropped_to``: the tier it got); one after a
    score that wasn't recommended, or a first score, that is ``entered`` the first time, ``re_entered`` after an
    earlier recommended score. ``net``: entered + re-entered − dropped. A pair either side of an imported score (an
    older format, kept on launch) isn't compared (``skipped_import``). Tiers are as kept when scored."""
    rec = set(RECOMMENDED)
    days: dict[str, dict[str, Any]] = {}
    at = datetime.fromisoformat(first_day + "T00:00:00+00:00").timestamp()
    while at <= now:
        days[_day(at)] = dict.fromkeys(QUALIFICATION, 0) | {"day": _day(at), "dropped_to": Counter()}
        at += 86400
    skipped = 0
    for day, seg, via, prev, prev_via, was in pairs:
        d = days.get(day)
        if d is None:
            continue
        if "import" in (via, prev_via):
            skipped += 1
            continue
        if prev in rec:
            d["rescored"] += 1
            if seg in rec:
                d["kept"] += 1
            else:
                d["dropped"] += 1
                d["dropped_to"][seg or "None"] += 1
        elif seg in rec:
            d["re_entered" if was else "entered"] += 1
    total = dict.fromkeys(QUALIFICATION, 0) | {"dropped_to": Counter()}
    for d in days.values():
        d["net"] = d["entered"] + d["re_entered"] - d["dropped"]
        for k in QUALIFICATION:
            total[k] += d[k]
        total["dropped_to"].update(d["dropped_to"])
    out = [d | {"dropped_to": dict(d["dropped_to"].most_common())} for d in sorted(days.values(), key=lambda x: x["day"], reverse=True)]
    return {
        "recommended_now": recommended_now,
        "ever_recommended": ever,
        "window": total | {"dropped_to": dict(total["dropped_to"].most_common()),
                           "kept_share": round(total["kept"] / total["rescored"], 3) if total["rescored"] else None},
        "by_day": out,
        "skipped_import": skipped,
    }  # fmt: skip


def _age(col: str) -> str:
    """SQL naming the AGE_BANDS band a time is in, as of ``:now``."""
    whens = " ".join(f"WHEN :now - {col} < {s:.0f} THEN '{b}'" for s, b in AGE_BANDS[:-1])
    return f"CASE {whens} ELSE '{AGE_BANDS[-1][1]}' END"


def _day(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(at))


def _family(place: str | None) -> str | None:
    """A leaderboard's kind, without its category: ``"PNL MONTH POLITICS"`` is ``"PNL MONTH"``."""
    return None if place is None else " ".join(place.split()[:2])


def _funnel(row: Iterable[Any]) -> dict[str, Any]:
    """A funnel's counts, and its yield: qualified traders per 1,000 wallets found, and the share of those fully
    evaluated who qualified."""
    f = dict(zip(FUNNEL, (int(x or 0) for x in row), strict=True))
    return f | {"qualified_per_1000_found": round(1000 * f["qualified"] / f["found"], 2) if f["found"] else None,
                "qualified_share_of_evaluated": round(f["qualified"] / f["fully_evaluated"], 4)
                if f["fully_evaluated"] else None}  # fmt: skip


def _ages(ats: list[float], now: float, fresh: float) -> dict[str, Any]:
    """How old these scores are, in hours (middle, 90th percentile, oldest), and how many are past ``fresh``."""
    hours = sorted((now - at) / 3600 for at in ats)
    if not hours:
        return {"traders": 0, "median_h": None, "p90_h": None, "oldest_h": None, "past_fresh": 0, "fresh_h": fresh / 3600}
    return {"traders": len(hours), "median_h": round(hours[len(hours) // 2], 1),
            "p90_h": round(hours[min(len(hours) - 1, int(0.9 * len(hours)))], 1), "oldest_h": round(hours[-1], 1),
            "past_fresh": sum(1 for h in hours if h * 3600 > fresh), "fresh_h": fresh / 3600}  # fmt: skip


_NUMBERS = re.compile(r"0x[0-9a-fA-F]+|\d+(?:\.\d+)?")


def _error_kind(error: str | None) -> str:
    """A failed try's error without its numbers and addresses, so tries that failed the same way count together."""
    return _NUMBERS.sub("…", error or "unknown")[:120]


def _by_day(first_day: str, now: float, new: list[tuple[str, str, int]], scored: list[tuple[str, str, int]],
            scored_wallets: dict[str, int], tallies: list[tuple[str, str, str, float]]) -> list[dict[str, Any]]:  # fmt: skip
    """Each UTC day from ``first_day`` to today: wallets found (by where first), scores kept (by what kept them, and
    wallets scored), and the ``daily`` tallies (``{metric: {key: n}}``), newest first."""
    days: dict[str, dict[str, Any]] = {}
    at = datetime.fromisoformat(first_day + "T00:00:00+00:00").timestamp()
    while at <= now:
        d = _day(at)
        days[d] = {"day": d, "new_wallets": 0, "new_by_first_source": {}, "scores": 0, "scores_by": {},
                   "wallets_scored": scored_wallets.get(d, 0), "tallies": {}}  # fmt: skip
        at += 86400
    for d, src, n in new:
        if d in days:
            days[d]["new_wallets"] += n
            days[d]["new_by_first_source"][src] = n
    for d, via, n in scored:
        if d in days:
            days[d]["scores"] += n
            days[d]["scores_by"][via] = n
    for d, metric, key, n in tallies:
        if d in days:
            days[d]["tallies"].setdefault(metric, {})[key] = n
    return sorted(days.values(), key=lambda x: x["day"], reverse=True)
