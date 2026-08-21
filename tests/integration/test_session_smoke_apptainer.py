"""Real-Apptainer / Slurm end-to-end smoke.

Companion to test_session_smoke_docker.py. Exercises the HPC path:
`apptainer exec` (and, when Slurm is also available, `botainer hpc
submit` → sbatch → screen wrap → in-container `botainer start
--in-container`).

SKIPS by default — needs `apptainer` (or `singularity`) on PATH; the
sbatch tests further require `sbatch`/`squeue`/`scancel`. The
dev-container this repo lives in has none of these; a Yale-Grace
login node has all of them.

Coverage gap addressed: #129, #214, plus the §A19 in-container
`screen_session_id` recording path (commit 21c1b66 added the code
+ a unit test for the helper; the real-cluster round-trip is the
next acceptance step the doc at `docs/HPC-WORKFLOW.md §8` says is
pending).

Scope at v0.1 (when actually executed):
- `botainer start --runtime apptainer` (foreground, no Slurm) boots
  an apptainer-exec process; `botainer attach` reattaches; `Ctrl-D`
  cleanly tears down.
- `botainer hpc submit` writes an sbatch script (verified via
  --dry-run first), submits it, returns a jobid; `squeue` shows the
  job RUNNING; `botainer nudge "ls"` injects via
  `srun --overlap screen -X stuff`; `botainer hpc stop` scancels.

Excluded:
- Container-runtime sidecars (#95/#291 — v0.2 feature, refused at
  compose in v0.1).
- Inter-job dependencies / array jobs (out of v0.1 scope).
"""

from __future__ import annotations

import shutil

import pytest

_APPTAINER = shutil.which("apptainer") or shutil.which("singularity")
_SBATCH = shutil.which("sbatch")


pytestmark = pytest.mark.skipif(
    _APPTAINER is None,
    reason=(
        "real-apptainer smoke needs `apptainer` or `singularity` on PATH. "
        "Run on an HPC login/compute node; the dev-container this repo "
        "lives in deliberately has neither."
    ),
)


@pytest.mark.apptainer
def test_real_apptainer_foreground_start_attach() -> None:
    """End-to-end apptainer-exec (no Slurm). See module docstring."""
    pytest.skip(
        "implementation pending real-host validation — see module "
        "docstring for the test plan",
    )


@pytest.mark.apptainer
@pytest.mark.slurm
@pytest.mark.skipif(
    _SBATCH is None,
    reason="needs sbatch on PATH (Slurm-bearing host).",
)
def test_real_hpc_submit_screen_nudge_stop_round_trip() -> None:
    """The §A19 HPC nudge round-trip: `botainer hpc submit` → sbatch
    → `screen -dmS botainer-${SLURM_JOB_ID}` → `botainer nudge` via
    `srun --overlap` → `botainer hpc stop`. See module docstring +
    docs/HPC-WORKFLOW.md §8."""
    pytest.skip(
        "implementation pending real-cluster validation — see module "
        "docstring for the test plan",
    )
