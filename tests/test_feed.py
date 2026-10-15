"""The Feed in two parts: Following (your followed traders' trades, signed in) and Recommended ($1,000 or more
from Traders' three sections). No network: Clerk's keys and the SDK are stand-ins."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Response
from fastapi.testclient import TestClient
from uselayer import Trader, TraderDetail, WhalePosition, WhaleTrade

from conftest import live_reads
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

    live_reads(monkeypatch, detail)
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: [])  # the shared feed: nobody in it
    return TestClient(engine.app)


def test_recommended_is_the_three_sections_and_bigger_trades_only(api: TestClient) -> None:
    r = api.get("/api/live", headers=auth()).json()
    assert {t["wallet"] for t in r["traders"]} == {COPY, RISE, WATCH}  # Worth copying, Rising, Worth watching; never Lucky
    assert r["min_usd"] == engine.RECOMMENDED_MIN_USD == 1000
    assert {b["usd"] for b in r["bets"]} == {2500.0}  # the $150 buys stay out of Recommended
    assert {b["wallet"]: b["copy_ok"] for b in r["bets"]} == {COPY: True, RISE: False, WATCH: False}  # neutral cards for the rest


def test_following_works_whatever_a_traders_status_and_each_card_says_what_we_know(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Followed from anywhere: a recommended trader, one Explore lists as checked (no edge proven), one with only a
    quick look, and one nobody has checked. Following shows each one's trades under their Polymarket name, with
    the verdict their page shows (the directory's fresh stored score, no read) or none, never a copy cue for those
    copying isn't offered for; and each one's next buy alerts their follower."""
    from signals.directory import QuickLook, Seen

    checked, quick = "0x" + "6" * 40, "0x" + "7" * 40
    d = engine.directory()
    d.add([Seen(checked, "leaderboard", "PNL MONTH OVERALL", rank=5), Seen(quick, "market", "Sports")])
    d.keep_scores([{"wallet": checked, "segment": "no_edge", "skill": "no", "copyable": "no", "bets": 30,
                    "format": engine.WORTH_FORMAT, "sample": []}], "background")  # fmt: skip
    d.quick_checked(quick, QuickLook(False, "not_enough_recent_evidence", 3, 9, None, time.time() - 3600, None, False))
    for w in (COPY, checked, quick, STRANGER):
        assert api.post("/api/me/follows", json={"wallet": w}, headers=auth()).status_code == 200
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: [])
    engine.live_poll_once()  # its next round: everyone followed, first
    body = api.get("/api/me/feed", headers=auth()).json()
    who = {t["wallet"]: t for t in body["traders"]}
    assert set(who) == {COPY, checked, quick, STRANGER} and (body["pending"], body["failed"]) == (0, 0)
    assert (who[COPY]["segment"], who[COPY]["copy_ok"]) == ("proven", True)
    assert (who[checked]["segment"], who[checked]["label"], who[checked]["copy_ok"]) == (
        "no_edge", engine.SEGMENTS["no_edge"][0], False)  # fmt: skip
    assert (who[quick]["segment"], who[quick]["copy_ok"]) == (None, False)
    assert (who[STRANGER]["segment"], who[STRANGER]["copy_ok"]) == (None, False)
    cards = {b["wallet"]: b for b in body["bets"] if b["usd"] == 2500.0}
    assert set(cards) == {COPY, checked, quick, STRANGER}  # every one's trades, every size
    assert {w: c["name"] for w, c in cards.items() if w != COPY} == {checked: "pm-66", quick: "pm-77", STRANGER: "pm-ee"}
    assert [w for w, c in cards.items() if c["copy_ok"]] == [COPY]  # no copy cue for the rest

    sent: list[str] = []
    monkeypatch.setattr(engine, "TG_TOKEN", "123:test")
    monkeypatch.setattr(engine, "tg", lambda method, **p: sent.append(p.get("text", "")) or {"message_id": len(sent)})
    for d_ in (engine._seen, engine._alerted):
        d_.clear()
    u = engine._load_follows()
    next(iter(u["users"].values()))["chat"] = "555"
    engine._save_follows(u)
    engine.alert_pass()  # first round: noted only
    real = engine.reads.whales.trades
    later = datetime.now(UTC)
    monkeypatch.setattr(engine.reads.whales, "trades", lambda venue, w, **k: [
        WhaleTrade("polymarket", w, f"pm-{w[2:4]}", f"{w[:6]}:0", "yes", "buy", 0.5, 400, 200.0, later, f"{w}new",
                   title="T")] + real(venue, w, **k))  # fmt: skip
    engine._cache.clear()
    assert engine.alert_pass() == 4  # one alert each, whoever they are
    assert all(t.startswith("<b>") for t in sent[:4])


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


def test_following_shows_their_sells_too_and_recommended_doesnt(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    at = datetime.now(UTC)

    def detail(venue: str, w: str, **k: Any) -> TraderDetail:
        mk = lambda m, action, price, usd, when, i: WhaleTrade("polymarket", w, None, f"{w[:6]}:{m}", "yes", action, price,  # noqa: E731
                                                               usd / price, usd, when, f"{w}{i}", title="T")  # fmt: skip
        trades = [mk("b", "buy", 0.5, 2500.0, at, 1),
                  mk("held", "sell", 0.6, 600.0, at - timedelta(minutes=3), 2), mk("held", "sell", 0.7, 700.0, at, 3),
                  mk("gone", "sell", 0.4, 2000.0, at, 4), mk("old", "sell", 0.5, 900.0, at - timedelta(hours=25), 5)]  # fmt: skip
        return TraderDetail(Trader(venue="polymarket", id=w, name="pm"), "visible", {},
                            [WhalePosition(f"{w[:6]}:b", None, "Yes", 10, 0.5, 0.51, None, None),
                             WhalePosition(f"{w[:6]}:held", None, "Yes", 10, 0.5, 0.66, None, None)], trades)  # fmt: skip

    live_reads(monkeypatch, detail)
    api.post("/api/me/follows", json={"wallet": WATCH}, headers=auth())
    body = api.get("/api/me/feed", headers=auth()).json()
    sells = {b["market"].partition(":")[2]: b for b in body["bets"] if b.get("side") == "sell"}
    assert set(sells) == {"held", "gone"}  # one per outcome, over the last FEED_HOURS
    held, gone = sells["held"], sells["gone"]
    assert (held["price"], held["usd"], held["held"], held["price_now"], held["status"]) == (pytest.approx(0.65), 1300.0, True, 0.66, None)
    assert (gone["held"], gone["price_now"], gone["status"]) == (False, None, None)  # no price made up once they hold none
    assert held["us"] is None and "copy_ok" not in held and held["category"] == "Politics"
    assert [(b["market"].partition(":")[2], b["status"]) for b in body["bets"] if b.get("side") != "sell"] == [("b", "in")]
    rec = api.get("/api/live", headers=auth()).json()
    assert rec["bets"] and not any(b.get("side") == "sell" for b in rec["bets"])  # Recommended: buys only


def test_the_feed_page_has_following_and_recommended(api: TestClient) -> None:
    page = api.get("/").text
    for words in ('"Following"', '"Recommended"', "#/recommended", "Only buys I can still get into", "Also ${did} by",
                  "Sold at <b>", "They no longer hold it.", "They still hold some.",
                  "Sign in to see the Feed", "Browse traders", "/api/me/feed", "from Sharps and Rising traders", "not as a signal to buy",
                  "All big trades right now", "Typically 3–5 min behind", "Typically read every 30–60 s"):  # fmt: skip
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
    asked = _counting(monkeypatch)
    gone = "0x" + "9" * 40
    engine._cache[f"live:{gone}"] = (time.monotonic() - engine.LIVE_KEEP_S - 1, None)  # nobody wants it any more
    assert engine.live_poll_once() == 4
    assert sorted(w for kind, w in asked if kind == "trades") == sorted([COPY, RISE, WATCH, STRANGER])  # never Lucky
    assert {kind for kind, _ in asked} == {"trades", "positions"}  # never the profile or stats
    assert f"live:{gone}" not in engine._cache
    asked.clear()
    api.get("/api/live", headers=auth())
    api.get("/api/me/feed", headers=auth())
    assert asked == []  # the pages read what the poller keeps
    engine.live_poll_once()
    assert asked == [("trades", STRANGER)]  # followed: every round (positions kept: same trades); Recommended: not yet


def _counting(monkeypatch: pytest.MonkeyPatch, trades: Any = None, clock: Any = None, read_s: float = 0.0) -> list[Any]:
    """Counts the poller's reads of Polymarket, as (kind, wallet), on top of the api fixture's stand-ins (or
    ``trades(wallet)``); each read moves ``clock`` on ``read_s``, as a real one takes time."""
    asked: list[Any] = []
    real_trades, real_positions = engine.reads.whales.trades, engine._open_positions

    def read(kind: str, w: str, fn: Any) -> Any:
        asked.append((kind, w))
        if clock is not None:
            clock.t += read_s
        return fn()

    monkeypatch.setattr(engine.reads.whales, "trades", lambda venue, w, **k: read(
        "trades", w, (lambda: trades(w)) if trades else (lambda: real_trades(venue, w, **k))))  # fmt: skip
    monkeypatch.setattr(engine, "_open_positions", lambda w, markets=None: read("positions", w, lambda: [] if trades else real_positions(w)))
    return asked


def test_positions_are_read_only_with_a_trade_to_price_and_kept_while_the_trades_dont_change(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime.now(UTC) - timedelta(hours=engine.FEED_HOURS + 1)
    quiet = [WhaleTrade("polymarket", COPY, "Cora", "0xq:0", "yes", "buy", 0.5, 10, 5.0, old, "q1")]
    asked = _counting(monkeypatch, trades=lambda w: list(quiet))
    engine._live_read(COPY)
    assert asked == [("trades", COPY)]  # nothing in the Feed's last day: no price to read
    engine._live_read(COPY, priced=True)
    assert asked[1:] == [("trades", COPY), ("positions", COPY)]  # a trade's own price check wants it anyway
    asked.clear()
    engine._live_read(COPY, 0, priced=True)
    assert asked == [("trades", COPY)]  # the same trades, positions under LIVE_PRICE_S old: kept
    quiet.insert(0, WhaleTrade("polymarket", COPY, "Cora", "0xq:0", "yes", "buy", 0.5, 10, 5.0, datetime.now(UTC), "q2"))
    asked.clear()
    engine._live_read(COPY, 0)
    assert asked == [("trades", COPY), ("positions", COPY)]  # a new trade: the price now read with it



def test_positions_left_out_are_read_in_batches_polymarkets_firewall_lets_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polymarket's firewall blocks a positions read whose market list passes ~1,300 characters (20 conditionIds of
    66 did, 2026-10-15): every batch stays well under it, and every market left out is still asked for."""
    asked: list[str] = []

    def positions(venue: str, w: str, limit: int = 50, market: str | None = None) -> list[Any]:
        if market is not None:
            asked.append(market)
        return []

    monkeypatch.setattr(engine.reads.whales, "positions", positions)
    markets = [f"0x{i:064x}:0" for i in range(37)]  # conditionIds as Polymarket's: 0x and 64 hex characters
    engine._open_positions(COPY, markets)
    assert all(len(m) <= 1100 for m in asked) and len(asked) == 3  # 15 + 15 + 7
    assert sorted(c for m in asked for c in m.split(",")) == sorted(m.partition(":")[0] for m in markets)


def test_open_positions_are_the_sdks_one_read_of_open_bets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The poller's positions come from uselayer's ``whales.positions``: one request, settled bets left out."""
    import httpx
    from uselayer.http import Http
    from uselayer.layer_api import LayerApi
    from uselayer.whales import Whales

    rows = [
        {"conditionId": "0xa", "outcomeIndex": 0, "size": 10, "curPrice": 0.5, "avgPrice": 0.4, "title": "A"},
        {"conditionId": "0xb", "outcomeIndex": 1, "size": 10, "curPrice": 0.7, "redeemable": True},  # settled
        {"conditionId": "0xc", "outcomeIndex": 0, "size": 10, "curPrice": 1},  # settled, not redeemed yet
    ]
    seen: list[tuple[str, str | None]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append((r.url.path, r.url.params.get("user")))
        return httpx.Response(200, json=rows if r.url.path == "/positions" else [])

    http = Http(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    monkeypatch.setattr(engine.reads, "whales", Whales(http, LayerApi(None, http)))
    got = engine._open_positions(COPY)
    assert [(p.market, p.avg_price, p.title) for p in got] == [("0xa:0", 0.4, "A")]
    assert seen == [("/positions", COPY)]  # no profile, no stats

def test_the_others_give_way_only_while_followed_traders_are_read_and_alerts_go_at_once_after(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.post("/api/me/follows", json={"wallet": STRANGER}, headers=auth())
    giving_way: dict[str, bool] = {}
    real = engine.reads.whales.trades
    monkeypatch.setattr(engine.reads.whales, "trades", lambda venue, w, **k: giving_way.setdefault(
        w, engine._reading_followed()) is None or real(venue, w, **k))  # fmt: skip
    engine._alert_wake.clear()
    engine.live_poll_once()
    assert giving_way == {STRANGER: True, COPY: False, RISE: False, WATCH: False}
    assert not engine._reading_followed() and engine._alert_wake.is_set()  # the alert pass runs now, not in 30 s


def test_each_round_reads_the_followed_first_then_who_just_traded_then_the_oldest_recommended(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.post("/api/me/follows", json={"wallet": STRANGER}, headers=auth())
    now = time.monotonic()
    for w in (COPY, RISE, WATCH):
        engine._cache[f"live:{w}"] = (now - engine.LIVE_REC_S - 1, engine._live_fetch(w, False))
    engine._cache[f"live:{WATCH}"] = (now - engine.LIVE_REC_S - 50, engine._cache[f"live:{WATCH}"][1])  # the oldest
    big = WhaleTrade("polymarket", RISE, "Rita", "0xr:0", "yes", "buy", 0.5, 4000, 2000.0, datetime.now(UTC), "fresh")
    monkeypatch.setattr(engine.reads.whales, "big_trades", lambda *a, **k: [big])
    asked = _counting(monkeypatch)
    engine.live_poll_once()
    assert [w for kind, w in asked if kind == "trades"] == [STRANGER, RISE, WATCH, COPY]
    asked.clear()
    engine._cache[f"live:{RISE}"] = (now - engine.LIVE_REC_S - 1, engine._cache[f"live:{RISE}"][1])
    engine.live_poll_once()
    assert [w for kind, w in asked if kind == "trades"] == [STRANGER, RISE]  # followed each round; a hint counts once


def test_a_feed_view_reads_only_a_few_traders_itself_and_says_the_rest_are_still_being_read(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    many = [score(f"0x{i:040x}", "proven", name=f"T{i}") for i in range(10)]
    monkeypatch.setattr(engine, "_worth_data", {"format": engine.WORTH_FORMAT, "scores": many})
    asked = _counting(monkeypatch)
    r = api.get("/api/live", headers=auth()).json()  # just after a restart: the poller hasn't read anyone yet
    assert len({w for _, w in asked}) == engine.LIVE_PAGE_READS
    assert (r["pending"], r["failed"]) == (10 - engine.LIVE_PAGE_READS, 0)
    engine.live_poll_once()
    asked.clear()
    r = api.get("/api/live", headers=auth()).json()
    assert asked == [] and (r["pending"], r["failed"]) == (0, 0)
    assert abs(r["as_of"] / 1000 - time.time()) < 5  # when the oldest of it was read: just now
    hit = engine._cache[f"live:{many[0]['wallet']}"]
    engine._cache[f"live:{many[0]['wallet']}"] = (hit[0] - 120, hit[1])
    assert time.time() - api.get("/api/live", headers=auth()).json()["as_of"] / 1000 == pytest.approx(120, abs=5)  # "Updated 2m ago"
    engine._cache[f"live:{many[0]['wallet']}"] = (hit[0] - engine.LIVE_SHOW_S - 1, hit[1])
    assert api.get("/api/live", headers=auth()).json()["failed"] == 1  # too old to show: it says so, and nobody waits on a read


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.parametrize("pool", [50, 200, 1000])
def test_the_feed_holds_up_as_the_recommended_pool_grows(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, pool: int
) -> None:
    """Twenty followed traders and ``pool`` Recommended ones, a third of them with no trade in the last day, each
    read taking 0.25 s, the poller's rounds 30 s apart, for 20 minutes: alerts never wait (everyone followed is
    under LIVE_ALERT_S old at every round's end), each round fits its slot, every Recommended trader gets read and
    refreshed, and once they have been a Feed view reads nothing at all."""
    clock = _Clock()
    monkeypatch.setattr(engine.time, "monotonic", clock)
    rec = [f"0x{i:040x}" for i in range(pool)]
    follows = [f"0x{i:040x}" for i in range(10**6, 10**6 + 20)]
    monkeypatch.setattr(engine, "_worth_data",
                        {"format": engine.WORTH_FORMAT, "scores": [score(w, "proven", name="R") for w in rec]})  # fmt: skip
    engine._save_follows({"users": {"u": {"follows": follows, "chat": None}}})
    now, old = datetime.now(UTC), datetime.now(UTC) - timedelta(days=3)
    trades = {w: [WhaleTrade("polymarket", w, "R", f"0x{i:x}:0", "yes", "buy", 0.5, 4000, 2000.0, old if i % 3 == 0 else now,
                             f"{w}t") for _ in range(1)] for i, w in enumerate(rec + follows)}  # fmt: skip
    asked = _counting(monkeypatch, trades=lambda w: trades[w], clock=clock, read_s=0.25)
    per_round, read_at = [], {}
    for _ in range(40):
        start, n = clock.t, len(asked)
        engine.live_poll_once()
        per_round.append(len(asked) - n)
        assert clock.t - start <= engine.LIVE_POLL_S + 0.5  # the round fits its slot (one read may run over)
        assert all(clock.t - engine._cache[f"live:{w}"][0] < engine.LIVE_ALERT_S for w in follows)
        for w in rec:
            if (hit := engine._live_kept(w)) is not None:
                read_at.setdefault(w, set()).add(hit[0])
        clock.t = max(clock.t, start + engine.LIVE_POLL_S)
    assert len(read_at) == pool  # everyone recommended was read...
    if pool <= 200:
        assert all(len(times) >= 3 for times in read_at.values())  # ...and kept up to date
    assert max(per_round) <= engine.LIVE_POLL_S / 0.25 + 1  # at most what the slot holds: never a burst
    reads_now = len(asked)
    r = engine.live(Response())
    assert len(asked) == reads_now and r["pending"] == 0 and r["failed"] == 0  # a view reads nothing
    oldest = (time.time() * 1000 - r["as_of"]) / 1000
    assert oldest <= (engine.LIVE_REC_S + engine.LIVE_POLL_S if pool <= 50 else engine.LIVE_SHOW_S)


def test_a_recommended_trader_whose_reads_keep_failing_is_tried_less_often_and_the_feed_says_so(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uselayer import VenueError

    api.post("/api/me/follows", json={"wallet": STRANGER}, headers=auth())
    engine.live_poll_once()  # everyone read once: WATCH's trades are kept
    broken = {WATCH, STRANGER}

    def trades(venue: str, w: str, **k: Any) -> Any:
        asked.append(w)
        if w in broken:
            raise VenueError("bad_request", f"Polymarket answered 400 for user={w}.", venue="polymarket", status=400)
        return real(venue, w, **k)

    asked: list[str] = []
    real = engine.reads.whales.trades
    monkeypatch.setattr(engine.reads.whales, "trades", trades)
    for w in (COPY, RISE, WATCH):  # Recommended's turn again
        engine._cache[f"live:{w}"] = (time.monotonic() - engine.LIVE_REC_S - 1, engine._cache[f"live:{w}"][1])
    engine.live_poll_once()
    assert WATCH in asked and engine._live_errors[WATCH]["count"] == 1
    asked.clear()
    engine._cache[f"live:{COPY}"] = (time.monotonic() - engine.LIVE_REC_S - 1, engine._cache[f"live:{COPY}"][1])
    engine.live_poll_once()
    assert WATCH not in asked and COPY in asked  # backing off: not every round, though it's the oldest
    assert asked.count(STRANGER) == 1 and engine._live_errors[STRANGER]["count"] == 2  # followed: every round
    engine._live_failed[WATCH] -= engine._live_backoff_s(1) + 1  # its wait is over
    engine.live_poll_once()
    assert engine._live_errors[WATCH]["count"] == 2 and engine._live_backoff_s(2) == 2 * engine.LIVE_REC_S
    assert engine._live_backoff_s(20) == engine.LIVE_FAIL_MAX_S
    stats = engine.feed_stats()
    # WATCH twice (backing off), STRANGER every round (followed): three
    assert sorted((f["failures_in_a_row"], f["status"], f["code"]) for f in stats["failing"]) == [
        (2, 400, "bad_request"), (3, 400, "bad_request")]  # fmt: skip
    assert WATCH not in repr(stats) and STRANGER not in repr(stats) and stats["skipped_backing_off"] >= 1
    assert api.get("/api/live", headers=auth()).json()["stale"] == 1  # WATCH: shown from its last good read, said so
    broken.clear()
    engine._live_failed[WATCH] -= engine._live_backoff_s(2) + 1
    engine.live_poll_once()
    assert WATCH not in engine._live_errors and api.get("/api/live", headers=auth()).json()["stale"] == 0


def test_the_feed_says_what_it_hasnt_read_and_how_old_it_is_never_no_trades(run_js: Any) -> None:
    got = run_js(("plural", "STALE_NOTE_MS", "feedGaps"), """(() => {
      const t = (n) => Array.from({ length: n }, (_, i) => ({ wallet: "0x" + i }));
      const now = 10 ** 7;
      return [
        feedGaps({ traders: t(3), bets: [], pending: 0, failed: 0 }, now - 30000, now),
        feedGaps({ traders: t(3), bets: [], pending: 3, failed: 0 }, now, now),
        feedGaps({ traders: t(3), bets: [], pending: 0, failed: 3 }, now, now),
        feedGaps({ traders: t(3), bets: [], pending: 1, failed: 1 }, now - 12 * 60000, now),
        feedGaps({ traders: t(3), bets: [1], pending: 0, failed: 0, stale: 1 }, now, now),
      ];
    })()""")
    assert got[0] == {"notes": [], "empty": None}  # everyone read, nothing traded: "None of … traded" stands
    assert got[1]["empty"][0] == "Still reading their trades" and got[1]["notes"] == ["Still reading 3 traders; their trades show in a minute."]
    assert got[2]["empty"][0] == "Their trades couldn't be read just now"
    assert got[3]["empty"][0] == "No trades from the traders read so far"
    assert got[3]["notes"] == ["1 trader couldn't be read just now; retrying.", "Still reading 1 trader; their trades show in a minute.",
                               "Some of these traders were last read 12 minutes ago."]  # fmt: skip
    assert got[4] == {"notes": ["1 trader couldn't be refreshed just now: showing their trades from the last good read; "
                                "retrying."], "empty": None}  # fmt: skip


def test_the_feed_page_rules(run_js: Any) -> None:
    got = run_js(("feed", "auth", "analyticsOn", "analyticsAccount", "accountIs", "LEAD", "leadOf", "oneCardPerTrade"), """(() => {
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


def test_analytics_knows_a_signed_in_person_by_their_clerk_id_only(run_js: Any) -> None:
    # PostHog hears who is signed in as Clerk's user id and nothing else; signing out starts a new anonymous visitor.
    got = run_js(("feed", "auth", "analyticsOn", "analyticsAccount", "accountIs"), """(() => {
      const calls = []; globalThis.window = { posthog: { identify: (...a) => calls.push(["identify", ...a]), reset: () => calls.push(["reset"]) } };
      accountIs("user_0"); accountIs(null); // analytics off (no key, or blocked): nothing is sent
      analyticsOn = true;
      accountIs("user_1"); accountIs("user_1"); accountIs("user_2"); accountIs(null); accountIs(null);
      return calls;
    })()""")
    assert got == [["identify", "user_1"], ["identify", "user_2"], ["reset"]]


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
