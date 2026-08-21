"""Runtime liveness checks for session records.

Previously this logic was triplicated across nudge.py, status.py, stop.py.
Consolidated here so changes (e.g., new runtime support) happen once.
"""

from __future__ import annotations

import shutil
import subprocess

from botainer.state import session_record


def is_session_alive(rec: session_record.SessionRecord) -> bool:
    """Best-effort liveness check. Returns False if we can verify it's dead.

    Per-runtime semantics (task #92 tightening):
    - Docker: `docker inspect` reports State.Status == "running".
    - Apptainer + Slurm: `squeue -j <jobid>` reports state contains RUNNING.
    - Apptainer (no Slurm jobid): assume DEAD. Previously assumed alive,
      which left foreground-exit sessions as zombie 'running forever'
      records. A session that should be alive has a jobid recorded.
    - Mock: never alive (no real container).
    - Docker without container_id recorded: dead.
    - Tools (docker / squeue) not on PATH: can't verify, assume alive
      (cluster-side tooling missing is operator error, not data state).
    """
    if rec.runtime == "docker":
        if rec.docker is None or not rec.docker.container_id:
            return False
        if not shutil.which("docker"):
            return True
        try:
            result = subprocess.run(
                [
                    "docker", "inspect", "-f", "{{.State.Status}}",
                    "--", rec.docker.container_id,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return True
        return result.returncode == 0 and result.stdout.strip() == "running"
    if rec.runtime == "apptainer":
        # Task #92: no jobid recorded → session is foreground-exited; treat dead.
        if rec.apptainer is None or not rec.apptainer.slurm_jobid:
            return False
        if not shutil.which("squeue"):
            return True
        try:
            result = subprocess.run(
                ["squeue", "-h", "-j", rec.apptainer.slurm_jobid, "-o", "%T"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return True
        return result.returncode == 0 and "RUNNING" in result.stdout
    return rec.runtime != "mock"
