"""Polymarket US twins of the Polymarket International markets Signals' traders trade.

Every trader Signals follows trades on Polymarket International: that's where trades are public. Someone in the US
trades on Polymarket US, a separate exchange with its own markets, prices and fees. Layer matches each Polymarket to
Kalshi, never the two Polymarkets to each other, so a US twin is found through Kalshi: the International market's
Kalshi match, and that same Kalshi market's Polymarket US match.

A twin is shown only when the whole chain checks out (``_twin``):

- both matches are live on Layer, verified (``auto_verified`` or ``human_verified``), and the same bet or the same
  bet with stated differences (``identical`` or ``equivalent_with_caveats``);
- both are matched to the very same Kalshi market (one outcome, not just the same event);
- the outcome is the trader's own: their outcome token is the International market Layer matched, or, in a plain
  Yes/No market matched to a plain Yes/No market on Polymarket US, it's the No of a matched Yes;
- the link goes to polymarket.us.

Every difference either match states (settlement source, timing, edge cases such as ties or postponements) comes
with the twin, so the page can say how the two markets may settle differently. A twin is the same market, never the
same price: the trader's entry was on International, and Polymarket US has its own order book.

``lookup`` answers None while the index hasn't been built or has gone stale (Layer can't be read, or there's no
Layer key): unknown, which the page shows as no claim either way. ``{"found": False}`` is a checked "no verified US
match"; it never means the market isn't listed on Polymarket US.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

VERIFIED = frozenset({"auto_verified", "human_verified"})
SAME_BET = frozenset({"identical", "equivalent_with_caveats"})
PAGE = 200  # Layer's largest page of matches
MAX_PAGES = 500  # 100,000 matches a venue at most; 9,780 International on 10/14, near the old cap of 10,000
REFRESH_S = 10 * 60
STALE_S = 45 * 60  # an index this old says nothing: a match may have been pulled since

# Layer's caveat codes, in plain words, for when a match states a code without its own note.
DIFFERENCES = {
    "source_differs": "The two exchanges name different sources for the result.",
    "timing_differs": "The two exchanges close or settle at different times.",
    "carveout_differs": "The two exchanges handle edge cases (a tie, a postponement, a void) differently.",
    "rounding_differs": "The two exchanges round the deciding number differently.",
    "definition_differs": "The two exchanges define the outcome in slightly different words.",
}
_YES, _NO = "yes", "no"


def _cid(market_id: str) -> str:
    return market_id.split(":", 1)[0]


def _us_url_ok(url: str | None) -> bool:
    try:
        u = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    return u.scheme == "https" and u.hostname == "polymarket.us"


def _ok(m: dict[str, Any], venue: str) -> bool:
    """A live, verified match of ``venue`` to Kalshi: the same bet, or the same bet with stated differences."""
    leg, k = m.get(venue), m.get("kalshi")
    return (isinstance(leg, dict) and isinstance(k, dict) and bool(leg.get("market_id")) and bool(k.get("market_id"))
            and m.get("tier") in VERIFIED and m.get("basis") in SAME_BET)  # fmt: skip


def _differences(*matches: dict[str, Any]) -> list[dict[str, str]]:
    """Each difference either match states, once: its own note when it has one, else the plain words for its code."""
    out: dict[str, str] = {}
    for m in matches:
        notes = m.get("caveat_notes") or {}
        for code in m.get("caveats") or []:
            text = notes.get(code) or DIFFERENCES.get(code) or "The two exchanges' rules differ in a stated way."
            out.setdefault(code, text)
    return [{"code": c, "text": t} for c, t in out.items()]


def _twin(intl: dict[str, Any], us: dict[str, Any], side: str, checked_at: str) -> dict[str, Any]:
    """The twin a trade on ``intl``'s outcome (``side`` "same") or on its No (``side`` "no") has on Polymarket US."""
    u = us["polymarket_us"]
    return {
        "found": True,
        "url": u["url"],
        "event": u.get("event"),
        # the outcome on Polymarket US: the matched one, or in a plain Yes/No market, its No ("No on Both teams to score")
        "outcome": u.get("outcome") if side == "same" else f"No on {u['outcome']}" if u.get("outcome") else "No",
        "via": "kalshi",
        "confidence": min(float(intl.get("confidence") or 0), float(us.get("confidence") or 0)),
        "differences": _differences(intl, us),
        "checked_at": checked_at,
    }


class UsTwins:
    """International outcome → its verified Polymarket US twin, rebuilt from Layer's two match lists."""

    def __init__(self) -> None:
        self._by_token: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}  # matched outcome token → (intl, us)
        self._by_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}  # Layer's International market id → same
        self._yes_no: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}  # condition id of a plain Yes/No pair
        self._built: float | None = None
        self._checked_at = ""
        self._lock = threading.Lock()
        self.error: str | None = None
        self.counts: dict[str, int] = {}

    def build(self, intl_matches: Iterable[dict[str, Any]], us_matches: Iterable[dict[str, Any]],
              now: float | None = None) -> None:  # fmt: skip
        """Join the International↔Kalshi and Kalshi↔Polymarket US matches on the Kalshi market."""
        us_by_kalshi: dict[str, dict[str, Any]] = {}
        dropped = 0
        for m in us_matches:
            if _ok(m, "polymarket_us") and _us_url_ok(m["polymarket_us"].get("url")):
                us_by_kalshi.setdefault(m["kalshi"]["market_id"], m)
            else:
                dropped += 1
        by_token: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        by_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        yes_no: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        intl_n = 0
        for m in intl_matches:
            intl_n += 1
            if not _ok(m, "polymarket"):
                dropped += 1
                continue
            us = us_by_kalshi.get(m["kalshi"]["market_id"])
            if us is None:
                continue
            pid = m["polymarket"]["market_id"]
            pair = (m, us)
            by_id[pid] = pair
            if m["polymarket"].get("yes_token_id"):
                by_token[str(m["polymarket"]["yes_token_id"])] = pair
            # A bare condition id is a plain Yes/No market matched on its Yes. Its No has a twin only when the
            # Polymarket US market is plain Yes/No too (a bare id there): then it's that market's No.
            if ":" not in pid and ":" not in us["polymarket_us"]["market_id"]:
                yes_no[pid] = pair
        with self._lock:
            self._by_token, self._by_id, self._yes_no = by_token, by_id, yes_no
            self._built = time.monotonic() if now is None else now
            self._checked_at = datetime.now(UTC).isoformat()
            self.error = None
            self.counts = {"international": intl_n, "us": len(us_by_kalshi), "twins": len(by_id), "dropped": dropped}

    def fresh(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self._built is not None and now - self._built < STALE_S

    def lookup(self, market: str | None, token: str | None = None, outcome: str | None = None,
               now: float | None = None) -> dict[str, Any] | None:  # fmt: skip
        """The verified Polymarket US twin of a trade's outcome (``found`` True), a checked "none" (``found``
        False), or None when there's nothing to go on (no index, or a stale one). ``market`` is Signals'
        ``<conditionId>:<outcome index>``, ``token`` the exact outcome token, ``outcome`` its name."""
        if not market or not self.fresh(now):
            return None
        with self._lock:
            by_token, by_id, yes_no, at = self._by_token, self._by_id, self._yes_no, self._checked_at
        said = (outcome or "").strip().lower()
        pair = by_token.get(token) if token else None
        if pair is None and not token:  # no token to go on: Layer's id for the same outcome
            pair = by_id.get(market)
        if pair is not None:
            return _twin(*pair, side="same", checked_at=at)
        cid = _cid(market)
        yn = yes_no.get(cid)
        if yn is not None:
            intl = yn[0]["polymarket"]
            # its Yes when the token is the matched one; with no token, outcome 0 named Yes
            if token is None and market == f"{cid}:0" and said == _YES:
                return _twin(*yn, side="same", checked_at=at)
            if said == _NO and token != intl.get("yes_token_id") and market != f"{cid}:0":
                return _twin(*yn, side="no", checked_at=at)
        return {"found": False}


twins = UsTwins()


def all_matches(read: Callable[..., list[Any]], venue: str) -> list[dict[str, Any]]:
    """Every live Layer match of ``venue`` to Kalshi, page by page (``read`` is ``Client.matches``). More than
    MAX_PAGES pages is an error, never a shorter list: an index built from part of the matches would quietly say
    "no verified US match" for the rest, so ``refresh`` keeps the last index and says why instead."""
    out: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        rows = read(titles=False, venue=venue, limit=PAGE, offset=page * PAGE)
        out += [r.to_dict() if hasattr(r, "to_dict") else r for r in rows]
        if len(rows) < PAGE:
            return out
    raise RuntimeError(f"Layer has more than {MAX_PAGES * PAGE:,} {venue} matches; raise us_match.MAX_PAGES")


def refresh(read: Callable[..., list[Any]]) -> None:
    """Rebuild ``twins`` from Layer. A failure keeps the last index (until it goes stale) and says why."""
    try:
        twins.build(all_matches(read, "polymarket"), all_matches(read, "polymarket_us"))
    except Exception as e:  # Layer down, no key, a bad page: the trades still show, without a US claim
        twins.error = f"{type(e).__name__}: {e}"


def loop(read: Callable[..., list[Any]], stop: threading.Event) -> None:
    while not stop.is_set():
        refresh(read)
        stop.wait(REFRESH_S)
