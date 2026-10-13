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
    assert body["traders"][1]["name"] == "pm-ee"  # their Polymarket name in the sheet too, as on their cards
    assert api.get("/api/me/feed", headers=auth("user_2")).json()["bets"] == []  # someone else's follows aren't yours


def test_the_feed_page_has_following_and_recommended(api: TestClient) -> None:
    page = api.get("/").text
    for words in ('"Following"', '"Recommended"', "#/recommended", "Only trades I can still get into", "Also bought by",
                  "See the trades of traders you follow", "/api/me/feed", "Worth copying, Rising and Worth watching",
                  "All big trades right now", "3–5 min behind"):  # fmt: skip
        assert words in page, words
    assert '"Traders we follow"' not in page


# ---- reads: one poller fills the cache, pages only read it, and misses on one key share one read ----


def test_concurrent_misses_on_one_key_share_one_read() -> None:
    import threading

    engine._cache.clear()
    calls, gate = [], threading.Event()

    def slow() -> str:
        calls.append(1)
        gate.wait(2)
        return "read"

    got: list[str] = []
    threads = [threading.Thread(target=lambda: got.append(engine.cached("k:one", 60, slow))) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join()
    assert got == ["read"] * 5 and len(calls) == 1


def test_the_poller_keeps_recommended_and_followed_traders_and_pages_only_read_it(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.post("/api/me/follows", json={"wallet": STRANGER}, headers=auth())
    asked: list[str] = []
    real = engine.reads.whales.trader
    monkeypatch.setattr(engine.reads.whales, "trader", lambda venue, w, **k: asked.append(w) or real(venue, w, **k))
    gone = "0x" + "9" * 40
    engine._cache[f"live:{gone}"] = (time.monotonic() - engine.LIVE_KEEP_S - 1, None)  # nobody wants it any more
    assert engine.live_poll_once() == 4
    assert sorted(asked) == sorted([COPY, RISE, WATCH, STRANGER])  # Recommended and followed, never Lucky
    assert f"live:{gone}" not in engine._cache
    asked.clear()
    api.get("/api/live")
    api.get("/api/me/feed", headers=auth())
    assert asked == []  # the pages read what the poller keeps
    engine.live_poll_once()
    assert asked == []  # read in the last 30 s: not again


def test_the_feed_page_rules(run_js: Any) -> None:
    got = run_js(("feed", "auth", "accountIs", "LEAD", "leadOf", "oneCardPerTrade"), """(() => {
      Object.assign(feed, { mine: { bets: [1] }, mineAt: 5, mineErr: "x" }); auth.uid = "user_1";
      accountIs("user_1"); const same = feed.mine !== null;
      accountIs("user_2"); const other = feed.mine === null && feed.mineAt === 0 && feed.mineErr === null && auth.uid === "user_2";
      feed.mine = { bets: [] }; accountIs(null); const out = feed.mine === null;
      const cards = oneCardPerTrade([
        { token: "t", status: "in", copy_ok: false, at: "2026-10-13T10:00:00Z", wallet: "0xa", name: "A" },
        { token: "t", status: "in", copy_ok: true, at: "2026-10-13T11:00:00Z", wallet: "0xb", name: "B" },
        { token: "t", status: "moved", copy_ok: true, at: "2026-10-13T09:00:00Z", wallet: "0xc", name: "C" }]);
      return [same, other, out, cards[0].wallet, cards[0].also.map((x) => x.wallet)];
    })()""")
    assert got == [True, True, True, "0xb", ["0xa", "0xc"]]


def test_one_polling_loop_however_often_the_feed_is_loaded(run_js: Any) -> None:
    got = run_js(("feed", "FEED_KEYS", "loadFeed"), """(async () => {
      let timers = 0, live = 0; const pending = [];
      globalThis.feedTimer = null; globalThis.auth = { uid: null };
      globalThis.setTimeout = () => ++timers; globalThis.clearTimeout = () => { timers = Math.max(0, timers - 1); };
      globalThis.feedView = () => "rec"; globalThis.onFeed = () => true; globalThis.renderFeed = () => {};
      globalThis.canFollowFeed = () => true;
      globalThis.api = () => new Promise((ok) => pending.push(ok));
      const a = loadFeed(), b = loadFeed();  // the listener calls it while a read is on its way
      pending[1]({ traders: [], bets: [] }); await b;
      pending[0]({ traders: [], bets: [], stale: true }); await a;
      return [timers, !!feed.data.stale];
    })()""")
    assert got == [1, False]  # one timer, and the stale answer was dropped
