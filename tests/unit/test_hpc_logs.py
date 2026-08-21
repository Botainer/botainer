"""Tests for `botainer hpc logs` — the C5 submit→watch convenience.

Real cluster output paths aren't reachable from this dev container; these tests
cover the resolution + refusal logic (no records found, jobid not found, wrong
cwd-vs-project) and the printed-path contract."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.cli.hpc import hpc as hpc_group


def test_logs_refuses_no_project_for_cwd(monkeypatch, tmp_path: Path) -> None:
    """State dir exists but cwd isn't a known botainer project → refuse with the
    --all-projects hint."""
    from botainer.state import dir as state_dir
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(hpc_group, ["logs"], catch_exceptions=True)
    assert result.exit_code == 2
    assert "no botainer project" in result.output
    assert "--all-projects" in result.output


def test_logs_refuses_jobid_not_in_records(monkeypatch, tmp_path: Path) -> None:
    """Explicit jobid that no session record carries → refuse with `hpc list` hint."""
    from botainer.state import dir as state_dir
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    result = CliRunner().invoke(
        hpc_group, ["logs", "--all-projects", "999999"], catch_exceptions=True
    )
    assert result.exit_code == 2
    assert "no botainer session record carries jobid" in result.output \
        or "no botainer sbatch jobs" in result.output


def test_logs_cwd_filter_uses_paths_not_path_history(monkeypatch, tmp_path: Path) -> None:
    """Regression (compose-at-submit review MEDIUM-1): the cwd→project filter
    must iterate ProjectListEntry.paths (a tuple[str]), NOT a non-existent
    `path_history` attribute. With ≥1 registered project the buggy code
    AttributeError'd — crashing the exact `hpc logs <jobid> -f` the submit output
    recommends. The prior tests missed it because a fresh state has NO projects,
    so the buggy comprehension never evaluated the attribute. Here we register a
    project and cd into it so the filter actually runs against a real entry."""
    from botainer.core import identity
    from botainer.state import dir as state_dir
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    # Sanity: the project is registered and its path is on the entry.
    entries = state_dir.list_projects()
    assert entries and any(str(proj) in (e.paths or ()) for e in entries)
    monkeypatch.chdir(proj)
    result = CliRunner().invoke(hpc_group, ["logs"], catch_exceptions=True)
    # No AttributeError crash: the project matches cwd, but has no sbatch
    # sessions → a clean refusal (exit 2), never a traceback.
    assert not isinstance(result.exception, AttributeError), result.output
    assert result.exit_code == 2


def test_hpc_attach_accepts_positional_jobid_and_accurate_help() -> None:
    """#56: `hpc attach <JOBID>` positional (consistent with `hpc logs`/`stop`),
    and the help no longer claims a non-existent 'pick from squeue' picker."""
    result = CliRunner().invoke(hpc_group, ["attach", "--help"])
    assert result.exit_code == 0
    assert "[JOBID]" in result.output          # positional argument exists
    assert "pick from squeue" not in result.output   # bogus claim removed
