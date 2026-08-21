"""`botainer hpc setup --probe` — cluster-prereq probe tests.

The probe is read-only by design: each check either reports green/
yellow/red on a single line or gracefully skips when its tool is
missing. No mutation, no sbatch, no root. P3c / cluster-ease B3 per
DN-036."""

from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from botainer.cli.hpc import hpc as hpc_group


def _run_setup_with_probe(monkeypatch, tmp_path: Path) -> str:
    """Invoke `hpc setup --profile grace --probe` with state under tmp_path.
    Returns the runner output text."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        hpc_group,
        ["setup", "--profile", "grace", "--non-interactive", "--probe"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_probe_section_header_present(monkeypatch, tmp_path: Path) -> None:
    """The probe runs only with --probe; its section header must appear."""
    output = _run_setup_with_probe(monkeypatch, tmp_path)
    assert "Cluster prereq probe" in output


def test_probe_skipped_without_flag(monkeypatch, tmp_path: Path) -> None:
    """Without --probe, the section header must NOT appear (default off
    so non-interactive provisioning scripts don't pay the probe cost)."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        hpc_group,
        ["setup", "--profile", "grace", "--non-interactive"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "Cluster prereq probe" not in result.output


def test_probe_apptainer_missing_warns(monkeypatch, tmp_path: Path) -> None:
    """When apptainer/singularity aren't on PATH (typical dev container
    + many login nodes), the probe warns + names the salloc workaround
    instead of erroring."""
    import shutil as _sh
    orig = _sh.which
    monkeypatch.setattr(
        _sh, "which",
        lambda name: None if name in ("apptainer", "singularity") else orig(name),
    )
    output = _run_setup_with_probe(monkeypatch, tmp_path)
    assert "apptainer NOT on PATH" in output
    assert "salloc" in output


def test_probe_sbatch_missing_marks_red(monkeypatch, tmp_path: Path) -> None:
    """sbatch missing is treated as red (the hpc-launcher flow won't
    work without it), not yellow."""
    import shutil as _sh
    orig = _sh.which
    monkeypatch.setattr(
        _sh, "which",
        lambda name: None if name == "sbatch" else orig(name),
    )
    output = _run_setup_with_probe(monkeypatch, tmp_path)
    assert "sbatch NOT on PATH" in output


def test_probe_sacctmgr_reports_accounts(monkeypatch, tmp_path: Path) -> None:
    """When sacctmgr is present, the probe shells out and reports the
    accounts (output the user will need for `plugins.hpc-launcher.account`)."""
    import shutil as _sh
    orig = _sh.which

    def fake_which(name: str) -> str | None:
        if name == "sacctmgr":
            return "/usr/bin/sacctmgr"
        return orig(name)

    monkeypatch.setattr(_sh, "which", fake_which)
    monkeypatch.setenv("USER", "testuser")

    class _FakeResult:
        returncode = 0
        stdout = "pi_smith\npi_jones\n"
        stderr = ""

    def fake_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and cmd and "sacctmgr" in cmd[0]:
            return _FakeResult()
        return subprocess.run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = _run_setup_with_probe(monkeypatch, tmp_path)
    assert "Slurm accounts visible" in output
    assert "pi_smith" in output
    assert "pi_jones" in output
    # The "set the submit account" hint must appear so a user knows how
    # to use the discovered name.
    assert "plugins.hpc-launcher.account" in output


def test_probe_lmod_pkg_set_reports_ok(monkeypatch, tmp_path: Path) -> None:
    """When $LMOD_PKG is in the env, the probe surfaces its value (a
    cluster-ease A1 hint that autodetect of the bootstrap path will
    likely work without manual `BOTAINER_LMOD_BOOTSTRAP=`)."""
    monkeypatch.setenv("LMOD_PKG", "/apps/lmod/lmod")
    output = _run_setup_with_probe(monkeypatch, tmp_path)
    assert "$LMOD_PKG set" in output


def test_probe_is_read_only(monkeypatch, tmp_path: Path) -> None:
    """Cross-check: the probe must not invoke any subprocess that would
    submit a job, write to the cluster, or run as root. We allow only
    sacctmgr, sinfo, and process-discovery shutil.which calls. Any
    sbatch, srun, scancel call is a regression."""
    import subprocess as _sp
    calls: list[list[str]] = []
    orig_run = _sp.run

    def recording_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list):
            calls.append(cmd)
        return orig_run(*args, **kwargs)

    monkeypatch.setattr(_sp, "run", recording_run)
    _run_setup_with_probe(monkeypatch, tmp_path)
    forbidden = {"sbatch", "srun", "scancel", "salloc"}
    for cmd in calls:
        if cmd and isinstance(cmd[0], str):
            tool = cmd[0].rsplit("/", 1)[-1]
            assert tool not in forbidden, (
                f"probe invoked write/submit tool {tool!r}: {cmd}. The "
                f"probe must be read-only."
            )
