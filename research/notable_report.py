"""Turn notable_replay.py's JSON into the readable results (Markdown): notification counts per person and day under
today's rule and each variant, real examples, what a rule might miss, and simulated summaries.

    uv run python research/notable_report.py research/notable-results.json --rule "new≥0.5× big≥2× rare≤3/day" \\
        > research/notable-results.md
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


def hm(t: float) -> str:
    return f"{datetime.fromtimestamp(t, UTC):%b %d %H:%M}"


def money(x: float | None) -> str:
    return "–" if x is None else f"${x:,.0f}"


def ratio(x: float | None) -> str:
    return "–" if x is None else f"{x:.1f}×"


def short(s: str, n: int = 48) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", type=Path)
    ap.add_argument("--rule", required=True, help="the variant the examples, misses and summaries are shown for")
    ap.add_argument("--also", nargs="*", default=[], help="more variants for the examples' columns")
    ap.add_argument("--detail", nargs="*", default=[], help="people to show examples and summaries for (default: all)")
    a = ap.parse_args()
    d = json.loads(a.results.read_text())
    names = d["names"]
    start = datetime.fromisoformat(d["window"][0]).replace(tzinfo=UTC).timestamp()
    events = {e["id"]: e for e in d["events"] if e["round"] >= start}
    nm = lambda w: names.get(w, w[:8])  # noqa: E731
    out: list[str] = []
    p = out.append

    p(f"# Notable vs routine: replay {d['window'][0]} → {d['window'][1]} UTC\n")
    p("## Followed traders\n")
    p("| Trader | Alerts a day (30 d or less, buys / sells) | Typical buy | Typical sell | Replay: buys / sells | new positions | ≥2× typical | ≤3/day this big |")
    p("|---|---|---|---|---|---|---|---|")
    by_trader: dict[str, list[dict]] = defaultdict(list)
    for e in events.values():
        by_trader[e["trader"]].append(e)
    for w, st in sorted(d["trader_stats"].items(), key=lambda kv: -len(by_trader.get(kv[0], []))):
        evs = by_trader.get(w, [])
        b = [e for e in evs if e["side"] == "buy"]
        s = [e for e in evs if e["side"] == "sell"]
        tb = statistics.median([e["typical"] for e in b if e["typical"]]) if any(e["typical"] for e in b) else None
        ts = statistics.median([e["typical"] for e in s if e["typical"]]) if any(e["typical"] for e in s) else None
        a30 = st.get("alerts_30d", {})
        days = st.get("history_days", 30)
        p(f"| {nm(w)}{'' if days >= 29 else f' ({days:.0f} d)'} | {a30.get('buy', 0) / days:.1f} / {a30.get('sell', 0) / days:.1f} | {money(tb)} | {money(ts)} | {len(b)} / {len(s)}"
          f" | {sum(1 for e in b if e['new'])} | {sum(1 for e in evs if (e['ratio'] or 0) >= 2)}"
          f" | {sum(1 for e in evs if e['per_day_as_big'] is not None and e['per_day_as_big'] <= 3)} |")  # fmt: skip
    p("")

    p("## Notifications a day\n")
    p("Immediate = buy, sell and group alerts sent now. Summaries count 1 each toward the 20. Dropped = past the 20-a-day cap.\n")
    for uid, rules in d["people"].items():
        p(f"### {uid}\n")
        days = sorted({day for r in rules.values() for day in r["days"]})
        p("| Rule | " + " | ".join(days) + " |")
        p("|---|" + "---|" * len(days))
        for rn, r in rules.items():
            cells = []
            for day in days:
                c = r["days"].get(day, {})
                imm = c.get("buy", 0) + c.get("sell", 0) + c.get("group", 0)
                cell = f"{imm} now + {c.get('summary', 0)} sum ({c.get('summary_items', 0)} in them)"
                if c.get("dropped"):
                    cell += f", **cap {c['cap_at'][-5:]}, {c['dropped']} dropped**"
                elif c.get("cap_at"):
                    cell += f", cap {c['cap_at'][-5:]}"
                cells.append(cell)
            p(f"| {rn} | " + " | ".join(cells) + " |")
        p("")

    def detail(owner: str) -> None:
        """Examples, what the rule might miss, and summaries, for one person."""
        rules = d["people"][owner]
        show = ["today", a.rule, *a.also]
        logs = {rn: {int(k): v for k, v in rules[rn]["log"].items()} for rn in show if rn in rules}

        def reason_of(rn: str, e: dict) -> str:
            """Why the variant called it notable (recomputed for display)."""
            if e["side"] == "buy":
                bits = []
                if e["new"]:
                    bits.append("new")
                if (e["ratio"] or 0) >= 2:
                    bits.append("≥2×")
                if e["per_day_as_big"] is not None:
                    bits.append(f"{e['per_day_as_big']:.1f}/day this big")
                return ", ".join(bits)
            bits = ["full exit" if e["exit"] else "partial"]
            if (e["ratio"] or 0) >= 2:
                bits.append("≥2×")
            if e["per_day_as_big"] is not None:
                bits.append(f"{e['per_day_as_big']:.1f}/day this big")
            return ", ".join(bits)

        def row(e: dict) -> str:
            cols = [nm(e["trader"]), hm(e["at"]), e["side"], f"{e['outcome_name']} · {short(e['title'])}", money(e["usd"]),
                    ratio(e["ratio"]), ("new" if e["new"] else "top-up") if e["side"] == "buy" else ("exit" if e["exit"] else "trim"),
                    reason_of(a.rule, e)] + [logs[rn].get(e["id"], "–") for rn in logs]  # fmt: skip
            return "| " + " | ".join(cols) + " |"

        head = "| Trader | Time (UTC) | Side | Outcome · market | $ | vs typical | Kind | Signals | " + " | ".join(logs) + " |"
        sep = "|---|---|---|---|---|---|---|---|" + "---|" * len(logs)

        p(f"## Examples ({owner})\n")
        p(f"What happened to each under today's rule and under `{a.rule}`" + (f" and {', '.join(a.also)}" if a.also else "") + ".\n")
        p(head)
        p(sep)
        picked: list[dict] = []
        for w, evs in sorted(by_trader.items(), key=lambda kv: -len(kv[1])):
            mine = [e for e in evs if e["id"] in logs["today"] or e["id"] in logs[a.rule]]
            if not mine:
                continue
            now_new = [e for e in mine if logs[a.rule].get(e["id"], "").startswith("now")]
            now_today_only = [e for e in mine if logs["today"].get(e["id"], "").startswith("now")
                              and not logs[a.rule].get(e["id"], "").startswith("now")]  # fmt: skip
            rest = sorted(mine, key=lambda e: -(e["usd"] or 0))
            for e in (now_new[:2] + now_today_only[:1] + rest[:1])[:4]:
                if e not in picked:
                    picked.append(e)
        for e in sorted(picked, key=lambda e: e["at"])[:24]:
            p(row(e))
        p("")

        p(f"## What `{a.rule}` might miss ({owner})\n")
        lg = logs[a.rule]
        summarized = [events[i] for i, v in lg.items() if i in events and v.startswith("summary")]
        cats = {
            "Big top-ups (≥1.5× typical, already held or bought lately)": [e for e in summarized if e["side"] == "buy" and not e["new"] and (e["ratio"] or 0) >= 1.5],
            "New positions just under the floor (0.35–0.5× typical)": [e for e in summarized if e["side"] == "buy" and e["new"] and 0.35 <= (e["ratio"] or 0) < 0.5],
            "Big buys too common for that trader to ping (≥2× typical, but more than the budget a day)": [e for e in summarized if e["side"] == "buy" and (e["ratio"] or 0) >= 2],
            "Big trims (≥1.5× their typical sell, still holding)": [e for e in summarized if e["side"] == "sell" and not e["exit"] and (e["ratio"] or 0) >= 1.5],
            "Exits summarized": [e for e in summarized if e["side"] == "sell" and e["exit"]],
        }
        for title, es in cats.items():
            p(f"**{title}:** {len(es)}" + (f", biggest {money(max(e['usd'] for e in es))}" if es else ""))
            for e in sorted(es, key=lambda e: -e["usd"])[:3]:
                p(f"- {nm(e['trader'])} {hm(e['at'])} {e['side']} {e['outcome_name']} · {short(e['title'], 60)}: {money(e['usd'])}, {ratio(e['ratio'])} typical, {reason_of(a.rule, e)}")
            p("")
        lt = logs["today"]
        lost = [events[i] for i, v in lt.items() if i in events and v.startswith("now") and not lg.get(i, "").startswith("now")]
        gained = [events[i] for i, v in lg.items() if i in events and v.startswith("now") and not lt.get(i, "").startswith("now")]
        p(f"**Sent now today, not under `{a.rule}`:** {len(lost)}")
        for e in sorted(lost, key=lambda e: -e["usd"])[:5]:
            p(f"- {nm(e['trader'])} {hm(e['at'])} {e['side']} {e['outcome_name']} · {short(e['title'], 60)}: {money(e['usd'])}, {ratio(e['ratio'])}, {reason_of(a.rule, e)} → {lg.get(e['id'])}")
        p(f"\n**Sent now under `{a.rule}`, not today:** {len(gained)}")
        for e in sorted(gained, key=lambda e: -e["usd"])[:5]:
            p(f"- {nm(e['trader'])} {hm(e['at'])} {e['side']} {e['outcome_name']} · {short(e['title'], 60)}: {money(e['usd'])}, {ratio(e['ratio'])}, {reason_of(a.rule, e)} (today: {lt.get(e['id'])})")
        p("")

        p(f"## Simulated summaries ({owner})\n")
        for rn in ("today", a.rule):
            ss = rules[rn]["summaries"]
            if not ss:
                continue
            p(f"### {rn}\n")
            for s in sorted(ss, key=lambda s: -s["rows"])[:1] + ss[:1]:
                p(f"_{hm(s['at'])} UTC, {s['rows']} trades{'' if s['sent'] else ', past the cap: not sent'}_\n")
                p("```\n" + s["text"].replace("<b>", "").replace("</b>", "") + "\n```\n")
                if rn != "today":
                    p("With each trader's biggest trade named (a proposed format):\n")
                    p("```\n" + s["text_plus"].replace("<b>", "").replace("</b>", "") + "\n```\n")

    for owner in a.detail or [u for u in d["people"] if u != "(NM-P alone)"]:
        detail(owner)
    print("\n".join(out))


if __name__ == "__main__":
    main()
