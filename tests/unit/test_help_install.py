"""Tests for botainer help install / help nudge."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from botainer.cli.help_install import help_group


def test_help_install_prints_pipx_first() -> None:
    """pipx is the recommended route, and it is the EDITABLE form.

    This test used to require the literal `pipx install botainer` — pinning an
    instruction that cannot work, since the PyPI name is unregistered and
    nothing is published under it. `botainer setup` prints this guide
    automatically on a first run, so the very first command a new user was
    handed was the one guaranteed to fail.
    """
    runner = CliRunner()
    result = runner.invoke(help_group, ["install", "--plain"])
    assert result.exit_code == 0
    pipx_idx = result.output.find("pipx install -e")
    venv_idx = result.output.find("python -m venv")
    assert pipx_idx >= 0, "the editable pipx install must be shown"
    assert venv_idx > pipx_idx, "pipx must be recommended above the venv fallback"


def test_help_install_says_botainer_is_not_published() -> None:
    """A user who tries the obvious command must find out why it failed."""
    runner = CliRunner()
    result = runner.invoke(help_group, ["install", "--plain"])
    assert "not on PyPI" in result.output or "NOT on PyPI" in result.output
    assert "will not work" in result.output


def test_help_install_mentions_conda_env_caveat() -> None:
    """Warning about installing into a conda env's Python."""
    runner = CliRunner()
    result = runner.invoke(help_group, ["install", "--plain"])
    assert result.exit_code == 0
    assert "conda env" in result.output.lower() or "conda env's python" in result.output.lower()


def test_help_install_covers_all_python_tools() -> None:
    """uv, conda, and pip are all explained."""
    runner = CliRunner()
    result = runner.invoke(help_group, ["install", "--plain"])
    assert "uv" in result.output
    assert "conda" in result.output
    assert "pip install" in result.output


def test_help_install_explains_network_modes() -> None:
    """Both network.mode: internet and network.mode: none paths are documented."""
    runner = CliRunner()
    result = runner.invoke(help_group, ["install", "--plain"])
    assert "mode: none" in result.output
    assert "mode: internet" in result.output


def test_help_install_explains_per_project_workflow() -> None:
    """The daily flow (init, login, start) is shown."""
    runner = CliRunner()
    result = runner.invoke(help_group, ["install", "--plain"])
    assert "botainer init" in result.output
    assert "botainer start" in result.output
    assert "botainer status" in result.output


def test_help_nudge_explains_tradeoff() -> None:
    runner = CliRunner()
    result = runner.invoke(help_group, ["nudge"])
    assert result.exit_code == 0
    # §A19: nudge wraps the agent in host-side screen (not tmux).
    assert "screen" in result.output.lower()
    assert "rate limit" in result.output.lower()


def test_help_nudge_covers_hpc() -> None:
    runner = CliRunner()
    result = runner.invoke(help_group, ["nudge"])
    assert "Slurm" in result.output or "srun --overlap" in result.output


def _stub_doctor_disk_ok(monkeypatch) -> None:
    """Doctor's setup preflight refuses when free disk < 5 GB. These
    tests are about the install-guide pointer UX, not disk capacity —
    stub the OS check so the test isn't fragile to dev-container disk
    pressure (the original test passed by luck of having enough free
    space; it fails when /tmp is tight)."""
    import shutil as _shutil
    _real = _shutil.disk_usage

    class _Stat:
        total = 100 * 1024**3
        used = 0
        free = 100 * 1024**3

    def _fake(path):  # noqa: ARG001
        return _Stat()

    monkeypatch.setattr(_shutil, "disk_usage", _fake)

    # Audit T6: `setup` now ABORTS when no container runtime is on PATH (the
    # dev container / CI has neither docker nor apptainer). These tests are
    # about the install-guide UX, not runtime detection — present apptainer so
    # setup proceeds (apptainer needs no daemon probe, unlike docker).
    _real_which = _shutil.which

    def _fake_which(name):
        if name in ("apptainer", "singularity"):
            return f"/usr/bin/{name}"
        return _real_which(name)

    monkeypatch.setattr(_shutil, "which", _fake_which)
    return _real  # not used; kept for clarity that we're overriding


def test_setup_shows_install_guide_pointer_first_time(
    tmp_path: Path, monkeypatch
) -> None:
    """First `botainer setup` includes a pointer to `botainer help install`."""
    from botainer.cli.main import cli
    _stub_doctor_disk_ok(monkeypatch)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(cli, ["setup"])
    assert result.exit_code == 0
    assert "botainer help install" in result.output


def test_setup_skips_install_guide_pointer_on_second_run(
    tmp_path: Path, monkeypatch
) -> None:
    """The .install-guide-shown marker suppresses subsequent prompts."""
    from botainer.cli.main import cli
    _stub_doctor_disk_ok(monkeypatch)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    first = runner.invoke(cli, ["setup"])
    assert "botainer help install" in first.output
    second = runner.invoke(cli, ["setup", "--force"])
    assert "botainer help install" not in second.output


def test_help_install_plain_mode_no_ansi_codes() -> None:
    """`--plain` mode (and non-TTY stdout) should not emit ANSI escapes."""
    runner = CliRunner()
    result = runner.invoke(help_group, ["install", "--plain"])
    assert "\x1b[" not in result.output


def test_install_guide_plugin_list_is_dynamic_and_complete() -> None:
    """The bundled-plugin list in the install guide is derived from
    BUILTIN_PLUGIN_NAMES — every bundled plugin (incl. the brokers) appears, and
    the placeholder is always filled. Guards against an earlier hardcoded
    list that silently drifted (missed agent-*-broker)."""
    from botainer.cli.help_install import _guide_text
    from botainer.plugins.builtin import BUILTIN_PLUGIN_NAMES

    text = _guide_text()
    assert "%%PLUGIN_LIST%%" not in text
    for name in BUILTIN_PLUGIN_NAMES:
        assert name in text, f"bundled plugin {name!r} missing from install guide"
