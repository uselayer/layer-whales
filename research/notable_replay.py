"""Dry run: which Telegram alerts are worth an immediate ping? Replays real trades from the cache that
notable_pull.py wrote, through production's alert pipeline (signals/app.py at b31049e), under today's rule and under
"notable now, routine in the 3-hour summary" variants. Reads nothing from Polymarket and changes nothing anywhere.

Two stages, as production has them:

1. Per followed trader, the alert pass's de-duplication (alert_pass): 30-second rounds, each seeing the trades made
   up to 15 s before it (Polymarket returns a trader's trade within ~1–14 s); fills of one outcome within 30 minutes
   add up (_add_up: the ``pending`` table, keyed by transaction hash as production keys it); one buy alert per trader
   and outcome per 6 h (ALERT_AGAIN_S), sells likewise; no alert at a settled price (≤ 2¢ or ≥ 98¢) and no buy alert
   with no open position to read the price now from. What comes out (NewBuy / NewSell) doesn't depend on who follows
   or on the delivery rule, so it's computed once.
2. Per person, delivery (_deliver, _cluster, _send, _told, _summaries): groups of 2+ followed traders on one outcome
   within GROUP_S (GROUP_FAST_S in Sports/Esports), the 20-a-day cap (a group, a summary each count once; at the cap
   one "limit reached" message, the rest dropped), and either
   - today: TRADER_DAILY = 5 buy/sell alerts per trader per day, the rest held for a summary 3 h after the first
     held trade, a sale of something that trader alerted you about (_told) still sent past the 5; or
   - a variant: notable trades sent now, routine ones held for the summary from the first one (TRADER_DAILY off,
     or kept as a backstop on notable ones with --backstop).

Approximations (production reads these live; the replay can't): the price now is the trader's own latest fill price
of that outcome (for the settled check), and "has an open position" is the holdings worked out from their trades
and their positions now (see Holdings). Whale alerts ("big trade in your markets") aren't replayed: they come from
the global feed, not a followed trader, and are added from alerts.db when given (--alerts-db).

    uv run python research/notable_replay.py --follows research/notable-cache/follows.json \\
        --from 2026-10-12 --to 2026-10-14T16:00 > research/notable-results.txt
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import heapq
import html
import json
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uselayer.whales import WhaleTrade, _polymarket_trade

CACHE = Path(__file__).parent / "notable-cache"
NMP = "0xcf7379b4b891c06d88807f6f70efa75378120215"  # NM-P: used up the owner's 20 alerts by 05:15 UTC on 2026-10-14

# ---- production's numbers (signals/app.py at b31049e) ----
ALERT_EVERY_S = 30
ALERT_MAX_AGE_S = 30 * 60
ALERT_AGAIN_S = 6 * 3600
DAILY_CAP = 20
TRADER_DAILY = 5
SUMMARY_S = 3 * 3600
TOLD_S = 30 * 86400
GROUP_S = 6 * 3600
GROUP_FAST_S = 3600
FAST_CATEGORIES = ("Sports", "Esports")
SETTLED_LOW, SETTLED_HIGH = 0.02, 0.98
COUNTED = ("buy", "sell", "group", "whale", "summary")
TRADER_KINDS = ("buy", "sell")
SITE = "https://usesignals.app"

SEEN_LAG_S = 15  # a trade shows in the trader's own reads this long after it's made (measured 1–14 s)
HISTORY_S = 30 * 86400  # "typical" and "new position" look back this far
THIN = 100  # fewer buys than this in 30 days: their last this many instead
DUST = 1.0  # shares: Polymarket's positions leave out anything under 1 (sizeThreshold=1), so "no longer hold it"


def _settled(p: float) -> bool:
    return p <= SETTLED_LOW or p >= SETTLED_HIGH


def _day(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).date().isoformat()


def _hm(t: float) -> str:
    return f"{datetime.fromtimestamp(t, UTC):%m-%d %H:%M}"


def _cents(p: float) -> str:
    c = p * 100
    return f"{c:.1f}¢" if c < 1 or 99 < c < 100 else f"{round(c)}¢"


def load_jsonl(path: Path) -> list[dict]:
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f]


# ---- holdings: what a trader held of one outcome token at a moment ----


class Holdings:
    """Shares of each outcome token a wallet held at any moment in the cached window. Forward from the window's
    start by its trades (buys +, sells −; a SPLIT adds and a MERGE takes from both outcomes of the market; a REDEEM
    empties the market), plus what it held when the window began. That opening amount is read back from its
    positions now (now − what the window's trades added) when the market hasn't been merged, split or redeemed
    since; otherwise it's the least the trades imply (what it sold beyond what it bought)."""

    def __init__(self, activity: list[dict], positions: list[dict]) -> None:
        now_size = {str(p["asset"]): float(p.get("size") or 0) for p in positions}
        cid_tokens: dict[str, set[str]] = defaultdict(set)
        for p in positions:
            cid_tokens[p["conditionId"]].add(str(p["asset"]))
            if p.get("oppositeAsset"):
                cid_tokens[p["conditionId"]].add(str(p["oppositeAsset"]))
        for r in activity:
            if r.get("type") == "TRADE" and r.get("asset"):
                cid_tokens[r["conditionId"]].add(str(r["asset"]))
        ev: dict[str, list[tuple[float, float, str]]] = defaultdict(list)  # token -> (ts, delta, kind)
        touched: set[str] = set()  # markets merged/split/redeemed in the window: positions now can't be walked back
        for r in sorted(activity, key=lambda r: r["timestamp"]):
            ts, kind = float(r["timestamp"]), r.get("type")
            if kind == "TRADE" and r.get("asset"):
                sz = float(r.get("size") or 0)
                ev[str(r["asset"])].append((ts, sz if str(r.get("side")).upper() == "BUY" else -sz, "t"))
            elif kind in ("MERGE", "SPLIT"):
                sz = float(r.get("size") or 0)
                touched.add(r["conditionId"])
                for tok in cid_tokens.get(r["conditionId"], ()):
                    ev[tok].append((ts, sz if kind == "SPLIT" else -sz, "m"))
            elif kind == "REDEEM":
                touched.add(r["conditionId"])
                for tok in cid_tokens.get(r["conditionId"], ()):
                    ev[tok].append((ts, 0.0, "r"))  # emptied: handled in the walk
        self._t: dict[str, list[float]] = {}
        self._cum: dict[str, list[float]] = {}
        self.opening: dict[str, float] = {}
        tok_cid = {tok: cid for cid, toks in cid_tokens.items() for tok in toks}
        for tok in set(ev) | set(now_size):
            rows = ev.get(tok, [])
            net, low = 0.0, 0.0
            for _, d, k in rows:
                if k == "r":
                    net = 0.0
                else:
                    net += d
                    low = min(low, net)
            if tok_cid.get(tok) not in touched and not any(k == "r" for _, _, k in rows):
                opening = max(0.0, now_size.get(tok, 0.0) - net, -low)
            else:
                opening = -low
            self.opening[tok] = opening
            ts_list, cum, h = [], [], opening
            for ts, d, k in rows:
                h = 0.0 if k == "r" else max(0.0, h + d)
                ts_list.append(ts)
                cum.append(h)
            self._t[tok], self._cum[tok] = ts_list, cum

    def at(self, token: str, t: float, inclusive: bool = True) -> float:
        """Shares of ``token`` held just after ``t`` (just before it, when not ``inclusive``)."""
        ts = self._t.get(token)
        if not ts:
            return self.opening.get(token, 0.0)
        i = (bisect.bisect_right if inclusive else bisect.bisect_left)(ts, t)
        return self._cum[token][i - 1] if i else self.opening.get(token, 0.0)


# ---- stage 1: the alert pass, per trader ----


@dataclass
class Event:
    """One alert-worthy trade as production's alert pass finds it (a NewBuy or NewSell), with what a notable rule
    would know about it at that moment."""

    side: str  # "buy" | "sell"
    trader: str
    name: str
    round: float  # when the round that found it ran (production's ``now``)
    at: float  # its first fill (production's b.at / sell.at)
    market: str
    outcome: str  # token
    outcome_name: str
    title: str
    usd: float
    price: float  # average across the fills
    category: str | None
    # what the rule looks at
    typical: float | None = None  # median buy (sell) of theirs, trailing 30 days or last 100, this one excluded
    typical_n: int = 0
    ratio: float | None = None  # usd / typical
    new: bool | None = None  # buys: first buy of this outcome in 30 days, not already holding it
    held_before: float = 0.0  # shares held just before (buys) / before this round's sells (sells)
    held_after: float = 0.0  # sells: shares left after
    exit: bool | None = None  # sells: they hold none of it any more
    later_usd: float = 0.0  # buys: more bought of it in the next 6 h that this alert didn't include (info only)
    # rarity: of this trader's alerts on this side (after the de-duplication above) over the 30 days before, how
    # many a day were this big or bigger. "Unusual for them" that also counts how often they trade
    per_day_as_big: float | None = None
    rate: float | None = None  # their alerts on this side a day, over the same 30 days
    id: int = 0


def _episodes(trades: list[WhaleTrade], side: str) -> list[tuple[float, float, str]]:
    """A trader's buys (sells) as production adds them up: per outcome, fills within 30 minutes of the first one
    make one, as (first fill's time, dollars, token). What "typical" is the median of: a fill alone is often a
    slice of one order."""
    out: list[tuple[float, float, str]] = []
    open_: dict[str, list[float]] = {}  # token -> [start, usd]
    for t in sorted((t for t in trades if t.action == side), key=lambda t: t.at):
        tok, ts = t.token or t.market, t.at.timestamp()
        cur = open_.get(tok)
        if cur and ts - cur[0] <= ALERT_MAX_AGE_S:
            cur[1] += t.usd
        else:
            if cur:
                out.append((cur[0], cur[1], tok))
            open_[tok] = [ts, t.usd]
    out += [(v[0], v[1], k) for k, v in open_.items()]
    return sorted(out)


class Typical:
    """Median dollars of a trader's buys (or sells), as episodes, over the 30 days before a moment."""

    def __init__(self, eps: list[tuple[float, float, str]]) -> None:
        self.ts = [e[0] for e in eps]
        self.usd = [e[1] for e in eps]
        self.tok = [e[2] for e in eps]

    def before(self, t: float) -> tuple[float | None, int]:
        hi = bisect.bisect_left(self.ts, t)  # strictly before: the trade itself is left out
        lo = bisect.bisect_left(self.ts, t - HISTORY_S)
        if hi - lo < THIN:
            lo = max(0, hi - THIN)
        xs = self.usd[lo:hi]
        return (statistics.median(xs) if xs else None), len(xs)

    def bought_since(self, tok: str, lo: float, hi: float) -> bool:
        a, b = bisect.bisect_left(self.ts, lo), bisect.bisect_left(self.ts, hi)
        return any(self.tok[i] == tok for i in range(a, b))


def trader_events(wallet: str, start: float, end: float, cats: dict[str, str | None]) -> tuple[list[Event], dict]:
    """Production's alert pass over one trader from ``start`` (their earlier trades only noted, as for a trader
    already watched) to ``end``: the NewBuys and NewSells it would make, in order."""
    activity = load_jsonl(CACHE / f"activity-{wallet}.jsonl.gz")
    positions = load_jsonl(CACHE / f"positions-{wallet}.jsonl.gz")
    trades = [_polymarket_trade(r) for r in activity if r.get("type") == "TRADE"]
    trades = [t for t in trades if t.size]
    trades.sort(key=lambda t: t.at)
    hold = Holdings(activity, positions)
    buys_typ, sells_typ = Typical(_episodes(trades, "buy")), Typical(_episodes(trades, "sell"))
    name = next((t.name for t in reversed(trades) if t.name), wallet[:6])
    last_price: dict[str, list[tuple[float, float]]] = defaultdict(list)  # token -> [(ts, price)]
    for t in trades:
        last_price[t.token or t.market].append((t.at.timestamp(), t.price))
    lp_ts = {k: [x[0] for x in v] for k, v in last_price.items()}

    def price_now(tok: str, now: float) -> float | None:
        i = bisect.bisect_right(lp_ts.get(tok, []), now)
        return last_price[tok][i - 1][1] if i else None

    # rounds: each trade seen at the first round SEEN_LAG_S after it was made
    by_round: dict[float, list[WhaleTrade]] = defaultdict(list)
    for t in trades:
        ts = t.at.timestamp()
        if ts + SEEN_LAG_S < start or ts > end:
            continue
        r = start + ((ts + SEEN_LAG_S - start) // ALERT_EVERY_S + 1) * ALERT_EVERY_S
        if r <= end:
            by_round[r].append(t)
    alerted: dict[str, float] = {}
    pending: dict[str, tuple[str, str, float, float, float]] = {}  # trade_id -> (market, token, size, usd, at)
    events: list[Event] = []
    stats = {"fills": 0, "buy_fills": 0, "sell_fills": 0, "pending_dupe_tx": 0, "skip": defaultdict(int)}
    for now in sorted(by_round):
        new = by_round[now]
        fills: dict[str, list[WhaleTrade]] = defaultdict(list)
        sold: dict[str, list[WhaleTrade]] = defaultdict(list)
        for t in new:
            stats["fills"] += 1
            if t.action in ("buy", "sell") and t.size and now - t.at.timestamp() <= ALERT_MAX_AGE_S:
                (fills if t.action == "buy" else sold)[t.market].append(t)
                stats[f"{t.action}_fills"] += 1
        for market, ts in sold.items():
            tok = ts[-1].token or market
            size, usd = sum(f.size for f in ts), sum(f.usd for f in ts)
            first = min(f.at.timestamp() for f in ts)
            after = hold.at(tok, now)
            held = after >= DUST  # market in prices: an open position (production reads it at ``now``)
            pnow = price_now(tok, now) if held else None
            price = usd / size
            if now - alerted.get(f"sell:{market}", -1e18) < ALERT_AGAIN_S:
                stats["skip"]["sell_again_6h"] += 1
                continue
            if _settled(price) or (pnow is not None and _settled(pnow)):
                stats["skip"]["sell_settled"] += 1
                continue
            alerted[f"sell:{market}"] = now
            typ, n = sells_typ.before(first)
            before = hold.at(tok, first, inclusive=False)
            t0 = max(ts, key=lambda f: f.at)
            events.append(Event("sell", wallet, name, now, first, market, tok, t0.outcome or "Yes", t0.title or "",
                                usd, price, cats.get(market.partition(":")[0]), typ, n,
                                usd / typ if typ else None, None, before, after, not held))  # fmt: skip
        # _add_up: pending keyed by transaction hash (INSERT OR IGNORE), so a second fill in one transaction isn't added
        for ts_ in fills.values():
            for f in ts_:
                if f.trade_id in pending:
                    stats["pending_dupe_tx"] += 1
                else:
                    pending[f.trade_id] = (f.market, f.token or f.market, f.size, f.usd, f.at.timestamp())
        for k in [k for k, v in pending.items() if v[4] < now - ALERT_MAX_AGE_S]:
            del pending[k]
        totals: dict[str, list[float]] = {}
        for m, _, size, usd, at in pending.values():
            cur = totals.setdefault(m, [0.0, 0.0, at])
            cur[0] += size
            cur[1] += usd
            cur[2] = min(cur[2], at)
        for market, ts in fills.items():
            tok = ts[-1].token or market
            held = hold.at(tok, now) >= DUST
            pnow = price_now(tok, now) if held else None
            if now - alerted.get(market, -1e18) < ALERT_AGAIN_S:
                stats["skip"]["buy_again_6h"] += 1
                continue
            if pnow is None:
                stats["skip"]["buy_no_position"] += 1
                continue
            if _settled(pnow):
                stats["skip"]["buy_settled"] += 1
                continue
            if market in totals and totals[market][0]:
                size, usd, first = totals[market]
            else:
                size, usd, first = sum(f.size for f in ts), sum(f.usd for f in ts), min(f.at.timestamp() for f in ts)
            alerted[market] = now
            for k in [k for k, v in pending.items() if v[0] == market]:  # _added_up
                del pending[k]
            typ, n = buys_typ.before(first)
            before = hold.at(tok, first, inclusive=False)
            new_pos = before < DUST and not buys_typ.bought_since(tok, first - HISTORY_S, first)
            t0 = max(ts, key=lambda f: f.at)
            events.append(Event("buy", wallet, name, now, first, market, tok, t0.outcome or "Yes", t0.title or "",
                                usd, usd / size, cats.get(market.partition(":")[0]), typ, n,
                                usd / typ if typ else None, new_pos, before))  # fmt: skip
    # what the 6-hour rule kept out of each buy alert: their further buys of it in the next 6 h (for "might be missed")
    by_tok: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for t in trades:
        if t.action == "buy":
            by_tok[t.token or t.market].append((t.at.timestamp(), t.usd))
    for e in events:
        if e.side == "buy":
            e.later_usd = sum(u for ts, u in by_tok[e.outcome] if e.round < ts <= e.round + ALERT_AGAIN_S)
    first_ts = trades[0].at.timestamp() if trades else start
    for side in ("buy", "sell"):
        window: list[float] = []  # sorted dollars of the side's alerts in the 30 days before
        queue: list[Event] = []
        j = 0
        side_evs = [e for e in events if e.side == side]
        for e in side_evs:
            while j < len(queue) and queue[j].at < e.at - HISTORY_S:
                window.pop(bisect.bisect_left(window, queue[j].usd))
                j += 1
            days = max(1.0, min(HISTORY_S, e.at - max(first_ts, start)) / 86400)
            e.per_day_as_big = (len(window) - bisect.bisect_left(window, e.usd)) / days
            e.rate = len(window) / days
            bisect.insort(window, e.usd)
            queue.append(e)
    med_fill =statistics.median([t.usd for t in trades if t.action == "buy"]) if trades else None
    stats["median_buy_fill_usd"] = med_fill
    stats["skip"] = dict(stats["skip"])
    return events, stats


# ---- stage 2: delivery, per person ----


@dataclass
class Rule:
    """``today``: production's rule. Otherwise a notable rule: a buy is notable when it's a new position of at least
    ``floor`` × their typical buy (``floor`` None: new positions aren't a reason) or at least ``k`` × typical (None:
    size isn't a reason); a sell when it's a full exit, a sale of something that trader's buy alert told you about,
    or at least ``k_sell`` × their typical sell. Groups (consensus) always go now. ``backstop``: TRADER_DAILY still
    caps one trader's notable alerts a day."""

    name: str
    today: bool = False
    trader_daily: int | None = TRADER_DAILY  # today's per-trader limit; None: before #98 (no limit, nothing held)
    floor: float | None = None
    k: float | None = None
    k_sell: float | None = None
    exits: bool = True
    told: bool = True
    backstop: bool = False
    # rarity: a new position, a big buy, a big sell (and an exit, with ``rare_exits``) is notable only when that
    # trader made at most this many alerts a day this big or bigger over the last 30 days
    budget: float | None = None
    rare_exits: bool = False
    group_gate: bool = False  # 2+ followed traders on one outcome ping only when one of their buys is notable
    # the rarity budget shared by the person's follows: each trader gets min(budget, person_budget / follows)
    person_budget: float | None = None

    def _rare(self, e: Event) -> bool:
        return self.budget is None or e.per_day_as_big is None or e.per_day_as_big <= self.budget

    def buy_reason(self, e: Event) -> str | None:
        r = e.ratio
        why = None
        if self.floor is not None and e.new and r is not None and r >= self.floor:
            why = "new position" + (f" ≥{self.floor:g}× typical" if self.floor else "")
        elif self.floor is not None and e.new and r is None:
            why = "new position (no typical yet)"
        elif self.k is not None and r is not None and r >= self.k:
            why = f"≥{self.k:g}× typical"
        if why and not self._rare(e):
            return None
        return why + (f", ≤{self.budget:g}/day this big" if why and self.budget is not None else "") if why else None

    def sell_reason(self, e: Event, told: bool) -> str | None:
        if self.exits and e.exit and (not self.rare_exits or self._rare(e)):
            return "full exit"
        if self.told and told:
            return "sold what we alerted"
        if self.k_sell is not None and e.ratio is not None and e.ratio >= self.k_sell and self._rare(e):
            return f"sell ≥{self.k_sell:g}× typical"
        return None


@dataclass
class Person:
    uid: str
    follows: list[str]
    alerts: list[dict] = field(default_factory=list)  # kind, trader, outcome, sent_at, trade_at, day, members
    buys: dict[str, list[tuple[str, float, float]]] = field(default_factory=lambda: defaultdict(list))
    held: list[dict] = field(default_factory=list)
    cap_days: dict[str, float] = field(default_factory=dict)
    counts: dict[tuple, int] = field(default_factory=lambda: defaultdict(int))  # (day, kind, trader) -> n
    dropped: list[dict] = field(default_factory=list)
    summaries: list[dict] = field(default_factory=list)
    log: dict[int, str] = field(default_factory=dict)  # event id -> what happened to it
    notable: set[tuple[str, str]] = field(default_factory=set)  # (trader, outcome) of buys the rule called notable

    def sent_today(self, day: str, kinds: tuple[str, ...] = COUNTED, trader: str | None = None) -> int:
        return sum(n for (d, k, tr), n in self.counts.items() if d == day and k in kinds and (trader is None or tr == trader))


def _cluster(rows: list[tuple[str, float, float]], at: float, window: float) -> list[tuple[str, float, float]]:
    runs: list[list[tuple[str, float, float]]] = []
    for r in sorted(rows, key=lambda r: r[2]):
        if runs and r[2] - runs[-1][-1][2] <= window:
            runs[-1].append(r)
        else:
            runs.append([r])
    return next((run for run in runs if run[0][2] <= at <= run[-1][2]), [])


def _told(p: Person, trader: str, outcome: str, now: float) -> bool:
    for a in p.alerts:
        if a["outcome"] == outcome and a["sent_at"] >= now - TOLD_S:
            if a["kind"] == "buy" and a["trader"] == trader:
                return True
            if a["kind"] == "group" and any(b[0] == trader for b in p.buys[outcome]):
                return True
    return False


def _send(p: Person, rule: Rule, now: float, kind: str, *, trader: str | None = None, outcome: str | None = None,
          usd: float | None = None, told: bool = False, trade_at: float | None = None, ev: Event | None = None,
          routine: bool = False, extra: dict | None = None) -> bool:  # fmt: skip
    """production's _send, with the rule's say on holding: True when it went out now."""
    day = _day(now)
    if p.sent_today(day) >= DAILY_CAP:
        p.cap_days.setdefault(day, now)
        if ev is not None:
            p.dropped.append({"id": ev.id, "kind": kind, "at": now})
            p.log[ev.id] = "dropped (cap)"
        return False
    if kind in TRADER_KINDS and trader:
        hold = False
        if rule.today:
            hold = rule.trader_daily is not None and p.sent_today(day, TRADER_KINDS, trader) >= rule.trader_daily and not told
        else:
            hold = routine or (rule.backstop and p.sent_today(day, TRADER_KINDS, trader) >= TRADER_DAILY and not told)
        if hold:
            p.held.append({"trader": trader, "outcome": outcome, "side": kind, "usd": usd, "at": now,
                           "id": ev.id if ev else None, "title": ev.title if ev else "",
                           "outcome_name": ev.outcome_name if ev else "", "exit": ev.exit if ev else None})  # fmt: skip
            if ev is not None:
                p.log[ev.id] = "summary"
            return False
    p.counts[(day, kind, trader if kind in TRADER_KINDS else None)] += 1
    p.alerts.append({"kind": kind, "trader": trader, "outcome": outcome, "sent_at": now, "trade_at": trade_at,
                     "day": day, **(extra or {})})  # fmt: skip
    if ev is not None:
        p.log[ev.id] = "now" if kind != "group" else "now (group)"
    return True


def _deliver(p: Person, rule: Rule, e: Event, reason: str | None) -> None:
    window = GROUP_FAST_S if e.category in FAST_CATEGORIES else GROUP_S
    p.buys[e.outcome].append((e.trader, e.price, e.at))
    rows = [r for r in p.buys[e.outcome] if r[2] >= e.at - 7 * 86400]
    run = _cluster(rows, e.at, window)
    group = None
    if run:
        group = next((a for a in reversed(p.alerts) if a["kind"] == "group" and a["outcome"] == e.outcome
                      and run[0][2] <= a["trade_at"] <= run[-1][2]), None)  # fmt: skip
    members: dict[str, tuple[str, float, float]] = {}
    for w, price, at in run:
        members.setdefault(w, (w, price, at))
    first = sorted(members.values(), key=lambda m: m[2])
    if reason is not None:
        p.notable.add((e.trader, e.outcome))
    if group:
        p.log[e.id] = "edit of group (no ping)"
        return
    # gated: 2+ traders only make a grouped ping when one of their buys of it is notable on its own
    gated = not rule.today and rule.group_gate and not any((m[0], e.outcome) in p.notable for m in first)
    if len(first) >= 2 and not gated:
        _send(p, rule, e.round, "group", outcome=e.outcome, trade_at=first[0][2], ev=e,
              extra={"members": [m[0] for m in first], "title": e.title, "outcome_name": e.outcome_name})
        return
    _send(p, rule, e.round, "buy", trader=e.trader, outcome=e.outcome, usd=e.usd, trade_at=e.at, ev=e,
          routine=not rule.today and reason is None)  # fmt: skip


def _summary_text(rows: list[dict], names: dict[str, str], tail: str) -> str:
    """production's _summary_text, as plain text (Telegram shows the <b> names bold)."""
    per: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        per.setdefault(r["trader"], {"buy": [], "sell": []})[r["side"]].append(r["usd"] or 0.0)
    lines = [f"More from traders you follow, since {datetime.fromtimestamp(min(r['at'] for r in rows), UTC):%H:%M} UTC:"]
    for trader, sides in sorted(per.items(), key=lambda kv: -sum(map(len, kv[1].values()))):
        parts = [f"{len(v)} {side}{'s' if len(v) != 1 else ''} (${sum(v):,.0f})" for side, v in sides.items() if v]
        n = sum(map(len, sides.values()))
        lines.append(f"• <b>{html.escape(names.get(trader, trader[:6]))}</b>: {n} more trade{'s' if n != 1 else ''}, {' and '.join(parts)}")
    lines.append(tail)
    return "\n".join([*lines, "Open your Feed"])


TODAY_TAIL = f"Each came after that trader's {TRADER_DAILY} alerts for the day. Every trade is in your Feed."
ROUTINE_TAIL = "None was unusual for that trader (in size, a new market for them, or an exit). Every trade is in your Feed."


def _summary_text_plus(rows: list[dict], names: dict[str, str], tail: str) -> str:
    """A proposed richer summary: production's per-trader line, plus each trader's biggest buy and sell in it."""
    per: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        per[r["trader"]].append(r)
    lines = [f"More from traders you follow, since {datetime.fromtimestamp(min(r['at'] for r in rows), UTC):%H:%M} UTC:"]
    for trader, rs in sorted(per.items(), key=lambda kv: -len(kv[1])):
        parts = []
        for side in ("buy", "sell"):
            v = [r for r in rs if r["side"] == side]
            if v:
                parts.append(f"{len(v)} {side}{'s' if len(v) != 1 else ''} (${sum(r['usd'] or 0 for r in v):,.0f})")
        lines.append(f"• <b>{html.escape(names.get(trader, trader[:6]))}</b>: {' and '.join(parts)}")
        for side, verb in (("buy", "Biggest buy"), ("sell", "Biggest sale")):
            v = [r for r in rs if r["side"] == side]
            if v:
                top = max(v, key=lambda r: r["usd"] or 0)
                lines.append(f"   {verb}: ${top['usd'] or 0:,.0f} {top['outcome_name']} · {top['title']}")
    lines.append(tail)
    return "\n".join([*lines, "Open your Feed"])


def _summaries(p: Person, rule: Rule, now: float, names: dict[str, str]) -> None:
    if not p.held or min(h["at"] for h in p.held) > now - SUMMARY_S:
        return
    rows = [h for h in p.held if not (h["side"] == "buy" and any(
        a["kind"] == "group" and a["outcome"] == h["outcome"] and a["sent_at"] >= h["at"] for a in p.alerts))]  # fmt: skip
    if rows:
        ok = _send(p, rule, now, "summary")
        text = _summary_text(rows, names, TODAY_TAIL if rule.today else ROUTINE_TAIL)
        plus = _summary_text_plus(rows, names, TODAY_TAIL if rule.today else ROUTINE_TAIL)
        p.summaries.append({"at": now, "sent": ok, "rows": len(rows), "ids": [h["id"] for h in rows], "text": text,
                            "text_plus": plus})  # fmt: skip
        if not ok:
            for h in rows:
                p.log[h["id"]] = "summary dropped (cap)"
    p.held = []


def deliver_all(person: Person, rule: Rule, events: list[Event], names: dict[str, str], end: float) -> Person:
    """Every round's buys, then sells, then summaries, for one person, as alert_pass does."""
    if rule.person_budget is not None and rule.budget is not None:
        rule = replace(rule, budget=min(rule.budget, rule.person_budget / max(1, len(person.follows))))
    rounds: dict[float, list[Event]] = defaultdict(list)
    for e in events:
        if e.trader in person.follows:
            rounds[e.round].append(e)
    heap = sorted(rounds)
    heapq.heapify(heap)
    done: set[float] = set()
    while heap:
        now = heapq.heappop(heap)
        if now in done or now > end:
            continue
        done.add(now)
        evs = rounds.get(now, [])
        for e in evs:
            if e.side == "buy":
                reason = None if rule.today else rule.buy_reason(e)
                _deliver(person, rule, e, reason)
        for e in evs:
            if e.side == "sell":
                told = _told(person, e.trader, e.outcome, now)
                reason = None if rule.today else rule.sell_reason(e, told)
                _send(person, rule, now, "sell", trader=e.trader, outcome=e.outcome, usd=e.usd, told=told,
                      trade_at=e.at, ev=e, routine=not rule.today and reason is None)  # fmt: skip
        _summaries(person, rule, now, names)
        if person.held:  # the round at which the oldest held trade is due
            due = min(h["at"] for h in person.held) + SUMMARY_S
            nxt = now + ((due - now) // ALERT_EVERY_S + (1 if (due - now) % ALERT_EVERY_S else 0)) * ALERT_EVERY_S
            if nxt not in done:
                heapq.heappush(heap, max(nxt, now + ALERT_EVERY_S))
    return person


RULES = [Rule("before #98", today=True, trader_daily=None), Rule("today", today=True)]
for floor in (None, 0.0, 0.5, 1.0):
    for k in (1.5, 2.0, 3.0, None):
        if floor is None and k is None:
            continue
        RULES.append(Rule(f"new{'-' if floor is None else f'≥{floor:g}×'} big{'-' if k is None else f'≥{k:g}×'}",
                          floor=floor, k=k, k_sell=k))  # fmt: skip
PRIMARY = "new≥0.5× big≥2×"
RULES.append(Rule(f"{PRIMARY} +backstop5", floor=0.5, k=2.0, k_sell=2.0, backstop=True))
for b in (1, 2, 3, 5):
    RULES.append(Rule(f"{PRIMARY} rare≤{b}/day", floor=0.5, k=2.0, k_sell=2.0, budget=b))
    RULES.append(Rule(f"{PRIMARY} rare≤{b}/day +exits", floor=0.5, k=2.0, k_sell=2.0, budget=b, rare_exits=True))
RULES.append(Rule(f"{PRIMARY} rare≤3/day +exits gated-groups", floor=0.5, k=2.0, k_sell=2.0, budget=3, rare_exits=True,
                  group_gate=True))  # fmt: skip
for pb in (6, 10):
    RULES.append(Rule(f"{PRIMARY} rare≤min(3,{pb}/follows) +exits gated-groups", floor=0.5, k=2.0, k_sell=2.0, budget=3,
                      rare_exits=True, group_gate=True, person_budget=pb))  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--follows", type=Path, required=True, help="follows.json (production's shape) or a list of wallets")
    ap.add_argument("--from", dest="start", required=True, help="first replayed UTC day")
    ap.add_argument("--to", dest="end", required=True, help="replay end (UTC, ISO)")
    ap.add_argument("--warmup-h", type=float, default=12, help="hours replayed before --from, not counted")
    ap.add_argument("--json", type=Path, default=Path(__file__).parent / "notable-results.json")
    a = ap.parse_args()
    start = datetime.fromisoformat(a.start).replace(tzinfo=UTC).timestamp()
    end = datetime.fromisoformat(a.end).replace(tzinfo=UTC).timestamp()
    warm = start - a.warmup_h * 3600
    raw = json.loads(a.follows.read_text())
    if isinstance(raw, list):
        users = {"(one person following these)": {"follows": [w.lower() for w in raw]}}
    else:
        users = {uid: {"follows": [w.lower() for w in u.get("follows") or []], "chat": u.get("chat")}
                 for uid, u in raw["users"].items() if u.get("follows")}  # fmt: skip
    cats = json.loads((CACHE / "categories.json").read_text()) if (CACHE / "categories.json").exists() else {}
    wallets = sorted({w for u in users.values() for w in u["follows"]})
    events: list[Event] = []
    tstats: dict[str, dict] = {}
    names: dict[str, str] = {}
    for w in wallets:
        if not (CACHE / f"activity-{w}.jsonl.gz").exists():
            print(f"(no cache for {w}: left out)")
            continue
        evs, st = trader_events(w, start - HISTORY_S - 86400, end, cats)  # the history too: rarity needs it
        st["alerts_30d"] = {s: sum(1 for e in evs if e.side == s and start - HISTORY_S <= e.round < start) for s in ("buy", "sell")}
        first = min((e.round for e in evs), default=start)  # a capped pull (notable_pull --max-rows) covers fewer days
        st["history_days"] = max(1.0, min(HISTORY_S, start - max(first, start - HISTORY_S)) / 86400)
        events += [e for e in evs if e.round >= warm]
        tstats[w] = st
        if evs:
            names[w] = evs[0].name
    events.sort(key=lambda e: (e.round, e.side != "buy", e.trader))
    for i, e in enumerate(events):
        e.id = i
    out: dict[str, Any] = {"window": [a.start, a.end], "warmup_h": a.warmup_h, "names": names, "trader_stats": tstats,
                           "events": [e.__dict__ for e in events], "people": {}}  # fmt: skip
    people = dict(users)
    if NMP in wallets and any(u["follows"] != [NMP] for u in users.values()):
        people["(NM-P alone)"] = {"follows": [NMP]}
    for uid, u in people.items():
        res = {}
        for rule in RULES:
            p = deliver_all(Person(uid, u["follows"]), rule, events, names, end)
            days: dict[str, dict] = {}
            for al in p.alerts:
                if al["sent_at"] < start:
                    continue
                d = days.setdefault(al["day"], defaultdict(int))
                d[al["kind"]] += 1
            for s in p.summaries:
                if s["at"] >= start:
                    days.setdefault(_day(s["at"]), defaultdict(int))["summary_items"] += s["rows"]
            for dr in p.dropped:
                if dr["at"] >= start:
                    days.setdefault(_day(dr["at"]), defaultdict(int))["dropped"] += 1
            for d, t in p.cap_days.items():
                if t >= start:
                    days.setdefault(d, defaultdict(int))["cap_at"] = _hm(t)  # type: ignore[assignment]
            res[rule.name] = {"days": {d: dict(v) for d, v in sorted(days.items())}, "log": p.log,
                              "summaries": [s for s in p.summaries if s["at"] >= start],
                              "alerts": [al for al in p.alerts if al["sent_at"] >= start]}  # fmt: skip
        out["people"][uid] = res
    a.json.write_text(json.dumps(out, default=str))
    print(f"wrote {a.json}: {len(events)} alert-worthy trades from {len(wallets)} wallets, {len(people)} people")


if __name__ == "__main__":
    main()
