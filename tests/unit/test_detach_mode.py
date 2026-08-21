"""Tests for `botainer start --detach` mode.

Covers:
- Docker adapter render_argv emits -d instead of -it
- Docker launch captures container_id from `docker run -d` stdout
- Composition.launch refuses --detach for non-docker runtimes
- start --detach CLI flag emits a session-started message and exits
  without attaching
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from botainer.adapters.docker import DockerAdapter
from botainer.core import composition
from botainer.core.refusal import Refused
from botainer.core.spec import (
    AgentRendering,
    Bind,
    BindMode,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    Provenance,
    SessionSpec,
)


def _minimal_docker_spec(**kw) -> SessionSpec:
    workspace_bind = Bind(
        source="/host/workspace",
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="workspace",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_WORKSPACE_RW",
    )
    return SessionSpec(
        session_id="detach-test-1",
        project_uuid="u",
        project_root="/proj",
        state_dir="/state",
        runtime="docker",
        image="ubuntu:24.04@sha256:" + "0" * 64,
        mount_plan=MountPlan(binds=(workspace_bind,)),
        network=NetworkSpec(mode=NetworkMode.NONE),
        **kw,
    )


def test_docker_render_argv_detach_uses_dash_d() -> None:
    spec = _minimal_docker_spec()
    argv = DockerAdapter().render_argv(spec, detach=True)
    assert "-d" in argv
    assert "-it" not in argv


def test_docker_render_argv_no_detach_uses_it() -> None:
    spec = _minimal_docker_spec()
    argv = DockerAdapter().render_argv(spec, detach=False)
    assert "-it" in argv
    assert "-d" not in argv


def test_docker_launch_detach_captures_container_id(monkeypatch) -> None:
    """When detach=True, docker run -d prints the full container ID to stdout;
    DockerAdapter.launch must capture it into RuntimeHandle.id."""
    spec = _minimal_docker_spec()

    # Fake `docker run -d` returning a container ID.
    fake_id = "abc1234567890def" * 4  # 64-char container ID
    mock_proc = MagicMock(returncode=0, stdout=fake_id + "\n", stderr="")
    with patch("shutil.which", return_value="/usr/bin/docker"), \
         patch("subprocess.run", return_value=mock_proc):
        handle = DockerAdapter().launch(spec, detach=True)
    assert handle.runtime == "docker"
    assert handle.id == fake_id
    assert handle.pid is None  # detach: no process attached


def test_docker_launch_detach_failure_raises_refused(monkeypatch) -> None:
    spec = _minimal_docker_spec()
    mock_proc = MagicMock(returncode=125, stdout="", stderr="docker: bad image\n")
    with patch("shutil.which", return_value="/usr/bin/docker"), \
         patch("subprocess.run", return_value=mock_proc):
        with pytest.raises(Refused, match="docker run -d exited 125"):
            DockerAdapter().launch(spec, detach=True)


def test_composition_launch_detach_refuses_apptainer() -> None:
    """Detach mode is Docker-only at v0.1.0."""
    # Bypass the helper to construct an apptainer spec directly.
    workspace_bind = Bind(
        source="/host/workspace",
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="workspace",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_WORKSPACE_RW",
    )
    spec = SessionSpec(
        session_id="d2",
        project_uuid="u",
        project_root="/p",
        state_dir="/s",
        runtime="apptainer",
        image="/path/to/img.sif",
        mount_plan=MountPlan(binds=(workspace_bind,)),
        network=NetworkSpec(mode=NetworkMode.NONE),
    )
    # ApptainerAdapter.validate runs first; for this test we expect a clean
    # refusal from composition.launch's runtime check before validation.
    with pytest.raises(Refused, match="--detach is only supported for the docker runtime"):
        composition.launch(spec, detach=True)


def test_composition_launch_detach_refuses_mock() -> None:
    workspace_bind = Bind(
        source="/host/workspace",
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="workspace",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_WORKSPACE_RW",
    )
    spec = SessionSpec(
        session_id="d3",
        project_uuid="u",
        project_root="/p",
        state_dir="/s",
        runtime="mock",
        image="mock-image",
        mount_plan=MountPlan(binds=(workspace_bind,)),
        network=NetworkSpec(mode=NetworkMode.NONE),
    )
    with pytest.raises(Refused, match="--detach is only supported"):
        composition.launch(spec, detach=True)


def test_cli_start_detach_json_output_format(monkeypatch, tmp_path: Path) -> None:
    """The --detach --json output is a single line of valid JSON with
    session_id, container_id, runtime, and nudge_supported keys."""
    import json as _json

    from click.testing import CliRunner

    from botainer.cli.start import start as start_cmd

    # Build a fake project + spec to run start --detach against. Use minimal
    # state so the project loads.
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".botainer").mkdir()
    (project_root / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    state_root = tmp_path / "botainer-state"
    monkeypatch.setenv("MY_BOTAINER", str(state_root))
    monkeypatch.chdir(project_root)

    # Patch composition.compose_session and composition.launch to avoid
    # needing a real config + plugins + docker.
    fake_spec = _minimal_docker_spec(
        plugins_enabled=("agent-claude", "git", "nudge")
    )
    fake_handle = type("H", (), {"id": "fakectn123", "runtime": "docker", "pid": None})()

    monkeypatch.setattr(
        composition, "compose_session",
        lambda *a, **k: fake_spec,
    )
    # Stub: return the spec unchanged (pre_session hooks return a spec now).
    monkeypatch.setattr(composition, "run_pre_session_hooks", lambda spec: spec)
    monkeypatch.setattr(composition, "launch", lambda spec, detach=False: fake_handle)
    # Capability summary needs to be quiet; with --json we set as_json=True.

    runner = CliRunner()
    result = runner.invoke(
        start_cmd, ["--detach", "--json", "--yes"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    # The output may contain the capability summary as JSON followed by our
    # own JSON line. Find the last { ... } block in the output.
    lines = [line for line in result.output.strip().split("\n") if line.startswith("{")]
    parsed = _json.loads(lines[-1])
    assert parsed["session_id"] == "detach-test-1"
    assert parsed["container_id"] == "fakectn123"
    assert parsed["runtime"] == "docker"
    assert parsed["nudge_supported"] is True


def _run_start_with_plugins(monkeypatch, tmp_path: Path, plugins):
    from click.testing import CliRunner
    from botainer.cli.start import start as start_cmd

    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".botainer").mkdir()
    (project_root / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "botainer-state"))
    monkeypatch.chdir(project_root)
    fake_spec = _minimal_docker_spec(plugins_enabled=plugins)
    fake_handle = type("H", (), {"id": "c1", "runtime": "docker", "pid": None})()
    monkeypatch.setattr(composition, "compose_session", lambda *a, **k: fake_spec)
    monkeypatch.setattr(composition, "run_pre_session_hooks", lambda spec: spec)
    monkeypatch.setattr(composition, "run_host_pre_launch_hooks", lambda spec: spec)
    monkeypatch.setattr(composition, "launch", lambda spec, detach=False: fake_handle)
    runner = CliRunner()
    return runner.invoke(start_cmd, ["--detach", "--yes"], catch_exceptions=False)


def test_cli_start_warns_when_shared_variant_enabled(monkeypatch, tmp_path: Path) -> None:
    """Readiness audit #2: the SHARED-credential warning is derived from the
    ACTUAL enabled auth-family variant (spec.plugins_enabled), not the policy
    default. A session that enabled `agent-claude-shared` MUST warn — even under
    an isolated-default policy."""
    result = _run_start_with_plugins(monkeypatch, tmp_path, ("agent-claude-shared",))
    assert "SHARED" in result.output, result.output
    assert "isolated" in result.output  # the remediation hint


def test_cli_start_no_shared_warning_for_isolated_variant(monkeypatch, tmp_path: Path) -> None:
    """No false SHARED warning when the isolated variant is enabled (the old
    policy-default logic would have mis-warned under a shared default)."""
    result = _run_start_with_plugins(monkeypatch, tmp_path, ("agent-claude-isolated",))
    assert "Auth mode: SHARED" not in result.output, result.output
