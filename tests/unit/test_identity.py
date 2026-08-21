"""Tests for project identity (UUID + path history + clone/fork)."""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import identity
from botainer.core.refusal import RefusalCategory, Refused
from botainer.state import dir as state_dir


def test_init_project_creates_uuid_and_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("MY_BOTAINER", str(state))
    result = identity.init_project(proj, agent="claude", force=False, non_interactive=True)
    assert result.was_existing is False
    assert (proj / ".botainer" / "project-id").exists()
    # Re-init without force re-uses the same UUID.
    again = identity.init_project(proj, agent="claude", force=False, non_interactive=True)
    assert again.was_existing
    assert again.project_id == result.project_id


def test_resolve_identity_records_first_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "s"
    proj = tmp_path / "p"
    proj.mkdir()
    monkeypatch.setenv("MY_BOTAINER", str(state))
    init = identity.init_project(proj, agent="claude", force=False, non_interactive=True)
    uid, _sd = identity.resolve_identity(proj, identity_accept=False)
    assert uid == init.project_id
    meta = state_dir.read_meta(
        state_dir.ensure_user_state_dir(create_if_missing=False).for_project(uid)
    )
    hist = meta["path_history"]
    assert isinstance(hist, list)
    assert hist[-1] == str(proj.resolve())


def test_resolve_identity_moved_path_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "s"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    proj1 = tmp_path / "proj1"
    proj1.mkdir()
    init = identity.init_project(proj1, agent="claude", force=False, non_interactive=True)
    # Now physically "move" the checkout: rename proj1 → proj2 (so old path no
    # longer exists).
    proj2 = tmp_path / "proj2"
    proj1.rename(proj2)
    uid, _ = identity.resolve_identity(proj2, identity_accept=False)
    assert uid == init.project_id
    meta = state_dir.read_meta(
        state_dir.ensure_user_state_dir(create_if_missing=False).for_project(uid)
    )
    # path_history is now record-shaped per v0.0.13 schema upgrade.
    last_record = meta["path_history"][-1]
    assert isinstance(last_record, dict)
    assert last_record["path"] == str(proj2.resolve())


def test_resolve_identity_clone_ambiguous_non_interactive_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "s"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    proj1 = tmp_path / "proj1"
    proj1.mkdir()
    init = identity.init_project(proj1, agent="claude", force=False, non_interactive=True)
    # Make a "second copy" that shares the same project-id but at a different path.
    proj2 = tmp_path / "proj2"
    proj2.mkdir()
    (proj2 / ".botainer").mkdir()
    (proj2 / ".botainer" / "project-id").write_text(init.project_id + "\n")
    # Now both proj1 and proj2 exist. Non-interactive → refuse.
    # Force non-tty via monkeypatch on sys.stdin.isatty.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(identity.IdentityChangeRefused):
        identity.resolve_identity(proj2, identity_accept=False)


def test_resolve_identity_clone_with_accept_records_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "s"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    proj1 = tmp_path / "proj1"
    proj1.mkdir()
    init = identity.init_project(proj1, agent="claude", force=False, non_interactive=True)
    proj2 = tmp_path / "proj2"
    proj2.mkdir()
    (proj2 / ".botainer").mkdir()
    (proj2 / ".botainer" / "project-id").write_text(init.project_id + "\n")
    uid, _ = identity.resolve_identity(proj2, identity_accept=True)
    assert uid == init.project_id


def test_resolve_identity_fork_mints_new_uuid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "s"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    proj1 = tmp_path / "proj1"
    proj1.mkdir()
    init = identity.init_project(proj1, agent="claude", force=False, non_interactive=True)
    proj2 = tmp_path / "proj2"
    proj2.mkdir()
    (proj2 / ".botainer").mkdir()
    (proj2 / ".botainer" / "project-id").write_text(init.project_id + "\n")

    def fake_prompt(_prompt: str) -> str:
        return "fork"

    uid, _ = identity.resolve_identity(proj2, identity_accept=False, prompt_fn=fake_prompt)
    assert uid != init.project_id
    # proj2's project-id should now contain the new UUID.
    pid_file = proj2 / ".botainer" / "project-id"
    assert pid_file.read_text().strip() == uid


def test_resolve_identity_tampered_project_id_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "s"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    (proj / ".botainer" / "project-id").write_text("not-a-uuid-at-all")
    with pytest.raises(Refused) as exc:
        identity.resolve_identity(proj, identity_accept=False)
    assert exc.value.category == RefusalCategory.PROJECT_ID_TAMPERED
