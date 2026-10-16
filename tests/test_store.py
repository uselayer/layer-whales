"""The store's housekeeping: the scorer's price cache kept to its newest files, so it can't use up the volume's
inodes again (2026-10-16: every page that reads directory.db answered 500)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from signals import app as engine


def test_the_price_cache_keeps_its_newest_files_and_removes_the_rest(tmp_path: Path) -> None:
    folder = tmp_path / "price-cache"
    folder.mkdir()
    for i in range(10):
        f = folder / f"{i}.json"
        f.write_text("[]")
        os.utime(f, (1_000 + i, 1_000 + i))  # 0.json oldest, 9.json newest
    (folder / "sub").mkdir()  # not a file: left alone
    assert engine.prune_price_cache(folder, keep=4, most=2) == 2  # launch's share: the oldest only
    assert sorted(p.name for p in folder.iterdir())[:2] == ["2.json", "3.json"]
    assert engine.prune_price_cache(folder, keep=4) == 4
    assert sorted(p.name for p in folder.iterdir()) == ["6.json", "7.json", "8.json", "9.json", "sub"]
    assert engine.prune_price_cache(folder, keep=4) == 0  # nothing more to do
    assert engine.prune_price_cache(tmp_path / "none", keep=4) == 0  # no cache yet


def test_launch_prunes_the_cache_before_anything_opens_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from typing import Any

    order: list[str] = []

    class Thread:
        def __init__(self, target: Any, **_: Any) -> None:
            self.name = getattr(target, "__name__", "thread")

        def start(self) -> None:
            order.append(self.name)

    monkeypatch.setattr(engine.threading, "Thread", Thread)
    monkeypatch.setattr(engine, "_store_housekeeping", lambda most=None: order.append(f"prune {most}"))
    monkeypatch.setattr(engine, "_directory_later", lambda write: order.append("directory write"))
    asyncio.run(engine.lifespan(engine.app).__aenter__())  # launch only: shutdown would stop the module's loops
    assert order[0] == f"prune {engine.PRICE_CACHE_LAUNCH_MOST}" and "_worth_loop" in order and "directory write" in order


def test_housekeeping_says_what_it_removed_and_warns_on_inodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.setattr(engine, "STORE_DIR", tmp_path)
    monkeypatch.setattr(engine, "PRICE_CACHE_KEEP", 1)
    (tmp_path / "price-cache").mkdir()
    for i in range(3):
        (tmp_path / "price-cache" / f"{i}.json").write_text("[]")
    full = os.statvfs_result((4096, 4096, 100, 50, 50, 1000, 50, 50, 0, 255))  # 95% of inodes used
    monkeypatch.setattr(engine.os, "statvfs", lambda p: full)
    engine._store_housekeeping()
    assert "store: 2 old price-cache files removed; inodes 95% used" in capsys.readouterr().out
    roomy = os.statvfs_result((4096, 4096, 100, 50, 50, 1000, 900, 900, 0, 255))
    monkeypatch.setattr(engine.os, "statvfs", lambda p: roomy)
    engine._store_housekeeping()
    assert capsys.readouterr().out == ""  # nothing removed, inodes fine: quiet
