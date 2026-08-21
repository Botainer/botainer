"""Unit tests for session_record (spec.json persistence)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from botainer.state import session_record as sr


def _record(**overrides) -> sr.SessionRecord:
    base = dict(
        session_id="abcdef0123456789",
        project_uuid="11111111-1111-4111-8111-111111111111",
        project_root="/proj",
        runtime="docker",
        image="ubuntu:24.04",
        host="host1",
        spec={"image": "ubuntu:24.04"},
    )
    base.update(overrides)
    return sr.SessionRecord(**base)


# NOTE (compose-at-submit, task #52): the three _record_hpc_in_container_runtime
# tests were removed — that helper (and the whole start.py --in-container path)
# is deleted. The HPC jobid + screen_session_id are now recorded LOGIN-SIDE by
# submit.py after sbatch parses "Submitted batch job N"
# (session_record.update_runtime(session_dir, slurm_jobid=…, screen_session_id=…)),
# and the compute node writes its hostname into <session_dir>/node itself. The
# Fable-5 review verified the jobid round-trip end to end (record written →
# list_sessions re-reads it → hpc stop --all finds it).


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    rec = _record()
    sr.write(tmp_path, rec)
    loaded = sr.read(tmp_path)
    assert loaded.session_id == rec.session_id
    assert loaded.project_uuid == rec.project_uuid
    assert loaded.runtime == "docker"
    assert loaded.image == "ubuntu:24.04"
    assert loaded.spec == {"image": "ubuntu:24.04"}


def test_write_creates_dir_with_secure_perms(tmp_path: Path) -> None:
    target = tmp_path / "session-1"
    sr.write(target, _record())
    assert target.is_dir()
    # File mode 0600.
    file_mode = (target / sr.RECORD_FILENAME).stat().st_mode & 0o777
    assert file_mode == 0o600


def test_atomic_write_no_partial_file(tmp_path: Path) -> None:
    """If the write is interrupted (simulated), the original record stays valid."""
    sr.write(tmp_path, _record(session_id="origsess"))
    # Now write again: the .tmp should not be left behind on success.
    sr.write(tmp_path, _record(session_id="new1234567890abc"))
    files = list(tmp_path.iterdir())
    tmp_files = [f for f in files if f.name.endswith(".tmp")]
    assert tmp_files == []
    loaded = sr.read(tmp_path)
    assert loaded.session_id == "new1234567890abc"


def test_update_runtime_docker_container_id(tmp_path: Path) -> None:
    sr.write(tmp_path, _record())
    sr.update_runtime(tmp_path, container_id="abc123def456")
    loaded = sr.read(tmp_path)
    assert loaded.docker is not None
    assert loaded.docker.container_id == "abc123def456"


def test_update_runtime_apptainer_slurm_jobid(tmp_path: Path) -> None:
    sr.write(tmp_path, _record(runtime="apptainer"))
    sr.update_runtime(
        tmp_path,
        slurm_jobid="12345",
        node="c1n1",
    )
    loaded = sr.read(tmp_path)
    assert loaded.apptainer is not None
    assert loaded.apptainer.slurm_jobid == "12345"
    assert loaded.apptainer.node == "c1n1"


def test_update_runtime_started_at(tmp_path: Path) -> None:
    sr.write(tmp_path, _record())
    sr.update_runtime(tmp_path, started_at="2026-05-16T22:00:00Z")
    loaded = sr.read(tmp_path)
    assert loaded.started_at == "2026-05-16T22:00:00Z"


def test_list_sessions_returns_newest_first(tmp_path: Path) -> None:
    sr.write(
        tmp_path / "a", _record(session_id="aaaaaaaa", started_at="2026-05-16T01:00:00Z")
    )
    sr.write(
        tmp_path / "b", _record(session_id="bbbbbbbb", started_at="2026-05-16T03:00:00Z")
    )
    sr.write(
        tmp_path / "c", _record(session_id="cccccccc", started_at="2026-05-16T02:00:00Z")
    )
    out = sr.list_sessions(tmp_path)
    assert [r.session_id for r in out] == ["bbbbbbbb", "cccccccc", "aaaaaaaa"]


def test_list_sessions_skips_unparseable(tmp_path: Path) -> None:
    sr.write(tmp_path / "good", _record(session_id="goodsess"))
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / sr.RECORD_FILENAME).write_text("not json{{{")
    out = sr.list_sessions(tmp_path)
    assert [r.session_id for r in out] == ["goodsess"]


def test_list_sessions_skips_launcher_internal_dirs_silently(
    tmp_path: Path, capsys
) -> None:
    """F1: launcher-internal dirs (_submit-scripts, _module-env) are not session
    records and lack spec.json — list_sessions must skip them SILENTLY, not spam
    `[botainer] skipped session record _submit-scripts: FileNotFoundError` on
    every status/stop/nudge/hpc-logs invocation."""
    sr.write(tmp_path / "goodsess1234", _record(session_id="goodsess1234"))
    (tmp_path / "_submit-scripts").mkdir()   # hpc-launcher writes submit scripts here
    (tmp_path / "_module-env").mkdir()       # hpc-modules scalar env-file dir
    (tmp_path / ".hidden").mkdir()
    out = sr.list_sessions(tmp_path)
    assert [r.session_id for r in out] == ["goodsess1234"]
    err = capsys.readouterr().err
    assert "skipped session record" not in err, err
    assert "_submit-scripts" not in err


def test_list_sessions_silent_on_incomplete_session_but_loud_on_corrupt(
    tmp_path: Path, capsys
) -> None:
    """A real hex session dir with NO spec.json = an aborted launch (crashed
    before writing the record — e.g. a missing-image failure). It must be skipped
    SILENTLY (the user shouldn't see a scary FileNotFoundError on `browser watch`).
    A dir with a MALFORMED spec.json is a genuinely corrupt record and IS surfaced
    (Task #211)."""
    sr.write(tmp_path / "goodsess5678", _record(session_id="goodsess5678"))
    (tmp_path / "abcd1234abcd").mkdir()            # hex dir, no spec.json = aborted
    corrupt = tmp_path / "deadbeefdead"
    corrupt.mkdir()
    (corrupt / sr.RECORD_FILENAME).write_text("not json{{{")   # malformed record
    out = sr.list_sessions(tmp_path)
    assert [r.session_id for r in out] == ["goodsess5678"]
    err = capsys.readouterr().err
    assert "abcd1234abcd" not in err                # incomplete → silent
    assert "deadbeefdead" in err                    # corrupt → surfaced
    assert "FileNotFoundError" not in err            # never the scary line


def test_schema_version_check_on_read(tmp_path: Path) -> None:
    bad = tmp_path
    bad.mkdir(exist_ok=True)
    (bad / sr.RECORD_FILENAME).write_text(
        json.dumps({"schema_version": 99, "session_id": "x"})
    )
    with pytest.raises(ValueError, match="schema_version"):
        sr.read(bad)


def test_from_spec(tmp_path: Path) -> None:
    """from_spec produces a record with host populated from socket.gethostname."""
    from botainer.core.spec import SessionSpec

    spec = SessionSpec(
        session_id="test123",
        project_uuid="uuid",
        project_root="/p",
        state_dir="/s",
        runtime="docker",
        image="ubuntu:24.04",
    )
    rec = sr.from_spec(spec)
    assert rec.session_id == "test123"
    assert rec.host == socket.gethostname()
    assert rec.runtime == "docker"
    assert rec.spec["image"] == "ubuntu:24.04"
