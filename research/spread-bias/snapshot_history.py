# Read-only: score headline figures from production directory.db, for the false-positive check.
# No result blobs, no names, no follows or accounts. Prints zlib+base64 JSON to stdout.
#   railway ssh --service signals -- python3 - < snapshot_history.py > hist.b64
import base64, json, sqlite3, zlib

con = sqlite3.connect("file:/data/directory.db?mode=ro", uri=True)
hist = con.execute("SELECT wallet, scored_at, via, segment, skill, bets, edge, edge_z, trading_pnl_window "
                   "FROM score_history ORDER BY wallet, scored_at").fetchall()
scores = con.execute("SELECT wallet, scored_at, via, format, segment, skill, style, bets, decisions, edge, edge_z, "
                     "trading_pnl_window, pnl, volume, active, window_start, window_end FROM scores").fetchall()
cats = con.execute("SELECT wallet, category, bets, edge, edge_z, strong FROM score_categories").fetchall()
out = {"history": hist, "scores": scores, "categories": cats,
       "counts": {"wallets": con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]}}
print(base64.b64encode(zlib.compress(json.dumps(out).encode(), 9)).decode())
