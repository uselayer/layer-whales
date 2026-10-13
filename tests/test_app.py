"""The engine's routes, with the SDK's whale reads replaced by fixed answers (no network)."""

from __future__ import annotations

import json
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
    # The last check found 0xabc copyable: the cost preview only prices a trader the page offers copying for.
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": [COPYABLE]})
    monkeypatch.setattr(engine, "_worth_one", {})
    return TestClient(engine.app)


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


SCORE = {"wallet": "0xabc", "name": "sharpie", "segment": "quiet", "action": "follow", "sample": [{"market": "m"}]}
COPYABLE = SCORE | {"label": "Quiet sharps", "copyable": "yes", "copy": {"mean": 0.02, "margin": 0.01, "n": 30, "z": 4.0, "low": 0.01, "high": 0.03}}


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
    assert s["trades_per_day"] == 2.0 and s["covered_days"] == 30  # 60 decisions over 30 days
    # a busy trader: the SDK's 5,000 fills (uselayer 0.8 says from when) cover only the last 10 days
    for read_from in ("2026-09-27T00:00:00+00:00", datetime(2026, 9, 27, tzinfo=UTC).timestamp()):
        busy = engine._board(kept | {"fills_from": read_from})
        assert (busy["trades_per_day"], busy["covered_days"]) == (6.0, 10)
    assert engine._board(kept | {"fills_from": "2026-08-01T00:00:00+00:00"})["covered_days"] == 30  # never past the window
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


def test_trader_pages_ask_for_closed_positions_and_the_feed_does_not(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[dict[str, Any]] = []

    def trader(*a: Any, **k: Any) -> TraderDetail:
        asked.append(k)
        return TraderDetail(AMY, "visible", {"pnl": 1000.0}, [], [TRADE])

    monkeypatch.setattr(engine.reads.whales, "trader", trader)
    api.get("/api/trader/polymarket/0xabc")
    assert asked[-1]["closed"] == 50  # the profile's Closed positions (uselayer 0.7)
    monkeypatch.setattr(engine, "_worth_data", {"scores": [SCORE | {"segment": "proven", "label": "Proven sharps"}]})
    monkeypatch.setattr(engine, "_categories", lambda markets: {})
    api.get("/api/live")
    assert asked[-1]["closed"] == 0  # the feed polls every 25 s: no closed-positions read


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
    assert api.get("/api/config").json() == {"venues": ["polymarket"], "clerk_key": None, "alerts": False}
    api.get("/api/trading-now")
    api.get("/api/top")  # "all" means the venues Signals reads
    assert api.get("/api/top?venue=kalshi").status_code == 404
    assert api.get("/api/trader/kalshi/amy").status_code == 404
    assert api.get("/api/links/polymarket/0xabc").status_code == 404  # there's no other venue to check
    assert asked == [("big", ("polymarket",)), ("top", "polymarket")]  # Kalshi was never asked for
    with_kalshi(monkeypatch)  # SIGNALS_VENUES=polymarket,kalshi
    assert api.get("/api/config").json()["venues"] == ["polymarket", "kalshi"]


def test_the_sandbox_is_gone(api: TestClient) -> None:
    # Signals places no orders and keeps no sandbox: the copy, My trades and reset routes are gone, and the
    # page sets no browser cookie (there's nothing of yours to find).
    for method, path in (("get", "/api/copy"), ("post", "/api/copy/traders"), ("post", "/api/trade/copy"),
                         ("get", "/api/copied"), ("get", "/api/paper"), ("post", "/api/paper/reset")):  # fmt: skip
        assert getattr(api, method)(path).status_code in (404, 405), path
    assert "set-cookie" not in api.get("/").headers


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


def test_what_copying_one_feed_trade_costs_now(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    class Preview:
        ok, price = True, 0.53

        def to_dict(self) -> dict[str, Any]:
            return {"ok": True, "price": 0.53, "total": 2.71, "payout": 5.0, "profit_if_win": 2.29}

    def preview(self: Any, trade: Any, **kw: Any) -> Preview:
        seen.append((self, trade, kw))
        return Preview()

    monkeypatch.setattr(Whales, "preview_copy", preview)
    body = {"wallet": "0xabc", "name": "ThorinCSGO", "market": "0xcid:1", "price": 0.52, "title": "Ducks vs. Jets",
            "outcome": "Jets", "at": "2026-10-09T20:00:00+00:00", "token": "tok-jets"}  # fmt: skip
    assert api.post("/api/trade/preview", json=body).json()["total"] == 2.71
    whales, trade, kw = seen[0]
    assert whales is engine.reads.whales  # a read: no sandbox, no store of yours
    assert (trade.venue, trade.market, trade.side, trade.action, trade.price) == ("polymarket", "0xcid:1", "yes", "buy", 0.52)
    assert kw == {"size": 5.0, "max_slippage": 0.03}  # no amount: 5 contracts, at most 3¢ above their price
    assert trade.token == "tok-jets"  # the exact outcome token reaches the SDK
    api.post("/api/trade/preview", json=body | {"spend": 50})
    assert seen[1][2] == {"spend": 50.0, "max_slippage": 0.03}  # an amount from the page, fee included
    assert api.post("/api/trade/preview", json=body | {"spend": 0}).status_code == 400
    assert not list(engine.STORE_DIR.iterdir())  # nothing kept


def test_the_preview_refuses_prices_you_cant_still_get_in_at(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The SDK only refuses a price more than 3¢ above theirs. The engine also refuses a settled market (2¢ or
    # less, 98¢ or more) and one more than 5¢ below what they paid, by the Feed's own rule (bet_status).
    now = {"price": 0.5}

    class Preview:
        ok = True

        @property
        def price(self) -> float:
            return now["price"]

        def to_dict(self) -> dict[str, Any]:
            return {"ok": True, "price": now["price"]}

    monkeypatch.setattr(Whales, "preview_copy", lambda self, trade, **kw: Preview())
    body = {"wallet": "0xabc", "market": "0xcid:1", "price": 0.60}
    for price, words in ((0.985, "98¢ or more"), (0.02, "2¢ or less"), (0.54, "6¢ below"), (0.64, "4¢ above")):
        now["price"] = price
        r = api.post("/api/trade/preview", json=body)
        assert r.status_code == 400 and words in r.json()["detail"], (price, r.text)
    assert engine.why_not_copy(0.97, 0.99) is not None  # their price was fine, the market's isn't any more
    now["price"] = 0.56  # 4¢ below: you can still get in
    assert api.post("/api/trade/preview", json=body).json()["price"] == 0.56


def test_why_not_copy_is_bet_status() -> None:
    # One rule: whatever the Feed calls "You can still get in", the engine prices, and nothing else.
    for paid in (0.05, 0.3, 0.6, 0.95):
        for now in [x / 200 for x in range(0, 201)]:
            assert (engine.why_not_copy(paid, now) is None) == (engine.bet_status(paid, now) == "in"), (paid, now)
    assert engine.why_not_copy(0.5, None) is not None


def _scored(segment: str, copyable: str, wallet: str = "0xabc") -> dict[str, Any]:
    return SCORE | {"wallet": wallet, "segment": segment, "label": segment, "copyable": copyable}


def test_copying_is_offered_only_for_recommended_tiers_with_copies_reliably_above_zero() -> None:
    def offer(seg: str, cp: str) -> tuple[bool, str | None]:
        return engine.copy_offer(_scored(seg, cp))

    assert offer("proven", "yes") == (True, None) and offer("quiet", "yes")[0] and offer("rising", "yes")[0]
    for seg in ("too_fast", "lucky", "no_edge", "no_view", "inactive"):
        ok, why = offer(seg, "yes")  # a good copy result never rescues another tier
        assert not ok and why
    assert "reliably above zero" in (offer("too_fast", "no")[1] or "")
    assert not offer("rising", "no")[0] and "reliably above zero" in (offer("rising", "no")[1] or "")
    assert not offer("rising", "unknown")[0] and "Too few" in (offer("rising", "unknown")[1] or "")
    assert not engine.copy_offer(SCORE)[0]  # no copyable verdict at all: not offered


def test_the_preview_refuses_a_trader_the_page_doesnt_offer_copying_for(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": [
        COPYABLE, _scored("too_fast", "no", "0xfast"), _scored("no_edge", "yes", "0xmeh")]})  # fmt: skip
    monkeypatch.setattr(Whales, "preview_copy", lambda self, trade, **kw: pytest.fail("previewed"))
    for wallet, words in (("0xfast", "No proven copy advantage"), ("0xmeh", "Not a trader we recommend")):
        r = api.post("/api/trade/preview", json={"wallet": wallet, "market": "0xcid:1", "price": 0.5})
        assert r.status_code == 400 and words in r.json()["detail"]

    class Lucky:
        def __init__(self, w: str) -> None:
            self.w = w

        def to_dict(self) -> dict[str, Any]:
            return _scored("lucky", "yes", self.w)

    # A wallet nobody has scored yet is scored first, then judged by the same rule.
    monkeypatch.setattr(engine.reads.whales, "score", Lucky)
    r = api.post("/api/trade/preview", json={"wallet": "0xnew", "market": "0xcid:1", "price": 0.5})
    assert r.status_code == 400 and "Not a trader we recommend" in r.json()["detail"]


def test_traders_and_the_feed_carry_the_same_copy_verdict(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    scores = [COPYABLE, _scored("rising", "no", "0xr")]
    monkeypatch.setattr(engine, "_worth_data",
                        {"finished": "2026-10-07T00:00:00+00:00", "format": engine.WORTH_FORMAT, "scores": scores})  # fmt: skip
    board = {s["wallet"]: s for s in api.get("/api/worth").json()["result"]["scores"]}
    assert board["0xabc"]["copy_ok"] is True and board["0xr"]["copy_ok"] is False and board["0xr"]["copy_why"]
    assert api.get("/api/worth/0xr").json()["copy_ok"] is False  # a trader's page
    buy = WhaleTrade("polymarket", "x", "x", "0xc:0", "yes", "buy", 0.4, 10, 4.0, datetime.now(UTC), "t9", title="A")
    monkeypatch.setattr(engine.reads.whales, "trader", lambda *a, **k: TraderDetail(AMY, "visible", {}, [], [buy]))
    monkeypatch.setattr(engine, "_categories", lambda markets: {})
    live = api.get("/api/live").json()
    assert {t["wallet"]: t["copy_ok"] for t in live["traders"]} == {"0xabc": True, "0xr": False}
    assert {b["wallet"]: b["copy_ok"] for b in live["bets"]} == {"0xabc": True, "0xr": False}  # the Feed's buttons


def test_a_result_kept_before_uselayer_0_8_is_judged_by_todays_copy_rule() -> None:
    old = {"format": 5, "scores": [
        # copies +1¢ ± 1.3¢ on 9 bets (z 1.5): copyable by the old rule, not by today's
        SCORE | {"wallet": "0xa", "segment": "quiet", "copyable": "yes",
                 "copy": {"mean": 0.01, "margin": 0.01307, "n": 9, "z": 1.5}},
        # copies +2¢ ± 0.5¢ on 40 bets: still reliably above zero with Student's t
        SCORE | {"wallet": "0xb", "segment": "proven", "copyable": "yes",
                 "copy": {"mean": 0.02, "margin": 0.005, "n": 40, "z": 7.8}},
        # not copyable before: never promoted
        SCORE | {"wallet": "0xc", "segment": "too_fast", "copyable": "no",
                 "copy": {"mean": 0.03, "margin": 0.01, "n": 40, "z": 5.9}},
    ]}  # fmt: skip
    a, b, c = engine._worth_upgrade(json.loads(json.dumps(old)))["scores"]
    assert (a["segment"], a["label"], a["copyable"]) == ("too_fast", "Sharp but too fast", "no")
    assert a["copy"]["margin"] > 0.01307 and a["copy"]["low"] < 0  # widened to t for 8 degrees of freedom
    assert (b["segment"], b["copyable"]) == ("proven", "yes") and b["copy"]["low"] > 0
    assert (c["segment"], c["copyable"]) == ("too_fast", "no")
    new = {"format": 6, "scores": [dict(old["scores"][0])]}
    assert engine._worth_upgrade(new)["scores"][0]["segment"] == "quiet"  # a current result is left as the SDK gave it


# ---- ranking: small samples don't top a sort, and missing weeks aren't losing weeks ----


def test_win_rate_ranks_by_the_bottom_of_its_range_and_needs_ten_settled_trades() -> None:
    def win(won: int, settled: int) -> float | None:
        return engine._board(SCORE | {"won": won, "settled": settled})["win_low"]  # type: ignore[no-any-return]

    assert win(1, 1) is None and win(9, 9) is None  # 100% on a handful doesn't rank
    assert win(45, 50) > win(10, 10) > win(6, 10)  # type: ignore[operator]  # 90% of 50 beats 100% of 10
    assert engine.wilson_low(0, 10) == 0.0 and 0 < engine.wilson_low(10, 10) < 1


def test_consistency_counts_only_weeks_with_history() -> None:
    young: list[float | None] = [None] * 9 + [120.0, -40.0, 300.0]  # their first trade was three weeks ago
    c = engine._consistency(young)
    assert (c["weeks_up"], c["weeks_known"]) == (2, 3)  # 2 of 3, not 2 of 12
    assert c["consistency_low"] is None  # 3 weeks: too few to rank on
    flat = engine._consistency([0.0] * 8 + [10.0] * 4)
    assert (flat["weeks_up"], flat["weeks_known"]) == (4, 12)  # a flat week they traded through isn't a profit
    assert engine._consistency([10.0] * 4)["consistency_low"] > engine._consistency([10.0] * 3 + [-1.0])["consistency_low"]
    assert engine._consistency(None) == {"weeks_up": None, "weeks_known": None, "consistency_low": None}
    s = engine._board(COPYABLE | {"pnl_weeks": young})
    assert (s["weeks_up"], s["weeks_known"]) == (2, 3)


def test_the_copy_estimate_comes_with_its_range_and_ranks_by_its_bottom() -> None:
    s = engine._board(COPYABLE | {"avg_price": 0.5})
    assert (s["copy_return"], s["copy_return_low"], s["copy_return_high"]) == pytest.approx((0.04, 0.02, 0.06))
    assert s["copy_rank"] == pytest.approx(0.02)
    few = engine._board(COPYABLE | {"avg_price": 0.5,
                                    "copy": {"mean": 0.05, "margin": 0.01, "n": 5, "z": 9, "low": 0.04, "high": 0.06}})  # fmt: skip
    assert few["copy_rank"] is None  # 5 bets: shown, but not ranked
    assert engine._board(SCORE)["copy_return"] is None and engine._board(SCORE)["copy_rank"] is None


def test_the_copy_estimate_divides_by_what_a_copier_paid_when_the_sdk_says() -> None:
    # uselayer 0.8.1: copy_entry, what a copier paid (at the ask) on the same bets as the copy result.
    s = engine._board(COPYABLE | {"avg_price": 0.5, "copy_entry": 0.4})
    assert (s["copy_return"], s["copy_return_low"], s["copy_return_high"]) == pytest.approx((0.05, 0.025, 0.075))
    assert s["copy_rank"] == pytest.approx(0.025)
    # A result kept by uselayer 0.8.0 has no copy_entry: their own average price, as before.
    assert engine._board(COPYABLE | {"avg_price": 0.5})["copy_return"] == pytest.approx(0.04)
    assert engine._board(COPYABLE | {"avg_price": 0.5, "copy_entry": None})["copy_return"] == pytest.approx(0.04)


# ---- Traders' sections: Worth copying, Rising, Worth watching ----


def test_each_recommended_trader_has_one_section_and_only_copying_offered_is_worth_copying() -> None:
    assert engine.section_of("quiet", True) == "copy" and engine.section_of("proven", True) == "copy"
    assert engine.section_of("rising", True) == "copy"  # Rising with copies reliably above zero: offered
    assert engine.section_of("rising", False) == "rising"
    assert engine.section_of("too_fast", False) == "watch"  # No proven copy advantage: Worth watching
    for seg in ("lucky", "no_edge", "no_view", "inactive", None):
        assert engine.section_of(seg, False) is None  # not recommended: only in "See all checked traders"
    fast = SCORE | {"segment": "too_fast", "copyable": "no", "copy": {"mean": 0.01, "margin": 0.02, "n": 40, "z": 1.0}}
    assert (engine._board(fast)["section"], engine._board(fast)["copy_ok"]) == ("watch", False)
    assert engine._board(COPYABLE)["section"] == "copy"
    assert engine._board(SCORE | {"segment": "rising", "copyable": "unknown"})["section"] == "rising"
    assert engine._board(SCORE | {"segment": "lucky"})["section"] is None


def test_the_traders_list_carries_each_traders_section(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fast = SCORE | {"wallet": "0xf", "segment": "too_fast", "copyable": "no"}
    rising = SCORE | {"wallet": "0xr", "segment": "rising", "copyable": "unknown"}
    lucky = SCORE | {"wallet": "0xl", "segment": "lucky"}
    monkeypatch.setattr(engine, "_worth_data", {"scores": [COPYABLE, fast, rising, lucky]})
    got = {s["wallet"]: s["section"] for s in api.get("/api/worth").json()["result"]["scores"]}
    assert got == {"0xabc": "copy", "0xf": "watch", "0xr": "rising", "0xl": None}


def test_the_page_has_the_sections_and_no_copy_cue_for_traders_copying_isnt_offered_for(api: TestClient) -> None:
    page = api.get("/").text
    for words in ("Worth copying", "Worth watching", "No trader's copies have been reliably profitable after fees lately.",
                  "Skilled traders worth keeping an eye on. Copying isn't offered: the price moves before you'd get in.",
                  "Not offered", "they paid", "Buy on Polymarket ↗", "weeks up",
                  "A week counts profit taken plus the change in value of trades still open.",
                  "at least half a cent above the middle price",
                  "Polymarket's public data runs 3–5 minutes behind"):  # fmt: skip
        assert words in page, words
    assert "weeks profitable" not in page


# ---- a wallet's account: when it was read, and never part of it shown as all of it ----


def test_a_wallets_account_says_when_it_was_read_and_which_part_is_missing(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uselayer import Account

    seen: list[str] = []

    def account(w: str) -> Account:
        seen.append(w)
        return Account(wallet=w, positions_usd=1234.57, cash_usd=None, read_at=datetime(2026, 10, 13, 3, tzinfo=UTC))

    monkeypatch.setattr(engine.reads.whales, "account", account)
    wallet = "0x" + "ab" * 20
    a = api.get("/api/account/0x" + "AB" * 20).json()
    assert (a["positions_usd"], a["cash_usd"], a["total_usd"], a["complete"], a["missing"]) == (
        1234.57, None, None, False, ["cash"])  # fmt: skip
    assert a["read_at"] == "2026-10-13T03:00:00+00:00"
    api.get(f"/api/account/{wallet}")
    assert seen == [wallet]  # kept for a minute: one read
    assert api.get("/api/account/0x123").status_code == 400
