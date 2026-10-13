"""Shared test helpers: run a few of the page's own JavaScript functions in Node, so page rules get tests too."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

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
