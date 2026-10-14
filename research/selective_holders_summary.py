import json
import statistics as st
import sys

H = json.load(open(sys.argv[1]))
H = [h for h in H if "m30" in h]
off = [h for h in H if not h["on_board"]]
print(f"holders read: {len(H)}; on the monthly profit top 1,000: {len(H) - len(off)}; off it: {len(off)}")


def bands(rows, label):
    n = len(rows)
    if not n:
        return
    freq = sum(1 for h in rows if h["m30"] >= 8)
    sel = sum(1 for h in rows if h["m30"] < 8 and h["m365"] >= 8)
    sparse = sum(1 for h in rows if h["m365"] < 8)
    print(f"  {label:34} n={n:4}  frequent (8+ in 30d) {freq / n:4.0%}  selective (<8 in 30d, 8+ in 365d) "
          f"{sel / n:4.0%}  under 8 in a year {sparse / n:4.0%}")


bands(H, "all holders")
bands(off, "off the leaderboard")
big = [h for h in off if h["usd_pos"] >= 10_000]
bands(big, "off the board, position >= 10K contracts")
for b in range(4):
    bands([h for h in off if h["band"] == b], f"off the board, market band {b} (0 = busiest)")
sel = [h for h in off if h["m30"] < 8 and h["m365"] >= 8]
if sel:
    print("\nselective holders off the board: single-market buys over 12 months")
    xs = sorted(h["m365"] for h in sel)
    print(f"  median {st.median(xs)}, 25th {xs[len(xs) // 4]}, 75th {xs[3 * len(xs) // 4]}")
    for k in (20, 40, 80, 180):
        print(f"  {k}+ markets in 12 months: {sum(1 for x in xs if x >= k)}/{len(xs)}")
    print(f"  median markets in 30 days: {st.median(h['m30'] for h in sel)}, in 90 days: {st.median(h['m90'] for h in sel)}")
