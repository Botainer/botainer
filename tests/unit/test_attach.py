"""Tests for botainer attach."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from botainer.cli import attach as attach_mod
from botainer.state import session_record as sr


def _setup_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
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


def _make_record(sid: str, runtime: str = "docker", cid: str = "abc123def456") -> sr.SessionRecord:
    return sr.SessionRecord(
        session_id=sid,
        project_uuid="u",
        project_root="/p",
        runtime=runtime,
        image="img",
        host="h",
        spec={},
        docker=sr.DockerHandle(container_id=cid) if runtime == "docker" else None,
        apptainer=sr.ApptainerHandle() if runtime == "apptainer" else None,
    )


def test_attach_no_project_refuses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(attach_mod.attach, [])
    assert result.exit_code == 2
    assert "not inside a botainer project" in result.output


def test_attach_no_sessions_refuses(tmp_path: Path, monkeypatch) -> None:
    _setup_project(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(attach_mod.attach, [])
    assert result.exit_code == 2
    # Task #147: attach now filters records to alive sessions and reports
    # 'no RUNNING sessions ... (0 total records, none alive)'.
    assert "no RUNNING sessions" in result.output or "no running sessions" in result.output


def test_attach_unknown_session_id_refuses(tmp_path: Path, monkeypatch) -> None:
    project_root, _ = _setup_project(tmp_path, monkeypatch)
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)
    rec = _make_record("aaaaaaaaaaaaaaaa")
    rec.project_uuid = uid
    sr.write(proj_paths.sessions_dir / rec.session_id, rec)

    runner = CliRunner()
    result = runner.invoke(attach_mod.attach, ["zzzz"])
    assert result.exit_code == 2
    assert "no session matches" in result.output


def test_attach_apptainer_with_slurm_redirects_to_srun_overlap(
    tmp_path: Path, monkeypatch
) -> None:
    project_root, _ = _setup_project(tmp_path, monkeypatch)
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)
    rec = sr.SessionRecord(
        session_id="apptest1apptest2",
        project_uuid=uid,
        project_root=str(project_root),
        runtime="apptainer",
        image="/sif",
        host="grace",
        spec={},
        apptainer=sr.ApptainerHandle(slurm_jobid="12345"),
    )
    sr.write(proj_paths.sessions_dir / rec.session_id, rec)
    runner = CliRunner()
    result = runner.invoke(attach_mod.attach, [], catch_exceptions=False)
    assert result.exit_code == 2
    assert "srun --overlap" in result.output
    assert "12345" in result.output


def test_attach_mock_runtime_refuses(tmp_path: Path, monkeypatch) -> None:
    project_root, _ = _setup_project(tmp_path, monkeypatch)
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)
    rec = sr.SessionRecord(
        session_id="mocktest1mocktest",
        project_uuid=uid,
        project_root=str(project_root),
        runtime="mock",
        image="img",
        host="h",
        spec={},
    )
    sr.write(proj_paths.sessions_dir / rec.session_id, rec)
    runner = CliRunner()
    result = runner.invoke(attach_mod.attach, [], catch_exceptions=False)
    assert result.exit_code == 2
    # Task #147: mock runtime is never alive per liveness.is_session_alive,
    # so attach's alive-filter rejects it before reaching the
    # 'mock runtime has nothing to attach' refusal. Both refusals are
    # correct; accept either message form.
    assert "mock runtime" in result.output or "no RUNNING sessions" in result.output


def test_attach_ambiguous_prefix_refuses(tmp_path: Path, monkeypatch) -> None:
    project_root, _ = _setup_project(tmp_path, monkeypatch)
    from botainer.core import identity
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)
    for sid in ("aaaa1111111111aa", "aaaa2222222222aa"):
        rec = _make_record(sid)
        rec.project_uuid = uid
        sr.write(proj_paths.sessions_dir / sid, rec)
    runner = CliRunner()
    result = runner.invoke(attach_mod.attach, ["aaaa"], catch_exceptions=False)
    assert result.exit_code == 2
    assert "ambiguous" in result.output
