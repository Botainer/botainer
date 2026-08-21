"""Append-only record of what happens to credentials on this host.

WHY THIS EXISTS. Every question asked during the credential review
turned out to be unanswerable: how often does a container actually refresh and
break away from the shared store? Has anyone ever logged in as a second account
mid-session? Is the staleness window minutes or hours? Both the maintainer and
the reviewer were guessing, and designs were being argued from those guesses.

A week of this log answers all of them from data. It was deliberately built
FIRST, before any of the fixes it informs, because it is read-only with respect
to every problem it observes and it turns the next round of decisions into
measurements rather than arguments.

WHERE. ``<state_root>/logs/credential-events.jsonl`` — host-private, 0600,
outside `shared-auth/` and outside `state/<uuid>/`, so it is bound into no
container. Same placement rule and the same reason as the SLURM output dir: a
log the agent can rewrite is worse than no log, because it reads as evidence.

NEVER A TOKEN. Not a value, not a prefix, not a length. This is enforced by
construction rather than by review: `record()` takes named, typed, enumerated
arguments — there is no free-form dict to slip a secret into, and every string
field that could carry one is either an enum or an identifier we chose.
"""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path

_LOG_NAME = "credential-events.jsonl"

# A single event is a few hundred bytes. 5 MB is tens of thousands of them —
# far more than the diagnosis needs, and small enough that a runaway loop
# cannot fill a disk. On reaching it we stop appending rather than rotating:
# a truncating rotation would silently discard the oldest evidence, which is
# usually the interesting evidence.
_MAX_BYTES = 5 * 1024 * 1024


class Event(str, Enum):
    """What happened. Closed set on purpose: a new situation needs a new member
    here, which forces whoever adds it to say what it means in the same edit."""

    # symlink (re)created; nothing else had happened
    LINKED = "linked"
    # the container refreshed and broke the symlink; SAME account, so the newer
    # token was adopted into the shared store. The ordinary case.
    ROTATION_PROMOTED = "rotation-promoted"
    # the break-away belongs to a DIFFERENT account, so it was NOT promoted —
    # promoting would switch every other project on this profile to it.
    ACCOUNT_CHANGE_HELD = "account-change-held"
    # the shared `oauthAccount` block was put back from the host-private record.
    # Needed because that file is written IN PLACE, so an account change lands
    # in the shared copy immediately and cannot simply be declined.
    IDENTITY_RESTORED = "identity-restored"
    # identity could not be read, so nothing was promoted. Absence of evidence
    # is not permission.
    IDENTITY_UNREADABLE = "identity-unreadable"
    # no prior record existed, so the current account was adopted as the
    # expectation. Trust-on-first-use, NOT verification — say so wherever shown.
    FIRST_USE_RECORDED = "first-use-recorded"
    # the user answered the prompt about a held account change.
    RESOLVED_BY_USER = "resolved-by-user"


class Verdict(str, Enum):
    MATCH = "match"
    DIFFERS = "differs"
    NO_RECORD = "no-record"
    UNREADABLE = "unreadable"
    NOT_CHECKED = "not-checked"


def log_path(state_root: Path) -> Path:
    return Path(state_root) / "logs" / _LOG_NAME


def record(
    state_root: Path,
    *,
    event: Event,
    agent: str,
    profile: str,
    project_uuid: str,
    verdict: Verdict = Verdict.NOT_CHECKED,
    detail: str = "",
    timestamp: str,
) -> None:
    """Append one event. NEVER raises — this is diagnostics, not control flow.

    A failure to log must never take down a session; a session that dies
    because its logger could not write is a worse outcome than a missing line.

    `timestamp` is passed in rather than read here so callers can be tested
    deterministically, and so the log agrees with whatever clock the caller
    already used for its own decision.

    `detail` is for short human context ("promoted from project state") and is
    truncated. It must not be built from credential material; the caller sites
    are few and each is reviewed.
    """
    try:
        d = Path(state_root) / "logs"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = d / _LOG_NAME
        try:
            if path.stat().st_size >= _MAX_BYTES:
                return
        except OSError:
            pass  # missing is fine; anything else, try the write and let it fail

        line = json.dumps({
            "ts": timestamp,
            "event": event.value,
            "agent": agent,
            "profile": profile,
            "project": project_uuid,
            "verdict": verdict.value,
            "detail": detail[:200],
        }, separators=(",", ":")) + "\n"

        # O_APPEND so concurrent sessions interleave whole lines rather than
        # corrupting each other. POSIX guarantees atomicity for a single
        # write() under PIPE_BUF (4096); an event is a few hundred bytes, and
        # the truncation of `detail` above keeps it that way.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        return  # never raise


def read_events(state_root: Path, *, limit: int = 500) -> list[dict]:
    """The most recent events, oldest first. Never raises; a malformed line is
    skipped rather than aborting the read, because a partially-written line
    should not hide the hundreds of good ones around it."""
    path = log_path(state_root)
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out[-limit:]


def summarise(events: list[dict]) -> dict[str, int]:
    """Counts per event type, for `auth doctor`.

    The point of the summary is the SHAPE: many rotations and zero account
    changes means shared mode is behaving; any account change at all is worth
    the user's attention.
    """
    counts: dict[str, int] = {}
    for e in events:
        k = str(e.get("event", "?"))
        counts[k] = counts.get(k, 0) + 1
    return counts


# ── who is holding the shared credential RIGHT NOW ────────────────────────────


def live_shared_holders(paths, *, exclude_uuid: str = "") -> list[dict]:
    """Other projects with a LIVE session using shared auth, newest first.

    THE MISSING ENFORCEMENT. Shared mode supports exactly ONE session at a
    time: refreshing mints a new refresh token and invalidates the old one
    (measured), so a second concurrent session logs the first out.
    Until nothing checked, nothing said so on any forward-facing
    surface, and the failure surfaced hours later as "expired" — which reads
    like a server problem, not like "you started two sessions".

    The maintainer, who WROTE the mode, did not know it had this constraint.
    That is why this is a check and not another paragraph: a warning nobody
    could have authored from knowledge was never going to be written.

    Every input here is host-side session bookkeeping the launcher owns. Best
    effort by construction — a project whose records are unreadable is skipped
    rather than blocking a launch, because a false refusal to start is worse
    than a missed warning.
    """
    import shutil as _shutil
    import socket as _socket

    from botainer.state import liveness as _liveness
    from botainer.state import session_record as _sr

    out: list[dict] = []
    try:
        children = sorted(paths.state_dir.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir() or child.is_symlink() or child.name == "by-name":
            continue
        if child.name == exclude_uuid:
            continue
        try:
            proj = paths.for_project(child.name)
            records = _sr.list_sessions(proj.sessions_dir)
        except Exception:                                       # noqa: BLE001
            continue
        for rec in records:
            if rec.ended_at:
                continue
            plugins = []
            spec = getattr(rec, "spec", None)
            if isinstance(spec, dict):
                plugins = spec.get("plugins_enabled") or []
            if not any(isinstance(p, str) and p.startswith("agent-")
                       and p.endswith("-shared") for p in plugins):
                continue
            # DO NOT CRY WOLF. is_session_alive answers "can't verify" with
            # TRUE ("assume alive"), which is right for `status` — better to
            # show a maybe-live row than hide one — and WRONG here, where the
            # same answer nags the user on every launch forever.
            #
            # The concrete case: a docker session record is verified by asking
            # docker, and a cluster login node has no docker. So one stale
            # record among a user's projects would report alive permanently and
            # warn at every single start. A warning that always fires is worse
            # than none — it teaches the user to ignore the channel, and the
            # next real one goes unread.
            #
            # So: only trust a liveness answer we can actually obtain. Docker
            # is host-local, so a record from another machine, or a host with
            # no docker, is UNVERIFIABLE and skipped. Apptainer+Slurm is
            # cluster-wide (squeue sees every node), so cross-host is fine.
            if rec.runtime == "docker":
                if rec.host and rec.host != _socket.gethostname():
                    continue
                if not _shutil.which("docker"):
                    continue
            try:
                if not _liveness.is_session_alive(rec):
                    continue
            except Exception:                                   # noqa: BLE001
                continue
            out.append({
                "uuid": child.name,
                "project_root": rec.project_root,
                "host": rec.host,
                "session_id": rec.session_id,
                "started_at": rec.started_at or "",
            })
            break        # one live session per project is enough to report
    out.sort(key=lambda h: h["started_at"], reverse=True)
    return out
