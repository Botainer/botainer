"""HPC job-dispatcher daemon core (#54, P3 — the host-side loop).

Runs on the LOGIN node as the user (one per user). For each request the caged
agent drops in `/jobs/in/<id>.json` it:
  1. reads the inbox file with O_NOFOLLOW + fstat (regular, owned-by-me, not
     world-writable) — the agent is assumed compromised (INV-1 trust boundary);
  2. validates the request against the project's `job_profiles` AND the
     root-owned site-policy ceiling (`check_profile_against_ceiling`) — the
     profile is untrusted, the ceiling is the boundary;
  3. composes the CAGED child job (`compose_child_job_argv`, INV-2 — §4 cage,
     argv-not-shell, no creds) and renders a `run/<id>.sbatch` (host-private,
     SLURM `--output` into host-only `run/`, never the agent-writable subtree);
  4. `sbatch`es it and records `out/<id>.status.json = queued`.
Polling squeue → `out/` and `scancel` on a `<id>.cancel` marker are the runtime
side. `sbatch`/`squeue`/`scancel` are injected as callables so the pure logic is
unit-testable without a scheduler.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from botainer.core.config import JobProfile, is_parallel_shape
from botainer.core.policy import JobPolicy, check_profile_against_ceiling
from botainer.core.refusal import RefusalCategory, Refused
from botainer.hpc import jobs as _jobs

_ID_RE = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True)
class SubmitResult:
    job_id: str
    state: str            # "queued" | "refused"
    slurm_job_id: str | None = None
    reason: str | None = None


def read_request_safely(in_dir: Path, filename: str) -> dict:
    """Read one inbox request with O_NOFOLLOW + fstat guards (INV-1).

    The inbox is agent-writable, so a compromised agent may plant a symlink or a
    non-regular / non-owned file. We open O_NOFOLLOW (refuse a symlink AT the
    path), then fstat the fd: regular file, owned by us, not world-writable.
    """
    path = in_dir / filename
    try:
        # O_NONBLOCK too: a FIFO planted by the agent would make a plain
        # O_RDONLY open BLOCK forever (no writer) — a trivial DoS on the
        # dispatcher. With O_NONBLOCK the open returns immediately and the
        # S_ISREG fstat below refuses the FIFO.
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise Refused(
            RefusalCategory.MOUNT_SOURCE_DENIED,
            f"inbox {filename}: cannot open O_NOFOLLOW ({exc}); "
            f"skipping (symlink or gone).",
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(RefusalCategory.MOUNT_SOURCE_DENIED,
                          f"inbox {filename}: not a regular file; skipping.")
        if st.st_uid != os.getuid():
            raise Refused(RefusalCategory.MOUNT_SOURCE_DENIED,
                          f"inbox {filename}: not owned by me; skipping.")
        if st.st_mode & stat.S_IWOTH:
            raise Refused(RefusalCategory.MOUNT_SOURCE_DENIED,
                          f"inbox {filename}: world-writable; skipping.")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                      f"inbox {filename}: malformed JSON ({exc}).") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if not isinstance(data, dict):
        raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                      f"inbox {filename}: top-level is not an object.")
    return data


def validate_request(
    request: dict,
    job_profiles: dict[str, JobProfile],
    jobs_policy: JobPolicy,
) -> tuple[str, JobProfile, list[str]]:
    """Validate an untrusted request → (profile_name, profile, command). Raises
    Refused (fail-closed) on anything wrong."""
    job_id = request.get("id")
    if not isinstance(job_id, str) or not _ID_RE.match(job_id):
        raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                      f"request id {job_id!r} is not 16-hex.")
    pname = request.get("profile")
    if not isinstance(pname, str) or pname not in job_profiles:
        raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                      f"unknown profile {pname!r} (not in job_profiles).")
    profile = job_profiles[pname]
    # SITE ceiling — the real trust boundary (the profile is untrusted).
    check_profile_against_ceiling(
        pname, profile.partition, profile.account, profile.gpus,
        profile.max_concurrent, jobs_policy,
        nodes=int(getattr(profile, "nodes", 1) or 1),
    )
    command = request.get("command")
    if not isinstance(command, list) or not command \
            or not all(isinstance(c, str) for c in command):
        raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                      "request command must be a non-empty argv list of strings.")
    return pname, profile, command


def render_child_sbatch(
    job_id: str, profile: JobProfile, mailbox: _jobs.JobMailbox,
    caged_argv: list[str], overrides: dict | None = None,
) -> str:
    """Render the sbatch script for a CAGED child job.

    `--output`/`--error` go to the host-private `run/` (never the agent-writable
    mailbox), matching the S1 directional-isolation discipline. #SBATCH values
    are the EFFECTIVE resources (profile defaults + any agent `overrides` bounded
    by the profile's opt-in max — jobs v2, resolve_resources) and are validated +
    shlex-quoted; the body execs the already-composed §4-caged argv, argv-not-shell.
    """
    import shlex

    from botainer.hpc.resources import resolve_resources
    res = resolve_resources(profile, overrides)

    def q(s: str) -> str:
        return shlex.quote(s)

    out = mailbox.run_dir / f"{job_id}.out"
    err = mailbox.run_dir / f"{job_id}.err"
    lines = [
        "#!/bin/bash",
        f"# botainer child job {job_id} (profile {res['partition'] or 'default'})",
        f"#SBATCH --job-name=botjob-{job_id[:8]}",
        f"#SBATCH --output={q(str(out))}",
        f"#SBATCH --error={q(str(err))}",
    ]
    if res["partition"]:
        lines.append(f"#SBATCH --partition={res['partition']}")
    if res["account"]:
        lines.append(f"#SBATCH --account={res['account']}")
    if res["time"]:
        lines.append(f"#SBATCH --time={res['time']}")
    if res["cpus"]:
        lines.append(f"#SBATCH --cpus-per-task={int(res['cpus'])}")
    if res["memory"]:
        lines.append(f"#SBATCH --mem={res['memory']}")
    if res["gpus"] and int(res["gpus"]) > 0:
        gres = f"gpu:{(res['gpu_type'] + ':') if res['gpu_type'] else ''}{int(res['gpus'])}"
        lines.append(f"#SBATCH --gres={gres}")
    # MPI / multi-node (#68): --nodes/--ntasks/--ntasks-per-node, and launch the
    # CAGED exec under `srun` so SLURM spawns the tasks across the allocation
    # (each rank an apptainer exec; PMI via srun). Single-task profiles keep the
    # plain `exec` (one caged process).
    nodes = int(res["nodes"] or 1)
    ntasks = res["ntasks"]
    ntasks_per_node = res["ntasks_per_node"]
    if nodes > 1:
        lines.append(f"#SBATCH --nodes={nodes}")
    if ntasks:
        lines.append(f"#SBATCH --ntasks={int(ntasks)}")
    if ntasks_per_node:
        lines.append(f"#SBATCH --ntasks-per-node={int(ntasks_per_node)}")
    # Whole-node allocation (#68 MPI): profile-set only (author, not agent) — the
    # agent's request cannot toggle it. Bounded like any other profile field.
    if profile.exclusive:
        lines.append("#SBATCH --exclusive")
    # `--constraint` (node features, e.g. a CPU generation): OPERATOR-FIXED like
    # `exclusive` above.
    #
    # MECHANISM (be precise, this is an sbatch directive): the protection is
    # STRUCTURAL — `constraint` has no `max_constraint` and no
    # `botainer-job submit --constraint`, so no agent-supplied value can reach
    # here at all. The value is whatever the operator wrote in config.yaml,
    # arriving via a parsed JobProfile.
    #
    # `JobProfile._validate_constraint` charset-checks it at parse time. That is
    # a FILTER and it backs up a different gap: the operator's own typo, and a
    # config.yaml edited by someone who is not the operator. It is NOT the sink
    # re-check that `_common.py` performs on the session path — this renderer
    # has no such re-check, and claiming otherwise would misstate the guarantee.
    if getattr(profile, "constraint", None):
        lines.append(f"#SBATCH --constraint={profile.constraint}")
    # ONE shared predicate with the JobProfile validator (config.is_parallel_shape)
    # — computed on the RESOLVED shape (post agent-override), so `mpi:` can't
    # validate against a parallel default yet resolve here to a single task
    # (sharp-edges MEDIUM).
    parallel = is_parallel_shape(nodes, ntasks, ntasks_per_node)
    lines.append("")
    lines.append("set -euo pipefail")
    if profile.mpi and not parallel:
        # Fail LOUD, never silently drop the MPI launch: an `mpi:` profile that
        # resolves to a single task (e.g. an agent `--nodes 1` override on a
        # multi-node profile) would otherwise take the plain `exec` path and run
        # non-parallel with no error — the exact silent no-op the validator guards
        # against for defaults. Refused here → submit_request records it as refused.
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"job resolves to a single task but its profile sets `mpi: "
            f"{profile.mpi}` — refusing to launch it non-parallel (would silently "
            f"drop the MPI/PMIx handshake). Request more tasks/nodes or use a "
            f"non-MPI profile.",
        )
    if parallel and profile.mpi:
        # Multi-node MPI (#68): srun launches one caged apptainer per rank, and a
        # fixed per-task shim forwards the PMIx handshake THROUGH the §4 cage. The
        # shim + env allowlist + socket bind live in jobs.mpi_srun_launch_lines so
        # the cage-adjacent security surface is in one audited place.
        lines.extend(_jobs.mpi_srun_launch_lines(profile.mpi, list(caged_argv)))
    else:
        launcher = "exec srun " if parallel else "exec "
        lines.append(launcher + " ".join(q(a) for a in caged_argv))
    lines.append("")
    return "\n".join(lines)


def submit_request(
    mailbox: _jobs.JobMailbox,
    filename: str,
    job_profiles: dict[str, JobProfile],
    jobs_policy: JobPolicy,
    image: str,
    child_binds: tuple = (),
    child_env: dict | None = None,
    *,
    sbatch: Callable[..., str],
    now: str = "",
) -> SubmitResult:
    """Full submit pipeline for ONE inbox request. `sbatch(script_path,
    profile_name) -> jobid` is injected; it raises Refused when the SCHEDULER
    rejects the job, so that reason reaches the agent verbatim instead of
    becoming "internal error handling request (CalledProcessError)".
    Fail-closed: any validation error → out/<id>.status.json=refused with a
    reason (kept for the agent to read), never raises to the caller."""
    try:
        request = read_request_safely(mailbox.in_dir, filename)
        pname, profile, command = validate_request(request, job_profiles, jobs_policy)
        job_id = request["id"]
        # jobs v2: per-request resource overrides (bounded by the profile's opt-in
        # max). resolve_resources raises Refused if an override exceeds the max or
        # targets a fixed resource; then re-check the RESOLVED gpus/nodes against
        # the site ceiling (an override must not exceed policy either).
        from botainer.core.policy import check_profile_against_ceiling
        from botainer.hpc.resources import (
            parse_mem_mb, parse_time_seconds, resolve_resources,
        )
        ov = request.get("resources")
        overrides = ov if isinstance(ov, dict) else None
        res = resolve_resources(profile, overrides)

        def _safe(fn, v):
            # The RESOLVED value for the site-cap check. Empty (profile didn't
            # set the dim) or unparseable → None = skip that cap (don't crash;
            # override values were already validated in resolve_resources).
            try:
                return fn(v) if v else None
            except (ValueError, TypeError):
                return None

        check_profile_against_ceiling(
            pname, res["partition"], res["account"], int(res["gpus"]),
            profile.max_concurrent, jobs_policy, nodes=int(res["nodes"]),
            cpus=int(res["cpus"]),
            mem_mb=_safe(parse_mem_mb, res["memory"]),
            time_seconds=_safe(parse_time_seconds, res["time"]))
        # Per-profile image (#68 MPI): an MPI-matched .sif may differ from the
        # agent's own image. Author-set only (validated no-leading-dash in config);
        # the agent's request cannot choose it. The §4 cage flags are emitted by
        # compose_child_job_argv regardless of which image runs.
        child_image = profile.image or image
        # Per-profile modules (#68): auto `module load` them inside the caged job
        # before the workload. Three-state: None/[] → none; [list] → load those.
        # (Warm/hot path exposes the module system but does not yet auto-preload —
        # a hot task can `module load` itself; cold auto-preload is here.)
        caged = _jobs.compose_child_job_argv(
            child_image, tuple(command), tuple(child_binds), child_env,
            preload_modules=tuple(profile.modules or ()),
            gpus=int(res["gpus"]))
        script = render_child_sbatch(job_id, profile, mailbox, caged, overrides)
        mailbox.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        script_path = mailbox.run_dir / f"{job_id}.sbatch"
        script_path.write_text(script, encoding="utf-8")
        slurm_id = sbatch(script_path, pname)
        # Backfill-hostile multi-node configs pend for a long time and used to do
        # so INVISIBLY. Record a plain-English warning on the status so `hpc
        # jobs-status` (+ the agent) can explain a slow multi-node job up front
        #. Not an error — the job is still submitted.
        _warns = []
        if int(res.get("nodes") or 1) > 1:
            if getattr(profile, "exclusive", False):
                _warns.append("multi-node + --exclusive: needs N whole idle nodes "
                              "at once; can pend a long time on shared partitions.")
            if not res.get("time"):
                _warns.append("multi-node with no time limit: gets the partition "
                              "default (often the max), which backfill can't slot "
                              "in — set `time:` for faster scheduling.")
        _status = {"id": job_id, "state": "queued", "profile": pname,
                   "slurm_job_id": slurm_id, "submitted_at": now}
        if _warns:
            _status["warnings"] = _warns
        _write_status(mailbox, job_id, _status)
        return SubmitResult(job_id, "queued", slurm_id)
    except Exception as exc:
        # Fail-closed on ANY error, not just Refused. A Refused is an expected
        # validation refusal (surfaced verbatim); anything else is an unexpected
        # bug or a hostile typed-JSON request that reached a rougher code path.
        # BOTH must record a `refused` status in out/ so the request is marked
        # processed and NEVER replayed — otherwise a single poison inbox file
        # crash-loops the dispatcher forever (a request never gets a status file,
        # so every cycle re-reads and re-crashes on it: audit HIGH).
        # Don't leak internals to the agent: only Refused's own message is shown.
        reason = (str(exc) if isinstance(exc, Refused)
                  else f"internal error handling request "
                       f"({type(exc).__name__})")
        # SECURITY (bug audit, HIGH-10): this used to RE-READ the
        # agent-writable inbox file with a plain read_text() — no O_NOFOLLOW, no
        # O_NONBLOCK — which is exactly what read_request_safely() exists to avoid.
        # `mkfifo in/<16hex>.json` made read_request_safely reject the non-regular
        # file, land here, and then BLOCK FOREVER opening the FIFO with no writer:
        # the poll loop dies permanently and silently (nothing raises, so the
        # daemon guard never notices). Strictly worse than the crash-loop the
        # comment above was written to prevent.
        #
        # The id is already known from the FILENAME, which the caller validated
        # before dispatching here — no second read of attacker-controlled data.
        rid = filename[:-5] if filename.endswith(".json") else None
        if isinstance(rid, str) and _ID_RE.match(rid):
            _write_status(mailbox, rid, {
                "id": rid, "state": "refused", "reason": reason,
            })
            return SubmitResult(rid, "refused", reason=reason)
        return SubmitResult("?", "refused", reason=reason)


# A status-file id becomes a FILENAME component in out/. Job ids are 16-hex, but
# pool-control ids are short operator-chosen labels, so this is deliberately wider
# than _ID_RE — it only has to be NON-PATH-SHAPED (no '/', no '.', no NUL).
_STATUS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _write_status(mailbox: _jobs.JobMailbox, job_id: str, rec: dict) -> None:
    # SECURITY (audits, CRITICAL/C1): every caller is supposed to
    # validate the id, but one did not (pool_control) and the id is
    # attacker-controlled — it comes from the caged agent's request JSON. Enforce
    # it HERE too, at the sink, so a future caller cannot re-open the hole: this
    # value is interpolated into a path and os.replace()d, so a path-shaped id
    # ("./../../../home/<user>/.claude/settings") wrote arbitrary host *.json.
    if not (isinstance(job_id, str) and _STATUS_ID_RE.match(job_id)):
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"status id {job_id!r} is not a safe filename component; refusing "
            f"to write a status file (a path-shaped id escapes the mailbox).",
        )
    mailbox.out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = mailbox.out_dir / f".{job_id}.status.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, mailbox.out_dir / f"{job_id}.status.json")


def poll_running(
    mailbox: _jobs.JobMailbox, running_slurm_ids: set[str],
    squeue_info: dict[str, tuple[str, str]] | None = None,
) -> None:
    """Advance queued→running/pending→completed for each tracked job given the
    slurm ids currently in the queue. A job whose slurm id has left the queue is
    completed and its host-only run/ logs are copied into out/ (agent RO).

    `squeue_info` (optional) maps slurm_id → (state_code, reason) from
    `squeue -o "%A %t %r"`, so a PENDING job is shown as `pending` (not
    mislabeled `running`) WITH its scheduler reason (e.g. `Resources`,
    `PartitionNodeLimit`) recorded — that reason is what tells the user whether a
    stuck job is scheduling contention or a bad request."""
    if not mailbox.out_dir.is_dir():
        return
    for p in sorted(mailbox.out_dir.glob("*.status.json")):
        rec = _read_status(p)
        if not rec:
            continue
        state = rec.get("state")
        sid = rec.get("slurm_job_id")
        if state not in ("queued", "running", "pending") or not sid:
            continue
        if str(sid) in running_slurm_ids:
            info = (squeue_info or {}).get(str(sid))
            if info:
                code, reason = info
                rec["state"] = "running" if code == "R" else "pending"
                if reason and reason not in ("None", "(null)", ""):
                    rec["squeue_reason"] = reason
                elif "squeue_reason" in rec:
                    del rec["squeue_reason"]
                _write_status(mailbox, rec["id"], rec)
            elif state == "queued":
                rec["state"] = "running"
                _write_status(mailbox, rec["id"], rec)
        else:
            rec["state"] = "completed"
            rec.pop("squeue_reason", None)
            _write_status(mailbox, rec["id"], rec)
            _copy_run_logs(mailbox, rec["id"])


def _read_status(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# States that count as "occupying a max_concurrent slot" for a profile.
# States that occupy a max_concurrent slot. `pending` MUST be here: a PD job is
# still holding an allocation slot — omitting it (sharp-edges) would let
# an agent flood past max_concurrent (job pends → slot "frees" → next deferred
# submits → repeat), silently defeating the live throttle.
_ACTIVE_STATES = ("queued", "running", "assigned", "pending")


def _active_counts(mailbox: _jobs.JobMailbox) -> dict[str, int]:
    """profile → number of its jobs currently queued/running/assigned (read from
    out/ statuses). The live concurrency used to enforce max_concurrent."""
    counts: dict[str, int] = {}
    if not mailbox.out_dir.is_dir():
        return counts
    for p in mailbox.out_dir.glob("*.status.json"):
        rec = _read_status(p)
        if isinstance(rec, dict) and rec.get("state") in _ACTIVE_STATES:
            prof = rec.get("profile")
            if isinstance(prof, str):
                counts[prof] = counts.get(prof, 0) + 1
    return counts


def _copy_run_logs(mailbox: _jobs.JobMailbox, job_id: str) -> None:
    """Copy the host-private run/<id>.out/.err into the agent-RO out/ as
    <id>.stdout/.stderr, so the agent can read its job's output. The agent never
    sees run/ directly."""
    for src_suffix, dst_suffix in (("out", "stdout"), ("err", "stderr")):
        src = mailbox.run_dir / f"{job_id}.{src_suffix}"
        if src.exists():
            try:
                (mailbox.out_dir / f"{job_id}.{dst_suffix}").write_text(
                    src.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
            except OSError:
                pass


def process_inbox_once(
    mailbox: _jobs.JobMailbox,
    job_profiles: dict[str, JobProfile],
    jobs_policy: JobPolicy,
    image: str,
    child_binds: tuple = (),
    child_env: dict | None = None,
    *,
    sbatch: Callable[..., str],
    now: str = "",
) -> list[SubmitResult]:
    """One dispatcher cycle: submit every NOT-yet-processed request in in/.
    A request is 'processed' once out/<id>.status.json exists."""
    import time as _time

    from botainer.hpc import pool as _pool
    results: list[SubmitResult] = []
    if not mailbox.in_dir.is_dir():
        return results
    # Live per-profile concurrency, used to ENFORCE max_concurrent below. Updated
    # in-cycle as we submit so we don't blow the cap within a single pass.
    active = _active_counts(mailbox)
    for entry in sorted(mailbox.in_dir.iterdir()):
        name = entry.name
        if not name.endswith(".json") or name.startswith("."):
            continue
        job_id = name[:-len(".json")]
        if not _ID_RE.match(job_id):
            continue
        _existing = _read_status(mailbox.out_dir / f"{job_id}.status.json")
        if _existing is not None and _existing.get("state") != "deferred":
            continue  # already handled (terminal/active); a `deferred` one is re-evaluated
        # HOT path (#68): a `hot` request runs on an idle WARM worker immediately
        # (no SLURM queue wait). Route to a matching idle worker; if none, fall
        # through to a normal cold sbatch. The worker validates + cages the task
        # (worker_process_one → compose_child_job_argv) exactly as the cold path.
        try:
            req = read_request_safely(mailbox.in_dir, name)
        except Exception:
            req = None
        if isinstance(req, dict) and req.get("kind") == "pool_control":
            continue  # handled by the CLI cycle (needs sbatch/config), not here
        if isinstance(req, dict) and req.get("hot"):
            prof = req.get("profile")
            if isinstance(prof, str) and prof in job_profiles:
                wid = _pool.find_idle_worker(mailbox, prof, now=_time.time())
                if wid:
                    try:
                        # STRUCTURAL: pass the id we derived from the FILENAME
                        # and already _ID_RE-validated above. route_hot_task must
                        # never read request["id"] — agent-supplied data must not
                        # become a path component at all (charset-filtering it is
                        # a catch-the-bad-input mitigation; not supplying it is a
                        # property).
                        _pool.route_hot_task(mailbox, wid, req, job_id=job_id)
                        _write_status(mailbox, job_id, {
                            "id": job_id, "state": "assigned", "profile": prof,
                            "worker": wid, "submitted_at": now})
                        (mailbox.in_dir / name).unlink()
                        results.append(SubmitResult(job_id, "assigned"))
                        continue
                    except Exception:
                        pass  # fall through to a cold submit
        # max_concurrent throttle: if the profile is already at its cap, DEFER
        # (leave for a later cycle) instead of submitting. Previously unenforced —
        # every request was sbatch'd regardless, so max_concurrent × max_nodes
        # (and the aggregate node count) had NO botainer bound (audit).
        _pn = req.get("profile") if isinstance(req, dict) else None
        _po = job_profiles.get(_pn) if isinstance(_pn, str) else None
        if _po is not None:
            _cap = int(getattr(_po, "max_concurrent", 1) or 1)
            if active.get(_pn, 0) >= _cap:
                _write_status(mailbox, job_id, {
                    "id": job_id, "state": "deferred", "profile": _pn,
                    "reason": f"waiting: profile at max_concurrent ({_cap})",
                    "submitted_at": now})
                results.append(SubmitResult(job_id, "deferred"))
                continue
        _r = submit_request(
            mailbox, name, job_profiles, jobs_policy, image, child_binds,
            child_env, sbatch=sbatch, now=now,
        )
        results.append(_r)
        if _r.state == "queued" and isinstance(_pn, str):
            active[_pn] = active.get(_pn, 0) + 1  # count it toward the cap this cycle
    return results
