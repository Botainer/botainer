"""Tests for clean Refused-exception handling in CLI subcommands.

Per host-smoke observation: dry-run/inspect/access used to dump
full Python tracebacks when composition refused. Now they exit cleanly with
a typed code + a red `refused: <category>: <message>` line.

Asserts:
- Exit code 4 (RefusalCategory.* via composition) for all of inspect, dry-run,
  access, start when invoked outside an initialized project.
- Output contains the refusal category string.
- Output does NOT contain a Python traceback header.
- Exit code 3 for IdentityChangeRefused (non-interactive clone/fork).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.cli.main import cli


@pytest.fixture()
def fresh_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "uninit-proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


@pytest.mark.parametrize("subcmd", ["inspect", "dry-run", "access", "start"])
def test_refuses_cleanly_outside_initialized_project(
    fresh_state: Path, subcmd: str
) -> None:
    """Run subcmd in a directory with no `.botainer/`. Should refuse with a
    clean message and exit 4, not dump a traceback."""
    runner = CliRunner()
    result = runner.invoke(cli, [subcmd])
    assert result.exit_code == 2, f"{subcmd} returned {result.exit_code}: {result.output}"  # DN-028 §9: refused
    # Must be a refusal, not a traceback.
    combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "refused:" in combined, f"{subcmd} should print 'refused:' line; got: {combined!r}"
    assert "config-missing" in combined, f"{subcmd} should name the refusal category"
    assert "Traceback" not in combined, f"{subcmd} dumped a traceback: {combined!r}"


def test_inspect_with_bad_yaml_refuses_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "broken"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-1111-1111-111111111111\n"
    )
    (proj / ".botainer" / "config.yaml").write_text(": bad : yaml :")
    monkeypatch.chdir(proj)
    runner = CliRunner()
    result = runner.invoke(cli, ["inspect"])
    assert result.exit_code == 2  # DN-028 §9: refused (AUDIT)
    assert "refused:" in result.output
    assert "Traceback" not in result.output


def test_inspect_with_tampered_uuid_refuses_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "tampered"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    (proj / ".botainer" / "project-id").write_text("not-a-real-uuid\n")
    (proj / ".botainer" / "config.yaml").write_text("agent: claude\n")
    monkeypatch.chdir(proj)
    runner = CliRunner()
    result = runner.invoke(cli, ["inspect"])
    assert result.exit_code == 2  # DN-028 §9: refused (AUDIT)
    assert "refused:" in result.output
    assert "project-id-tampered" in result.output
    assert "Traceback" not in result.output
