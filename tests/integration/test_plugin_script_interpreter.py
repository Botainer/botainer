"""Regression test: plugin .py scripts run under sys.executable
(botainer's Python), not the system Python via shebang.

real-host failure on Grace: `botainer hpc submit` died
with `ModuleNotFoundError: No module named 'yaml'`. Root cause: the
plugin's submit.py was invoked via the kernel-honored shebang
`#!/usr/bin/env python3`, which resolved to /usr/bin/python3 on
Grace — a system Python without botainer's deps (pyyaml, click,
pydantic).

This test pins: when a plugin script ends in .py, the dispatch
argv MUST start with sys.executable, NOT the script path. This
makes dependency availability deterministic (matches botainer's
own venv) and incidentally tightens PATH-injection resistance.

Two dispatch sites tested:
  - botainer.plugins.manifest.declared_commands (the `plugin <name>
    <verb>` dispatcher; what `botainer hpc submit` ends up using)
  - botainer.cli.auth._invoke_plugin_login (the `auth login`
    dispatcher; preventive — login.py imports only stdlib today but
    the pattern should be consistent)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

from botainer.plugins import builtin as plugin_builtin
from botainer.state import dir as state_dir


@pytest.fixture
def installed_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()
    return state


# ─────────── manifest.declared_commands dispatcher ───────────


def test_manifest_dispatcher_uses_sys_executable_for_py_scripts(
    installed_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `plugin <name> <verb>` handler must use sys.executable for
    .py scripts — not the shebang's /usr/bin/env python3."""
    from botainer.plugins import lifecycle as lifecycle_module
    from botainer.plugins.manifest import declared_commands

    # Pick any installed plugin with a .py command. hpc-launcher's
    # submit is the canonical case (it's the one that failed).
    plugin = next(
        p for p in lifecycle_module.list_installed()
        if p.name == "hpc-launcher"
    )
    cmds = declared_commands(plugin)
    assert "submit" in cmds, "hpc-launcher must declare a `submit` command"
    submit_cmd = cmds["submit"]

    # Set up the runtime context the handler reads (project + state).
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    # We don't need a real project — just enough that find_project_root
    # returns SOMETHING; if it doesn't find one, it falls back to cwd.

    # Capture the argv that the handler would pass to subprocess.
    captured_argv: list[str] = []

    def fake_call(argv, **kwargs):
        captured_argv.extend(argv)
        return 0

    with mock.patch("subprocess.call", side_effect=fake_call), \
         mock.patch("sys.exit"):  # handler calls sys.exit(rc); mock it
        from click.testing import CliRunner
        runner = CliRunner()
        # Pass no args (the underlying script will fail validation,
        # but we don't reach the script — we mock subprocess.call).
        runner.invoke(submit_cmd, [])

    assert captured_argv, "subprocess.call was never invoked"
    assert captured_argv[0] == sys.executable, (
        f"Plugin .py script must be invoked under sys.executable "
        f"({sys.executable!r}), got argv[0] = {captured_argv[0]!r}. "
        f"This is the 2026-05-19 Grace `ModuleNotFoundError: yaml` bug."
    )
    assert captured_argv[1].endswith(".py"), (
        f"argv[1] should be the script path; got {captured_argv[1]!r}"
    )


def test_manifest_dispatcher_refuses_tampered_project_id(
    installed_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7 validator-parity audit: the `plugin <name> <verb>`
    dispatcher is the UNIQUE path `botainer hpc submit` traverses — it does
    NOT go through compose_session/resolve_identity. A tampered, git-shareable
    .botainer/project-id (newline payload) flowed raw into BOTAINER_PROJECT_UUID
    and then unquoted into the generated sbatch script (HIGH sbatch injection).
    The dispatcher must now validate via identity._validate_uuid and refuse
    (exit 5) BEFORE subprocess.call is reached."""
    from botainer.plugins import lifecycle as lifecycle_module
    from botainer.plugins.manifest import declared_commands

    plugin = next(
        p for p in lifecycle_module.list_installed()
        if p.name == "hpc-launcher"
    )
    submit_cmd = declared_commands(plugin)["submit"]

    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    # The exploit: a project-id with a mid-string newline injects #SBATCH
    # directives + a shell line into the rendered sbatch script. read_project_id
    # only .strip()s the ends, so the interior newline survives.
    (proj / ".botainer" / "project-id").write_text(
        "aaaa\n#SBATCH --mail-user=attacker@evil\nrm -rf $HOME # \n",
        encoding="utf-8",
    )
    monkeypatch.chdir(proj)

    reached_subprocess = False

    def fake_call(argv, **kwargs):  # pragma: no cover - must NOT run
        nonlocal reached_subprocess
        reached_subprocess = True
        return 0

    with mock.patch("subprocess.call", side_effect=fake_call):
        from click.testing import CliRunner
        result = CliRunner().invoke(submit_cmd, [])

    assert not reached_subprocess, (
        "dispatcher reached subprocess.call with a tampered project-id — "
        "the uuid validation chokepoint did not fire BEFORE export/launch"
    )
    assert result.exit_code == 5, (
        f"dispatcher must refuse a tampered project-id with exit 5; "
        f"got {result.exit_code}. output: {result.output!r}"
    )


# ─────────── auth._invoke_plugin_login dispatcher ───────────


def test_auth_login_dispatcher_uses_sys_executable_for_py_scripts(
    installed_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login script invocation must use sys.executable for .py
    scripts (same pattern as the manifest dispatcher)."""
    from botainer.cli import auth as auth_module

    captured_argv: list[str] = []

    class FakeProc:
        def __init__(self) -> None:
            self.returncode = 0

    def fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        return FakeProc()

    with mock.patch.object(auth_module.subprocess, "run", side_effect=fake_run):
        # agent-claude-shared has a .py login script.
        rc = auth_module._invoke_plugin_login(
            "agent-claude-shared",
            shared=True,
            profile="default",
        )

    assert rc == 0
    assert captured_argv, "subprocess.run was never invoked"
    assert captured_argv[0] == sys.executable, (
        f"Login .py script must be invoked under sys.executable "
        f"({sys.executable!r}), got argv[0] = {captured_argv[0]!r}."
    )
    assert captured_argv[1].endswith(".py")
