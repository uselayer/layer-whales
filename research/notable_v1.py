"""Follow-up to notable_replay.py, for a simplest-reliable V1 (owner's review of #106, 2026-10-14):

1. 7-day vs 30-day rarity (a 7-day backfill is cheaper), on alerts (after the alert pass's de-duplication) or on
   episodes (fills of one outcome within 30 min, straight from trades: what a backfill can build without replaying
   the alert pass).
2. A fixed rarity budget per trader (B a day this big), never divided by how many traders someone follows.
3. Fewer, richer summaries: every 3, 6 or 12 hours, naming each trader's largest buys and sells.
4. Nothing dropped at the cap: the 20 a day is split into immediate alerts (20 minus the summaries' slots) and the
   summaries; a notable trade past the immediate limit goes at the top of the next summary, and routine trades are
   always held for it (production drops them once the cap is hit).
5. Buys, sells and grouped buys kept apart: counted apart, and in their own sections of the summary.

Stage 1 (per trader: notable_replay.trader_events) is computed once and kept in notable-cache/events.pkl.

    uv run python research/notable_v1.py --follows research/notable-scenarios.json \\
        --from 2026-10-12 --to 2026-10-14T16:00 > research/notable-v1-results.md
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import math
import pickle
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from notable_replay import (ALERT_EVERY_S, CACHE, DAILY_CAP, FAST_CATEGORIES, GROUP_FAST_S, GROUP_S, HISTORY_S, NMP,
                            TOLD_S, Event, Person, Rule, _cluster, _day, deliver_all, trader_events)  # fmt: skip

TG_LIMIT = 4096  # Telegram's longest message, in characters
PAGE = 500  # /activity rows per read


def _cents(p: float) -> str:
    c = p * 100
    return f"{c:.1f}¢" if c < 1 or 99 < c < 100 else f"{round(c)}¢"


def _short(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass
class V1:
    """A notable buy: (a new position ≥ ``floor`` × typical, or ≥ ``k`` × typical) and rare: at most ``B`` a day this
    big or bigger for that trader, over ``window`` days of their alerts (``basis`` "a") or episodes ("e"). A notable
    sell: one their buy alert told you about, or (a full exit, or ≥ ``k`` × their typical sell) and rare. A group
    pings when one member's buy is notable. ``summary_h``: hours from the first held trade to its summary."""

    name: str
    window: int = 7
    basis: str = "e"
    B: float = 3.0
    floor: float = 0.5
    k: float = 2.0
    summary_h: float = 6.0
    nodrop: bool = True
    person_budget: float | None = None  # #106's min(B, person_budget / follows), for comparison

    def rarity(self, e: Event) -> float | None:
        return getattr(e, f"rare_{self.basis}{self.window}")

    def rare(self, e: Event, budget: float) -> bool:
        r = self.rarity(e)
        return r is None or r <= budget

    def size(self, e: Event) -> tuple[float | None, bool | None]:
        return (e.ratio7, e.new7) if self.window == 7 else (e.ratio, e.new)

    def buy(self, e: Event, budget: float) -> str | None:
        r, new = self.size(e)
        if new and (r is None or r >= self.floor):
            why = "new position"
        elif r is not None and r >= self.k:
            why = f"{r:.0f}× their usual"
        else:
            return None
        return why if self.rare(e, budget) else None

    def sell(self, e: Event, told: bool, budget: float) -> str | None:
        if told:
            return "sold what we alerted"
        r, _ = self.size(e)
        if e.exit and self.rare(e, budget):
            return "full exit"
        if r is not None and r >= self.k and self.rare(e, budget):
            return f"{r:.0f}× their usual sell"
        return None

    @property
    def summaries_a_day(self) -> int:
        return math.ceil(24 / self.summary_h)

    @property
    def immediate_cap(self) -> int:
        return DAILY_CAP - self.summaries_a_day


@dataclass
class Box:
    """One person's state under a V1 rule."""

    follows: list[str]
    sent: list[dict] = field(default_factory=list)  # immediate alerts: kind buy/sell/group
    buys: dict[str, list[tuple[str, float, float]]] = field(default_factory=lambda: defaultdict(list))
    notable: set[tuple[str, str]] = field(default_factory=set)
    held: list[dict] = field(default_factory=list)
    summaries: list[dict] = field(default_factory=list)
    log: dict[int, str] = field(default_factory=dict)

    def today(self, day: str, kinds: tuple[str, ...] = ("buy", "sell", "group")) -> int:
        return sum(1 for a in self.sent if a["day"] == day and a["kind"] in kinds)

    def summaries_today(self, day: str) -> int:
        return sum(1 for s in self.summaries if s["day"] == day)


def _hold(b: Box, e: Event, kind: str, now: float, why: str | None, members: list[str] | None = None) -> None:
    b.held.append({"e": e, "kind": kind, "at": now, "why": why, "members": members})
    b.log[e.id] = f"summary{' ★' if why else ''}"


def _now(b: Box, rule: V1, e: Event, kind: str, now: float, why: str, members: list[str] | None = None) -> None:
    """Send now if today's immediate alerts allow; past them, to the top of the next summary (never dropped)."""
    day = _day(now)
    if b.today(day) >= rule.immediate_cap:
        _hold(b, e, kind, now, why, members)
        return
    b.sent.append({"kind": kind, "trader": e.trader, "outcome": e.outcome, "at": now, "day": day, "why": why,
                   "members": members, "id": e.id})  # fmt: skip
    b.log[e.id] = f"now{' (group)' if kind == 'group' else ''}"


def _told(b: Box, trader: str, outcome: str, now: float) -> bool:
    for a in b.sent:
        if a["outcome"] == outcome and a["at"] >= now - TOLD_S:
            if a["kind"] == "buy" and a["trader"] == trader:
                return True
            if a["kind"] == "group" and trader in (a["members"] or []):
                return True
    return False


STAR_MAX = 8  # unusual trades listed in a summary; the rest are counted ("+N more unusual trades")
GROUP_MAX = 3


def summary_text(rows: list[dict], names: dict[str, str]) -> tuple[str, bool]:
    """The proposed summary: unusual trades that came past the immediate limit first (at most STAR_MAX, biggest
    first), then routine buys and sells per trader (count, dollars, their largest one), then outcomes 2+ followed
    traders bought (GROUP_MAX). Kept under Telegram's 4,096 characters, whole trader lines at a time ("+N more in
    your Feed"). Returns (text, whether lines were left out to fit)."""
    e_ = html.escape
    nm = lambda w: e_(names.get(w, w[:6]))  # noqa: E731
    head = f"Since {datetime.fromtimestamp(min(r['at'] for r in rows), UTC):%H:%M} UTC, from traders you follow:"
    body: list[str] = []
    star = sorted((r for r in rows if r["why"]), key=lambda r: -r["e"].usd)
    if star:
        body.append("<b>Unusual for them, past today's alert limit</b>")
        for r in star[:STAR_MAX]:
            e = r["e"]
            verb = "sold" if r["kind"] == "sell" else "bought"
            who = " + ".join(nm(m) for m in (r["members"] or [e.trader]))
            body.append(f"• <b>{who}</b> {verb} {e_(e.outcome_name)} at {_cents(e.price)} (${e.usd:,.0f}, {r['why']})"
                        f" · {e_(_short(e.title))}")  # fmt: skip
        if len(star) > STAR_MAX:
            body.append(f"+{len(star) - STAR_MAX} more unusual trades in your Feed")
    for kind, title in (("buy", "Buys"), ("sell", "Sells")):
        per: dict[str, list[Event]] = defaultdict(list)
        for r in rows:
            if r["kind"] == kind and not r["why"]:
                per[r["e"].trader].append(r["e"])
        if not per:
            continue
        body.append(f"<b>{title}</b>")
        for w, es in sorted(per.items(), key=lambda kv: -sum(e.usd for e in kv[1])):
            big = max(es, key=lambda e: e.usd)
            exited = " (exited)" if kind == "sell" and big.exit else ""
            body.append(f"• <b>{nm(w)}</b>: {len(es)} {title.lower()[:-1]}{'s' if len(es) != 1 else ''}, ${sum(e.usd for e in es):,.0f}."
                        f" Largest ${big.usd:,.0f} {e_(big.outcome_name)} · {e_(_short(big.title, 48))}{exited}")  # fmt: skip
    groups = list({r["e"].outcome: r for r in rows if r["kind"] == "group" and not r["why"]}.values())  # one per outcome
    if groups:
        body.append("<b>Bought by 2+ traders you follow</b>")
        for r in groups[:GROUP_MAX]:
            e = r["e"]
            body.append(f"• {' + '.join(nm(m) for m in r['members'])}: {e_(e.outcome_name)} · {e_(_short(e.title))}")
        if len(groups) > GROUP_MAX:
            body.append(f"+{len(groups) - GROUP_MAX} more in your Feed")
    tail = ("Every trade is in your Feed." if star else "Each was usual for that trader. Every trade is in your Feed.") + "\nOpen your Feed"
    dropped = 0
    while True:  # too long for Telegram: drop the last trader lines, keeping section heads, and say how many
        more = [f"+{dropped} more traders in your Feed"] if dropped else []
        text = "\n".join([head, *body, *more, tail])
        bullets = [i for i, ln in enumerate(body) if ln.startswith("• ")]
        if len(text) <= TG_LIMIT or not bullets:
            return text, dropped > 0
        body.pop(bullets[-1])
        dropped += 1



def deliver_v1(follows: list[str], rule: V1, events: list[Event], names: dict[str, str], end: float) -> Box:
    b = Box(follows)
    budget = rule.B if rule.person_budget is None else min(rule.B, rule.person_budget / max(1, len(follows)))
    rounds: dict[float, list[Event]] = defaultdict(list)
    for e in events:
        if e.trader in follows:
            rounds[e.round].append(e)
    times = sorted(rounds)
    due: float | None = None
    i = 0
    while i < len(times) or (due is not None and due <= end):
        now = times[i] if i < len(times) and (due is None or times[i] <= due) else due
        if i < len(times) and now == times[i]:
            i += 1
        for e in rounds.get(now, []):
            if e.side != "buy":
                continue
            why = rule.buy(e, budget)
            if why:
                b.notable.add((e.trader, e.outcome))
            window = GROUP_FAST_S if e.category in FAST_CATEGORIES else GROUP_S
            b.buys[e.outcome].append((e.trader, e.price, e.at))
            run = _cluster([r for r in b.buys[e.outcome] if r[2] >= e.at - 7 * 86400], e.at, window)
            members = list(dict.fromkeys(m[0] for m in sorted(run, key=lambda m: m[2])))
            if len(members) >= 2:
                if any(a["kind"] == "group" and a["outcome"] == e.outcome and a["at"] >= run[0][2] for a in b.sent):
                    b.log[e.id] = "edit of group (no ping)"
                    continue
                if any((m, e.outcome) in b.notable for m in members):
                    _now(b, rule, e, "group", now, why or "a member's buy is unusual", members)
                else:
                    _hold(b, e, "group", now, None, members)
                continue
            if why:
                _now(b, rule, e, "buy", now, why)
            else:
                _hold(b, e, "buy", now, None)
        for e in rounds.get(now, []):
            if e.side == "sell":
                why = rule.sell(e, _told(b, e.trader, e.outcome, now), budget)
                _now(b, rule, e, "sell", now, why) if why else _hold(b, e, "sell", now, None)
        # summaries: SUMMARY after the first held trade, at most summaries_a_day a UTC day (else the next day)
        if b.held and min(h["at"] for h in b.held) <= now - rule.summary_h * 3600 and \
                b.summaries_today(_day(now)) < rule.summaries_a_day:  # fmt: skip
            rows = [h for h in b.held if not (h["kind"] == "buy" and any(
                a["kind"] == "group" and a["outcome"] == h["e"].outcome and a["at"] >= h["at"] for a in b.sent))]  # fmt: skip
            if rows:
                text, cut = summary_text(rows, names)
                b.summaries.append({"at": now, "day": _day(now), "rows": len(rows), "stars": sum(1 for r in rows if r["why"]),
                                    "by_kind": {k: sum(1 for r in rows if r["kind"] == k) for k in ("buy", "sell", "group")},
                                    "chars": len(text), "cut": cut, "text": text,
                                    "waits": [now - r["at"] for r in rows if r["why"]]})  # fmt: skip
            b.held = []
        due = None
        if b.held:
            first = min(h["at"] for h in b.held) + rule.summary_h * 3600
            nxt = math.ceil(first / ALERT_EVERY_S) * ALERT_EVERY_S
            if b.summaries_today(_day(nxt)) >= rule.summaries_a_day:  # the next UTC day's first round
                nxt = (math.floor(nxt / 86400) + 1) * 86400
            due = max(nxt, now + ALERT_EVERY_S)
    return b


def load_events(wallets: list[str], start: float, end: float, warm: float) -> tuple[list[Event], dict, dict]:
    key = (4, tuple(sorted(wallets)), start, end, warm)  # 4: alert units: settled prices out, 60-s add-up
    pkl = CACHE / "events.pkl"
    if pkl.exists():
        with gzip.open(pkl, "rb") as f:
            got = pickle.load(f)
        if got["key"] == key:
            return got["events"], got["names"], got["stats"]
    cats = json.loads((CACHE / "categories.json").read_text())
    events, names, stats = [], {}, {}
    for w in wallets:
        evs, st = trader_events(w, start - HISTORY_S - 86400, end, cats)
        stats[w] = st
        events += [e for e in evs if e.round >= warm]
        if evs:
            names[w] = evs[0].name
    events.sort(key=lambda e: (e.round, e.side != "buy", e.trader))
    for i, e in enumerate(events):
        e.id = i
    with gzip.open(pkl, "wb") as f:
        pickle.dump({"key": key, "events": events, "names": names, "stats": stats}, f)
    return events, names, stats


def backfill_cost(wallets: list[str], names: dict[str, str], end: float) -> list[tuple]:
    """Per wallet: trades a day over the last 7 days of the cache, and the /activity reads a 7- or 30-day backfill
    takes (500 rows a read; a wallet whose cache holds fewer days is scaled up from its rate)."""
    out = []
    for w in wallets:
        with gzip.open(CACHE / f"activity-{w}.jsonl.gz", "rt") as f:
            ts = [json.loads(ln)["timestamp"] for ln in f if '"TRADE"' in ln]
        ts = [t for t in ts if t <= end]
        if not ts:
            continue
        have = (end - min(ts)) / 86400
        d7 = sum(1 for t in ts if t >= end - 7 * 86400) / min(7, have)
        d30 = sum(1 for t in ts if t >= end - 30 * 86400) / min(30, have)
        out.append((names.get(w, w[:8]), d7, math.ceil(d7 * 7 / PAGE) + 1, math.ceil(d30 * 30 / PAGE) + 1, have < 29))
    return sorted(out, key=lambda r: -r[1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--follows", type=Path, required=True)
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    a = ap.parse_args()
    start = datetime.fromisoformat(a.start).replace(tzinfo=UTC).timestamp()
    end = datetime.fromisoformat(a.end).replace(tzinfo=UTC).timestamp()
    warm = start - 12 * 3600
    users = {u: v["follows"] for u, v in json.loads(a.follows.read_text())["users"].items()}
    users["(NM-P alone)"] = [NMP]
    wallets = sorted({w for f in users.values() for w in f})
    events, names, stats = load_events(wallets, start, end, warm)
    nm = lambda w: names.get(w, w[:8])  # noqa: E731
    in_window = [e for e in events if e.round >= start]
    days = sorted({_day(e.round) for e in in_window})
    out: list[str] = []
    p = out.append

    p(f"# V1 follow-up: replay {a.start} → {a.end} UTC\n")

    # ---- 1. 7 vs 30 days, alerts vs episodes ----
    p("## 1. Rarity over 7 days vs 30 days\n")
    p("Which trades each measure calls notable (B = 3 a day, floor 0.5×, 2× typical), before delivery, over every followed"
      " and Recommended trader. Agreement is per trade.\n")
    base = V1("a30", window=30, basis="a")
    variants = [V1("a7", window=7, basis="a"), V1("u30", window=30, basis="u"), V1("u7", window=7, basis="u"),
                V1("e7", window=7, basis="e")]
    label = {"a": "alerts (needs the alert pass)", "u": "alert units (trades only)", "e": "episodes (trades only)"}
    def flag(r: V1, e: Event) -> bool:
        return bool(r.buy(e, r.B) if e.side == "buy" else r.sell(e, False, r.B))
    p("| Measure | Notable trades (3 days) | Both with 30-day alerts | Only this one | Only 30-day alerts |")
    p("|---|---|---|---|---|")
    b0 = {e.id for e in in_window if flag(base, e)}
    p(f"| 30-day, {label['a']} (#106) | {len(b0)} | – | – | – |")
    for r in variants:
        s = {e.id for e in in_window if flag(r, e)}
        p(f"| {r.window}-day, {label[r.basis]} | {len(s)} | {len(s & b0)} | {len(s - b0)} | {len(b0 - s)} |")
    p("")
    p("Per trader, notable trades a day (buys / sells):\n")
    cols = [base, *variants]
    p("| Trader | " + " | ".join(f"{r.window}-day {r.basis}" for r in cols) + " |")
    p("|---|" + "---|" * len(cols))
    nd = max(1, (end - start) / 86400)
    for w in sorted(wallets, key=lambda w: -sum(1 for e in in_window if e.trader == w)):
        es = [e for e in in_window if e.trader == w]
        if not es:
            continue
        cells = []
        for r in cols:
            nb = sum(1 for e in es if e.side == "buy" and flag(r, e)) / nd
            ns = sum(1 for e in es if e.side == "sell" and flag(r, e)) / nd
            cells.append(f"{nb:.1f} / {ns:.1f}")
        p(f"| {nm(w)} | " + " | ".join(cells) + " |")
    p("")
    # where 7-day and 30-day alert units disagree: the biggest trades only one of them flags
    u7, u30 = V1("u7", window=7, basis="u"), V1("u30", window=30, basis="u")
    only7 = sorted((e for e in in_window if flag(u7, e) and not flag(u30, e)), key=lambda e: -e.usd)[:6]
    only30 = sorted((e for e in in_window if flag(u30, e) and not flag(u7, e)), key=lambda e: -e.usd)[:6]
    for title, es in (("Notable over 7 days only (alert units, biggest)", only7), ("Notable over 30 days only (alert units, biggest)", only30)):
        p(f"**{title}:**")
        for e in es:
            p(f"- {nm(e.trader)} {datetime.fromtimestamp(e.at, UTC):%b %d %H:%M} {e.side} {e.outcome_name} · {_short(e.title)}:"
              f" ${e.usd:,.0f}; 7 days: {e.rare_u7 or 0:.1f}/day this big, {e.ratio7 or 0:.1f}× typical ·"
              f" 30 days: {e.rare_u30 or 0:.1f}/day, {e.ratio or 0:.1f}×")
        p("")

    # ---- backfill cost ----
    p("## API cost\n")
    p("Backfill reads when someone follows a trader (`/activity`, 500 rows a read, plus 1 positions read), from each"
      " wallet's trades a day over the last 7 days of the cache. After the backfill, nothing extra: the poller already"
      " reads every followed trader's last 100 trades every 30 s (and 500 when there's a gap), so the sizes are kept"
      " from those reads.\n")
    p("| Trader | Trades a day | 7-day backfill reads | 30-day backfill reads |")
    p("|---|---|---|---|")
    tot7 = tot30 = 0
    for name, perday, r7, r30, short in backfill_cost(wallets, names, end):
        tot7 += r7
        tot30 += r30
        p(f"| {name}{' (scaled from fewer days)' if short else ''} | {perday:,.0f} | {r7} | {r30} |")
    p(f"| **All {len(wallets)}** | | **{tot7}** | **{tot30}** |\n")

    # ---- 2. fixed budget vs one shared by follows ----
    p("## 2. A fixed budget per trader vs one divided by follows\n")
    p("Trades notable with a fixed B = 3 a day per trader that #106's min(3, 10 ÷ follows) would have called routine"
      " (7-day alert units), per scenario over the 3 days:\n")
    fixed = V1("u7", window=7, basis="u")
    for uid, follows in users.items():
        shared = min(3, 10 / max(1, len(follows)))
        es = [e for e in in_window if e.trader in follows]
        lost = [e for e in es if (fixed.buy(e, 3) if e.side == "buy" else fixed.sell(e, False, 3))
                and not (fixed.buy(e, shared) if e.side == "buy" else fixed.sell(e, False, shared))]  # fmt: skip
        big = sorted(lost, key=lambda e: -e.usd)[:3]
        p(f"- **{uid}** ({len(follows)} follows, shared budget {shared:.2f}/day): {len(lost)} trades"
          + (": biggest " + "; ".join(f"{nm(e.trader)} ${e.usd:,.0f} {e.outcome_name} · {_short(e.title, 40)}" for e in big) if big else ""))
    p("")

    # ---- 3–5: delivery ----
    rules = [
        V1("shared budget (#106 formula), 30-day alerts, 6-h", window=30, basis="a", summary_h=6, person_budget=10),
        V1("fixed B 3, 30-day alerts, 6-h", window=30, basis="a", summary_h=6),
        V1("fixed B 3, 7-day units, 3-h", window=7, basis="u", summary_h=3),
        V1("fixed B 3, 7-day units, 6-h", window=7, basis="u", summary_h=6),
        V1("fixed B 3, 7-day units, 12-h", window=7, basis="u", summary_h=12),
        V1("fixed B 2, 7-day units, 6-h", window=7, basis="u", B=2, summary_h=6),
        V1("fixed B 3, 30-day units, 6-h", window=30, basis="u", summary_h=6),
    ]
    p("## 3–5. Delivery: nothing dropped, buys / sells / groups apart, fewer summaries\n")
    p("Per day: immediate buys / sells / groups, then summaries (★ = unusual trades that came past the immediate limit and"
      " went at the top of a summary). The immediate limit is 20 minus the summaries' slots (3-h: 12, 6-h: 16, 12-h: 18),"
      " so the total never passes 20 and nothing is dropped. Today's rule (#98) for comparison, with its drops.\n")
    results: dict[str, dict[str, Box]] = {}
    for uid, follows in users.items():
        p(f"### {uid}\n")
        p("| Rule | " + " | ".join(days) + " |")
        p("|---|" + "---|" * len(days))
        prod = deliver_all(Person(uid, follows), Rule("today", today=True), events, names, end)
        cells = []
        for d in days:
            k = defaultdict(int)
            for al in prod.alerts:
                if al["day"] == d:
                    k[al["kind"]] += 1
            dropped = sum(1 for x in prod.dropped if _day(x["at"]) == d)
            cells.append(f"{k['buy']} / {k['sell']} / {k['group']} now · {k['summary']} sum" + (f" · **{dropped} dropped**" if dropped else ""))
        p("| today (#98) | " + " | ".join(cells) + " |")
        results[uid] = {}
        for r in rules:
            b = deliver_v1(follows, r, events, names, end)
            results[uid][r.name] = b
            cells = []
            for d in days:
                k = defaultdict(int)
                for al in b.sent:
                    if al["day"] == d:
                        k[al["kind"]] += 1
                ss = [s for s in b.summaries if s["day"] == d]
                stars = sum(s["stars"] for s in ss)
                cells.append(f"{k['buy']} / {k['sell']} / {k['group']} now · {len(ss)} sum ({sum(s['rows'] for s in ss)} trades"
                             + (f", ★{stars}" if stars else "") + ")")
            p(f"| {r.name} | " + " | ".join(cells) + " |")
        p("")
    # summary sizes
    p("### Summary sizes\n")
    p("| Person | Rule | Summaries | Trades in each (median / max) | Characters (median / max) | Lines left out to fit 4,096 |"
      " ★ unusual past the limit | ★ wait, hours (median / max) |")
    p("|---|---|---|---|---|---|---|---|")
    for uid, rs in results.items():
        for rn, b in rs.items():
            ss = [s for s in b.summaries if s["at"] >= start]
            if not ss:
                continue
            rows = sorted(s["rows"] for s in ss)
            ch = sorted(s["chars"] for s in ss)
            waits = sorted(w / 3600 for s in ss for w in s["waits"])
            wcell = f"{waits[len(waits) // 2]:.1f} / {waits[-1]:.1f}" if waits else "–"
            p(f"| {uid} | {rn} | {len(ss)} | {rows[len(rows) // 2]} / {rows[-1]} | {ch[len(ch) // 2]:,} / {ch[-1]:,} |"
              f" {sum(s['cut'] for s in ss)} | {len(waits)} | {wcell} |")
    p("")
    # sample summaries
    p("### Sample summaries (proposed format)\n")
    for uid, rn in (("owner (live follows: NM-P + 0xf2f6)", "fixed B 3, 7-day units, 6-h"),
                    ("owner (live follows: NM-P + 0xf2f6)", "fixed B 3, 7-day units, 12-h"),
                    ("hypothetical: owner + all 17 Recommended", "fixed B 3, 7-day units, 6-h"),
                    ("hypothetical: owner + all 17 Recommended", "fixed B 3, 7-day units, 12-h")):
        ss = [s for s in results[uid][rn].summaries if s["at"] >= start]
        if not ss:
            continue
        s = max(ss, key=lambda s: (s["stars"], s["rows"]))
        p(f"**{uid}, {rn}: {datetime.fromtimestamp(s['at'], UTC):%b %d %H:%M} UTC, {s['rows']} trades, {s['chars']:,} characters**\n")
        p("```\n" + s["text"].replace("<b>", "").replace("</b>", "") + "\n```\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
