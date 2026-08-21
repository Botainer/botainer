"""One dispatcher per project, and a liveness answer that works cross-node.

Observed behaviour that motivated this (run, not reasoned about):

    A started (project A); record = 58689 <host>
    B started (project B); record = 58694 <host>   <- B overwrote A's record
    status -> "running (pid 58694)"                <- while A serves project A

    SIGTERM (what autodispatch.stop actually sends) -> process dead,
    record STILL PRESENT -> next reader told "NOT running, restart it"
    -> user starts a second dispatcher on a mailbox that already has one.

The re-submit guard is a read-then-act status-file check, not a lock, so two
dispatchers on one mailbox submit every request twice and bill the allocation
twice.

These tests pin the properties, not the prose:
  - two DIFFERENT projects never contend
  - the same project cannot be claimed twice
  - a heartbeat, not a pid, decides liveness (so it answers across login nodes)
  - a holder that lost its claim stops dispatching
  - release only ever removes the holder's OWN claim
"""
from __future__ import annotations

import json
import os
import socket
import time

import pytest

from botainer.hpc import dispatcher_claim as claim
from botainer.hpc.jobs import JobMailbox


def _mailbox(tmp_path, name):
    root = tmp_path / name
    mb = JobMailbox(root=root, in_dir=root / "in", out_dir=root / "out",
                    run_dir=root / "run")
    mb.run_dir.mkdir(parents=True, exist_ok=True)
    return mb


def test_two_different_projects_do_not_contend(tmp_path):
    """The bug in one line: project B's dispatcher used to evict project A's."""
    a, b = _mailbox(tmp_path, "projA"), _mailbox(tmp_path, "projB")

    won_a, _ = claim.acquire(a, "/p/A")
    won_b, _ = claim.acquire(b, "/p/B")

    assert won_a and won_b, "two projects must both get a dispatcher"
    assert claim.state(a)[0] == "running"
    assert claim.state(b)[0] == "running", (
        "project B's claim did not survive project A's — this is the per-user "
        "record bug, reproduced")

    # ...and B going away must not affect A.
    claim.release(b)
    assert claim.state(a)[0] == "running", "releasing B killed A's record"
    assert claim.state(b)[0] == "stopped"


def test_same_project_cannot_be_claimed_twice(tmp_path):
    mb = _mailbox(tmp_path, "proj")
    won_first, _ = claim.acquire(mb, "/p")
    won_second, blocking = claim.acquire(mb, "/p")

    assert won_first
    assert not won_second, (
        "a second dispatcher claimed a project that already had one — two on "
        "one mailbox double-submit every job")
    assert blocking is not None and blocking.pid == os.getpid()


def test_liveness_is_a_heartbeat_not_a_pid(tmp_path):
    """Cross-node: a pid from login1 is meaningless on login2; a beat is not."""
    mb = _mailbox(tmp_path, "proj")
    claim.claim_file(mb).write_text(json.dumps({
        "pid": 999999, "host": "login1.example", "beat_at": time.time(),
        "project": "/p"}))

    state, detail = claim.state(mb)

    assert state == "running", (
        f"a fresh heartbeat from another login node was read as {state!r} — "
        "the mailbox is on shared home, so that dispatcher IS serving us")
    assert "login1.example" in detail


def test_expired_heartbeat_is_stale_even_on_another_host(tmp_path):
    mb = _mailbox(tmp_path, "proj")
    claim.claim_file(mb).write_text(json.dumps({
        "pid": 999999, "host": "login1.example",
        "beat_at": time.time() - (claim.STALE_AFTER_SECONDS + 30),
        "project": "/p"}))

    state, _ = claim.state(mb)

    assert state == "stale", "a dispatcher that stopped beating is gone"


def test_dead_local_pid_is_stale_even_with_a_fresh_beat(tmp_path):
    """Proof beats timing: same host + no such pid means it is finished."""
    mb = _mailbox(tmp_path, "proj")
    claim.claim_file(mb).write_text(json.dumps({
        "pid": 999999, "host": socket.gethostname(), "beat_at": time.time(),
        "project": "/p"}))

    state, _ = claim.state(mb)

    assert state == "stale"


def test_abandoned_claim_can_be_taken_over(tmp_path):
    """A crashed dispatcher must not block the project forever."""
    mb = _mailbox(tmp_path, "proj")
    claim.claim_file(mb).write_text(json.dumps({
        "pid": 999999, "host": "login1.example",
        "beat_at": time.time() - (claim.STALE_AFTER_SECONDS + 30),
        "project": "/p"}))

    won, _ = claim.acquire(mb, "/p")

    assert won, "an abandoned claim was never reclaimable"
    assert claim.read_claim(mb).pid == os.getpid()


def test_a_holder_that_lost_its_claim_stops(tmp_path):
    """beat() is the kill switch: if we were taken over, we must not dispatch."""
    mb = _mailbox(tmp_path, "proj")
    claim.acquire(mb, "/p")
    assert claim.beat(mb, "/p") is True

    # someone else took over
    claim.claim_file(mb).write_text(json.dumps({
        "pid": os.getpid() + 1, "host": socket.gethostname(),
        "beat_at": time.time(), "project": "/p"}))

    assert claim.beat(mb, "/p") is False, (
        "a dispatcher whose claim was taken kept beating — both would then "
        "dispatch, which is the double-submit")


def test_release_never_removes_someone_elses_claim(tmp_path):
    mb = _mailbox(tmp_path, "proj")
    other = {"pid": os.getpid() + 1, "host": socket.gethostname(),
             "beat_at": time.time(), "project": "/p"}
    claim.claim_file(mb).write_text(json.dumps(other))

    claim.release(mb)

    assert claim.read_claim(mb) is not None, (
        "release() deleted a claim belonging to another process")


def test_claim_lives_in_the_host_private_dir(tmp_path):
    """The agent must not be able to read, forge or symlink-target the claim.

    run/ is the only mailbox dir never bound into a container (jobs.py INV-1).
    """
    mb = _mailbox(tmp_path, "proj")
    assert claim.claim_file(mb).parent == mb.run_dir
    assert mb.run_dir.name == "run"


def test_unreadable_claim_does_not_wedge_the_project(tmp_path):
    mb = _mailbox(tmp_path, "proj")
    claim.claim_file(mb).write_text("{ truncated")

    won, _ = claim.acquire(mb, "/p")

    assert won, "a corrupt claim file blocked the dispatcher forever"


@pytest.mark.parametrize("sig_name", ["SIGTERM"])
def test_sigterm_releases_the_claim(tmp_path, sig_name):
    """The REAL session-end path. Python's default SIGTERM runs no `finally`,
    which is why the old record was always left behind."""
    import signal
    import subprocess
    import sys
    import textwrap

    mb_root = tmp_path / "proj"
    (mb_root / "run").mkdir(parents=True)
    script = textwrap.dedent(f"""
        import signal, sys, time
        sys.path.insert(0, {str(__import__("pathlib").Path("/workspace"))!r})
        from botainer.hpc.dispatcher_claim import acquire, release
        from botainer.hpc.jobs import JobMailbox
        from pathlib import Path
        root = Path({str(mb_root)!r})
        mb = JobMailbox(root=root, in_dir=root/"in", out_dir=root/"out",
                        run_dir=root/"run")
        def on_term(s, f): raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, on_term)
        acquire(mb, "/p")
        try:
            while True: time.sleep(0.05)
        except KeyboardInterrupt:
            pass
        finally:
            release(mb)
    """)
    p = subprocess.Popen([sys.executable, "-c", script])
    for _ in range(100):
        if (mb_root / "run" / "dispatcher.json").exists():
            break
        time.sleep(0.05)
    assert (mb_root / "run" / "dispatcher.json").exists(), "never claimed"

    p.send_signal(getattr(signal, sig_name))
    p.wait(timeout=10)

    assert not (mb_root / "run" / "dispatcher.json").exists(), (
        "SIGTERM left the claim behind — the next reader is told 'NOT "
        "running, restart it' and starts a second dispatcher")


def test_dispatcher_start_actually_runs(tmp_path, monkeypatch):
    """Drive the real command far enough to reach its loop.

    THIS TEST EXISTS BECAUSE ITS ABSENCE SHIPPED A CRASH. `dispatcher_start`
    gained a log call referencing `_identity`, which is imported locally in a
    DIFFERENT function — NameError before the first cycle, so the dispatcher
    died instantly and every dispatched job would have pended forever. The full
    2304-test suite passed: the one test that drove this function had been
    deleted when this file was rewritten for the claim architecture.

    A unit test of the claim is not a test of the command. Anything that runs
    only inside `dispatcher_start` — the claim, the SIGTERM handler, the log,
    the heartbeat kill switch — is invisible unless something invokes it.
    """
    import json
    from click.testing import CliRunner
    from botainer.cli import hpc as m

    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "44444444-4444-4444-8444-444444444444")
    (proj / ".botainer" / "config.yaml").write_text(
        "agent: claude\n"
        "job_profiles:\n"
        "  quick:\n"
        "    partition: general\n"
        "    cpus: 1\n"
        "    memory: 4G\n"
        '    time: "00:10:00"\n')
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))

    seen = {}

    def one_cycle(_p):
        mb = m._project_mailbox(proj)
        seen["claim"] = json.loads((mb.run_dir / "dispatcher.json").read_text())
        seen["log"] = (mb.run_dir / "dispatcher.log").read_text()
        raise KeyboardInterrupt

    monkeypatch.setattr(m, "_run_dispatcher_cycle", one_cycle)
    res = CliRunner().invoke(
        m.dispatcher_start, ["--project", str(proj), "--interval", "1"])

    assert res.exception is None or isinstance(res.exception, SystemExit), (
        f"dispatcher_start raised before its loop: {res.exception!r}")
    assert seen.get("claim"), "no claim was taken; the loop was never reached"
    assert "started pid=" in seen.get("log", ""), (
        "the dispatcher log was never written — an auto-started dispatcher has "
        "stdout and stderr on /dev/null, so this file is the ONLY place its "
        "errors can appear")
