"""EOF on the daemon's stdout must not leave `proc.poll()` racing. (#222)

WHAT ACTUALLY WENT WRONG — and the first diagnosis of it was wrong, which is
worth recording because the wrong one was plausible and self-confirming.

Both broker hooks decide between two error messages like this:

    _ok, _why = wait_for_ready(proc, ...)
    if not _ok:
        if proc.poll() is not None:
            return _daemon_died()          # read the daemon's OWN stderr
        ...terminate...  "broker did not become ready: ..."

`wait_for_ready` returns on EOF from the daemon's stdout. The kernel delivers
that EOF during `exit_files()` — BEFORE the child becomes reapable in
`exit_notify()`. Under load the parent wins that gap, `poll()` returns None for
a process that has already exited, and the hook takes the "alive but never
reported ready" branch: it terminates a corpse and prints a generic message,
never reading the stderr file where the daemon said why it failed.

THE FIRST DIAGNOSIS WAS THAT THE STDERR FILE WAS EMPTY — a flush race — and the
first fix waited inside `_daemon_died()`. That fix was unreachable by
construction: `_daemon_died()` is only called after `poll()` has already
returned non-None, which sets `returncode`, which makes `Popen.wait()` return
instantly. Measurement killed it: at the moment of failure the stderr file
already held the daemon's real reason (171 bytes, in the expired-token fixture)
on every run, and `_daemon_died()` was never entered at all. The defect was the
BRANCH, not the read. Recorded because "read the code, it agrees with me" is how
the wrong version survived.

WHY THESE TESTS DRIVE REAL SUBPROCESSES. The race is between two kernel events
in a real process teardown; a mocked `poll()` would encode whatever belief the
author already held, which is the hollow-test shape this suite keeps producing.
`wait_for_ready` takes `proc` as a parameter, so a real child that closes stdout
and then lingers reproduces it exactly, deterministically, with no load needed.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from botainer.broker.readiness import wait_for_ready

ENDPOINT = "tcp=127.0.0.1:51234"

# Closes stdout, lingers, THEN exits: EOF reaches the parent well before the
# child is reapable. This is the real teardown ordering, slowed down so the
# window is deterministic instead of load-dependent.
_CLOSES_STDOUT_THEN_LINGERS = (
    "import os, sys, time; os.close(1); time.sleep(0.4); sys.exit(3)"
)

# Never closes stdout, never speaks: the "alive but silent" case, which must
# still be reported as alive and must NOT be waited on.
_HANGS_SILENTLY = "import time; time.sleep(30)"


def _spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def test_a_daemon_that_has_exited_is_reported_as_exited(request) -> None:
    """The branch the hook takes must be the true one.

    `poll() is not None` is the hook's entire test for "read its stderr and tell
    the user what it said" vs "generic timeout message". Before the fix this was
    None here, so the user got the generic message and the daemon's own reason
    was never read.
    """
    proc = _spawn(_CLOSES_STDOUT_THEN_LINGERS)
    request.addfinalizer(lambda: (proc.kill(), proc.wait()))

    ok, why = wait_for_ready(proc, ENDPOINT, timeout=10.0)

    assert not ok, f"a daemon that never said ready was reported ready: {why!r}"
    assert "closed stdout" in why, (
        f"took the {why!r} path, not the EOF path this test exists to pin. "
        "Under enough starvation the child exits before wait_for_ready is even "
        "entered, the loop-top poll() wins, and the assertion below is then "
        "satisfied by the UNFIXED module — a vacuous pass, not a flake.")
    assert proc.poll() is not None, (
        "wait_for_ready returned while the daemon's exit status was still "
        "unsettled, so the hook's `if proc.poll() is not None` asks a racing "
        "question and takes the 'alive but never reported ready' branch for a "
        "process that is already dead")


def test_the_exit_CODE_is_available_not_merely_non_none(request) -> None:
    """`_daemon_died()` reports the code, so a settled-but-wrong code is a bug.

    Separate from the branch assertion above: a fix that merely made `poll()`
    non-None without reaping correctly would satisfy that one and still report
    the wrong thing to the user.
    """
    proc = _spawn(_CLOSES_STDOUT_THEN_LINGERS)
    request.addfinalizer(lambda: (proc.kill(), proc.wait()))

    _ok, why = wait_for_ready(proc, ENDPOINT, timeout=10.0)

    assert "closed stdout" in why, (
        f"took the {why!r} path, not the EOF path; see the note in the test "
        "above — this assertion would otherwise pass on the unfixed module")
    assert proc.returncode == 3, (
        f"expected the daemon's real exit code 3, got {proc.returncode!r}")


def test_a_daemon_that_is_still_ALIVE_is_not_waited_for(request) -> None:
    """Guards the fix against becoming "wait everywhere".

    Settling on the timeout path would add _SETTLE_TIMEOUT to every genuinely
    hung daemon, on top of the readiness timeout the user already paid. The
    hook's terminate branch is correct for this case and depends on `poll()`
    still reporting None.
    """
    proc = _spawn(_HANGS_SILENTLY)
    request.addfinalizer(lambda: (proc.kill(), proc.wait()))

    started = time.monotonic()
    ok, why = wait_for_ready(proc, ENDPOINT, timeout=0.5)
    elapsed = time.monotonic() - started

    assert not ok
    assert "timed out" in why, f"expected the timeout exit, got {why!r}"
    assert proc.poll() is None, (
        "a live daemon was reported as exited; the hook would try to read a "
        "stderr file for a process that is still running")
    assert elapsed < 3.0, (
        f"the timeout path waited {elapsed:.1f}s — the settle is being applied "
        "to a daemon that is alive, which adds latency to every hung start")


def test_a_daemon_that_exited_long_ago_still_reports_its_code(request) -> None:
    """No regression on the path that already worked.

    Here the child is reapable before `wait_for_ready` is even called, so the
    old code got this right. It must stay right.
    """
    proc = _spawn("import os, sys; os.close(1); sys.exit(7)")
    request.addfinalizer(lambda: (proc.kill(), proc.wait()))
    proc.wait(timeout=10)

    ok, why = wait_for_ready(proc, ENDPOINT, timeout=10.0)

    assert not ok
    assert proc.returncode == 7, f"got {proc.returncode!r}; why={why!r}"
