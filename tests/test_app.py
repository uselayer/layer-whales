"""The engine's routes, with the SDK's whale reads replaced by fixed answers (no network)."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from uselayer import Trader, TraderDetail, Whales, WhalePosition, WhaleTrade

from signals import app as engine

AMY = Trader(venue="kalshi", id="amy", name="amy", rank=1, pnl=1000.0, volume=5000.0, volume_unit="contracts")
TRADE = WhaleTrade(
    "kalshi", "amy", "amy", "KX-A", "yes", "buy", 0.4, 10, 4.0, datetime(2026, 10, 7, tzinfo=UTC), "t1"
)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    w = engine.reads.whales
    monkeypatch.setattr(w, "top", lambda *a, **k: [AMY])
    monkeypatch.setattr(w, "big_trades", lambda *a, **k: [TRADE])
    monkeypatch.setattr(
        w, "trader", lambda *a, **k: TraderDetail(AMY, "visible", {"pnl": 1000.0}, [], [TRADE])
    )
    monkeypatch.setattr(w, "links", lambda *a, **k: [])
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "_boxes", {})
    return TestClient(engine.app)


def local() -> engine.Sandbox:
    return engine.sandbox(engine.LOCAL)


def with_kalshi(monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGNALS_VENUES=polymarket,kalshi: Signals reads Kalshi too (off by default)."""
    monkeypatch.setattr(engine, "VENUES", ("polymarket", "kalshi"))


def test_pages_and_reads(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    with_kalshi(monkeypatch)
    assert "Signals" in api.get("/").text
    assert 'href="/static/icon.svg"' in api.get("/").text  # the in-app logo as the browser icon
    for path in ("/static/icon.svg", "/favicon.ico"):
        r = api.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert api.get("/api/top").json()[0]["name"] == "amy"
    assert api.get("/api/trading-now").json()[0]["trader"] == "amy"
    d = api.get("/api/trader/kalshi/amy").json()
    assert d["visibility"] == "visible" and d["trades"][0]["market"] == "KX-A"
    assert api.get("/api/links/kalshi/amy").json()["links"] == []


def test_copy_rules_are_saved_and_each_copied_trade_is_listed_with_its_result(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_kalshi(monkeypatch)
    monkeypatch.setattr(engine.Copier, "poll", lambda self: [])
    monkeypatch.setattr(engine, "_copy_loop", lambda: None)
    r = api.put("/api/copy/rules", json={"size": 3, "max_above": 0.02}).json()
    assert r["rules"]["size"] == 3 and r["rules"]["traders"] == []
    r = api.post("/api/copy/traders", json={"venue": "polymarket", "id": "0xabc", "name": "sharpie", "categories": ["Sports"]}).json()
    r = api.post("/api/copy/traders", json={"venue": "kalshi", "id": "amy"}).json()
    assert [t["id"] for t in r["rules"]["traders"]] == ["0xabc", "amy"]
    cp = local().copiers["polymarket:0xabc"]  # copied where they bet: Polymarket
    assert (cp.size, cp.venue, cp.max_slippage, cp.categories, cp.copy_sells) == (3, "polymarket", 0.02, ("Sports",), False)
    assert local().copiers["kalshi:amy"].venue == "kalshi"
    assert local().load_rules()["size"] == 3  # saved to disk
    assert [t["id"] for t in api.delete("/api/copy/traders/kalshi/amy").json()["rules"]["traders"]] == ["0xabc"]

    event = {"at": "2026-10-07T00:00:00+00:00", "status": "copied", "order_id": "o1", "venue": "kalshi",
             "market": "KX-A", "side": "yes", "source": {"title": "A game"}}  # fmt: skip
    local().copied_file().write_text(json.dumps(event | {"trader": {"name": "sharpie"}}) + "\n")

    class Result:
        def to_dict(self) -> dict[str, Any]:
            return {"status": "won", "pnl": 2.5}

    monkeypatch.setattr(Whales, "copy_results", lambda self, ids: {i: Result() for i in ids})
    out = api.get("/api/copied").json()
    assert out["total"] == {"net": 2.5, "trades": 1, "open": 0, "won": 1, "lost": 0, "void": 0}
    assert out["rows"][0]["trader"]["name"] == "sharpie" and out["rows"][0]["result"]["status"] == "won"
    api.post("/api/paper/reset")
    assert api.get("/api/copied").json()["rows"] == []
    assert local().load_rules()["traders"] == [] and local().copiers == {}  # nobody copied any more
    assert local().load_rules()["size"] == 3  # the size and price limits stay


SCORE = {"wallet": "0xabc", "name": "sharpie", "segment": "quiet", "action": "follow", "sample": [{"market": "m"}]}


class _Score:
    def to_dict(self) -> dict[str, Any]:
        return SCORE | {"wallet": "0xnew"}


def test_worth_lists_the_last_check_without_bets_and_scores_new_wallets_on_demand(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "_worth_data", None)
    monkeypatch.setattr(engine, "worth_refresh", lambda: None)
    assert api.get("/api/worth").json()["result"] is None
    engine.STORE_DIR.joinpath("worth-following.json").write_text(
        '{"finished": "2026-10-07T00:00:00+00:00", "counts": {"quiet": 1}, "coverage": {}, "sources": {},'
        f' "scores": [{json.dumps(SCORE)}]}}'
    )
    r = api.get("/api/worth").json()["result"]
    assert r["counts"] == {"quiet": 1} and r["scores"][0]["name"] == "sharpie" and "sample" not in r["scores"][0]
    assert api.get("/api/worth/0xABC").json()["sample"] == [{"market": "m", "url": None}]
    monkeypatch.setattr(engine.reads.whales, "score", lambda w: _Score())
    assert api.get("/api/worth/0xnew").json()["wallet"] == "0xnew"


def test_worth_adds_the_leaderboard_columns_from_what_the_check_kept(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = SCORE | {
        "decisions": 60, "window": ["2026-09-07T00:00:00+00:00", "2026-10-07T00:00:00+00:00"],
        "copy": {"mean": 0.01, "margin": 0.02, "n": 40, "z": 1.0},
        "recent": [{"at": "2026-10-06T12:00:00+00:00"}],
        "sample": [{"usd": 10.0, "price": 0.4, "at": "2026-10-01T00:00:00+00:00"},
                   {"usd": 30.0, "price": 0.6, "at": "2026-10-02T00:00:00+00:00"},
                   {"usd": 500.0, "price": 0.5, "at": "2026-10-03T00:00:00+00:00"}],
    }  # fmt: skip
    monkeypatch.setattr(engine, "_worth_data", {"finished": "2026-10-07T00:00:00+00:00", "scores": [kept]})
    s = api.get("/api/worth").json()["result"]["scores"][0]
    assert s["typical_usd"] == 30.0  # the median buy: one big bet doesn't set it
    assert s["trades_per_day"] == 2.0  # 60 decisions over 30 days
    assert s["last_buy"] == "2026-10-06T12:00:00+00:00"
    assert s["copy_return"] == pytest.approx(0.02)  # 1¢ kept on a 50¢ buy
    assert "pnl_weeks" not in s and "sample_data" not in s  # profit history only from the SDK
    # a wallet scored on demand has no won/settled from the check: worked out from its checked bets
    settled = [{"payout": 1, "price": 0.6}, {"payout": 0, "price": 0.4}, {"payout": None, "price": 0.5}]
    assert engine._board(SCORE | {"sample": settled})["won"] == 1 and engine._board(SCORE | {"sample": settled})["settled"] == 2
    # the SDK's own figures win once it has them (uselayer 0.6)
    monkeypatch.setattr(engine, "_worth_data", {"finished": "2026-10-07T00:00:00+00:00",
                                                "scores": [kept | {"typical_usd": 99.0, "last_buy": "2026-10-07T00:00:00+00:00"}]})  # fmt: skip
    s = api.get("/api/worth").json()["result"]["scores"][0]
    assert (s["typical_usd"], s["last_buy"]) == (99.0, "2026-10-07T00:00:00+00:00")


def test_sample_profit_history_is_local_only(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_worth_data", {"finished": "2026-10-07T00:00:00+00:00", "scores": [SCORE | {"pnl_window": 500.0}]})
    monkeypatch.setenv("SIGNALS_MOCK", "1")
    s = api.get("/api/worth").json()["result"]["scores"][0]
    assert s["sample_data"] and len(s["pnl_weeks"]) == 12 and len(s["pnl_daily"]) == 91  # the SDK's shape
    assert s["pnl_daily"][0][1] == 0.0
    assert sum(s["pnl_weeks"]) == pytest.approx(s["pnl_daily"][-1][1] - s["pnl_daily"][-85][1], abs=0.1)  # weeks add up
    monkeypatch.setattr(engine, "HOSTED", True)  # usesignals.app: never made-up numbers for real traders
    s = api.get("/api/worth").json()["result"]["scores"][0]
    assert "sample_data" not in s and "pnl_weeks" not in s


def test_bet_status_marks_settled_bets_instead_of_calling_them_bargains() -> None:
    s = engine.bet_status
    assert s(0.52, 0.525) == "in"  # half a cent above
    assert s(0.52, 0.55) == "in"  # exactly the 3¢ copying allows
    assert s(0.52, 0.49) == "in"  # a little cheaper: still their trade, at a better price
    assert s(0.52, 0.47) == "in"  # exactly 5¢ below
    assert s(0.52, 0.30) == "moved"  # fell 22¢: the market turned against them, a different trade
    assert s(0.44, 0.07) == "moved"  # the 44¢ → 7¢ trade that showed "You can still get in"
    assert s(0.43, 0.48) == "moved"
    assert s(0.518, 0.001) == "lost"  # was "Yes, 51.8¢ cheaper now"
    assert s(0.435, 1.0) == "won"  # was "Price moved 56.5¢ up"
    assert s(0.5, 0.02) == "lost" and s(0.5, 0.98) == "won"
    assert s(0.735, None) == "closed"  # sold, or settled and paid out
    assert s(None, 0.5) is None


def test_live_bets_carry_status_and_category(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    at = datetime.now(UTC)

    def buy(m: str, p: float) -> WhaleTrade:
        return WhaleTrade("polymarket", "0xabc", "sharpie", m, "yes", "buy", p, 100, p * 100, at, m)

    def pos(m: str, now: float) -> WhalePosition:
        return WhalePosition(m, None, "Yes", 100, 0.5, now, None, None)

    monkeypatch.setattr(engine, "_worth_data", {"scores": [SCORE | {"segment": "proven", "label": "Proven sharps"}]})
    detail = TraderDetail(AMY, "visible", {}, [pos("c1:0", 0.51), pos("c2:0", 0.001)], [buy("c1:0", 0.5), buy("c2:0", 0.5)])
    monkeypatch.setattr(engine.reads.whales, "trader", lambda *a, **k: detail)

    class Book:
        def get(self, ids: Any) -> dict[str, Any]:
            return {c: {"tags": [{"label": "Sports"}]} for c in ids if c == "c1"}

    class Scorer:
        markets = Book()

    monkeypatch.setattr(engine.reads.whales, "scorer", lambda *a, **k: Scorer())
    r = api.get("/api/live").json()
    by = {b["market"]: b for b in r["bets"]}
    assert (by["c1:0"]["status"], by["c1:0"]["category"]) == ("in", "Sports")
    assert (by["c2:0"]["status"], by["c2:0"]["category"]) == ("lost", None)
    assert r["traders"][0]["segment"] == "proven"
    raw = [{"conditionId": "c1", "outcomeIndex": 0, "eventSlug": "ita-isr", "slug": "will-italy-win"}]
    monkeypatch.setattr(engine.reads.whales._poly, "positions", lambda *a, **k: raw)
    open_ = api.get("/api/trader/polymarket/0xabc").json()["positions"]
    assert open_[1]["status"] == "lost"
    assert [p["url"] for p in open_] == ["https://polymarket.com/event/ita-isr/will-italy-win", None]
    assert api.get("/static/fonts/HostGrotesk-latin-var.woff2").status_code == 200


def test_big_trades_carry_their_market_category(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The Feed's Big trades filter by your markets, like the rest of the Feed.
    poly = WhaleTrade("polymarket", "0xabc", None, "c1:0", "yes", "buy", 0.4, 1000, 400.0, datetime(2026, 10, 7, tzinfo=UTC), "p1")
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: [poly, TRADE])

    class Book:
        def get(self, ids: Any) -> dict[str, Any]:
            return {c: {"tags": [{"label": "Sports"}]} for c in ids}

    class Scorer:
        markets = Book()

    monkeypatch.setattr(engine.reads.whales, "scorer", lambda *a, **k: Scorer())
    with_kalshi(monkeypatch)
    rows = api.get("/api/trading-now").json()
    assert [(t["venue"], t["category"]) for t in rows] == [("polymarket", "Sports"), ("kalshi", None)]


def test_signals_reads_polymarket_only_unless_kalshi_is_switched_on(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The SDK covers both venues; Signals asks it for Polymarket only (SIGNALS_VENUES), never reads and hides.
    asked: list[Any] = []
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: asked.append(("big", k.get("venues"))) or [])
    monkeypatch.setattr(engine.reads.whales, "top", lambda venue, **k: asked.append(("top", venue)) or [])
    monkeypatch.setattr(engine.reads.whales, "trader", lambda *a, **k: asked.append(("trader", a[0])) or None)
    monkeypatch.setattr(engine.reads.whales, "links", lambda *a, **k: asked.append(("links",)) or [])
    assert engine.VENUES == ("polymarket",)  # the default
    assert api.get("/api/config").json() == {"venues": ["polymarket"]}
    api.get("/api/trading-now")
    api.get("/api/top")  # "all" means the venues Signals reads
    assert api.get("/api/top?venue=kalshi").status_code == 404
    assert api.get("/api/trader/kalshi/amy").status_code == 404
    assert api.get("/api/links/polymarket/0xabc").status_code == 404  # there's no other venue to check
    assert asked == [("big", ("polymarket",)), ("top", "polymarket")]  # Kalshi was never asked for
    r = api.post("/api/copy/traders", json={"venue": "kalshi", "id": "amy"})
    assert r.status_code == 400 and "Polymarket" in r.json()["detail"]

    # A Kalshi copy rule saved before stays saved but doesn't run, and My trades says why.
    monkeypatch.setattr(engine.Copier, "poll", lambda self: [])
    monkeypatch.setattr(engine, "_copy_loop", lambda: None)
    local().save_rules({"size": 5, "max_above": 0.03, "traders": [{"venue": "kalshi", "id": "amy"}]})
    local().apply_rules()
    assert local().copiers == {}
    rule = api.get("/api/copy").json()["rules"]["traders"][0]
    assert rule["id"] == "amy" and rule["error"].startswith("Paused")
    with_kalshi(monkeypatch)  # SIGNALS_VENUES=polymarket,kalshi: it runs again, nothing to bring back
    local().apply_rules()
    assert list(local().copiers) == ["kalshi:amy"]
    assert api.get("/api/config").json() == {"venues": ["polymarket", "kalshi"]}
    api.post("/api/paper/reset")


def test_the_feed_follows_recommended_traders_only(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # uselayer 0.6.0: a sharp who stopped buying is "inactive", and the feed doesn't follow them.
    scores = [
        SCORE | {"wallet": w, "segment": seg, "label": label}
        for w, seg, label in (("0xa", "rising", "Rising"), ("0xb", "inactive", "Not trading lately"))
    ]
    monkeypatch.setattr(engine, "_worth_data", {"scores": scores})
    monkeypatch.setattr(engine.reads.whales, "trader", lambda *a, **k: TraderDetail(AMY, "visible", {}, [], []))
    assert [t["wallet"] for t in api.get("/api/live").json()["traders"]] == ["0xa"]


def test_market_links_go_to_the_event_on_polymarket() -> None:
    u = engine.polymarket_url
    assert u("nfl-lar-sea") == "https://polymarket.com/event/nfl-lar-sea"
    assert u("nfl-lar-sea", "nfl-lar-sea") == "https://polymarket.com/event/nfl-lar-sea"  # a one-market event
    assert u("nfl-lar-sea", "spread-rams-3pt5") == "https://polymarket.com/event/nfl-lar-sea/spread-rams-3pt5"
    assert u(None) is None and u("0xabc123") is None  # no event, or a condition id standing in for one
    s = engine.with_links({"recent": [{"event": "ita-isr"}], "sample": [{"event": "0xdead"}]})
    assert s["recent"][0]["url"] == "https://polymarket.com/event/ita-isr" and s["sample"][0]["url"] is None


def test_copy_one_feed_trade_now_after_a_payout_preview(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_copy_loop", lambda: None)
    seen: list[Any] = []

    class Preview:
        def to_dict(self) -> dict[str, Any]:
            return {"ok": True, "price": 0.53, "total": 2.71, "payout": 5.0, "profit_if_win": 2.29}

    class Event:
        status = "copied"

        def to_dict(self) -> dict[str, Any]:
            return {"status": "copied", "order_id": "pm-1", "venue": "polymarket", "market": "0xcid:1",
                    "side": "yes", "at": "2026-10-09T00:00:00+00:00", "source": {"title": "Ducks vs. Jets"}}  # fmt: skip

    def preview(self: Any, trade: Any, **kw: Any) -> Preview:
        seen.append((trade, kw))
        return Preview()

    monkeypatch.setattr(Whales, "preview_copy", preview)
    monkeypatch.setattr(Whales, "copy_trade", lambda self, trade, **kw: Event())
    body = {"wallet": "0xabc", "name": "ThorinCSGO", "market": "0xcid:1", "price": 0.52, "title": "Ducks vs. Jets",
            "outcome": "Jets", "at": "2026-10-09T20:00:00+00:00", "token": "tok-jets"}  # fmt: skip
    assert api.post("/api/trade/preview", json=body).json()["total"] == 2.71
    trade, kw = seen[0]
    assert (trade.venue, trade.market, trade.side, trade.action, trade.price) == ("polymarket", "0xcid:1", "yes", "buy", 0.52)
    assert kw == {"size": 5.0, "max_slippage": 0.03}  # the copy rules
    assert trade.token == "tok-jets"  # the exact outcome token reaches the SDK
    api.post("/api/trade/preview", json=body | {"spend": 50})
    assert seen[1][1] == {"spend": 50.0, "max_slippage": 0.03}  # an amount from the page, fee included
    assert api.post("/api/trade/preview", json=body | {"spend": 0}).status_code == 400
    out = api.post("/api/trade/copy", json=body).json()
    assert out["order_id"] == "pm-1" and out["trader"]["name"] == "ThorinCSGO"
    assert json.loads(local().copied_file().read_text().splitlines()[-1])["order_id"] == "pm-1"  # in My trades
    api.post("/api/paper/reset")


def test_my_trades_has_a_copy_right_after_it_is_made(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class Event:
        status = "copied"

        def __init__(self, order_id: str) -> None:
            self.order_id = order_id

        def to_dict(self) -> dict[str, Any]:
            return {"status": "copied", "order_id": self.order_id}

    def rows(box: engine.Sandbox) -> dict[str, Any]:
        lines = box.copied_file().read_text().splitlines() if box.copied_file().exists() else []
        return {"rows": [json.loads(x)["order_id"] for x in lines]}

    monkeypatch.setattr(engine, "_copied_now", rows)
    who = {"venue": "polymarket", "id": "0xabc", "name": "sharpie"}
    assert api.get("/api/copied").json()["rows"] == []  # kept for 30 s from here
    local().record(Event("o1"), who)
    assert api.get("/api/copied").json()["rows"] == ["o1"]  # not the answer from before the copy

    def copy_while_reading(box: engine.Sandbox) -> dict[str, Any]:
        out = rows(box)
        box.record(Event("o2"), who)  # lands after this answer read the file
        return out

    monkeypatch.setattr(engine, "_copied_now", copy_while_reading)
    engine._copied_refresh(local())  # e.g. the background refresh, racing a copy
    monkeypatch.setattr(engine, "_copied_now", rows)
    assert api.get("/api/copied").json()["rows"] == ["o1", "o2"]


def test_my_trades_groups_repeat_copies_and_compares_with_theirs() -> None:
    def copy(at: str, size: float, usd: float, pnl: float | None, status: str = "won") -> dict[str, Any]:
        return {"at": at, "trader": {"id": "0xabc", "name": "ThorinCSGO"}, "venue": "polymarket", "market": "0xcid:1",
                "side": "yes", "source": {"title": "Ducks vs. Jets", "outcome": "Jets", "price": usd / size, "size": size, "usd": usd},
                "result": {"status": status, "contracts": 5.0, "avg_price": 0.53, "cost": 2.65, "fees": 0.06,
                           "payout": 1.0 if status == "won" else None, "mark": None, "pnl": pnl}}  # fmt: skip

    rows = [copy("2026-10-09T02:00:00+00:00", 800, 416, 2.29), copy("2026-10-09T01:00:00+00:00", 500, 260, 2.29)]
    (g,) = engine._compare(rows)  # two copies of the same outcome from the same trader: one group
    assert len(g["copies"]) == 2 and g["at"] == "2026-10-09T02:00:00+00:00"
    assert g["you"] == {"contracts": 10.0, "avg_price": 0.53, "fees": 0.12, "put_in": 5.42, "pnl": 4.58,
                        "return": pytest.approx(4.58 / 5.42, abs=1e-4), "status": "won", "value": 1.0}  # fmt: skip
    th = g["theirs"]
    assert th["price"] == pytest.approx(0.52) and th["contracts"] == 1300 and th["put_in"] == 676
    assert th["pnl"] == 624 and th["return"] == pytest.approx(1 / 0.52 - 1, abs=1e-4)
    assert th["at_their_price"] == pytest.approx(4.8)  # your 10 contracts at their 52¢

    feed_copy = copy("2026-10-09T03:00:00+00:00", 1, 0.25, None, status="open")  # sizes unknown, no bid yet
    feed_copy["source"] |= {"size": 0.0, "usd": 0.0, "price": 0.25}
    feed_copy["market"] = "0xother:0"
    g2 = engine._compare([feed_copy])[0]
    assert g2["theirs"]["price"] == 0.25 and g2["theirs"]["put_in"] is None and g2["theirs"]["return"] is None


def test_hosted_each_browser_gets_its_own_sandbox(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    with_kalshi(monkeypatch)
    monkeypatch.setattr(engine, "HOSTED", True)
    monkeypatch.setattr(engine.Copier, "poll", lambda self: [])
    monkeypatch.setattr(engine, "_copy_loop", lambda: None)
    monkeypatch.setattr(engine, "MAX_TRADERS", 2)
    visitors = engine.STORE_DIR / "visitors"
    amy, bo = (TestClient(engine.app, base_url="https://testserver") for _ in range(2))

    assert amy.get("/").headers["cache-control"] == "private, no-store"  # a cached page would share one id
    sid = amy.cookies[engine.SID_COOKIE]
    assert engine._SID.fullmatch(sid)
    assert amy.get("/api/paper").json()["net"] == 0.0 and not visitors.exists()  # looking makes no folder
    amy.post("/api/copy/traders", json={"venue": "kalshi", "id": "amy"})
    assert [t["id"] for t in amy.get("/api/copy").json()["rules"]["traders"]] == ["amy"]
    assert (visitors / sid / "copy-rules.json").exists()

    assert bo.get("/api/copy").json()["rules"]["traders"] == []  # bo's own sandbox, from his first call
    assert bo.cookies[engine.SID_COOKIE] != sid
    amy.post("/api/copy/traders", json={"venue": "kalshi", "id": "cy"})
    r = amy.post("/api/copy/traders", json={"venue": "kalshi", "id": "di"})
    assert r.status_code == 400 and "up to 2 traders" in r.json()["detail"]
    bo.post("/api/paper/reset")
    assert len(amy.get("/api/copy").json()["rules"]["traders"]) == 2  # bo's reset leaves amy's alone

    sneaky = TestClient(engine.app, base_url="https://testserver", cookies={engine.SID_COOKIE: "../../etc"})
    sneaky.get("/api/copy")
    assert all(s == engine.LOCAL or engine._SID.fullmatch(s) for s in engine._boxes)  # a made-up id gets a new one

    # A restart resumes copying for browsers here in the last two weeks, not for ones away longer.
    away = visitors / ("b" * 32) / "copy-rules.json"
    away.parent.mkdir()
    away.write_text(json.dumps({"size": 5, "max_above": 0.03, "traders": [{"venue": "kalshi", "id": "amy"}]}))
    os.utime(away, (time.time() - 15 * 86400,) * 2)
    engine._boxes.clear()
    engine.resume_copying()
    assert set(engine._boxes[sid].copiers) == {"kalshi:amy", "kalshi:cy"} and "b" * 32 not in engine._boxes
    engine._boxes[sid].seen -= 15 * 86400
    engine._tidy(time.time())
    assert sid not in engine._boxes  # away two weeks: copying stops until they're back


def test_copiers_of_one_trader_share_one_read(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(engine.reads.whales, "trades", lambda venue, id, limit=50: calls.append(id) or [TRADE])
    a, b = engine.Sandbox("a" * 32), engine.Sandbox("b" * 32)
    assert a.paper().whales.trades("kalshi", "amy") == b.paper().whales.trades("kalshi", "amy") == [TRADE]
    assert calls == ["amy"]
    a.close()
    b.close()
