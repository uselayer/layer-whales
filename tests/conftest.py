"""Shared test helpers: run a few of the page's own JavaScript functions in Node, so page rules get tests too."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from signals import us_match

PAGE = Path(__file__).parent.parent / "signals" / "static" / "index.html"
_TOP = re.compile(r"^(?://|const |let |function |async function |setInterval|document\.|window\.|api\(|</script>)")


def js_defs(*names: str) -> str:
    """The source of top-level ``const NAME = …`` / ``function NAME(…)`` definitions in the page's script, each up to
    the next top-level line."""
    lines = PAGE.read_text().splitlines()
    out = []
    for name in names:
        start = next(i for i, ln in enumerate(lines)
                     if re.match(rf"^(?:const|let) {re.escape(name)}\b|^(?:async )?function {re.escape(name)}\(", ln))  # fmt: skip
        end = next(i for i in range(start + 1, len(lines)) if _TOP.match(lines[i]) or not lines[i].strip())
        out.append("\n".join(lines[start:end]))
    return "\n".join(out)


def live_reads(monkeypatch: pytest.MonkeyPatch, detail: Any) -> None:
    """Stands ``detail`` (a stand-in for the SDK's ``whales.trader``, called with venue and wallet) in for every
    read of a trader: their page's full read, and the Feed's poller's two (their trades, then open positions)."""
    from dataclasses import replace

    from signals import app as engine

    def trades(venue: str, w: str, **k: Any) -> list[Any]:
        d = detail(venue, w)  # Polymarket's trades carry the trader's name, as their profile has it
        return [replace(t, name=t.name or d.trader.name) for t in d.trades]

    monkeypatch.setattr(engine.reads.whales, "trader", detail)
    monkeypatch.setattr(engine.reads.whales, "trades", trades)
    monkeypatch.setattr(engine, "_open_positions", lambda w, markets=None: detail("polymarket", w).positions)


@pytest.fixture(autouse=True)
def fresh_poller() -> None:
    """Every test starts with nothing the Feed's poller kept about traders' positions, failures or hints."""
    from signals import app as engine

    for kept in (engine._live_priced, engine._live_failed, engine._live_hinted, engine._live_errors):
        kept.clear()
    engine._live_round["ended"] = float("-inf")
    engine._retry_state["started"] = False  # each test's alerts.db is new: as after a start


@pytest.fixture(autouse=True)
def checks_read_like_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scoring on the spot reads through ``engine.checker()`` (its own budget); tests stand in for it at
    ``engine.reads.whales``, as for every other read. tests/test_directory_evaluation.py checks the real one."""
    from signals import app as engine

    monkeypatch.setattr(engine, "checker", lambda: engine.reads.whales)


@pytest.fixture(autouse=True)
def no_pace_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Following a trader measures how busy they are on a thread that reads Polymarket: not in tests (they call
    ``engine._pace_after_follow`` themselves, with a stand-in for the reads)."""
    from signals import app as engine

    monkeypatch.setattr(engine, "_pace_later", lambda uid, trader: None)
    monkeypatch.setattr(engine, "_pace_later_quiet", lambda trader: None)


@pytest.fixture(autouse=True)
def no_us_twins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with no Polymarket US index (as before Layer's matches are read): no test sees another's."""
    monkeypatch.setattr(us_match, "twins", us_match.UsTwins())


@pytest.fixture
def run_js() -> Any:
    """``run_js(names, expr)``: defines the page's ``names``, evaluates ``expr`` (awaited when it's a promise) and returns its JSON value."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node isn't installed: the page's JavaScript rules can't be run here")

    def run(names: tuple[str, ...], expr: str) -> Any:
        code = js_defs(*names) + f"\nPromise.resolve({expr}).then((v) => process.stdout.write(JSON.stringify(v)));"
        r = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30, check=False)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    return run
