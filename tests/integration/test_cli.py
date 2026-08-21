"""CLI surface tests via click's CliRunner."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.cli.main import cli


def test_version() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_help_lists_core_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for verb in (
        "init",
        "setup",
        "start",
        "attach",
        "stop",
        "status",
        "list",
        "inspect",
        "dry-run",
        "access",
        "doctor",
        "policy",
        "plugin",
    ):
        assert verb in result.output, verb


def test_setup_writes_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Stub doctor's disk check so the test isn't fragile to dev-container
    # disk pressure (the preflight refuses on free < 5 GB).
    import shutil as _shutil

    class _Stat:
        total = 100 * 1024**3
        used = 0
        free = 100 * 1024**3

    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: _Stat())
    # Audit T6: setup now aborts without a container runtime on PATH; present
    # apptainer (no daemon probe needed) so this policy-writing test proceeds.
    _rw = _shutil.which
    monkeypatch.setattr(
        _shutil, "which",
        lambda n: f"/usr/bin/{n}" if n in ("apptainer", "singularity") else _rw(n),
    )
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    runner = CliRunner()
    result = runner.invoke(cli, ["setup"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "s" / "policy.yaml").exists()


def test_init_writes_project_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = tmp_path / "proj"
    proj.mkdir()
    runner = CliRunner()
    monkeypatch.chdir(proj)
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    assert (proj / ".botainer" / "project-id").exists()
    assert (proj / ".botainer" / "config.yaml").exists()


def test_dry_run_prints_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = tmp_path / "proj"
    proj.mkdir()
    runner = CliRunner()
    monkeypatch.chdir(proj)
    runner.invoke(cli, ["setup"])
    runner.invoke(cli, ["init"])
    from tests.conftest import append_image_to_config
    append_image_to_config(proj)
    result = runner.invoke(cli, ["dry-run"])
    assert result.exit_code == 0, result.output
    assert "Adapter:" in result.output


def test_inspect_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = tmp_path / "proj"
    proj.mkdir()
    runner = CliRunner()
    monkeypatch.chdir(proj)
    runner.invoke(cli, ["setup"])
    runner.invoke(cli, ["init"])
    from tests.conftest import append_image_to_config
    append_image_to_config(proj)
    result = runner.invoke(cli, ["inspect"])
    assert result.exit_code == 0, result.output
    assert "Session:" in result.output
    assert "Reminder:" in result.output


def test_inspect_json_emits_dict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = tmp_path / "proj"
    proj.mkdir()
    runner = CliRunner()
    monkeypatch.chdir(proj)
    runner.invoke(cli, ["setup"])
    runner.invoke(cli, ["init"])
    from tests.conftest import append_image_to_config
    append_image_to_config(proj)
    result = runner.invoke(cli, ["inspect", "--json"])
    assert result.exit_code == 0, result.output
    import json

    parsed = json.loads(result.output)
    assert "session_id" in parsed
    assert "mount_plan" in parsed
    # AUDIT (MEDIUM): --json must stay clean JSON — the pre_session
    # incompleteness caveat is suppressed in --json mode (machine surface).
    assert "pre_session hooks have NOT run" not in result.output


def test_inspect_human_view_warns_plan_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AUDIT (MEDIUM): inspect calls compose_session only (no
    pre_session hooks), so its mount plan OMITS hook-contributed binds (the
    agent credential bind, git overlay, …). DN-028 §3 calls inspect the
    pre-launch 'see exactly what will happen' surface, so the human (tree) view
    must flag the omission — dry-run already did; inspect was silent."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = tmp_path / "proj"
    proj.mkdir()
    runner = CliRunner()
    monkeypatch.chdir(proj)
    runner.invoke(cli, ["setup"])
    runner.invoke(cli, ["init"])
    from tests.conftest import append_image_to_config
    append_image_to_config(proj)
    result = runner.invoke(cli, ["inspect"])  # human tree view
    assert result.exit_code == 0, result.output
    # #160 (S2): caveat now covers host_pre_launch (hpc-modules) too.
    assert "host_pre_launch hooks have NOT run" in result.output
    assert "dry-run" in result.output  # the remediation pointer


def test_inspect_protection_view(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = tmp_path / "proj"
    proj.mkdir()
    runner = CliRunner()
    monkeypatch.chdir(proj)
    runner.invoke(cli, ["setup"])
    runner.invoke(cli, ["init"])
    from tests.conftest import append_image_to_config
    append_image_to_config(proj)
    result = runner.invoke(cli, ["inspect", "--protection"])
    assert result.exit_code == 0, result.output
    assert "Protection view" in result.output


def test_access_prints_agent_view(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = tmp_path / "proj"
    proj.mkdir()
    runner = CliRunner()
    monkeypatch.chdir(proj)
    runner.invoke(cli, ["setup"])
    runner.invoke(cli, ["init"])
    from tests.conftest import append_image_to_config
    append_image_to_config(proj)
    result = runner.invoke(cli, ["access"])
    assert result.exit_code == 0, result.output
    assert "AGENT_ACCESS" in result.output


def test_doctor_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "runtime." in result.output


def test_policy_show(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    runner = CliRunner()
    runner.invoke(cli, ["setup"])
    result = runner.invoke(cli, ["policy", "show"])
    assert result.exit_code == 0, result.output
    assert "policy" in result.output.lower()


def test_list_with_no_projects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    runner = CliRunner()
    runner.invoke(cli, ["setup"])
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "no projects" in result.output.lower() or result.output.strip() == ""


def test_attach_outside_project_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """attach run outside a project refuses with a clear hint (no longer a stub)."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["attach"])
    assert result.exit_code != 0
    assert "not inside a botainer project" in result.output


def test_plugin_list_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    runner = CliRunner()
    result = runner.invoke(cli, ["plugin", "list"])
    assert result.exit_code == 0
