"""The background's share of Polymarket's reads: one budget for everything that runs behind the site (the
directory's daily sweep and its evaluation), so a page or the Feed's poller never queues behind it.

The SDK already paces each Polymarket host at 8 reads a second for everything the engine reads. ``Budget``
holds background reads to ``rate`` a second on top of that, and ``BackgroundHttp`` is the SDK's client with
every Polymarket read (the data API and Gamma) taking its turn from the budget first, retries included.

A rate limit (429) from Polymarket means something read too fast, whatever it was: the background halves its
rate and pauses every read for as long as Polymarket asks (at least ``COOL_S``), then climbs back to its full
rate a step every ``RECOVER_S`` without another one.

"Check now" (a trader page scoring a wallet on the spot) has a budget of its own too (the engine's ``checker``), so
manual checks can't take the Feed's share either. Both budgets give way while the Feed's poller reads followed traders (``yield_to``): half their rate, so alerts
come first.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from uselayer import VenueError

from signals import monitoring

POLYMARKET_HOSTS = frozenset({"data-api.polymarket.com", "gamma-api.polymarket.com"})
COOL_S = 30.0  # the least a 429 pauses the background for
RECOVER_S = 120.0  # after a 429, the rate goes back up a step each time this passes without another
MIN_RATE = 0.5
BACKOFF_S = (2.0, 5.0, 15.0, 30.0)  # waits before each retry of a read that failed (a 5xx, a timeout)
YIELD_SHARE = 0.5  # a budget's rate while what it gives way to (the Feed's followed traders) is being read


class Budget:
    """Reads a second for the background, shared by every thread that reads for it. Each read takes the next
    free slot (``1 / rate`` apart), so the rate holds however many threads ask at once. While ``yield_to()`` is
    true, slots are YIELD_SHARE of the rate."""

    def __init__(
        self, rate: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
        yield_to: Callable[[], bool] | None = None, name: str = "background",
    ) -> None:
        self.name = name  # what monitoring counts its reads as (``lane``)
        self.target = self.rate = rate
        self._clock, self._sleep, self._yield_to = clock, sleep, yield_to
        self._lock = threading.Lock()
        self._next = 0.0  # the next free slot
        self._paused_until = 0.0
        self._changed = 0.0  # when the rate last went down or up
        self.reads = self.limited = 0

    def take(self) -> None:
        """Waits for the next slot."""
        with self._lock:
            now = self._clock()
            if self.rate < self.target and now - self._changed >= RECOVER_S:
                self.rate, self._changed = min(self.target, self.rate * 1.5), now
            slot = max(self._next, now, self._paused_until)
            self._next = slot + 1 / (self.rate * (YIELD_SHARE if self._yield_to and self._yield_to() else 1.0))
            self.reads += 1
        if slot > now:
            self._sleep(slot - now)

    def rate_limited(self, wait_s: float | None = None) -> None:
        """Polymarket answered 429: half the rate, and no read for ``wait_s`` (at least COOL_S)."""
        with self._lock:
            now = self._clock()
            self.rate, self._changed = max(MIN_RATE, self.rate / 2), now
            self._paused_until = max(self._paused_until, now + max(wait_s or 0.0, COOL_S))
            self.limited += 1

    def state(self) -> dict[str, Any]:
        """For /api/directory: the rate now and its target, reads taken, 429s, and how long the pause has left."""
        with self._lock:
            paused = max(0.0, self._paused_until - self._clock())
        return {"per_second": round(self.rate, 2), "target": self.target, "reads": self.reads,
                "rate_limited": self.limited, "paused_s": round(paused)}  # fmt: skip


class BackgroundHttp:
    """The SDK's ``Http`` with Polymarket's reads taken from a ``Budget``. Retries are made here rather than in
    the SDK, so each one takes its turn too: a 429 or a 5xx or a timeout is tried again up to ``retries`` times,
    waiting BACKOFF_S (or what Polymarket asked). Any other host (Polygon, for cash) goes straight through."""

    def __init__(self, http: Any, budget: Budget, *, retries: int = len(BACKOFF_S),
                 sleep: Callable[[float], None] = time.sleep) -> None:  # fmt: skip
        self._http, self.budget, self._retries, self._sleep_fn = http, budget, retries, sleep

    def request(self, method: str, url: str, **kw: Any) -> Any:
        return self.request_with_headers(method, url, **kw)[0]

    def request_with_headers(self, method: str, url: str, **kw: Any) -> Any:
        if (urlsplit(url).hostname or "") not in POLYMARKET_HOSTS:
            return self._http.request_with_headers(method, url, **kw)
        retries = min(self._retries, kw.pop("retries", self._retries))
        attempt = 0
        while True:
            self.budget.take()
            monitoring.usage.add("lane", self.budget.name)
            try:
                return self._http.request_with_headers(method, url, retries=0, **kw)
            except VenueError as e:
                limited = e.status == 429 or e.code == "rate_limited"
                if limited:
                    self.budget.rate_limited(e.retry_after_s)
                if not e.retryable or attempt >= retries:
                    raise
                if not limited:  # a 429's wait is the budget's pause, which the next take() keeps to
                    self._sleep_fn(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)] + random.random())
                attempt += 1

    def __getattr__(self, name: str) -> Any:  # anything else the SDK asks of its client
        return getattr(self._http, name)

