"""Tests for botainer/cli/status.py + botainer/cli/stop.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from botainer.cli import status as status_mod
from botainer.cli import stop as stop_mod
from botainer.state import session_record as sr


def _setup_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Build a fake project with state dir; return (project_root, sessions_dir)."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".botainer").mkdir()
    (project_root / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    state_root = tmp_path / "botainer-state"
    monkeypatch.setenv("MY_BOTAINER", str(state_root))
    monkeypatch.chdir(project_root)
    return project_root, state_root


def _make_running_docker_record(sid: str, cid: str = "abc123def456789a") -> sr.SessionRecord:
    return sr.SessionRecord(
        session_id=sid,
        project_uuid="11111111-1111-4111-8111-111111111111",
        project_root="/proj",
        runtime="docker",
        image="ubuntu:24.04",
        host="testhost",
        spec={
            "session_id": sid,
            "project_uuid": "11111111-1111-4111-8111-111111111111",
            "project_root": "/proj",
            "state_dir": "/state",
            "runtime": "docker",
            "image": "ubuntu:24.04",
        },
        docker=sr.DockerHandle(container_id=cid),
        screen_session_id=f"botainer-{cid[:12]}",
        started_at="2026-05-16T22:00:00+00:00",
    )


# ────────── status ──────────


def test_status_no_project_refuses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(status_mod.status, [])
    assert result.exit_code == 2
    assert "not inside a botainer project" in result.output


def test_status_empty_project_says_no_sessions(tmp_path: Path, monkeypatch) -> None:
    _setup_project(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(status_mod.status, [], catch_exceptions=False)
    assert result.exit_code == 0
    assert "no running sessions" in result.output


def test_status_shows_running_docker_session(tmp_path: Path, monkeypatch) -> None:
    project_root, _ = _setup_project(tmp_path, monkeypatch)
    # Build a session record under the project's state dir.
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)

    rec = _make_running_docker_record("abc1234567890abc")
    rec.project_uuid = uid
    rec.spec["project_uuid"] = uid
    sr.write(proj_paths.sessions_dir / rec.session_id, rec)

    # Stub liveness check to return True.
    monkeypatch.setattr("botainer.state.liveness.is_session_alive", lambda r: True)

    runner = CliRunner()
    result = runner.invoke(status_mod.status, [], catch_exceptions=False)
    assert result.exit_code == 0
    assert "abc123456789" in result.output
    assert "docker" in result.output
    assert "▶" in result.output
    # Includes nudge hint when socket is recorded.
    assert "botainer nudge" in result.output


def test_status_json_mode(tmp_path: Path, monkeypatch) -> None:
    project_root, _ = _setup_project(tmp_path, monkeypatch)
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)

    rec = _make_running_docker_record("sidsidsidsidsid1")
    rec.project_uuid = uid
    rec.spec["project_uuid"] = uid
    sr.write(proj_paths.sessions_dir / rec.session_id, rec)

    monkeypatch.setattr("botainer.state.liveness.is_session_alive", lambda r: True)
    runner = CliRunner()
    result = runner.invoke(status_mod.status, ["--json"], catch_exceptions=False)
    parsed = json.loads(result.output)
    assert len(parsed["sessions"]) == 1
    assert parsed["sessions"][0]["session_id"] == "sidsidsidsidsid1"
    assert parsed["sessions"][0]["alive"] is True


def test_status_all_includes_stopped(tmp_path: Path, monkeypatch) -> None:
    project_root, _ = _setup_project(tmp_path, monkeypatch)
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)

    rec = _make_running_docker_record("stopped1stopped1")
    rec.project_uuid = uid
    rec.spec["project_uuid"] = uid
    rec.ended_at = "2026-05-16T23:00:00+00:00"
    sr.write(proj_paths.sessions_dir / rec.session_id, rec)

    monkeypatch.setattr("botainer.state.liveness.is_session_alive", lambda r: False)
    runner = CliRunner()
    # Without --all: nothing shown.
    result = runner.invoke(status_mod.status, [], catch_exceptions=False)
    assert "stopped1" not in result.output
    # With --all: shown.
    result = runner.invoke(status_mod.status, ["--all"], catch_exceptions=False)
    assert "stopped1" in result.output
    assert "■" in result.output  # not running indicator


# ────────── stop ──────────


def test_stop_no_project_refuses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(stop_mod.stop, [])
    assert result.exit_code == 2
    assert "not inside a botainer project" in result.output


def test_stop_empty_says_no_sessions(tmp_path: Path, monkeypatch) -> None:
    _setup_project(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(stop_mod.stop, [], catch_exceptions=False)
    assert "no sessions" in result.output


def test_stop_ambiguous_prefix_refuses(tmp_path: Path, monkeypatch) -> None:
    project_root, _ = _setup_project(tmp_path, monkeypatch)
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)

    for sid in ("abcdef1111111111", "abcdef2222222222"):
        rec = _make_running_docker_record(sid)
        rec.project_uuid = uid
        rec.spec["project_uuid"] = uid
        sr.write(proj_paths.sessions_dir / sid, rec)

    runner = CliRunner()
    result = runner.invoke(stop_mod.stop, ["abcdef"], catch_exceptions=False)
    assert result.exit_code == 2
    assert "ambiguous prefix" in result.output


def test_stop_unique_prefix_invokes_docker_stop(tmp_path: Path, monkeypatch) -> None:
    project_root, _ = _setup_project(tmp_path, monkeypatch)
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)

    rec = _make_running_docker_record("aaaa11111111aaaa", cid="dockercontainer-abc")
    rec.project_uuid = uid
    rec.spec["project_uuid"] = uid
    sr.write(proj_paths.sessions_dir / rec.session_id, rec)

    mock_proc = MagicMock(returncode=0, stdout="docker-abc\n", stderr="")
    with patch("shutil.which", return_value="/usr/bin/docker"), \
         patch("subprocess.run", return_value=mock_proc) as mock_run:
        runner = CliRunner()
        result = runner.invoke(stop_mod.stop, ["aaaa"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "stopped" in result.output
    # Verify docker stop was called with the right container ID.
    call_args = [c.args[0] for c in mock_run.call_args_list]
    assert any("stop" in a and "dockercontainer-abc" in a for a in call_args)


def test_stop_force_uses_docker_kill(tmp_path: Path, monkeypatch) -> None:
    project_root, _ = _setup_project(tmp_path, monkeypatch)
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)

    rec = _make_running_docker_record("bbbb11111111bbbb", cid="dockercontainer-xyz")
    rec.project_uuid = uid
    rec.spec["project_uuid"] = uid
    sr.write(proj_paths.sessions_dir / rec.session_id, rec)

    mock_proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch("shutil.which", return_value="/usr/bin/docker"), \
         patch("subprocess.run", return_value=mock_proc) as mock_run:
        runner = CliRunner()
        result = runner.invoke(
            stop_mod.stop, ["bbbb", "--force"], catch_exceptions=False
        )
    assert result.exit_code == 0
    call_args = [c.args[0] for c in mock_run.call_args_list]
    assert any("kill" in a and "dockercontainer-xyz" in a for a in call_args)


# ────────── broker health surfacing (mid-session broker death) ──────────
#
# Broker START failure is already fail-closed + surfaced (the pre_session hook
# returns nonzero; plugins/hooks.py refuses the launch). A broker that dies
# MID-session used to be invisible: the caged agent just gets "can't connect to
# api". These pin the `botainer status` warning that makes it visible.
# See the host finding + TROUBLESHOOTING "Auth / credentials".


def _record_with_broker(sid: str, pid: int) -> sr.SessionRecord:
    rec = _make_running_docker_record(sid)
    rec.extra_runtime_handle = {"broker": {"pid": pid}}
    return rec


def _write_live_session(tmp_path: Path, monkeypatch, rec: sr.SessionRecord):
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(Path.cwd(), identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)
    rec.project_uuid = uid
    rec.spec["project_uuid"] = uid
    sr.write(proj_paths.sessions_dir / rec.session_id, rec)
    monkeypatch.setattr("botainer.state.liveness.is_session_alive", lambda r: True)


def test_status_warns_when_broker_died_midsession(tmp_path: Path, monkeypatch) -> None:
    _setup_project(tmp_path, monkeypatch)
    # A pid that cannot exist -> os.kill raises ProcessLookupError -> DOWN.
    _write_live_session(tmp_path, monkeypatch, _record_with_broker("abc1234567890abc", 999_999_999))

    result = CliRunner().invoke(status_mod.status, [], catch_exceptions=False)
    assert result.exit_code == 0
    assert "credential broker for this session is NOT running" in result.output
    assert "broker-daemon.err" in result.output        # points at the reason
    assert "botainer auth login" in result.output      # points at the fix


def test_status_quiet_when_broker_alive(tmp_path: Path, monkeypatch) -> None:
    _setup_project(tmp_path, monkeypatch)
    import os as _os
    # Our own pid is definitely alive.
    _write_live_session(tmp_path, monkeypatch, _record_with_broker("abc1234567890abc", _os.getpid()))

    result = CliRunner().invoke(status_mod.status, [], catch_exceptions=False)
    assert result.exit_code == 0
    assert "NOT running" not in result.output


def test_status_no_broker_key_is_silent(tmp_path: Path, monkeypatch) -> None:
    """Non-broker sessions (and older records) must not grow a spurious warning."""
    _setup_project(tmp_path, monkeypatch)
    _write_live_session(tmp_path, monkeypatch, _make_running_docker_record("abc1234567890abc"))

    result = CliRunner().invoke(status_mod.status, [], catch_exceptions=False)
    assert result.exit_code == 0
    assert "NOT running" not in result.output


def test_status_json_exposes_broker_alive(tmp_path: Path, monkeypatch) -> None:
    _setup_project(tmp_path, monkeypatch)
    _write_live_session(tmp_path, monkeypatch, _record_with_broker("abc1234567890abc", 999_999_999))

    result = CliRunner().invoke(status_mod.status, ["--json"], catch_exceptions=False)
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["sessions"][0]["broker_alive"] is False
