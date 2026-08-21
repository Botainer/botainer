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
import time
from dataclasses import dataclass
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


# ── layout ──


def pool_root(mb: _jobs.JobMailbox) -> Path:
    """Host-private pool tree, a sibling of the mailbox run/ (never bound in)."""
    return mb.run_dir / "pool"


def worker_dir(mb: _jobs.JobMailbox, worker_id: str) -> Path:
    return pool_root(mb) / worker_id


def worker_in_dir(mb: _jobs.JobMailbox, worker_id: str) -> Path:
    return worker_dir(mb, worker_id) / "in"


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


def find_idle_worker(mb: _jobs.JobMailbox, profile: str, *, now: float,
                     stale_after: float = 90.0) -> str | None:
    """Return the id of an IDLE worker for ``profile`` whose heartbeat is fresh
    (seen within ``stale_after`` s) and whose inbox is empty, else None."""
    for w in read_pool_state(mb):
        if w.profile != profile:
            continue
        beat = read_beat(mb, w.worker_id)
        if not beat or beat.get("state") != "idle":
            continue
        if now - float(beat.get("ts", 0)) > stale_after:
            continue  # stale — worker likely dead / its allocation ended
        # Don't double-assign: only if its inbox is currently empty.
        try:
            if any(worker_in_dir(mb, w.worker_id).glob("*.json")):
                continue
        except OSError:
            continue
        return w.worker_id
    return None


# ── hot routing (dispatcher → idle worker) ──


def route_hot_task(mb: _jobs.JobMailbox, worker_id: str, request: dict,
                   *, job_id: str) -> None:
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
    tmp = ind / f".{jid}.json.tmp"
    tmp.write_text(json.dumps(request), encoding="utf-8")
    os.replace(tmp, ind / f"{jid}.json")


# ── the worker: process one task (caged), inside its allocation ──

# run_caged(argv, out_path, err_path) -> returncode. Injected so tests don't run
# apptainer; production wires it to subprocess.run writing the caged exec's
# stdout/err to the host-private worker run/ dir.
RunCaged = Callable[[list[str], Path, Path], int]


def worker_process_one(
    mb: _jobs.JobMailbox,
    worker_id: str,
    image: str,
    *,
    binds: tuple = (),
    env: dict | None = None,
    run_caged: RunCaged,
    write_status: Callable[[str, dict], None],
    now_iso: str = "",
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
    try:
        request = json.loads(task.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        task.unlink(missing_ok=True)
        return None
    jid = request.get("id")
    command = request.get("command")
    if not (isinstance(jid, str) and _ID_RE.match(jid)
            and isinstance(command, list) and command
            and all(isinstance(c, str) for c in command)):
        task.unlink(missing_ok=True)
        if isinstance(jid, str) and _ID_RE.match(jid):
            write_status(jid, {"id": jid, "state": "refused",
                               "reason": "malformed hot task"})
        return jid if isinstance(jid, str) else None
    # Compose + re-verify the §4 cage (INV-2) — SAME chokepoint as the cold path.
    try:
        caged = _jobs.compose_child_job_argv(image, tuple(command), tuple(binds),
                                             env, gpus=gpus)
    except Exception as exc:  # Refused (bad command/image) → fail closed
        task.unlink(missing_ok=True)
        write_status(jid, {"id": jid, "state": "refused", "reason": str(exc)})
        return jid
    write_status(jid, {"id": jid, "state": "running", "worker": worker_id,
                       "started_at": now_iso})
    wrun = worker_dir(mb, worker_id) / "run"
    wrun.mkdir(parents=True, exist_ok=True, mode=0o700)
    out_p, err_p = wrun / f"{jid}.out", wrun / f"{jid}.err"
    rc = run_caged(caged, out_p, err_p)
    task.unlink(missing_ok=True)
    write_status(jid, {"id": jid, "state": "completed", "worker": worker_id,
                       "exit_code": int(rc), "finished_at": now_iso})
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
            write_status=write_status, now_iso="", gpus=gpus)
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
                         idle_timeout: int, *, quote, out_dir: Path) -> str:
    """sbatch script for a WARM worker: holds the profile's allocation and runs
    the worker loop (`botainer hpc pool worker`). Mirrors render_child_sbatch's
    #SBATCH directives (partition/account/time/cpus/mem/gres + MPI nodes/ntasks)
    but the body is the loop, not a one-shot caged exec. `quote` is shlex.quote.

    `out_dir` MUST be host-private (never bound into any container) — see the
    --output note below.
    """
    q = quote
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
        f"#SBATCH --error={q(str(out_dir / 'worker.err'))}",
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
