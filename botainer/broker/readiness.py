"""Wait for the broker daemon's OWN readiness marker, not for "a port answers".

WHY (#216, found by review 2026-09-04). Both broker hooks allocated a port by
binding 0, reading the number, and CLOSING the socket; the daemon rebinds it
later — after resolving the credential and, in subscription mode, possibly a
network OAuth refresh. That is a window of hundreds of milliseconds to seconds
in which the port belongs to nobody.

The hooks then waited by CONNECTING to the port:

    for _ in range(60):
        if _tcp_accepting(host, port):   # someone answered
            break

Anything that grabs the port in the window satisfies that check on the first
iteration. The daemon has not exited yet, so the liveness guard beside it does
not fire either, and the hook reports success. The daemon then dies with
EADDRINUSE — `allow_reuse_address` is SO_REUSEADDR, which does not permit a
second listener — and the container talks to the squatter for the whole session.

It does not get the credential: the daemon is the only thing that holds it. It
does get the agent's prompts and whatever the agent sends, and it can return
arbitrary content to an agent running with `--sandbox danger-full-access`.

This matters more for codex than for Claude: codex has no unix-socket support,
so it is TCP even under apptainer, where 127.0.0.1 is the shared loopback of a
multi-user login node.

WHAT THIS CHANGES, precisely. The daemon already prints

    BOTAINER-BROKER-READY tcp=127.0.0.1:51234

on stdout after `server_bind()` succeeds, with a comment saying it exists "so
the spawning hook can wait for readiness instead of polling the socket path" —
and both hooks passed `stdout=DEVNULL` and polled anyway. Reading it means
readiness is proven by OUR process, and the endpoint it reports is compared to
the endpoint we intend to use.

IT DOES NOT CLOSE THE RACE, and saying otherwise would be the kind of
overstatement this project keeps correcting. A squatter can still take the port
between our close and the daemon's bind. What changes is the OUTCOME: instead of
a silent takeover reported as success, the daemon fails to bind, we never see
the marker, and the session refuses with the daemon's own stderr. A loud failure
instead of a quiet compromise.

The structural fix is to pass the already-bound socket to the daemon by fd
inheritance, so there is no window at all. That is a larger change to
daemon_main's argument surface and is tracked, not done here.

ONE IMPLEMENTATION, IMPORTED BY BOTH PLUGINS. A readiness protocol duplicated in
two hooks is the sibling-drift shape (#136): three hardenings once applied to
agent-claude-shared and never to agent-codex-shared. Both hooks already import
from `botainer.*`, so there is no reason for a second copy of this.
"""
from __future__ import annotations

import select
import subprocess
import time

READY_MARKER = "BOTAINER-BROKER-READY"

# How long to let an already-exiting daemon become reapable. Measured in the
# real hook path under load: median ~216us, worst observed ~2.5ms. Sub-millisecond
# typically, milliseconds at the tail — nowhere near the bound.
#
# The bound exists so a daemon that closes stdout and then WEDGES cannot hang the
# hook. If it expires, `proc.poll()` still reports None and the caller takes its
# "alive but never reported ready" branch, which is the correct outcome for a
# wedged daemon.
_SETTLE_TIMEOUT = 5.0


def _settle(proc) -> None:
    """Make ``proc.poll()`` authoritative before we report the daemon gone.

    THE RACE THIS REMOVES (#233). EOF on the daemon's stdout is delivered by the
    kernel during ``exit_files()`` — *before* the child becomes reapable in
    ``exit_notify()``. Both hooks then asked:

        _ok, _why = wait_for_ready(proc, ...)
        if not _ok:
            if proc.poll() is not None:
                return _daemon_died()      # report the daemon's OWN reason
            ... terminate; "did not become ready" ...

    Under load the parent wins that gap. `poll()` returns None for a daemon that
    has in fact already exited, so the hook takes the wrong branch: it reports a
    generic "did not become ready" and never reads the stderr file where the
    daemon said *why* — an expired credential, a bind failure, a bad upstream.
    Measured directly: at the moment of failure the stderr file held 171 bytes
    of the real reason and nothing looked at it.

    THIS IS A BOUNDED WAIT, NOT A STRUCTURAL GUARANTEE, and the distinction is
    worth stating because the first version of this docstring got it wrong.

    It claimed EOF "already MEANS the daemon is gone", so `poll()` was merely
    re-asking a settled question. That is false, and the counterexample is in
    this module's own test suite: a child that does `os.close(1)` and then
    lingers produces EOF 0.4s before it exits. The `_SETTLE_TIMEOUT` comment
    above says the same thing — "closes stdout and then WEDGES" — so the file
    contradicted itself. A bounded wait described as a property is the
    filter-documented-as-a-guarantee error, which this project treats as the
    same class as a false safety verdict.

    What is true, and enough:

      * EOF means the daemon can NEVER report ready, so this call is over
        either way; nothing is lost by waiting.
      * IF the daemon is exiting, EOF precedes reapability, so the caller's
        `poll()` would race. Waiting removes that race.
      * IF it is not exiting, the bound expires, `poll()` reports None, and the
        caller correctly terminates it.

    Every outcome is right, and the wait is capped. For the real daemon the
    stronger property does hold — it never forks (verified: neither
    daemon_main nor daemon.py forks; they use threads), so EOF does imply exit —
    but `wait_for_ready` accepts any `proc`, including fakes in this suite, so
    the function must not depend on it.

    Fixing it here rather than in the hooks is the point of this module: both
    broker plugins call `wait_for_ready`, and patching two copies is the
    sibling-drift shape (#136) this file's docstring already warns about.

    NEVER RAISES, for the same reason `wait_for_ready` never does.
    """
    # NARROW ON PURPOSE. `except Exception` also swallowed AttributeError, and
    # this suite has a `_Proc` fake with no `.wait()` — so the next EOF test
    # written with that fixture would pass while this function silently did
    # nothing. TimeoutExpired is the only exception a real Popen raises here and
    # the only one the NEVER RAISES contract needs absorbed; anything else is a
    # broken caller and should say so.
    #
    # KeyboardInterrupt/SystemExit are BaseException and were never caught by
    # the old clause either. That is load-bearing: Popen.wait re-raises
    # KeyboardInterrupt after a grace wait, so Ctrl-C during a broker start
    # still works.
    try:
        proc.wait(timeout=_SETTLE_TIMEOUT)
    except subprocess.TimeoutExpired:
        pass


def wait_for_ready(proc, expect_endpoint: str, *,
                   timeout: float = 30.0) -> tuple[bool, str]:
    """Block until the daemon says it is listening, or give up.

    `proc` must have been spawned with ``stdout=subprocess.PIPE``.
    `expect_endpoint` is the string the daemon will report, e.g.
    ``"tcp=127.0.0.1:51234"`` or ``"socket=/path"``.

    Returns ``(ready, detail)``. `detail` is for the caller's error message:
    what was seen instead, or why the wait ended.

    NEVER RAISES. A hook that dies while waiting for readiness leaves an orphan
    daemon holding the real credential on a live socket — strictly worse than
    reporting a failure the caller can act on.

    WHICH RETURNS SETTLE ``proc.poll()``, EXHAUSTIVELY (#222). Callers
    distinguish "died" from "alive but silent" with `poll()`, and EOF arrives
    before the child is reapable, so an unsettled return makes that question
    race. Of the six failure returns here:

      SETTLED — a dying daemon can reach these
        * the loop-top `proc.poll()` return (already settled by definition)
        * the stdout-unusable return (select or readline raised)
        * the EOF return

      NOT SETTLED, deliberately
        * TIMEOUT. The daemon was alive at a poll ≤0.25s earlier. Waiting on a
          genuinely hung daemon would add `_SETTLE_TIMEOUT` on top of the
          `timeout` already paid. A daemon that dies inside that window produces
          EOF, and EOF is settled — so reaching the timeout with a corpse needs
          another holder of the write end, i.e. a fork, which the real daemon
          never does. If it were reached, the cost is the daemon's specific
          reason, not correctness: the hook's terminate-a-corpse path is safe
          because `Popen.send_signal` polls first and suppresses
          `ProcessLookupError`.
        * ENDPOINT MISMATCH. The daemon is alive and listening on the wrong
          endpoint; "not exited" is the true answer.
        * STDOUT NOT CAPTURED. The caller passed DEVNULL, so there is no marker
          to wait for and no EOF to observe. Nothing about the daemon is known
          here, and this function should not invent it.
    """
    if proc.stdout is None:
        # Caller passed DEVNULL. Say so rather than silently degrading to the
        # old behaviour, which is the defect this module exists to remove.
        return False, "daemon stdout was not captured; cannot read the marker"

    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, (f"timed out after {timeout:g}s waiting for "
                           f"{READY_MARKER}; saw: {' | '.join(seen[-3:]) or '(nothing)'}")
        if proc.poll() is not None:
            return False, (f"daemon exited {proc.returncode} before reporting "
                           f"ready; saw: {' | '.join(seen[-3:]) or '(nothing)'}")
        try:
            r, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.25))
            # readline() IS INSIDE THE GUARD, and that is the point of its shape.
            # The guard used to wrap select() alone — but select only fails when
            # the fd is numerically unusable (>= FD_SETSIZE), which needs a
            # thousand open fds in a hook that holds a handful. Every realistic
            # way the pipe goes bad surfaces one line later, in readline():
            # closing the file object raises ValueError there, deterministically;
            # closing the fd raises OSError EBADF.
            #
            # Unguarded, that made this function RAISE, contradicting the
            # NEVER RAISES contract above and producing exactly the harm that
            # contract names — the hook dies and orphans a daemon still holding
            # the real credential on a live socket.
            line = proc.stdout.readline() if r else None
        except (OSError, ValueError):
            # Our end of the pipe is unusable, so the daemon will never reach us
            # again whether or not it is still running. Settle either way: if it
            # died we want its reason, and if it is alive the caller terminates
            # it after the bounded wait.
            _settle(proc)
            return False, "daemon stdout closed while waiting for the marker"
        if line is None:
            continue          # select timed out; nothing readable yet
        if not line:
            # EOF: the daemon closed stdout without ever saying ready. It is on
            # its way out but is not necessarily reapable YET, so settle before
            # the caller asks `proc.poll()` — see `_settle` (#233).
            _settle(proc)
            return False, (f"daemon closed stdout without {READY_MARKER}; "
                           f"saw: {' | '.join(seen[-3:]) or '(nothing)'}")
        text = line.decode("utf-8", "replace").strip() if isinstance(line, bytes) \
            else line.strip()
        if not text:
            continue
        seen.append(text)
        if text.startswith(READY_MARKER):
            reported = text[len(READY_MARKER):].strip()
            if reported != expect_endpoint:
                # It bound SOMETHING, but not what we are about to tell the
                # container to use. Refuse rather than hand the container an
                # endpoint the daemon is not on.
                return False, (f"daemon reported {reported!r} but this session "
                               f"expects {expect_endpoint!r}")
            return True, reported
