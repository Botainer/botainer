"""Auto-start the HPC job dispatcher for a session's lifetime.

THE fix for "the jobs feature is unusable": without this, when the caged agent
runs `botainer-job submit` it writes a request to the mailbox that NOTHING
processes — the job sits `pending` forever unless the user manually runs
`botainer hpc dispatcher start` in a separate terminal. So the agent's only
recourse was to tell the user to go start a dispatcher, which is exactly the
"write the job and tell me to run it" dead-end.

``maybe_start()`` spawns the dispatcher poll-loop as a detached background
process when (a) the project enabled jobs (the ``botainer-job`` bind is present
in the composed spec) and (b) ``sbatch`` is actually on PATH (a real scheduler
host — on a laptop there is nothing to submit to, so we skip and let requests
pend, which is the correct behavior there). It is FULLY GUARDED: any failure
logs a one-line warning and returns ``None`` — the session launches regardless.
``stop()`` reaps it at session end; the dispatcher loop also self-exits via the
launcher-PID watchdog (so a crashed launcher doesn't leave an orphan poller).
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys

from botainer.core.spec import SessionSpec

_BOTAINER_JOB_TARGET = "/usr/local/bin/botainer-job"


def jobs_enabled(spec: SessionSpec) -> bool:
    """True iff the composed spec wired the job mailbox + botainer-job (i.e. the
    project declared ``job_profiles``). Same signal AGENT_HINTS keys on."""
    return any(b.target == _BOTAINER_JOB_TARGET for b in spec.mount_plan.binds)


def maybe_start(spec: SessionSpec, *, interval: int = 10) -> int | None:
    """Spawn the detached dispatcher loop for this session, or return None.

    None (no dispatcher) when: jobs aren't enabled, or there is no ``sbatch`` on
    PATH (not a scheduler host), or the spawn failed (warned, non-fatal)."""
    if not jobs_enabled(spec):
        return None
    if not shutil.which("sbatch"):
        # Jobs enabled but no scheduler here (laptop / dev). The dispatcher
        # would have nothing to submit to; skip. The agent's `botainer-job
        # submit` still records a pending request (correct — it can't run here).
        return None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "botainer.cli.main", "hpc", "dispatcher",
             "start", "--project", spec.project_root, "--interval", str(interval)],
            env={**os.environ, "BOTAINER_LAUNCHER_PID": str(os.getpid())},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc.pid
    except OSError as exc:
        sys.stderr.write(
            f"[botainer] job dispatcher auto-start failed: {exc}\n"
            f"  Jobs the agent submits will stay `pending`. You can run the "
            f"dispatcher manually: `botainer hpc dispatcher start`.\n"
        )
        return None


def stop(pid: int | None) -> None:
    """Reap the dispatcher spawned by maybe_start (best-effort)."""
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
