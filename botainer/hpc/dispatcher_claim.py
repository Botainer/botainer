"""Exactly one dispatcher per project — as a property, not a convention.

THE FAILURE THIS PREVENTS:

The dispatcher process is per-project — ``maybe_start`` spawns it with
``--project <this project>`` and each cycle services that one project's mailbox.
Its bookkeeping must be per-project too. A single per-user record, such as
``$MY_BOTAINER/dispatcher/pid`` with no project and no host in the path, breaks
as soon as two projects run side by side:

    A started; record = 58689 <host>
    B started; record = 58694 <host>      <- B silently overwrites A's record
    status    -> "running (pid 58694)"    <- while A is the one serving project A

and on the real session-end path (``autodispatch.stop`` sends SIGTERM, which
runs no ``finally``) such a record is left behind pointing at a dead pid, so the
next reader is told "NOT running - restart it on the LOGIN node". Following that
advice puts a SECOND dispatcher on a mailbox that already has one, and the only
guard against re-submitting a request is a read-then-act status-file check
(``dispatcher._submit_new``) — not a lock. Two dispatchers therefore double-
submit, which spends the user's allocation twice.

WHY A HEARTBEAT RATHER THAN A PID:

``$MY_BOTAINER`` is a shared home on a cluster, so a record written on login1 is
read on login2, where that pid number belongs to an unrelated process. There is
no way to check a remote pid. A heartbeat needs no such check: the holder
rewrites its claim every cycle, and any reader anywhere decides liveness from
the timestamp. A pid-based record can only ever admit that a cross-host answer
is unknown; a heartbeat answers it.

WHERE THE CLAIM LIVES:

``<mailbox>/run/dispatcher.json``. ``run/`` is host-private and NEVER bound into
any container (jobs.py INV-1), so the caged agent cannot read the claim, cannot
plant a symlink where it goes, and cannot forge a heartbeat to make its own
dispatcher look alive. Putting it in ``in/`` or ``out/`` would hand the agent a
lever on host process control.

WHAT THIS IS NOT: a security boundary. It stops ACCIDENTAL doubles — two
sessions, or a session plus a user following a stale "restart it" hint. A user
determined to run two dispatchers can still delete the claim. The cost of a race
here is a duplicate sbatch, not an escape.
"""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from botainer.hpc.jobs import JobMailbox

# A claim whose heartbeat is older than this is treated as abandoned and may be
# taken over. The dispatcher beats once per cycle (default interval 10-15s), so
# this is ~8 missed beats — long enough that a slow NFS stat or a busy login
# node never evicts a live holder, short enough that a crashed dispatcher does
# not block the next session for the length of a working day.
STALE_AFTER_SECONDS = 120

_CLAIM_NAME = "dispatcher.json"


@dataclass(frozen=True)
class Claim:
    pid: int
    host: str
    beat_at: float
    project: str

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.beat_at)

    @property
    def is_local(self) -> bool:
        return self.host == socket.gethostname()

    @property
    def expired(self) -> bool:
        return self.age > STALE_AFTER_SECONDS


def claim_file(mailbox: JobMailbox) -> Path:
    return mailbox.run_dir / _CLAIM_NAME


def _payload(project: str) -> dict:
    return {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "beat_at": time.time(),
        "project": str(project),
    }


def read_claim(mailbox: JobMailbox) -> Claim | None:
    """The current claim, or None if there is none / it is unreadable.

    An unparseable claim is treated as absent: it is our own file, so garbage
    means a truncated write, and refusing to start forever because of one is
    worse than taking it over.
    """
    path = claim_file(mailbox)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        return Claim(
            pid=int(raw["pid"]),
            host=str(raw["host"]),
            beat_at=float(raw["beat_at"]),
            project=str(raw.get("project", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _pid_is_dead_here(claim: Claim) -> bool:
    """True only when we can PROVE the holder is gone: same host, no such pid.

    PermissionError means the number now belongs to another user, i.e. it was
    recycled after our dispatcher died — also proof. On a different host we can
    prove nothing from a pid, which is what the heartbeat is for.
    """
    if not claim.is_local:
        return False
    try:
        os.kill(claim.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def acquire(mailbox: JobMailbox, project: str) -> tuple[bool, Claim | None]:
    """Try to become this project's dispatcher.

    Returns ``(won, blocking_claim)``. On success ``blocking_claim`` is None; on
    failure it is the live claim that stopped us, so the caller can say who and
    where.

    O_CREAT|O_EXCL is the fast path and the only one with no race at all. When a
    claim already exists we take it over ONLY if it is provably finished: the
    holder's pid is gone on this host, or nobody has beaten it in
    STALE_AFTER_SECONDS. Takeover then writes-and-verifies rather than
    unlink-then-create: two racers both replace the file, but only the one whose
    content survives believes it won, so they cannot both proceed. (Unlink-then-
    create loses that property — the second unlinker deletes the first winner's
    fresh claim and both then succeed.)
    """
    mailbox.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = claim_file(mailbox)
    body = json.dumps(_payload(project))

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    except OSError:
        # NOT a refusal. This is a shared network home — NFS/VAST — where a
        # transient EIO/ESTALE, or an O_EXCL whose atomicity the server does not
        # honour, is an I/O hiccup and NOT evidence that anyone else holds the
        # claim. Returning False here would make the dispatcher decline to start
        # and say nothing useful, so the user's jobs pend forever: precisely the
        # silent-failure class this module was written to remove, reintroduced
        # by its own error handling. Fall through and try the takeover path;
        # if the filesystem is genuinely broken, the write-and-verify below
        # fails loudly instead.
        pass
    else:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        return True, None

    existing = read_claim(mailbox)
    if existing is not None and not existing.expired and not _pid_is_dead_here(existing):
        return False, existing

    # Abandoned (or unreadable). Take over, then confirm the file is ours.
    tmp = path.with_name(f".{_CLAIM_NAME}.{os.getpid()}")
    try:
        tmp.write_text(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False, existing
    won = read_claim(mailbox)
    if won is not None and won.pid == os.getpid() and won.is_local:
        return True, None
    return False, won


def beat(mailbox: JobMailbox, project: str) -> bool:
    """Refresh our heartbeat. False ONLY on proof that someone else took over.

    A holder that has been taken over must not keep dispatching — that is the
    double-submit case — so the caller stops when this returns False.

    It is a kill switch, so it fires on POSITIVE EVIDENCE and nothing else. A
    failed write is not evidence: on a shared network home an I/O error is a
    hiccup, and treating it as a takeover would kill a perfectly good dispatcher
    and leave the user's jobs pending with a message blaming a dispatcher that
    does not exist. Wrong in the direction that goes quiet, which is the
    direction that costs an evening.
    """
    current = read_claim(mailbox)
    if current is not None and not (current.pid == os.getpid() and current.is_local):
        return False
    try:
        path = claim_file(mailbox)
        tmp = path.with_name(f".{_CLAIM_NAME}.{os.getpid()}")
        tmp.write_text(json.dumps(_payload(project)))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass  # could not refresh; keep dispatching (see above)
    return True


def release(mailbox: JobMailbox) -> None:
    """Drop our claim. Best-effort, and only ever removes OUR OWN.

    Guarded because the SIGTERM path (session end) and the Ctrl-C path both come
    through here, and a dispatcher that was already taken over must not delete
    its successor's claim on the way out.
    """
    current = read_claim(mailbox)
    if current is None:
        return
    if not (current.pid == os.getpid() and current.is_local):
        return
    try:
        claim_file(mailbox).unlink()
    except OSError:
        pass


def state(mailbox: JobMailbox) -> tuple[str, str]:
    """(state, detail) for this project's dispatcher: running|stale|stopped.

    Unlike the pid-only check this replaces, the answer is meaningful from any
    login node: a fresh heartbeat means someone is alive somewhere, and a stale
    one means nobody is, wherever they were.
    """
    claim = read_claim(mailbox)
    if claim is None:
        return "stopped", "no dispatcher has claimed this project"
    where = "here" if claim.is_local else f"on {claim.host}"
    if _pid_is_dead_here(claim):
        return "stale", (f"pid {claim.pid} died here without releasing its "
                         f"claim (last beat {int(claim.age)}s ago)")
    if claim.expired:
        return "stale", (f"claimed by pid {claim.pid} {where}, but its last "
                         f"heartbeat was {int(claim.age)}s ago — it is gone")
    return "running", f"pid {claim.pid} {where}, last beat {int(claim.age)}s ago"
