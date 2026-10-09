"""The engine's routes, with the SDK's whale reads replaced by fixed answers (no network)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from uselayer import Trader, TraderDetail, Whales, WhalePosition, WhaleTrade

from whales_demo import app as engine

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
    monkeypatch.setattr(engine, "_paper", None)
    engine._copiers.clear()
    engine._recent.clear()
    engine._copied_cache.update(at=0.0, data=None)
    return TestClient(engine.app)


def test_pages_and_reads(api: TestClient) -> None:
    assert "Layer Whales" in api.get("/").text
    assert api.get("/api/top").json()[0]["name"] == "amy"
    assert api.get("/api/trading-now").json()[0]["trader"] == "amy"
    d = api.get("/api/trader/kalshi/amy").json()
    assert d["visibility"] == "visible" and d["trades"][0]["market"] == "KX-A"
    assert api.get("/api/links/kalshi/amy").json()["links"] == []


def test_copy_rules_are_saved_and_each_copied_trade_is_listed_with_its_result(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine.Copier, "poll", lambda self: [])
    monkeypatch.setattr(engine, "_copy_loop", lambda: None)
    r = api.put("/api/copy/rules", json={"size": 3, "max_above": 0.02}).json()
    assert r["rules"]["size"] == 3 and r["rules"]["traders"] == []
    r = api.post("/api/copy/traders", json={"venue": "polymarket", "id": "0xabc", "name": "sharpie", "categories": ["Sports"]}).json()
    r = api.post("/api/copy/traders", json={"venue": "kalshi", "id": "amy"}).json()
    assert [t["id"] for t in r["rules"]["traders"]] == ["0xabc", "amy"]
    cp = engine._copiers["polymarket:0xabc"]  # copied where they bet: Polymarket
    assert (cp.size, cp.venue, cp.max_slippage, cp.categories, cp.copy_sells) == (3, "polymarket", 0.02, ("Sports",), False)
    assert engine._copiers["kalshi:amy"].venue == "kalshi"
    assert engine.load_rules()["size"] == 3  # saved to disk
    assert [t["id"] for t in api.delete("/api/copy/traders/kalshi/amy").json()["rules"]["traders"]] == ["0xabc"]

    event = {"at": "2026-10-07T00:00:00+00:00", "status": "copied", "order_id": "o1", "venue": "kalshi",
             "market": "KX-A", "side": "yes", "source": {"title": "A game"}}  # fmt: skip
    engine.copied_file().write_text(json.dumps(event | {"trader": {"name": "sharpie"}}) + "\n")

    class Result:
        def to_dict(self) -> dict[str, Any]:
            return {"status": "won", "pnl": 2.5}

    monkeypatch.setattr(Whales, "copy_results", lambda self, ids: {i: Result() for i in ids})
    out = api.get("/api/copied").json()
    assert out["total"] == {"net": 2.5, "trades": 1, "open": 0, "won": 1, "lost": 0, "void": 0}
    assert out["rows"][0]["trader"]["name"] == "sharpie" and out["rows"][0]["result"]["status"] == "won"
    api.post("/api/paper/reset")
    assert api.get("/api/copied").json()["rows"] == []


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
    assert api.get("/api/worth/0xABC").json()["sample"] == [{"market": "m"}]
    monkeypatch.setattr(engine.reads.whales, "score", lambda w: _Score())
    assert api.get("/api/worth/0xnew").json()["wallet"] == "0xnew"


def test_bet_status_marks_settled_bets_instead_of_calling_them_bargains() -> None:
    s = engine.bet_status
    assert s(0.52, 0.525) == "in"  # half a cent above
    assert s(0.52, 0.55) == "in"  # exactly the 3¢ copying allows
    assert s(0.52, 0.30) == "in"  # cheaper, still open
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
    assert api.get("/api/trader/polymarket/0xabc").json()["positions"][1]["status"] == "lost"
    assert api.get("/static/fonts/HostGrotesk-latin-var.woff2").status_code == 200


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
            "outcome": "Jets", "at": "2026-10-09T20:00:00+00:00"}  # fmt: skip
    assert api.post("/api/trade/preview", json=body).json()["total"] == 2.71
    trade, kw = seen[0]
    assert (trade.venue, trade.market, trade.side, trade.action, trade.price) == ("polymarket", "0xcid:1", "yes", "buy", 0.52)
    assert kw == {"size": 5.0, "max_slippage": 0.03}  # the copy rules
    api.post("/api/trade/preview", json=body | {"spend": 50})
    assert seen[1][1] == {"spend": 50.0, "max_slippage": 0.03}  # an amount from the page, fee included
    assert api.post("/api/trade/preview", json=body | {"spend": 0}).status_code == 400
    out = api.post("/api/trade/copy", json=body).json()
    assert out["order_id"] == "pm-1" and out["trader"]["name"] == "ThorinCSGO"
    assert json.loads(engine.copied_file().read_text().splitlines()[-1])["order_id"] == "pm-1"  # in My trades
    api.post("/api/paper/reset")
