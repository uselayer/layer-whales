"""The engine's routes, with the SDK's whale reads replaced by fixed answers (no network)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from uselayer import Trader, TraderDetail, Whales, WhaleTrade

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
    r = api.put("/api/copy/rules", json={"size": 3, "max_above": 0.02, "copy_to": "kalshi"}).json()
    assert r["rules"]["size"] == 3 and r["rules"]["traders"] == []
    r = api.post("/api/copy/traders", json={"venue": "polymarket", "id": "0xabc", "name": "sharpie", "categories": ["Sports"]}).json()
    r = api.post("/api/copy/traders", json={"venue": "kalshi", "id": "amy"}).json()
    assert [t["id"] for t in r["rules"]["traders"]] == ["0xabc", "amy"]
    cp = engine._copiers["polymarket:0xabc"]
    assert (cp.size, cp.venue, cp.max_slippage, cp.categories, cp.copy_sells) == (3, "kalshi", 0.02, ("Sports",), False)
    assert engine.load_rules()["copy_to"] == "kalshi"  # saved to disk
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
