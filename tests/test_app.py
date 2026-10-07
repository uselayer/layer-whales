"""The engine's routes, with the SDK's whale reads replaced by fixed answers (no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from uselayer import Trader, TraderDetail, WhaleTrade

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
    return TestClient(engine.app)


def test_pages_and_reads(api: TestClient) -> None:
    assert "Layer Whales" in api.get("/").text
    assert api.get("/api/top").json()[0]["name"] == "amy"
    assert api.get("/api/trading-now").json()[0]["trader"] == "amy"
    d = api.get("/api/trader/kalshi/amy").json()
    assert d["visibility"] == "visible" and d["trades"][0]["market"] == "KX-A"
    assert api.get("/api/links/kalshi/amy").json()["links"] == []


def test_follow_and_stop(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine.Copier, "poll", lambda self: [])
    r = api.post("/api/follow", json={"venue": "kalshi", "id": "amy", "size": 3}).json()
    assert r["following"]["id"] == "amy" and r["size"] == 3 and r["copy_to"] == "polymarket_us"
    assert api.get("/api/follow").json()["following"]["id"] == "amy"
    assert api.delete("/api/follow").json()["following"] is None
    assert api.get("/api/paper").json()["net"] == 0
