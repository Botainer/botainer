"""Warm/hot worker pool core logic (botainer/hpc/pool.py).

SLURM + apptainer are injected (run_caged / clock), so these exercise the real
task-processing, idle-timeout, stop, and hot-routing logic hermetically.
"""
from __future__ import annotations

import json
from pathlib import Path

from botainer.hpc import jobs as _jobs
from botainer.hpc import pool


def _mb(tmp_path: Path) -> _jobs.JobMailbox:
    root = tmp_path / "hpc-jobs"
    mb = _jobs.JobMailbox(root=root, in_dir=root / "in",
                          out_dir=root / "out", run_dir=root / "run")
    for d in (mb.root, mb.in_dir, mb.out_dir, mb.run_dir):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return mb


_IMG = "botainer/agent-claude:0.1@sha256:" + "a" * 64
_JID = "0123456789abcdef"


def _status_writer(mb):
    seen = {}

    def _w(jid, rec):
        seen.setdefault(jid, []).append(rec["state"])
        (mb.out_dir / f"{jid}.status.json").write_text(json.dumps(rec))
    return seen, _w


def test_worker_process_one_runs_caged_and_records_status(tmp_path) -> None:
    mb = _mb(tmp_path)
    wid = pool.new_worker_id("abcdef012345")
    pool.ensure_worker_dirs(mb, wid)
    pool.route_hot_task(mb, wid, {"version": "botainer-job-v1", "id": _JID,
                                  "profile": "gpu", "command": ["echo", "hi"]},
                        job_id=_JID)
    seen, writer = _status_writer(mb)
    ran = {}

    def _run(argv, out_p, err_p):
        ran["argv"] = argv
        return 0

    jid = pool.worker_process_one(mb, wid, _IMG, run_caged=_run,
                                  write_status=writer)
    assert jid == _JID
    # It ran a §4-caged apptainer exec of the pinned image (INV-2).
    assert ran["argv"][:2] == ["apptainer", "exec"]
    assert "--containall" in ran["argv"] and _IMG in ran["argv"]
    assert ran["argv"][-2:] == ["echo", "hi"]
    # Status went running → completed; the task was consumed.
    assert seen[_JID] == ["running", "completed"]
    assert not any(pool.worker_in_dir(mb, wid).glob("*.json"))


def test_worker_process_one_empty_inbox_returns_none(tmp_path) -> None:
    mb = _mb(tmp_path)
    wid = pool.new_worker_id("abcdef012345")
    pool.ensure_worker_dirs(mb, wid)
    _seen, writer = _status_writer(mb)
    assert pool.worker_process_one(mb, wid, _IMG, run_caged=lambda *a: 0,
                                   write_status=writer) is None


def test_worker_process_one_refuses_a_runtime_command(tmp_path) -> None:
    mb = _mb(tmp_path)
    wid = pool.new_worker_id("abcdef012345")
    pool.ensure_worker_dirs(mb, wid)
    # command[0] is a container runtime → the cage compose refuses it.
    pool.route_hot_task(mb, wid, {"id": _JID, "command": ["apptainer", "exec", "x"]},
                        job_id=_JID)
    seen, writer = _status_writer(mb)
    called = {"n": 0}

    def _run(*_a):
        called["n"] += 1
        return 0

    pool.worker_process_one(mb, wid, _IMG, run_caged=_run, write_status=writer)
    assert called["n"] == 0                       # never ran
    assert seen[_JID] == ["refused"]


def test_worker_loop_exits_on_idle_timeout(tmp_path) -> None:
    mb = _mb(tmp_path)
    wid = pool.new_worker_id("abcdef012345")
    clock = {"t": 1000.0}
    reason = pool.worker_loop(
        mb, wid, _IMG, idle_timeout=30,
        run_caged=lambda *a: 0, write_status=lambda *a: None,
        now=lambda: clock["t"],
        sleep=lambda _s: clock.__setitem__("t", clock["t"] + 20),
        poll_interval=20, max_iterations=10)
    assert reason == "idle-timeout"


def test_worker_loop_stops_on_stop_marker(tmp_path) -> None:
    mb = _mb(tmp_path)
    wid = pool.new_worker_id("abcdef012345")
    pool.ensure_worker_dirs(mb, wid)
    (pool.worker_dir(mb, wid) / "STOP").write_text("")
    reason = pool.worker_loop(
        mb, wid, _IMG, idle_timeout=300,
        run_caged=lambda *a: 0, write_status=lambda *a: None,
        now=lambda: 1000.0, sleep=lambda _s: None, max_iterations=3)
    assert reason == "stopped"


def test_find_idle_worker_matches_profile_and_freshness(tmp_path) -> None:
    mb = _mb(tmp_path)
    wid = pool.new_worker_id("abcdef012345")
    pool.ensure_worker_dirs(mb, wid)
    pool.write_pool_state(mb, [pool.WorkerRec(wid, "555", "gpu", 300, "now")])
    pool.write_beat(mb, wid, "idle", 1000.0)
    # fresh + idle + right profile → matched
    assert pool.find_idle_worker(mb, "gpu", now=1000.0) == wid
    # wrong profile → no match
    assert pool.find_idle_worker(mb, "cpu", now=1000.0) is None
    # stale heartbeat → no match
    assert pool.find_idle_worker(mb, "gpu", now=2000.0, stale_after=90) is None
    # busy → no match
    pool.write_beat(mb, wid, "busy", 1000.0)
    assert pool.find_idle_worker(mb, "gpu", now=1000.0) is None


def test_find_idle_worker_skips_worker_with_queued_task(tmp_path) -> None:
    mb = _mb(tmp_path)
    wid = pool.new_worker_id("abcdef012345")
    pool.ensure_worker_dirs(mb, wid)
    pool.write_pool_state(mb, [pool.WorkerRec(wid, "555", "gpu", 300, "now")])
    pool.write_beat(mb, wid, "idle", 1000.0)
    pool.route_hot_task(mb, wid, {"id": _JID, "command": ["echo"]}, job_id=_JID)
    # inbox not empty → not eligible (avoid double-assign)
    assert pool.find_idle_worker(mb, "gpu", now=1000.0) is None
