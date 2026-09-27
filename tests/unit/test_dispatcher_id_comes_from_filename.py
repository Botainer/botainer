"""One request has ONE id, and it comes from the filename.

THE DEFECT (#207, found by the jobs tzar 2026-08-31). The dispatch cycle keyed
its idempotence check on the FILENAME (`dispatcher.py:489`), while
`submit_request` took its id from the request BODY. Two ids for one request,
each separately validated, neither reconciled. A request whose body `id`
differed produced TWO status files: the real one under the body id, and an
ORPHAN under the filename id that no later cycle would read. The agent's view of
its own job and the dispatcher's view then lived under different names.

STRUCTURAL FIX, following pool.py's `route_hot_task`, which had the identical
defect fixed as CRITICAL-2 and which CLAUDE.md cites as the worked example of
turning a filter into a property: the body `id` is no longer CONSULTED. A
divergent one is inert, not "rejected".

ON SEVERITY, recorded so nobody reads this test as guarding something bigger
than it did. The tzar reported unbounded resubmission ("3 cycles -> 3 sbatch
calls... forever"). It did not reproduce: measured one sbatch call over ten
cycles at three concurrency settings, because cycle 2 wrote a `deferred` status
under the FILENAME id and that is the key the check reads. The defect was
disagreeing records, not a scheduler flood. A first reproduction attempt using
max_concurrent=1 masked the behaviour entirely and nearly produced the opposite
wrong conclusion.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from botainer.core.config import JobProfile
from botainer.core.policy import JobPolicy
from botainer.hpc import dispatcher
from botainer.hpc import jobs as _jobs

FNAME_ID = "a" * 16
BODY_ID = "b" * 16


def _mailbox(tmp_path: Path) -> _jobs.JobMailbox:
    for d in ("in", "out", "run"):
        (tmp_path / d).mkdir()
    return _jobs.JobMailbox(root=tmp_path, in_dir=tmp_path / "in",
                            out_dir=tmp_path / "out", run_dir=tmp_path / "run")


def _profiles() -> dict:
    return {"quick": JobProfile(partition="p", cpus=1, time="00:10:00")}


def _cycles(mb, n: int):
    calls: list[str] = []

    def sbatch(script_path, profile_name):
        calls.append(profile_name)
        return str(1000 + len(calls))

    for _ in range(n):
        dispatcher.process_inbox_once(mb, _profiles(), JobPolicy(), "img",
                                      sbatch=sbatch, now="2026-08-31T00:00:00Z")
    return calls


def test_a_divergent_body_id_writes_no_orphan_status(tmp_path) -> None:
    """The observable defect: one request, two status files, one unreadable."""
    mb = _mailbox(tmp_path)
    (mb.in_dir / f"{FNAME_ID}.json").write_text(json.dumps(
        {"id": BODY_ID, "profile": "quick", "command": ["hostname"]}))

    _cycles(mb, 4)
    written = sorted(p.name for p in mb.out_dir.iterdir())

    assert written == [f"{FNAME_ID}.status.json"], (
        f"expected exactly one status file, keyed on the FILENAME id; got "
        f"{written}. A file under the body id is an orphan — no cycle reads it."
    )


def test_the_status_record_carries_the_filename_id(tmp_path) -> None:
    """Not just the path — the `id` INSIDE the record too, since that is what
    `botainer-job status` shows the agent."""
    mb = _mailbox(tmp_path)
    (mb.in_dir / f"{FNAME_ID}.json").write_text(json.dumps(
        {"id": BODY_ID, "profile": "quick", "command": ["hostname"]}))

    _cycles(mb, 2)
    rec = json.loads((mb.out_dir / f"{FNAME_ID}.status.json").read_text())

    assert rec["id"] == FNAME_ID
    assert rec["id"] != BODY_ID


def test_a_matching_id_is_unaffected(tmp_path) -> None:
    """The ordinary case — every real request botainer-job writes — must behave
    exactly as before. A fix that changed the normal path would be a worse bug
    than the one it closed."""
    mb = _mailbox(tmp_path)
    (mb.in_dir / f"{FNAME_ID}.json").write_text(json.dumps(
        {"id": FNAME_ID, "profile": "quick", "command": ["hostname"]}))

    calls = _cycles(mb, 4)

    assert len(calls) == 1, "one request, one submission"
    rec = json.loads((mb.out_dir / f"{FNAME_ID}.status.json").read_text())
    assert rec["state"] == "queued"
    assert rec["id"] == FNAME_ID


def test_submission_happens_exactly_once_either_way(tmp_path) -> None:
    """Pins the severity claim rather than leaving it in a commit message: the
    divergence did not cause repeated submission, before OR after the fix."""
    mb = _mailbox(tmp_path)
    (mb.in_dir / f"{FNAME_ID}.json").write_text(json.dumps(
        {"id": BODY_ID, "profile": "quick", "command": ["hostname"]}))

    assert len(_cycles(mb, 10)) == 1
