"""Monitoring (the directory's phase 6): what the engine did each day, counted as it happens and kept across
restarts, for the owner's /api/monitoring.

What the directory already keeps (wallets and when each was found, every score, the sweeps) is counted from
directory.db itself (``Directory.monitoring``). What it doesn't keep is counted here, in memory, and added to
directory.db's ``daily`` table about once a minute (``Directory.tally``), by UTC day:

- ``api``: every request the SDK's client sends (one httpx client for every venue read: the Feed, pages, the
  background, "Check now"), by host and how it ended (``2xx``, ``4xx``, ``429``, ``5xx``, ``error``: no answer).
- ``lane``: the Polymarket reads taken from each budget (``background``, ``check``); the rest are the Feed's and
  the pages'.
- ``evaluated``: each wallet the background evaluation looked at, as ``kind:outcome`` (``new:dormant``).
- ``infra``: CPU seconds, memory GB-seconds, volume GB-seconds and bytes served, from which a day's Railway cost is
  estimated at Railway's list rates (RATES). ``infra_max``: the most memory and volume used that day.

Counting never gets in the way: a tally that can't be written stays in memory for the next try, and a restart
loses at most the last minute.
"""

from __future__ import annotations

import os
import resource
import shutil
import sys
import threading
import time
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

# Railway's list prices (docs.railway.com/pricing/plans, read 2026-10-14), a month being 30 days. An estimate of
# the engine's own share: Railway's usage page is the bill; Vercel, Clerk and PostHog aren't in it.
RATES = {"cpu_vcpu_month": 20.0, "ram_gb_month": 10.0, "volume_gb_month": 0.15, "egress_gb": 0.05}
MONTH_S = 30 * 86400
GB = 1024**3
SAMPLE_S = 60  # resources are read, and the tallies kept, this often
VOLUME_EVERY = 10  # the volume's use is read every this many samples


def day_of(at: float) -> str:
    """A time's UTC day, as the tallies key it (``2026-10-14``)."""
    return time.strftime("%Y-%m-%d", time.gmtime(at))


def status_class(status: int | None) -> str:
    """How a request ended, as ``api`` counts it."""
    if status is None:
        return "error"
    if status == 429:
        return "429"
    return f"{status // 100}xx"


class Usage:
    """Counts kept in memory until they're written (``take``), plus the totals since launch. Safe from any thread.
    A metric ending in ``_max`` keeps the largest value of the day instead of a sum."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str, str], float] = {}
        self._launch: dict[tuple[str, str], float] = {}
        self.started = clock()

    def add(self, metric: str, key: str, n: float = 1.0, at: float | None = None) -> None:
        k = (day_of(self._clock() if at is None else at), metric, key)
        with self._lock:
            if metric.endswith("_max"):
                self._pending[k] = max(self._pending.get(k, n), n)
                self._launch[(metric, key)] = max(self._launch.get((metric, key), n), n)
            else:
                self._pending[k] = self._pending.get(k, 0.0) + n
                self._launch[(metric, key)] = self._launch.get((metric, key), 0.0) + n

    def take(self) -> list[tuple[str, str, str, float]]:
        """What's waiting to be written, as ``(day, metric, key, n)``, and forgets it."""
        with self._lock:
            rows = [(*k, n) for k, n in self._pending.items()]
            self._pending.clear()
        return rows

    def put_back(self, rows: list[tuple[str, str, str, float]]) -> None:
        """Rows ``take`` gave that couldn't be written: kept for the next try (without counting them twice)."""
        with self._lock:
            for day, metric, key, n in rows:
                k = (day, metric, key)
                self._pending[k] = max(self._pending.get(k, n), n) if metric.endswith("_max") else self._pending.get(k, 0.0) + n

    def since_launch(self, metric: str) -> dict[str, float]:
        with self._lock:
            return {k: n for (m, k), n in self._launch.items() if m == metric}


usage = Usage()


# ---- reads: every request the SDK's client sends ----


def watch(client: Any, counts: Usage | None = None) -> None:
    """Counts every request ``client`` (an httpx.Client: the SDK's ``Http._client``) sends, by host and how it ended.
    A request without an answer (a timeout, no connection) counts as ``error``: sent, never answered."""
    hooks = client.event_hooks

    def sent(request: Any) -> None:
        request.extensions["signals_counted"] = True
        (counts or usage).add("api_sent", request.url.host or "")

    def answered(response: Any) -> None:
        if response.request.extensions.get("signals_counted"):
            (counts or usage).add("api", f"{response.request.url.host or ''} {status_class(response.status_code)}")

    client.event_hooks = {"request": [*hooks.get("request", []), sent], "response": [*hooks.get("response", []), answered]}


def api_counts(sent: dict[str, float], answered: dict[str, float]) -> dict[str, dict[str, int]]:
    """``{host: {"requests": n, "2xx": n, ..., "error": n}}`` from the ``api_sent`` and ``api`` tallies: a request
    sent and never answered is an ``error``."""
    out: dict[str, dict[str, int]] = {}
    for host, n in sent.items():
        out.setdefault(host, {})["requests"] = int(n)
    for key, n in answered.items():
        host, _, cls = key.rpartition(" ")
        out.setdefault(host, {}).setdefault("requests", 0)
        out[host][cls] = out[host].get(cls, 0) + int(n)
    for host, c in out.items():
        c["error"] = c.get("error", 0) + max(0, c["requests"] - sum(v for k, v in c.items() if k not in ("requests", "error")))
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["requests"]))


# ---- the engine's resources, and what they cost ----


def _rss_bytes() -> int | None:
    """The memory the engine holds now (Linux, as Railway runs it), else None."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _cpu_s() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # bytes on macOS, KB on Linux


class Sampler:
    """Reads the engine's CPU time, memory and the volume's use every SAMPLE_S (``sample``), as ``infra`` tallies."""

    def __init__(self, store: Path, counts: Usage | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.store, self._counts, self._clock = store, counts, clock
        self._at, self._cpu = clock(), _cpu_s()
        self._samples = 0
        self.volume: int | None = None

    def _c(self) -> Usage:
        return self._counts or usage

    def sample(self) -> None:
        now, cpu = self._clock(), _cpu_s()
        elapsed = max(0.0, now - self._at)
        self._c().add("infra", "cpu_s", max(0.0, cpu - self._cpu))
        self._at, self._cpu = now, cpu
        if (rss := _rss_bytes()) is not None:
            self._c().add("infra", "ram_gb_s", rss / GB * elapsed)
            self._c().add("infra_max", "ram_bytes", rss)
        if self._samples % VOLUME_EVERY == 0:
            try:
                self.volume = shutil.disk_usage(self.store).used
            except OSError:
                self.volume = None
        self._samples += 1
        if self.volume is not None:
            self._c().add("infra", "volume_gb_s", self.volume / GB * elapsed)
            self._c().add("infra_max", "volume_bytes", self.volume)

    def now(self) -> dict[str, Any]:
        """The engine as it is: memory now and at its most since launch, the volume's use, CPU seconds used."""
        rss = _rss_bytes()
        return {"ram_mb": None if rss is None else round(rss / 2**20), "peak_ram_mb": round(_peak_rss_bytes() / 2**20),
                "volume_gb": None if self.volume is None else round(self.volume / GB, 2), "cpu_s": round(_cpu_s())}  # fmt: skip


def cost(infra: dict[str, float]) -> dict[str, float]:
    """A day's (or any span's) ``infra`` tallies in dollars at RATES, each part and the total."""
    parts = {
        "cpu": infra.get("cpu_s", 0.0) / MONTH_S * RATES["cpu_vcpu_month"],
        "ram": infra.get("ram_gb_s", 0.0) / MONTH_S * RATES["ram_gb_month"],
        "volume": infra.get("volume_gb_s", 0.0) / MONTH_S * RATES["volume_gb_month"],
        "egress": infra.get("egress_bytes", 0.0) / GB * RATES["egress_gb"],
    }
    return {k: round(v, 4) for k, v in parts.items()} | {"total": round(sum(parts.values()), 4)}


class CountBytesOut:
    """ASGI middleware: the bytes of every response the engine sends (``infra`` ``egress_bytes``), what Railway
    bills as egress (Polymarket's answers coming in are free)."""

    def __init__(self, app: Callable[..., Awaitable[None]], counts: Usage | None = None) -> None:
        self.app, self._counts = app, counts

    def _c(self) -> Usage:
        return self._counts or usage

    async def __call__(self, scope: MutableMapping[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def counted(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                self._c().add("infra", "egress_bytes", len(message["body"]))
            await send(message)

        await self.app(scope, receive, counted)
