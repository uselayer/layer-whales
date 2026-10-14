"""Check the replay against what production actually did: the alert-worthy buys it found (alerts.db ``buys``: one
row per buy delivered to a person, written before the cap or the per-trader limit is checked) and the alerts it
sent (``alerts``), from a read-only dump of production's alerts.db (not kept in the repo: it has account ids).

Stage 1 (notable_replay.trader_events) is started when the person followed the traders, as production started
watching them then (their earlier trades only noted), and its buys are matched to production's by trader and outcome
token, first fill within 2 minutes. Then delivery under the rule production ran then (before #98: no per-trader
limit) is compared with the alerts production sent.

    uv run python research/notable_calibrate.py <alertsdb.json> --from 2026-10-14T03:54 --to 2026-10-14T16:00
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from notable_replay import CACHE, Person, Rule, deliver_all, trader_events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", type=Path)
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    a = ap.parse_args()
    start = datetime.fromisoformat(a.start).replace(tzinfo=UTC).timestamp()
    end = datetime.fromisoformat(a.end).replace(tzinfo=UTC).timestamp()
    d = json.loads(a.dump.read_text())
    cats = json.loads((CACHE / "categories.json").read_text())
    prod = [b for b in d["buys"] if start <= b["at"] <= end]
    traders = sorted({b["trader"] for b in d["buys"]})
    events = []
    for w in traders:
        events += trader_events(w, start, end, cats)[0]
    buys = [e for e in events if e.side == "buy"]
    print(f"window {a.start} → {a.end} UTC, traders {', '.join(w[:10] for w in traders)}")
    print(f"alert-worthy buys: production {len(prod)}, replay {len(buys)}")
    used: set[int] = set()
    matched, diffs, usd_ratio = 0, [], []
    for e in buys:
        hit = next((i for i, b in enumerate(prod) if i not in used and b["trader"] == e.trader and b["outcome"] == e.outcome
                    and abs(b["at"] - e.at) <= 120), None)  # fmt: skip
        if hit is not None:
            used.add(hit)
            matched += 1
            diffs.append(prod[hit]["at"] - e.at)
            if prod[hit]["usd"]:
                usd_ratio.append(e.usd / prod[hit]["usd"])
    print(f"matched (same trader and outcome, first fill within 2 min): {matched}")
    print(f"replay only: {len(buys) - matched}; production only: {len(prod) - len(used)}")
    if usd_ratio:
        usd_ratio.sort()
        print(f"replay $ / production $ on matched: median {usd_ratio[len(usd_ratio) // 2]:.3f},"
              f" share within 1%: {sum(1 for r in usd_ratio if abs(r - 1) <= 0.01) / len(usd_ratio):.0%}")
    po = [prod[i] for i in range(len(prod)) if i not in used]
    if po:
        print("production-only by hour:", sorted(Counter(datetime.fromtimestamp(b['at'], UTC).strftime('%H') for b in po).items()))
    sent = [x for x in d["alerts"] if start <= x["sent_at"] <= end]
    p = deliver_all(Person("calibration", traders), Rule("before #98", today=True, trader_daily=None), events, {}, end)
    cap_prod = next((x["sent_at"] for x in sent if x["kind"] == "cap"), None)
    f = lambda t: datetime.fromtimestamp(t, UTC).strftime("%H:%M") if t else "never"  # noqa: E731
    print(f"alerts: production {sum(1 for x in sent if x['kind'] != 'cap')} ({Counter((x['trader'] or '-')[:10] for x in sent if x['kind'] != 'cap')}),"
          f" cap message {f(cap_prod)}")  # fmt: skip
    print(f"alerts: replay {len(p.alerts)} ({Counter((x['trader'] or '-')[:10] for x in p.alerts)}),"
          f" cap reached {f(next(iter(p.cap_days.values()), None))}")  # fmt: skip
    first_prod = [(f(x["sent_at"]), (x["trader"] or "")[:6], (x["outcome"] or "")[-6:]) for x in sent if x["kind"] == "buy"]
    first_rep = [(f(x["sent_at"]), (x["trader"] or "")[:6], (x["outcome"] or "")[-6:]) for x in p.alerts]
    same = len({x[1:] for x in first_prod} & {x[1:] for x in first_rep})
    print(f"the 20 alerts: {same} of production's {len(first_prod)} are the same trader and outcome in the replay")


if __name__ == "__main__":
    main()
