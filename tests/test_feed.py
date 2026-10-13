"""The Feed in two parts: Following (your followed traders' trades, signed in) and Recommended ($1,000 or more
from Traders' three sections). No network: Clerk's keys and the SDK are stand-ins."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from uselayer import Trader, TraderDetail, WhalePosition, WhaleTrade

from signals import app as engine

FAPI = "clerk.signals.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
COPY, RISE, WATCH, LUCKY, STRANGER = ("0x" + c * 40 for c in "abcde")


def auth(sub: str = "user_1") -> dict[str, str]:
    now = int(time.time())
    t = jwt.encode({"sub": sub, "iss": f"https://{FAPI}", "iat": now, "exp": now + 60, "azp": engine.SITE}, KEY, algorithm="RS256")
    return {"Authorization": f"Bearer {t}"}


def score(w: str, seg: str, copyable: str = "no", name: str = "x") -> dict[str, Any]:
    return {"wallet": w, "name": name, "segment": seg, "label": seg, "copyable": copyable, "copy": None}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    engine._cache.clear()
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "CLERK_KEY", "pk_test_" + base64.b64encode(f"{FAPI}$".encode()).decode().rstrip("="))
    monkeypatch.setattr(engine, "_signing_key", lambda t: KEY.public_key())
    monkeypatch.setattr(engine, "_categories", lambda markets: {m: "Politics" for m in markets})
    scores = [score(COPY, "proven", "yes", "Cora"), score(RISE, "rising", name="Rita"), score(WATCH, "too_fast", name="Walt"),
              score(LUCKY, "lucky", name="Lucky")]  # fmt: skip
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": scores})
    at = datetime.now(UTC)

    def detail(venue: str, w: str, **k: Any) -> TraderDetail:
        buys = [WhaleTrade("polymarket", w, None, f"{w[:6]}:{i}", "yes", "buy", 0.5, usd / 0.5, usd, at, f"{w}{i}", title="T")
                for i, usd in enumerate((150.0, 2500.0))]  # fmt: skip
        return TraderDetail(Trader(venue="polymarket", id=w, name=f"pm-{w[2:4]}"), "visible", {},
                            [WhalePosition(b.market, None, "Yes", b.size, 0.5, 0.51, None, None) for b in buys], buys)  # fmt: skip

    monkeypatch.setattr(engine.reads.whales, "trader", detail)
    return TestClient(engine.app)


def test_recommended_is_the_three_sections_and_bigger_trades_only(api: TestClient) -> None:
    r = api.get("/api/live").json()
    assert {t["wallet"] for t in r["traders"]} == {COPY, RISE, WATCH}  # Worth copying, Rising, Worth watching; never Lucky
    assert r["min_usd"] == engine.RECOMMENDED_MIN_USD == 1000
    assert {b["usd"] for b in r["bets"]} == {2500.0}  # the $150 buys stay out of Recommended
    assert {b["wallet"]: b["copy_ok"] for b in r["bets"]} == {COPY: True, RISE: False, WATCH: False}  # neutral cards for the rest


def test_following_is_your_traders_trades_every_size_and_only_yours(api: TestClient) -> None:
    assert api.get("/api/me/feed").status_code == 401  # signed out: the page shows a sign-in prompt
    for w in (WATCH, STRANGER):
        api.post("/api/me/follows", json={"wallet": w}, headers=auth())
    r = api.get("/api/me/feed", headers=auth())
    assert r.headers["cache-control"] == "private, no-store"
    body = r.json()
    assert [t["wallet"] for t in body["traders"]] == [WATCH, STRANGER]
    assert sorted(b["usd"] for b in body["bets"]) == [150.0, 150.0, 2500.0, 2500.0]  # every size
    stranger = next(b for b in body["bets"] if b["wallet"] == STRANGER)
    assert (stranger["name"], stranger["segment"], stranger["copy_ok"], stranger["status"]) == ("pm-ee", None, False, "in")
    assert api.get("/api/me/feed", headers=auth("user_2")).json()["bets"] == []  # someone else's follows aren't yours


def test_the_feed_page_has_following_and_recommended(api: TestClient) -> None:
    page = api.get("/").text
    for words in ('"Following"', '"Recommended"', "#/recommended", "Only trades I can still get into", "Also bought by",
                  "See the trades of traders you follow", "/api/me/feed", "Worth copying, Rising and Worth watching",
                  "All big trades right now", "3–5 min behind"):  # fmt: skip
        assert words in page, words
    assert '"Traders we follow"' not in page
