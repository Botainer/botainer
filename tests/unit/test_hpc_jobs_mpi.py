"""#68 Phase-2: multi-node MPI (PMIx) launch — the per-task bootstrap shim, its
gating, and the security properties that make forwarding env INTO the §4 cage safe.

The recipe (DN-001 §9 Q2): a multi-node MPI child runs as
one caged `apptainer exec` per rank under `srun --mpi=<flavor> --export=NONE`, and
a fixed per-task shim forwards the PMIX_*/PMI_*/SLURM_* handshake env +
$PMIX_SERVER_TMPDIR socket ACROSS the cage via APPTAINERENV_*/APPTAINER_BIND
(which apptainer injects AFTER --cleanenv/--containall, so they survive the cage
byte-for-byte)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from botainer.core.config import JobProfile
from botainer.core.refusal import Refused
from botainer.hpc import dispatcher, jobs
from botainer.hpc.jobs import JobMailbox

IMG = "/apps/mpi.sif"


def _caged() -> list[str]:
    return jobs.compose_child_job_argv(IMG, ("python", "train.py", "--epochs", "50"))


def _mailbox(tmp_path) -> JobMailbox:
    root = tmp_path / "hpc-jobs" / "u"
    return JobMailbox(root=root, in_dir=root / "in", out_dir=root / "out",
                      run_dir=root / "run")


# ───────────────────── structure of the launch lines ─────────────────────

def test_launch_lines_have_mpi_selector_and_export_none() -> None:
    lines = jobs.mpi_srun_launch_lines("pmix", _caged())
    exec_line = lines[-1]
    assert exec_line.startswith("exec srun --mpi=pmix --export=NONE ")
    # --export=NONE is the security lynchpin (bounds the shim's env sweep to
    # slurmstepd-set vars). It must always accompany the shim.
    assert "--export=NONE" in exec_line


def test_mpi_launcher_is_srun_never_mpirun_prrte() -> None:
    """SECURITY INVARIANT (spawn cage-escape closed by construction): Slurm's
    mpi/pmix plugin hardcodes dynamic-spawn → PMIX_ERR_NOT_SUPPORTED, so under
    `srun --mpi=pmix` (our launcher) a caged rank can't ask the uncaged host stepd
    to spawn an uncaged task — on EVERY Slurm cluster, no detection needed. That
    guarantee holds ONLY because botainer launches via `srun`, never mpirun /
    mpiexec / prterun / prun (PRRTE *does* implement spawn). Pin it so a refactor
    can't reopen the hole. (research DN-022)"""
    line = jobs.mpi_srun_launch_lines("pmix", _caged())[-1]
    assert line.startswith("exec srun --mpi=pmix ")
    for prrte in ("mpirun", "mpiexec", "orterun", "prterun", " prun"):
        assert prrte not in line, f"MPI launch must not use {prrte!r} (reopens spawn escape)"


def test_launch_resolves_apptainer_absolute_in_batch_env() -> None:
    """--export=NONE clears PATH in the task, so argv[0] ('apptainer') must be
    resolved to an absolute path in the batch env (which still has PATH)."""
    lines = jobs.mpi_srun_launch_lines("pmix", _caged())
    body = "\n".join(lines)
    assert 'BOT_APPTAINER_BIN="$(command -v apptainer' in body
    # fail-closed if apptainer isn't found
    assert ':?apptainer not found' in body
    # the resolved binary is what actually runs (not the bare literal)
    assert '"$BOT_APPTAINER_BIN"' in lines[-1]


def test_launch_passes_caged_argv_after_argv0_verbatim() -> None:
    """Everything after argv[0] is handed to the shim as positional params and
    re-exec'd verbatim (`exec "$@"`) — the cage argv is NOT rebuilt."""
    caged = _caged()
    exec_line = jobs.mpi_srun_launch_lines("pmix", caged)[-1]
    # the shim then the resolved binary then caged_argv[1:] (starts at "exec")
    assert exec_line.rstrip().endswith(
        'bash "$BOT_APPTAINER_BIN" exec --containall --cleanenv --no-privs '
        "--drop-caps all /apps/mpi.sif python train.py --epochs 50"
    )
    # the §4 cage flags survive into what runs
    for flag in ("--containall", "--cleanenv", "--no-privs"):
        assert flag in exec_line


def test_shim_is_compile_time_constant() -> None:
    """The shim must interpolate NO agent/config text — every expansion in it is a
    bash builtin against slurmstepd-set env, not a Python format field."""
    shim = jobs._PMIX_BOOTSTRAP_SHIM
    assert "{}" not in shim and "%s" not in shim
    assert "APPTAINERENV_" in shim
    assert "psec=native" in shim               # no munge needed in the container
    assert "PMIX_SERVER_TMPDIR" in shim        # binds the per-step socket dir
    assert "OMPI_MCA_pml=ob1" in shim          # cage-safe: skip UCX's CMA path
    assert "btl_vader_single_copy_mechanism=none" in shim  # no CMA (segfaults caged)
    assert 'exec "$@"' in shim                  # argv-not-shell tail
    assert "'" not in shim                      # so shlex.quote wraps it cleanly


# ───────────────────── refusals / gating ─────────────────────

def test_unknown_flavor_refused() -> None:
    with pytest.raises(Refused, match="unknown MPI flavor"):
        jobs.mpi_srun_launch_lines("openmpi", _caged())


def test_launch_refuses_uncaged_argv() -> None:
    """The shim may only wrap a §4-caged apptainer exec — never a bare command."""
    with pytest.raises(Refused, match="caged"):
        jobs.mpi_srun_launch_lines("pmix", ["python", "train.py"])


def test_launch_refuses_argv_missing_cage_flag() -> None:
    argv = ["apptainer", "exec", "--cleanenv", "--no-privs", IMG, "python"]
    with pytest.raises(Refused, match="missing"):
        jobs.mpi_srun_launch_lines("pmix", argv)


# ───────────────────── render integration ─────────────────────

def test_render_mpi_profile_uses_shim(tmp_path) -> None:
    p = JobProfile(description="mpi", nodes=2, ntasks=8, ntasks_per_node=4,
                   mpi="pmix", time="01:00:00")
    script = dispatcher.render_child_sbatch("abc0123456789def", p,
                                            _mailbox(tmp_path), _caged())
    assert "#SBATCH --nodes=2" in script
    assert "srun --mpi=pmix --export=NONE" in script
    assert "APPTAINERENV_" in script


def test_render_parallel_without_mpi_stays_plain_srun(tmp_path) -> None:
    """A multi-task profile WITHOUT `mpi:` must NOT get the PMIx grant — it's an
    independent-tasks job, no cross-node handshake."""
    p = JobProfile(description="par", ntasks=4)
    script = dispatcher.render_child_sbatch("abc0123456789def", p,
                                            _mailbox(tmp_path), _caged())
    assert "exec srun apptainer exec" in script
    assert "--mpi=" not in script
    assert "APPTAINERENV_" not in script


def test_render_single_task_stays_plain_exec(tmp_path) -> None:
    p = JobProfile(description="single")
    script = dispatcher.render_child_sbatch("abc0123456789def", p,
                                            _mailbox(tmp_path), _caged())
    assert "exec apptainer exec" in script
    assert "srun" not in script


# ───────────────────── behavioural: the shim actually forwards ─────────────────────

@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_shim_forwards_env_binds_tmpdir_and_never_injects(tmp_path) -> None:
    """End-to-end: run the real shim with a simulated `--export=NONE` task env (only
    slurmstepd-set vars present) and a stub apptainer, and assert it (1) forwards
    PMIX_*/SLURM_* via APPTAINERENV_*, (2) does NOT cross a non-prefixed secret,
    (3) binds $PMIX_SERVER_TMPDIR, (4) forwards a metachar-laden value LITERALLY
    (no shell injection), (5) execs the caged argv verbatim."""
    stub = tmp_path / "apptainer"
    marker = tmp_path / "PWNED"
    stub.write_text(
        "#!/bin/bash\n"
        'echo "ARGV=$*"\n'
        'echo "NS=$APPTAINERENV_PMIX_NAMESPACE"\n'
        'echo "PROCID=$APPTAINERENV_SLURM_PROCID"\n'
        'echo "PSEC=$APPTAINERENV_PMIX_MCA_psec"\n'
        'echo "PML=$APPTAINERENV_OMPI_MCA_pml"\n'
        'echo "VADER=$APPTAINERENV_OMPI_MCA_btl_vader_single_copy_mechanism"\n'
        'echo "URI=$APPTAINERENV_PMIX_SERVER_URI2"\n'
        'echo "SECRET=${APPTAINERENV_SECRET_TOKEN:-NONE}"\n'
        'echo "BIND=$APPTAINER_BIND"\n'
    )
    stub.chmod(0o755)

    # env -i: only what srun --export=NONE would leave — SLURM_*/PMIX_* + one stray
    task_env = {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "PMIX_NAMESPACE": "slurm.pmix.1591154.7",
        "PMIX_SERVER_URI2": "pmix:1:/tmp/pmix-1; touch " + str(marker) + " #",
        "PMIX_SERVER_TMPDIR": str(tmp_path / "spmix_appdir_607.0"),
        "SLURM_PROCID": "3",
        "SECRET_TOKEN": "must-not-cross",
    }
    argv = ["/bin/bash", "-c", jobs._PMIX_BOOTSTRAP_SHIM, "bash", str(stub),
            "exec", "--containall", IMG, "prog"]
    out = subprocess.run(argv, env=task_env, capture_output=True, text=True,
                         check=True).stdout

    assert "ARGV=exec --containall /apps/mpi.sif prog" in out   # verbatim
    assert "NS=slurm.pmix.1591154.7" in out                     # PMIX_ forwarded
    assert "PROCID=3" in out                                    # SLURM_ forwarded
    assert "PSEC=native" in out                                 # psec pinned
    assert "PML=ob1" in out                                     # cage-safe MPI env
    assert "VADER=none" in out                                  # CMA off (no segfault)
    assert "SECRET=NONE" in out                                 # NOT crossed
    assert f"BIND={tmp_path / 'spmix_appdir_607.0'}" in out     # tmpdir bound
    # the metachar value rode through as literal data — the `; touch` did NOT run
    assert not marker.exists(), "shim allowed shell injection from a PMIX_ value"
    assert "URI=pmix:1:/tmp/pmix-1; touch" in out               # forwarded literally


# ───────────────────── config: the `mpi:` profile field ─────────────────────

@pytest.mark.parametrize("flavor", ["pmix", "pmi2"])
def test_profile_mpi_accepts_known_flavors(flavor: str) -> None:
    p = JobProfile(mpi=flavor, ntasks=4)
    assert p.mpi == flavor


def test_profile_mpi_rejects_unknown_flavor() -> None:
    with pytest.raises(ValueError, match="must be 'pmix' or 'pmi2'"):
        JobProfile(mpi="openmpi", ntasks=4)


def test_profile_mpi_blank_normalises_to_none() -> None:
    assert JobProfile(mpi="").mpi is None
    assert JobProfile().mpi is None


def test_profile_mpi_requires_parallel_shape() -> None:
    """`mpi:` without ntasks/nodes>1 would take the single `exec` path and be a
    silent no-op — refused loudly."""
    with pytest.raises(ValueError, match="needs a parallel shape"):
        JobProfile(mpi="pmix")  # single task, nodes=1


@pytest.mark.parametrize("shape", [
    {"nodes": 2},
    {"ntasks": 4},
    {"ntasks_per_node": 2},
])
def test_profile_mpi_parallel_shapes_ok(shape: dict) -> None:
    p = JobProfile(mpi="pmix", **shape)
    assert p.mpi == "pmix"


def test_profile_mpi_with_warm_pool_refused() -> None:
    """A warm worker runs one caged process, never srun --mpi — so a hot MPI task
    would lose its PMIx wiring. (Caught by the warm+parallel combo check.)"""
    with pytest.raises(ValueError, match="warm_pool_size"):
        JobProfile(mpi="pmix", ntasks=4, warm_pool_size=2)


# ───── sharp-edges MEDIUM: validator/dispatcher shape must not drift ─────

def test_profile_mpi_ntasks_one_refused() -> None:
    """`mpi: pmix` + `ntasks: 1` used to VALIDATE (truthiness) but resolve to a
    non-parallel `exec` in the dispatcher (`ntasks==1` → `1>1` False) — silently
    dropping the MPI launch. Now refused loudly at config-load."""
    with pytest.raises(ValueError, match="needs a parallel shape"):
        JobProfile(mpi="pmix", ntasks=1)


def test_is_parallel_shape_predicate() -> None:
    """The ONE predicate the validator and the dispatcher both use — no drift."""
    from botainer.core.config import is_parallel_shape
    assert is_parallel_shape(1, 1, None) is False       # ntasks:1 is NOT parallel
    assert is_parallel_shape(1, None, None) is False     # single task
    assert is_parallel_shape(2, None, None) is True      # multi-node
    assert is_parallel_shape(1, 4, None) is True         # multi-task
    assert is_parallel_shape(1, None, 2) is True         # ntasks-per-node


def test_render_mpi_resolved_single_task_refused_loud(tmp_path) -> None:
    """An `mpi:` profile whose RESOLVED shape is a single task (e.g. an agent
    `--nodes 1` override on a multi-node profile) must be REFUSED, not silently run
    non-parallel. Simulated by an override taking nodes back to 1."""
    p = JobProfile(description="mpi", nodes=4, max_nodes=8, mpi="pmix")
    with pytest.raises(Refused, match="single task.*mpi"):
        dispatcher.render_child_sbatch("abc0123456789def", p, _mailbox(tmp_path),
                                       _caged(), {"nodes": 1})


def test_mpi_launch_refuses_missing_drop_caps_all() -> None:
    """The structural re-check mirrors assert_caged_child_job: `--drop-caps` must be
    followed by `all` (a bare `--drop-caps` drops nothing)."""
    argv = ["apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
            "--drop-caps", IMG, "python"]   # no `all` after --drop-caps
    with pytest.raises(Refused, match="drop-caps all"):
        jobs.mpi_srun_launch_lines("pmix", argv)


# ───── security review: sbatch-submission env scrub parity ─────

def test_sbatch_env_scrubs_apptainerenv_injection_vars(monkeypatch) -> None:
    """`_sbatch_env` must strip APPTAINERENV_*/SINGULARITYENV_* (mirrors the
    session-launch scrub) so a host-login-poisoned APPTAINERENV_LD_PRELOAD can't
    ride sbatch --export=ALL into a caged child on the non-MPI launch paths."""
    from botainer.cli import hpc as hpc_cli
    monkeypatch.setenv("APPTAINERENV_LD_PRELOAD", "/evil.so")
    monkeypatch.setenv("SINGULARITYENV_FOO", "bar")
    monkeypatch.setenv("SLURM_MEM_PER_CPU", "4096")
    monkeypatch.setenv("SLURM_CONF", "/etc/slurm/slurm.conf")
    monkeypatch.setenv("HOME", "/home/u")
    env = hpc_cli._sbatch_env()
    assert "APPTAINERENV_LD_PRELOAD" not in env
    assert "SINGULARITYENV_FOO" not in env
    assert "SLURM_MEM_PER_CPU" not in env      # existing behaviour preserved
    assert env.get("SLURM_CONF") == "/etc/slurm/slurm.conf"  # kept
    assert env.get("HOME") == "/home/u"        # unrelated env untouched
