"""Security-audit Finding 1 — SLURM output directional isolation.

The confirmed-PoC escape class: SLURM's slurmstepd writes a job's --output as
the UNCAGED user, following symlinks. If that path lives in a directory the
caged agent can write to (v0.0.x: the RW-bound /jobs; v0.1.0: the RW-bound
state/<uuid>/sessions/_outputs), a prompt-injected agent plants a symlink and
slurmstepd writes attacker content into an arbitrary host file → full
policy-cage escape.

Fix: SLURM output goes to a HOST-ONLY per-project dir that is NOT bind-mounted
into the container. These tests pin:
  1. the output dir is NOT under the container-bound state/<uuid>/ subtree;
  2. the two independent implementations of the path (botainer-side
     StatePaths.hpc_job_output_dir + the standalone hpc-launcher
     _job_output_dir) agree — no cross-module drift;
  3. render_sbatch_script's #SBATCH --output points at the host-only dir;
  4. prepare_host_paths refuses (tripwire) a symlink at the output dir.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HPC_HOST_HELPER = REPO / "plugins" / "hpc-launcher" / "host_helper"


def _load_common():
    spec = importlib.util.spec_from_file_location(
        "hpc_launcher_common_outputiso", HPC_HOST_HELPER / "_common.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hpc_launcher_common_outputiso"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_output_dir_not_under_container_bound_state_subtree(tmp_path: Path) -> None:
    """The job-output dir must NOT be under state/<uuid>/ (which
    to_apptainer_argv binds RW into the container). This is the whole fix."""
    common = _load_common()
    uuid = "12345678-1234-5234-9234-123456789012"
    out = common._job_output_dir(tmp_path, uuid)
    bound_subtree = tmp_path / "state" / uuid
    assert not str(out).startswith(str(bound_subtree)), (
        f"output dir {out} is UNDER the container-bound {bound_subtree} — a "
        f"caged agent could symlink it (Finding 1 regression)."
    )


def test_botainer_and_launcher_output_paths_agree(tmp_path: Path) -> None:
    """StatePaths.hpc_job_output_dir (botainer side, used by `hpc logs`) and
    the standalone _job_output_dir (hpc-launcher, used by submit) must produce
    the SAME path — else logs read a different dir than sbatch wrote. Same
    cross-module-parity discipline as the .sif naming test."""
    from botainer.state.dir import StatePaths
    common = _load_common()
    uuid = "12345678-1234-5234-9234-123456789012"
    botainer_side = StatePaths(root=tmp_path).hpc_job_output_dir(uuid)
    launcher_side = common._job_output_dir(tmp_path, uuid)
    assert botainer_side == launcher_side, (
        f"drift: botainer={botainer_side} launcher={launcher_side}"
    )


def test_render_sbatch_output_is_host_only_dir(tmp_path: Path) -> None:
    """The rendered #SBATCH --output must point at the host-only dir."""
    common = _load_common()
    uuid = "11111111-1111-1111-1111-111111111111"
    plan = common.SubmissionPlan(
        project_root=tmp_path, project_uuid=uuid, state_root=tmp_path / "sr",
        profile="default", partition="day", account="a", time_minutes=60,
        cpus=1, memory_gb=None, gpus=0, gpu_type=None,
        apptainer_image="x.sif", submission_mode="submit", existing_jobid=None,
    )
    script = plan.render_sbatch_script()
    expected = common._job_output_dir(tmp_path / "sr", uuid) / "slurm-%j.out"
    assert f"#SBATCH --output={expected}" in script
    # And NOT the old container-bound path.
    assert "sessions/_outputs" not in script


def test_prepare_host_paths_tripwire_refuses_symlink_output_dir(tmp_path: Path) -> None:
    """prepare_host_paths must REFUSE (tripwire) if the output dir is already a
    symlink — near-unambiguous evidence of an attempted output-redirect."""
    common = _load_common()
    uuid = "11111111-1111-1111-1111-111111111111"
    state_root = tmp_path / "sr"
    # Pre-plant the output dir as a symlink to elsewhere.
    out_dir = common._job_output_dir(state_root, uuid)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    out_dir.symlink_to(elsewhere)
    plan = common.SubmissionPlan(
        project_root=tmp_path, project_uuid=uuid, state_root=state_root,
        profile="default", partition="day", account="a", time_minutes=60,
        cpus=1, memory_gb=None, gpus=0, gpu_type=None,
        apptainer_image="x.sif", submission_mode="submit", existing_jobid=None,
    )
    with pytest.raises(SystemExit, match="SYMLINK"):
        plan.prepare_host_paths()


def test_prepare_host_paths_creates_0700_output_dir(tmp_path: Path) -> None:
    """Happy path: the output dir is created mode 0700 (owner-only)."""
    import stat as _stat
    common = _load_common()
    uuid = "11111111-1111-1111-1111-111111111111"
    state_root = tmp_path / "sr"
    plan = common.SubmissionPlan(
        project_root=tmp_path, project_uuid=uuid, state_root=state_root,
        profile="default", partition="day", account="a", time_minutes=60,
        cpus=1, memory_gb=None, gpus=0, gpu_type=None,
        apptainer_image="x.sif", submission_mode="submit", existing_jobid=None,
    )
    plan.prepare_host_paths()
    out_dir = common._job_output_dir(state_root, uuid)
    assert out_dir.is_dir() and not out_dir.is_symlink()
    assert _stat.S_IMODE(out_dir.stat().st_mode) == 0o700
