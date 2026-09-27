"""#216: readiness must be the daemon's own word, not "a port answers".

Both broker hooks allocated an ephemeral port by binding 0 and CLOSING the
socket, then waited by connecting to that port. Anything that grabbed the port
in the window — between our close and the daemon's bind — satisfied the check on
the first iteration. The daemon then died with EADDRINUSE and the container
talked to the squatter for the whole session, reported as a successful start.

The daemon has always printed `BOTAINER-BROKER-READY <endpoint>` after binding,
with a comment saying it exists so the hook can wait on it. Both hooks passed
stdout=DEVNULL and polled the socket anyway.

FIXTURE NOTE. These use a REAL os.pipe(), not a BytesIO. wait_for_ready selects
on the fd, so a BytesIO stand-in has no fileno() and every case falls into the
"stdout closed" branch — a fixture that makes the code path run while testing
none of it. The pipe is what a subprocess actually hands us.
"""
from __future__ import annotations

import os
from pathlib import Path

from botainer.broker.readiness import READY_MARKER, wait_for_ready


class _Proc:
    """A stand-in daemon: a real pipe for stdout, plus a scripted exit."""

    def __init__(self, chunks=(), returncode=None, keep_open=False):
        rfd, wfd = os.pipe()
        for c in chunks:
            os.write(wfd, c)
        if not keep_open:
            os.close(wfd)          # EOF after the scripted output
        else:
            self._wfd = wfd        # held open: the daemon is still running
        self.stdout = os.fdopen(rfd, "rb", buffering=0)
        self._rc = returncode
        self.returncode = returncode

    def poll(self):
        return self._rc


def test_the_marker_for_our_endpoint_is_ready() -> None:
    p = _Proc([f"{READY_MARKER} tcp=127.0.0.1:51234\n".encode()])
    ok, detail = wait_for_ready(p, "tcp=127.0.0.1:51234", timeout=2)
    assert ok is True
    assert detail == "tcp=127.0.0.1:51234"


def test_chatter_before_the_marker_does_not_confuse_it() -> None:
    p = _Proc([b"broker: resolving credential\n",
               b"broker: refreshed access token\n",
               f"{READY_MARKER} socket=/run/b.sock\n".encode()])
    ok, detail = wait_for_ready(p, "socket=/run/b.sock", timeout=2)
    assert ok is True


def test_a_marker_for_a_DIFFERENT_endpoint_is_refused() -> None:
    """THE ONE THAT MATTERS. The daemon bound something, but not what this
    session is about to tell the container to use. Handing the container an
    endpoint the daemon is not on is how a squatter receives the session."""
    p = _Proc([f"{READY_MARKER} tcp=127.0.0.1:9999\n".encode()])
    ok, detail = wait_for_ready(p, "tcp=127.0.0.1:51234", timeout=2)
    assert ok is False
    assert "9999" in detail and "51234" in detail, detail


def test_a_daemon_that_exits_before_the_marker_is_refused() -> None:
    """EADDRINUSE looks exactly like this, and the OLD check PASSED on it:
    the port answers, because the squatter is the one on it.

    The daemon's reason goes to stderr, which is redirected to the session's
    *-daemon.err file and is not on this pipe — so this asserts only what
    wait_for_ready can honestly know (it exited, and with what code). Pointing
    at the .err file is the hook's job; test_a_failed_wait_points_at_the_err_file
    below pins that half."""
    p = _Proc([], returncode=1)
    ok, detail = wait_for_ready(p, "tcp=127.0.0.1:51234", timeout=2)
    assert ok is False
    assert "exited 1" in detail, detail


def test_silence_gives_up_rather_than_hanging_the_launch() -> None:
    """A daemon that binds nothing and says nothing must not wedge `start`
    forever. Pipe held OPEN so this is a genuine timeout, not an EOF."""
    p = _Proc([], keep_open=True)
    ok, detail = wait_for_ready(p, "tcp=127.0.0.1:51234", timeout=0.5)
    assert ok is False
    assert "timed out" in detail, detail


def test_a_caller_that_did_not_capture_stdout_is_told_so() -> None:
    """Passing DEVNULL is the pre-fix behaviour. Degrading silently back to it
    would reintroduce the defect invisibly, so name it instead."""
    class _NoOut:
        stdout = None
        def poll(self): return None
    ok, detail = wait_for_ready(_NoOut(), "tcp=x", timeout=1)
    assert ok is False
    assert "not captured" in detail


def _hook_src(family: str) -> str:
    repo = Path(__file__).resolve().parents[2]
    return (repo / "plugins" / f"agent-{family}-broker" / "hooks"
            / "start_broker.py").read_text(encoding="utf-8")


def test_both_broker_hooks_use_it_and_capture_stdout() -> None:
    """The sibling half (#136): a readiness protocol implemented twice drifts.
    Three hardenings once applied to agent-claude-shared were never applied to
    agent-codex-shared. This fails if either plugin goes back to DEVNULL or
    drops the wait."""
    for fam in ("codex", "claude"):
        src = _hook_src(fam)
        assert "wait_for_ready" in src, f"{fam} does not wait for the marker"
        assert "stdout=subprocess.PIPE" in src, (
            f"{fam} spawns with stdout uncaptured, so the marker is unreadable")
        assert "stdout=subprocess.DEVNULL" not in src, (
            f"{fam} still has a DEVNULL stdout spawn")


def test_neither_hook_still_polls_the_socket_for_readiness() -> None:
    """The defect was not "no wait", it was "the WRONG wait". Leaving the
    connect-loop in beside the marker would restore it: whichever succeeds
    first wins, and the squatter's answer is the fast one."""
    for fam in ("codex", "claude"):
        src = _hook_src(fam)
        # The DEFINITION and the syscall, not the name: both hooks carry a
        # comment naming _tcp_accepting to say why it must not come back, and a
        # bare substring check would fail on that comment.
        assert "def _tcp_accepting" not in src, (
            f"{fam} still defines the connect-poll helper")
        assert "create_connection" not in src, (
            f"{fam} still probes the port to decide readiness")


def test_a_failed_wait_points_at_the_err_file() -> None:
    """wait_for_ready cannot see stderr, so the hook must send the reader to
    where the daemon's reason actually went. A refusal with no next step is the
    shape this project keeps having to fix."""
    for fam in ("codex", "claude"):
        src = _hook_src(fam)
        assert "daemon.err" in src, (
            f"{fam} never names the file holding the daemon's own reason")
