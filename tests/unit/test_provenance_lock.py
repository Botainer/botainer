"""installed.lock concurrency — flock serializes mutations (AUDIT).

installed.lock is mutated by append_lock() (append a line) AND by
cli/image.py._record_image_digest() (read-modify-WRITE the whole file).
Concurrent without a lock → lost update (a rewrite based on a stale read
clobbers a concurrent append). provenance.lock_for() flocks a sibling guard so
both paths serialize.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from botainer.plugins import provenance as p


def _entry(name: str) -> p.ProvenanceEntry:
    return p.ProvenanceEntry(
        name=name, version="1", source="s", tree_sha="x",
        image_digest=None, installed_at="t", tier="third-party",
    )


def test_lock_for_serializes_holders(tmp_path: Path) -> None:
    """lock_for must give mutual exclusion: concurrent holders never overlap
    (each call opens its own fd, so flock serializes even within one process)."""
    lock = tmp_path / "installed.lock"
    state = {"cur": 0, "max": 0}
    guard = threading.Lock()

    def worker() -> None:
        with p.lock_for(lock):
            with guard:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.03)  # window in which an overlap would be observed
            with guard:
                state["cur"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max"] == 1, (
        f"lock_for allowed {state['max']} concurrent holders — not mutually "
        f"exclusive; installed.lock mutations could lose updates"
    )


def test_concurrent_appends_all_survive(tmp_path: Path) -> None:
    """All concurrent append_lock() writes survive (the lock serializes them)."""
    lock = tmp_path / "installed.lock"
    names = [f"p{i}" for i in range(12)]
    threads = [threading.Thread(target=p.append_lock, args=(lock, _entry(n)))
               for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = {e.name for e in p.read_lock(lock)}
    assert got == set(names), f"lost an append under concurrency: missing {set(names) - got}"
