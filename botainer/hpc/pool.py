"""Warm/hot worker pool for agent-dispatched HPC jobs (#68 part 2).

The plain dispatcher cold-submits a fresh `sbatch` per task, so every task waits
in the SLURM queue. A WARM WORKER is instead a persistent sbatch job that holds
its allocation and runs a LOOP: it polls its own task inbox, runs each task as a
§4-CAGED `apptainer exec` inside the held allocation (no re-queue → "hot", runs
immediately), heart-beats idle/busy, and self-exits after an idle timeout (or on
a stop marker). The agent starts/stops the pool from inside via `botainer-job
pool …`; the host-side dispatcher routes `submit --hot` tasks to an idle worker.

Trust model (unchanged from the cold path): the task the worker runs is composed
by `jobs.compose_child_job_argv` (INV-2 §4 cage — same isolation as the agent,
pinned image, argv workload, no credentials) and re-verified by
`jobs.assert_caged_child_job`. The per-worker mailbox is HOST-PRIVATE (never
bound into any container); the agent reaches results only through the main
mailbox out/ (RO). The worker loop runs host-side as the invoking user (same as
the dispatcher) inside a SLURM allocation the user's own account was granted.

Pure functions (``worker_process_one``/``worker_loop``/``find_idle_worker``/…)
take injectable clock + task-runner so the logic is unit-tested without SLURM.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from botainer.core.refusal import Refused, RefusalCategory
from botainer.hpc import jobs as _jobs

_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_WORKER_RE = re.compile(r"^w[0-9a-f]{12}$")
POOL_STATE_VERSION = "botainer-pool-v1"

# A worker writes this file with its liveness; the dispatcher reads it to route.
_BEAT = "beat.json"
_STOP = "STOP"  # stop marker the manager drops into a worker dir
_WORKER_ERR = "worker.err"  # Slurm's --error for a worker; see worker_stderr_path

# ONE threshold, not two defaults that happen to match. `find_idle_worker`
# refuses to route to a worker whose heartbeat is older than this, and
# `worker_diagnosis` calls that same worker "stale" — if those two numbers drift
# apart, `pool status` reports a healthy pool while every `--hot` request
# silently degrades to a cold queue submit. The refuting review of this change
# showed the pairing TEST had a blind band when they were two literals, so the
# fix is to have one value, not to test two harder.
STALE_AFTER_S = 90.0

# States a worker writes on its way OUT. These are terminal: the worker itself
# said so, so age does not make them stale and there is nothing to diagnose.
# `exiting` = self-released on its idle timeout (the DESIGNED end of a warm
# worker's life, and therefore the steady state of every pool that goes quiet);
# `stopped` = it found the STOP marker someone dropped for it.
TERMINAL_BEATS = ("exiting", "stopped")


# ── layout ──


def pool_root(mb: _jobs.JobMailbox) -> Path:
    """Host-private pool tree, a sibling of the mailbox run/ (never bound in)."""
    return mb.run_dir / "pool"


def worker_dir(mb: _jobs.JobMailbox, worker_id: str) -> Path:
    return pool_root(mb) / worker_id


def worker_in_dir(mb: _jobs.JobMailbox, worker_id: str) -> Path:
    return worker_dir(mb, worker_id) / "in"


def worker_stderr_path(mb: _jobs.JobMailbox, worker_id: str) -> Path:
    """Where Slurm writes this worker's stderr — the ONE definition.

    `render_worker_sbatch` emits `#SBATCH --error=<this>` and
    `_tail_worker_stderr` reads it. They used to name the path independently: the
    renderer took an `out_dir` argument and the reader rebuilt it from
    `worker_dir`, so passing a different `out_dir` would have left the "its own
    last words" diagnosis silently reading a file Slurm never writes — and the
    existing sbatch test only asserted the path was SOMEWHERE under run/, so it
    would have stayed green (found by a refuting review). One function,
    two callers, no equality left to break.
    """
    return worker_dir(mb, worker_id) / _WORKER_ERR


def worker_beat_path(mb: _jobs.JobMailbox, worker_id: str) -> Path:
    """The worker's heartbeat file. Exported because "the file is there but will
    not parse" is a different answer from "there is no file", and only the second
    means the worker never ran."""
    return worker_dir(mb, worker_id) / _BEAT


def ensure_worker_dirs(mb: _jobs.JobMailbox, worker_id: str) -> Path:
    wd = worker_dir(mb, worker_id)
    for d in (pool_root(mb), wd, wd / "in", wd / "run"):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return wd


def new_worker_id(rand_hex: str) -> str:
    """`w` + 12 hex. `rand_hex` is caller-supplied (secrets.token_hex(6))."""
    wid = "w" + rand_hex[:12]
    if not _WORKER_RE.match(wid):
        raise ValueError(f"bad worker id {wid!r}")
    return wid


# ── pool state (which workers exist) ──


@dataclass
class WorkerRec:
    worker_id: str
    slurm_job_id: str
    profile: str
    idle_timeout: int
    started_at: str


def _state_path(mb: _jobs.JobMailbox) -> Path:
    return pool_root(mb) / "state.json"


def read_pool_state(mb: _jobs.JobMailbox) -> list[WorkerRec]:
    p = _state_path(mb)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[WorkerRec] = []
    for w in doc.get("workers", []):
        try:
            out.append(WorkerRec(
                worker_id=w["worker_id"], slurm_job_id=w.get("slurm_job_id", ""),
                profile=w["profile"], idle_timeout=int(w.get("idle_timeout", 0)),
                started_at=w.get("started_at", "")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def write_pool_state(mb: _jobs.JobMailbox, workers: list[WorkerRec]) -> None:
    pool_root(mb).mkdir(parents=True, exist_ok=True, mode=0o700)
    doc = {"version": POOL_STATE_VERSION,
           "workers": [w.__dict__ for w in workers]}
    tmp = _state_path(mb).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, _state_path(mb))


# ── worker heartbeat (idle/busy liveness) ──


def write_beat(mb: _jobs.JobMailbox, worker_id: str, state: str, ts: float) -> None:
    wd = worker_dir(mb, worker_id)
    wd.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = wd / (_BEAT + ".tmp")
    tmp.write_text(json.dumps({"state": state, "ts": ts, "worker_id": worker_id}),
                   encoding="utf-8")
    os.replace(tmp, wd / _BEAT)


def read_beat(mb: _jobs.JobMailbox, worker_id: str) -> dict | None:
    try:
        return json.loads((worker_dir(mb, worker_id) / _BEAT).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def task_slot_released(mb: _jobs.JobMailbox, worker_id: str) -> bool:
    """Whether this worker's existing lifecycle has released its task slot.

    A missing/stale heartbeat is not evidence of death: long tasks do not
    refresh it. Pool removal is administrative evidence, not a scheduler check.
    Missing or damaged pool state must never be mistaken for an empty pool.
    """
    if not isinstance(worker_id, str) or not _WORKER_RE.fullmatch(worker_id):
        return False
    beat = read_beat(mb, worker_id)
    if (isinstance(beat, dict) and beat.get("worker_id") == worker_id
            and beat.get("state") in TERMINAL_BEATS):
        return True
    try:
        doc = json.loads(_state_path(mb).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(doc, dict) or doc.get("version") != POOL_STATE_VERSION:
        return False
    workers = doc.get("workers")
    if not isinstance(workers, list) or any(
        not isinstance(w, dict) or not isinstance(w.get("worker_id"), str)
        or not _WORKER_RE.fullmatch(w["worker_id"])
        or not isinstance(w.get("profile"), str) or not w["profile"]
        or not isinstance(w.get("slurm_job_id"), str)
        or type(w.get("idle_timeout")) is not int
        or not isinstance(w.get("started_at"), str) for w in workers
    ):
        return False
    return all(w["worker_id"] != worker_id for w in workers)


def worker_diagnosis(mb: _jobs.JobMailbox, rec: WorkerRec, *,
                     now: float | None = None,
                     stale_after: float = STALE_AFTER_S,
                     reason_bytes: int = 600) -> dict:
    """What state is this worker ACTUALLY in — and if it is not running, WHY.

    THE DEFECT THIS EXISTS FOR. A worker that dies before its first heartbeat —
    the commonest cause being an image it cannot resolve — left three traces and
    a user could reach none of them: `pool status` printed `state=?`, the
    agent-facing `out/pool.json` printed `"state": "?"`, and the worker's own
    reason went to `run/pool/<id>/worker.err`, which NO command read. Slurm wrote
    that file because we asked it to (host-private, deliberately, so a caged
    agent cannot pre-plant it) and then nothing ever looked.

    "?" is the wrong answer twice over: it does not distinguish NEVER STARTED from
    HEARTBEAT WENT STALE, and those have different causes and different fixes.

    A terminal heartbeat is not a fault. A worker normally self-releases on
    its idle timeout and writes `exiting`; aging that terminal state into
    `stale` would mislabel normal completion and suggest checking a job that
    has already finished. A terminal word is honored at any age.

    ONE OWNER, because the CLI and the agent-facing manifest are two surfaces
    describing one thing, and this subsystem's whole history is two surfaces
    disagreeing. Returns a dict rather than a dataclass so `out/pool.json` can
    embed it without a second mapping step to drift.

    Keys:
      state      the worker's own word while its heartbeat is fresh ("idle"/
                 "busy"); its terminal word at ANY age ("exiting"/"stopped");
                 "stale" (a heartbeat that stopped without a terminal word);
                 "never-started" (no heartbeat file at all); "beat-unreadable"
                 (a heartbeat file that will not parse — it DID start, so saying
                 "never-started" there would be a lie).
      terminal   True when the worker itself reported it was finishing. Callers
                 use this instead of matching on state words.
      last_state the worker's own last word, or "" — so a `busy`→dead worker can
                 be distinguished from an `idle`→dead one. Only the first may
                 have taken a hot task down with it.
      age_s      seconds since the last heartbeat, or None if there was never one.
      reason     the TAIL of the worker's stderr, or "". Read ONLY when the state
                 is neither live nor terminal: a running worker's stderr is not a
                 diagnosis, and a worker that told us it was exiting has already
                 given its reason.
    """
    out = classify_worker_beat(
        mb, rec.worker_id,
        now=time.time() if now is None else now, stale_after=stale_after)
    out["reason"] = ""
    if not out["terminal"] and out["state"] not in ("idle", "busy"):
        out["reason"] = _tail_worker_stderr(mb, rec.worker_id, reason_bytes)
    return out


def classify_worker_beat(mb: _jobs.JobMailbox, worker_id: str, *, now: float,
                         stale_after: float = STALE_AFTER_S) -> dict:
    """Read one worker's heartbeat and say what it means. No stderr, no I/O beyond
    the beat file — so the ROUTER can use the same answer the human surface shows.

    `find_idle_worker` used to carry its own copy of the freshness comparison. Two
    copies of one rule is how `pool status` came to be able to call a worker "idle"
    that the router had already given up on; the refuting review measured the blind
    band that left in the test meant to pin them together. There is now one
    comparison, and the routing decision is DERIVED from the word a user is shown.
    """
    beat = read_beat(mb, worker_id)
    if not beat:
        # The file existing but not parsing is a different fact: it DID start, so
        # "never-started" there would be a lie, and the two have different fixes.
        state = ("beat-unreadable" if worker_beat_path(mb, worker_id).exists()
                 else "never-started")
        return {"state": state, "terminal": False, "last_state": "", "age_s": None}
    # A non-numeric `ts` is not reachable through `write_beat` (atomic, always a
    # float), but the file is on disk and this runs in the dispatcher's publish
    # step as well as in the router, so a parse failure must not become the outage.
    try:
        age = now - float(beat.get("ts", 0) or 0)
    except (TypeError, ValueError):
        age = None
    last = str(beat.get("state", "") or "")
    aged = None if age is None else round(age, 1)
    if last in TERMINAL_BEATS:
        return {"state": last, "terminal": True, "last_state": last, "age_s": aged}
    if age is not None and age <= stale_after:
        return {"state": last or "?", "terminal": False, "last_state": last,
                "age_s": aged}
    return {"state": "stale", "terminal": False, "last_state": last, "age_s": aged}


def reap_exited_workers(mb: _jobs.JobMailbox) -> list[str]:
    """Drop records of workers that TOLD US they had finished. Returns their ids.

    A record only ever left pool state on an explicit `pool stop`, so every worker
    that self-released on its idle timeout kept occupying a `max_concurrent` slot
    for ever. Measured by a refuting review: with the shipped example
    profile (`max_concurrent: 2`, `warm_pool_size: 2`) two idled-out records make
    `pool start` start ZERO workers and say nothing useful, and since
    `max_concurrent` defaults to 1, ONE idle-out exhausts a default profile. The
    documented recovery — "the agent re-warms with `botainer-job pool start`" — was
    therefore the broken path.

    WHY THIS IS SAFE WITHOUT A QUEUE LOOKUP, which is the whole reason it can land
    here: a terminal beat is the worker's OWN report that it is done, so no live
    allocation is being forgotten. A worker still PENDING in the queue has no beat
    at all and is NOT reaped — reaping on "no heartbeat" would double-start workers
    the moment a queue was busy, which is why the `never-started`/`stale` half stays
    open (it needs `squeue` or a grace period, and a live Slurm to verify against).
    """
    keep, reaped = [], []
    for w in read_pool_state(mb):
        (reaped if worker_diagnosis(mb, w)["terminal"] else keep).append(w)
    if reaped:
        write_pool_state(mb, keep)
    return [w.worker_id for w in reaped]


def _tail_worker_stderr(mb: _jobs.JobMailbox, worker_id: str,
                        limit: int = 600) -> str:
    """The last `limit` bytes of a worker's own stderr, or "".

    Tail rather than head: a Python traceback puts the useful line last. Bounded
    because this is interpolated into CLI output and into a JSON file the agent
    reads — an unbounded read of a file a runaway worker filled would be a second
    defect on top of the first. Never raises: a missing or unreadable file means
    "no reason recorded", which is itself worth saying and is not a crash.
    """
    path = worker_stderr_path(mb, worker_id)
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            data = fh.read(limit)
    except OSError:
        return ""
    return data.decode("utf-8", "replace").strip()


def find_idle_worker(mb: _jobs.JobMailbox, profile: str, *, now: float,
                     stale_after: float = STALE_AFTER_S) -> str | None:
    """Return the id of an IDLE worker for ``profile`` whose heartbeat is fresh
    (seen within ``stale_after`` s) and whose inbox is empty, else None.

    Routability is DERIVED from `classify_worker_beat` — the same call that decides
    the word `pool status` prints — so "the router refuses it but the status says
    idle" is not a state this code can be in.
    """
    for w in read_pool_state(mb):
        if w.profile != profile:
            continue
        if classify_worker_beat(mb, w.worker_id, now=now,
                                stale_after=stale_after)["state"] != "idle":
            continue  # not idle, or stale/terminal — its allocation may be gone
        # Don't double-assign: only if its inbox is currently empty.
        try:
            ind = worker_in_dir(mb, w.worker_id)
            if any(ind.glob(".*.prepared")) or any(ind.glob("*.json")):
                continue
        except OSError:
            continue
        return w.worker_id
    return None


# ── hot routing (dispatcher → idle worker) ──


class HotHandoffPending(Exception):
    """A durable handoff exists; it must be resumed, never cold-submitted."""


def _handoff_paths(mb: _jobs.JobMailbox, directory: str):
    for prepared in sorted(pool_root(mb).glob(f"w*/{directory}/.*.prepared")):
        jid = prepared.name[1:-len(".prepared")]
        wid = prepared.parent.parent.name
        if _ID_RE.fullmatch(jid) and _WORKER_RE.fullmatch(wid):
            yield jid, wid, prepared


def hot_handoff_ids(mb: _jobs.JobMailbox) -> set[str]:
    """IDs durably owned by prepared or abandoned handoffs, never fresh work."""
    return {jid for directory in ("in", "abandoned")
            for jid, _, _ in _handoff_paths(mb, directory)}


def _handoff_assignment(prepared: Path, jid: str, wid: str) -> dict | None:
    packet = json.loads(prepared.read_text(encoding="utf-8"))
    assignment = packet.get("assignment") if isinstance(packet, dict) else None
    if (not isinstance(assignment, dict) or assignment.get("id") != jid
            or assignment.get("worker") != wid or assignment.get("state") != "assigned"
            or packet.get("id") != jid or not isinstance(assignment.get("profile"), str)):
        return None
    return assignment


def hot_handoff_statuses(mb: _jobs.JobMailbox) -> dict[str, dict]:
    """Host-side reporting for handoffs whose public status may be unavailable."""
    records = {}
    for directory in ("in", "abandoned"):
        for jid, wid, prepared in _handoff_paths(mb, directory):
            try:
                assignment = _handoff_assignment(prepared, jid, wid)
            except (OSError, ValueError):
                assignment = None
            if assignment is None:
                matches = [w for w in read_pool_state(mb) if w.worker_id == wid]
                assignment = ({"profile": matches[0].profile} if len(matches) == 1
                              and isinstance(matches[0].profile, str) and matches[0].profile else {})
            reason = "hot handoff pending delivery"
            if directory == "abandoned":
                reason = "hot handoff abandoned; recorded reason unavailable"
                try:
                    detail = json.loads(prepared.with_name(prepared.name + ".reason.json").read_text())
                    if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
                        reason = "hot handoff abandoned: " + detail["reason"][:300]
                except (OSError, ValueError):
                    pass
            records[jid] = {**(assignment or {}), "id": jid, "worker": wid,
                            "state": "handoff" if directory == "in" else "abandoned",
                            "reason": reason}
    return records


def _abandon_handoff(prepared: Path, jid: str, reason: str) -> None:
    destination = prepared.parent.parent / "abandoned" / prepared.name
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise OSError("handoff evidence already exists")
    reason_path = destination.with_name(destination.name + ".reason.json")
    temporary = reason_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"reason": reason}), encoding="utf-8")
    os.replace(temporary, reason_path)
    os.replace(prepared, destination)
    # Keep the durable ID tombstone, even if public status is missing/damaged.
    print(f"hot task {jid}: handoff abandoned: {reason}", file=sys.stderr)


def resume_hot_handoffs(mb: _jobs.JobMailbox, write_status: Callable) -> set[str]:
    """Finish only a positively recorded, host-private prepared handoff.

    Moving preparation into the runnable inbox also removes its recovery record.
    Abandoned records retain identity ownership without occupying that inbox.
    """
    reserved = hot_handoff_ids(mb)
    for jid, wid, prepared in _handoff_paths(mb, "in"):
        try:
            if ((prepared.parent / f"{jid}.task.json").exists()
                    or (prepared.parent / f"{jid}.json").exists()
                    or (worker_dir(mb, wid) / "run" / f"{jid}.json").exists()):
                print(f"hot task {jid}: conflicting worker ownership; handoff retained",
                      file=sys.stderr)
                continue
            status_path = mb.out_dir / f"{jid}.status.json"
            missing = object()
            try:
                existing = json.loads(status_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                existing = missing  # Preparation may precede the first status.
            except ValueError:
                existing = None
            if existing is not missing and (
                not isinstance(existing, dict)
                or existing.get("id") != jid or existing.get("slurm_job_id")
                or existing.get("state") not in ("assigned", "deferred")
                or (existing.get("state") == "assigned" and existing.get("worker") != wid)
            ):
                _abandon_handoff(prepared, jid, "conflicting public status preserved")
                continue
            try:
                assignment = _handoff_assignment(prepared, jid, wid)
            except ValueError:
                assignment = None
            if assignment is None:
                write_status(jid, {"id": jid, "worker": wid, "state": "refused",
                                  "reason": "invalid prepared hot handoff; task not published"})
                _abandon_handoff(prepared, jid, "invalid prepared metadata")
                continue
            if task_slot_released(mb, wid):
                write_status(jid, {**assignment, "state": "refused",
                                  "reason": "worker released before handoff; prepared task not published"})
                _abandon_handoff(prepared, jid, "worker released before handoff")
                continue
            write_status(jid, assignment)
            os.replace(prepared, prepared.parent / f"{jid}.task.json")
            try:
                (mb.in_dir / f"{jid}.json").unlink(missing_ok=True)
            except OSError:
                pass  # Runnable ownership has transferred; never retry cold.
        except (OSError, ValueError):
            print(f"hot task {jid}: handoff unavailable; retained for recovery", file=sys.stderr)
    return reserved


def route_hot_task(mb: _jobs.JobMailbox, worker_id: str, request: dict,
                   *, job_id: str, assignment: dict | None = None,
                   write_status: Callable | None = None) -> None:
    """Drop a validated task request into a worker's inbox (host→worker). The
    worker runs it immediately; status flows back through the MAIN mailbox out/.

    `job_id` is the id the CALLER derived from the inbox FILENAME and validated.
    It is the only thing that becomes a path here.
    """
    # SECURITY (bug audit, CRITICAL-2): this used to read
    # `request["id"]` — a separate, attacker-chosen field inside the CAGED agent's
    # JSON — and interpolate it straight into a host path.
    # `id="./../../../../tmp/pwn"` escaped the state root entirely (the leading "."
    # of the tmp name merges with a leading "." in the id, so BOTH the tmp and the
    # final path were craftable). Overwriting `pool/state.json` was the escalation:
    # read_pool_state does no validation, so the agent would then control
    # worker_id / slurm_job_id, which feed mkdir() and `scancel`.
    #
    # STRUCTURAL FIX: the agent's `id` field is no longer consulted at all — the
    # path is built from the caller's filename-derived id. The check below is a
    # cheap belt-and-braces assertion on OUR OWN value, not a filter on attacker
    # input; if it ever fires, the caller is wrong, not the agent.
    jid = job_id
    if not isinstance(jid, str) or not _ID_RE.match(jid):
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"hot-task request id {jid!r} is not a 16-hex job id; refusing to "
            f"route it (a path-shaped id could escape the worker mailbox).",
        )
    ind = worker_in_dir(mb, worker_id)
    ind.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Couple host-authored metadata to the runnable task in ONE publication.
    # A separate status read can be stale/unavailable on another cluster node.
    # The distinct host-only suffix identifies this format; legacy raw requests
    # cannot smuggle a reserved assignment field into trusted worker metadata.
    packet = {**request, "id": jid,
              "assignment": dict(assignment or {"id": jid, "worker": worker_id})}
    tmp = ind / f".{jid}.task.json.tmp"
    tmp.write_text(json.dumps(packet), encoding="utf-8")
    prepared = ind / f".{jid}.prepared"
    try:
        os.replace(tmp, prepared)
    except OSError as exc:
        if prepared.exists():
            raise HotHandoffPending("hot task preparation exists; handoff awaits retry") from exc
        raise
    try:
        if write_status is not None:
            write_status(jid, packet["assignment"])
        os.replace(prepared, ind / f"{jid}.task.json")
    except Exception as exc:
        raise HotHandoffPending("hot task handoff awaits retry") from exc


# ── the worker: process one task (caged), inside its allocation ──

# run_caged(argv, out_path, err_path) -> returncode. Injected so tests don't run
# apptainer; production wires it to subprocess.run writing the caged exec's
# stdout/err to the host-private worker run/ dir.
RunCaged = Callable[[list[str], Path, Path], int]


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _publish_task_log(source: Path, destination: Path) -> dict:
    """Publish a finished regular stream atomically, with bounded memory.

    Read at most its initial size; growth or shrinkage is an explicit failure.
    Nonblocking/no-follow open prevents a substituted FIFO or symlink from
    blocking the worker or exposing another host file. Neither directory is
    writable by the task. Temporary copies and raw output remain host-private
    until the final rename into the existing read-only results directory.
    """
    temporary = None
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return {"state": "unavailable", "reason": "not a regular log file"}
            with tempfile.NamedTemporaryFile(dir=source.parent, prefix=".hot-log-",
                                             delete=False) as output:
                temporary = Path(output.name)
                remaining = info.st_size
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        raise OSError("log changed during publication")
                    output.write(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    raise OSError("log changed during publication")
            os.replace(temporary, destination)
        return {"state": "published", "bytes": info.st_size}
    except OSError:
        # Exception text may contain host paths; publish an outcome, not paths.
        return {"state": "unavailable", "reason": "log publication failed"}
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # A private temporary-file cleanup cannot erase the outcome.


def worker_process_one(
    mb: _jobs.JobMailbox,
    worker_id: str,
    image: str,
    *,
    binds: tuple = (),
    env: dict | None = None,
    run_caged: RunCaged,
    write_status: Callable[[str, dict], None],
    now: Callable[[], float] = time.time,
    gpus: int = 0,
) -> str | None:
    """Pick up ONE task from this worker's inbox, run it §4-CAGED in the held
    allocation, and record status in the MAIN mailbox out/. Returns the job id
    processed, or None if the inbox was empty. Fail-closed: a bad request is
    marked `refused`; a non-zero exit is `completed` with the rc recorded."""
    ind = worker_in_dir(mb, worker_id)
    try:
        pending = sorted(ind.glob("*.json"))
    except OSError:
        return None
    if not pending:
        return None
    task = pending[0]
    # The validated filename owns all results, including tasks routed by an
    # older dispatcher. A body id must never select another task's records.
    has_assignment = task.name.endswith(".task.json")
    jid = task.name[:-len(".task.json")] if has_assignment else task.stem
    if not _ID_RE.fullmatch(jid):
        task.unlink(missing_ok=True)
        return None
    try:
        request = json.loads(task.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        request = None
    record = {"id": jid, "worker": worker_id}
    if has_assignment:
        assigned = request.get("assignment") if isinstance(request, dict) else None
    else:
        # Legacy raw packets did not carry host metadata. Their body fields
        # remain untrusted; use only the independently host-written status.
        try:
            assigned = json.loads((mb.out_dir / f"{jid}.status.json").read_text())
        except (OSError, ValueError):
            assigned = None
    assignment_matches = (isinstance(assigned, dict) and assigned.get("id") == jid
                          and assigned.get("worker") == worker_id)
    if assignment_matches:
        for key in ("profile", "submitted_at", "worker_slurm_job_id"):
            if key in assigned:
                record[key] = assigned[key]
    # An exclusive claim prevents replay even after a failed status write.
    # Keep the inbox entry until terminal publication: it is also the busy
    # guard when a dispatcher has read an older idle heartbeat.
    wrun = worker_dir(mb, worker_id) / "run"
    wrun.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.link(task, wrun / f"{jid}.json")
    except FileExistsError as exc:
        raise RuntimeError("earlier hot task claim exists; not re-executed; outcome unknown; "
                           "inspect the worker before using pool stop/start") from exc
    def finish(rec):
        write_status(jid, rec)
        task.unlink(missing_ok=True)
    if has_assignment and not assignment_matches:
        finish({**record, "state": "refused", "reason": "hot task assignment is invalid"})
        return jid
    command = request.get("command") if isinstance(request, dict) else None
    if not (isinstance(command, list) and command
            and all(isinstance(c, str) for c in command)):
        finish({**record, "state": "refused", "reason": "malformed hot task"})
        return jid
    # Compose + re-verify the same cage as the cold path.
    try:
        caged = _jobs.compose_child_job_argv(image, tuple(command), tuple(binds),
                                             env, gpus=gpus)
    except Exception as exc:
        finish({**record, "state": "refused", "reason": str(exc)})
        return jid
    record.update(state="running", started_at=_timestamp(now()))
    write_status(jid, dict(record))
    out_p, err_p = wrun / f"{jid}.out", wrun / f"{jid}.err"
    try:
        rc = run_caged(caged, out_p, err_p)
    except Exception:
        record.update(state="failed", reason="hot task runner failed; execution outcome unavailable")
    else:
        record.update(state="completed", exit_code=int(rc))
    record["finished_at"] = _timestamp(now())
    record["logs"] = {
        "stdout": _publish_task_log(out_p, mb.out_dir / f"{jid}.stdout"),
        "stderr": _publish_task_log(err_p, mb.out_dir / f"{jid}.stderr"),
    }
    unavailable = [name for name, result in record["logs"].items()
                   if result["state"] != "published"]
    if unavailable:
        record["warnings"] = ["Hot task output unavailable: " + ", ".join(unavailable)
                              + ". Execution outcome is recorded separately."]
    # Terminal status is visible only after both publication attempts finish.
    finish(record)
    return jid


def worker_loop(
    mb: _jobs.JobMailbox,
    worker_id: str,
    image: str,
    idle_timeout: int,
    *,
    binds: tuple = (),
    env: dict | None = None,
    run_caged: RunCaged,
    write_status: Callable[[str, dict], None],
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 2.0,
    max_iterations: int | None = None,
    gpus: int = 0,
) -> str:
    """Run the warm worker until it idles out or is told to stop. Returns the
    exit reason ('idle-timeout' | 'stopped'). ``max_iterations`` bounds the loop
    for tests. ``now``/``sleep``/``run_caged``/``write_status`` are injected."""
    last_active = now()
    ensure_worker_dirs(mb, worker_id)
    write_beat(mb, worker_id, "idle", now())
    it = 0
    while max_iterations is None or it < max_iterations:
        it += 1
        if (worker_dir(mb, worker_id) / _STOP).exists():
            write_beat(mb, worker_id, "stopped", now())
            return "stopped"
        write_beat(mb, worker_id, "busy", now())
        jid = worker_process_one(
            mb, worker_id, image, binds=binds, env=env, run_caged=run_caged,
            write_status=write_status, now=now, gpus=gpus)
        t = now()
        if jid is not None:
            last_active = t
            write_beat(mb, worker_id, "idle", t)
            continue
        write_beat(mb, worker_id, "idle", t)
        if idle_timeout > 0 and (t - last_active) >= idle_timeout:
            write_beat(mb, worker_id, "exiting", t)
            return "idle-timeout"
        sleep(poll_interval)
    return "idle-timeout"


# ── production wiring: sbatch render + host-side caged runner ──


def make_subprocess_runner() -> RunCaged:
    """The production run_caged: run the §4-caged apptainer exec via subprocess,
    tee its stdout/err to the host-private worker run/ files. Returns the rc.
    (Imported lazily so the pure logic above stays dependency-light.)"""
    import subprocess

    def _run(argv: list[str], out_p: Path, err_p: Path) -> int:
        with open(out_p, "wb") as o, open(err_p, "wb") as e:
            return subprocess.run(argv, stdout=o, stderr=e,
                                  stdin=subprocess.DEVNULL).returncode
    return _run


def render_worker_sbatch(worker_id: str, profile, project_root: str,
                         idle_timeout: int, *, quote, mb: _jobs.JobMailbox) -> str:
    """sbatch script for a WARM worker: holds the profile's allocation and runs
    the worker loop (`botainer hpc pool worker`). Mirrors render_child_sbatch's
    #SBATCH directives (partition/account/time/cpus/mem/gres + MPI nodes/ntasks)
    but the body is the loop, not a one-shot caged exec. `quote` is shlex.quote.

    Both stream paths come from the mailbox via `worker_dir`/`worker_stderr_path`,
    so they are host-private BY CONSTRUCTION (see the --output note below) and the
    stderr path is by definition the one `worker_diagnosis` later reads back. This
    used to take an `out_dir` argument, which made both of those a convention the
    caller had to get right.
    """
    q = quote
    out_dir = worker_dir(mb, worker_id)
    # SECURITY (bug audit, CRITICAL-1): this used to emit NO --output /
    # --error, so Slurm fell back to `<submit-dir>/slurm-%j.out`. The submit CWD is
    # inherited from `botainer start` — i.e. the PROJECT ROOT, which is bound RW at
    # /workspace. slurmstepd writes that file as the UNCAGED user and (stock Slurm)
    # follows symlinks, so a caged agent could pre-plant `slurm-<N>.out` pointing at
    # e.g. ~/.ssh/authorized_keys and have the host write through it. That is the
    # same escape primitive already fixed on the child-job path (task #48); the warm
    # -pool worker path was missed. Send both streams into the host-private pool
    # tree (mb.run_dir/pool/<worker>), which is never bound into a container.
    lines = [
        "#!/bin/bash",
        f"# botainer warm worker {worker_id} (profile {profile.partition or 'default'})",
        f"#SBATCH --job-name=botpool-{worker_id[:9]}",
        f"#SBATCH --output={q(str(out_dir / 'worker.out'))}",
        f"#SBATCH --error={q(str(worker_stderr_path(mb, worker_id)))}",
    ]
    if profile.partition:
        lines.append(f"#SBATCH --partition={profile.partition}")
    if profile.account:
        lines.append(f"#SBATCH --account={profile.account}")
    if profile.time:
        lines.append(f"#SBATCH --time={profile.time}")
    if profile.cpus:
        lines.append(f"#SBATCH --cpus-per-task={int(profile.cpus)}")
    if profile.memory:
        lines.append(f"#SBATCH --mem={profile.memory}")
    if profile.gpus and int(profile.gpus) > 0:
        gres = f"gpu:{(profile.gpu_type + ':') if profile.gpu_type else ''}{int(profile.gpus)}"
        lines.append(f"#SBATCH --gres={gres}")
    # Same constraint as the cold path (dispatcher.render_child_sbatch). Without
    # this a warm worker would hold an allocation on ANY node in the partition
    # while `--hot` tasks routed to it believed they had the profile's pinned
    # hardware — the pool would silently invalidate exactly the benchmark
    # comparability the constraint exists to provide.
    if getattr(profile, "constraint", None):
        lines.append(f"#SBATCH --constraint={profile.constraint}")
    nodes = int(getattr(profile, "nodes", 1) or 1)
    if nodes > 1:
        lines.append(f"#SBATCH --nodes={nodes}")
    lines.append("")
    lines.append("set -euo pipefail")
    # Pass the GPU count so the worker adds `--nv` to each hot task's caged exec
    # (the allocation has the GPU via --gres above, but --containall hides it
    # without --nv — same fix as the cold path, jobs.compose_child_job_argv).
    lines.append("exec python3 -m botainer.cli.main hpc pool worker "
                 f"{q(worker_id)} --project {q(project_root)} "
                 f"--idle-timeout {int(idle_timeout)} "
                 f"--gpus {int(profile.gpus or 0)}")
    lines.append("")
    return "\n".join(lines)
