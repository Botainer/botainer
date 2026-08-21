"""Tests for the by-name symlinks under state/by-name/.

These give users a discoverable view of their project state dirs without
breaking the UUID-canonical layout.
"""

from __future__ import annotations

import os
from pathlib import Path

from botainer.state import dir as state_dir


def test_by_name_symlink_created(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state-root"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    state_dir.ensure_project_dirs(
        paths, "11111111-1111-4111-8111-111111111111", project_name="myproject"
    )
    link = paths.state_dir / "by-name" / "myproject-11111111"
    assert link.is_symlink()
    assert link.resolve() == (
        paths.state_dir / "11111111-1111-4111-8111-111111111111"
    ).resolve()


def test_by_name_symlink_omitted_when_name_missing(tmp_path: Path, monkeypatch) -> None:
    """No name → no by-name dir entry."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state-root"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    state_dir.ensure_project_dirs(paths, "22222222-2222-4222-8222-222222222222")
    by_name = paths.state_dir / "by-name"
    if by_name.exists():
        # Empty is also acceptable.
        assert list(by_name.iterdir()) == []


def test_by_name_symlink_refreshed_on_rename(tmp_path: Path, monkeypatch) -> None:
    """Calling ensure_project_dirs with a different name updates symlink set."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state-root"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uuid = "33333333-3333-4333-8333-333333333333"
    state_dir.ensure_project_dirs(paths, uuid, project_name="old-name")
    state_dir.ensure_project_dirs(paths, uuid, project_name="new-name")
    by_name = paths.state_dir / "by-name"
    # New name's symlink exists.
    assert (by_name / "new-name-33333333").is_symlink()
    # Old name's symlink may still exist (we don't garbage-collect old names);
    # that's intentional: a user could re-symlink to old paths.
    # Both should resolve to the same canonical UUID dir.
    assert (by_name / "new-name-33333333").resolve() == (paths.state_dir / uuid).resolve()


def test_by_name_sanitization(tmp_path: Path, monkeypatch) -> None:
    """Special characters in project names are sanitized."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state-root"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    state_dir.ensure_project_dirs(
        paths,
        "44444444-4444-4444-8444-444444444444",
        project_name="weird/name with$pace!",
    )
    by_name = paths.state_dir / "by-name"
    links = list(by_name.iterdir())
    assert len(links) == 1
    link_name = links[0].name
    # No slashes, no spaces, no $/!.
    assert "/" not in link_name
    assert " " not in link_name
    assert "$" not in link_name
    assert "!" not in link_name
    assert link_name.endswith("-44444444")


def test_sanitize_empty_string_yields_a_word_not_punctuation() -> None:
    # Was `_`, so an unnamed project listed as `_-1a2b3c4d` and every project
    # whose name did not survive sanitizing looked identical. `project` at
    # least reads as one. See tests/unit/test_project_name_sanitizing.py.
    assert state_dir._sanitize_for_dirname("") == "project"


def test_sanitize_long_name_truncated() -> None:
    """Names longer than 64 chars are truncated to avoid filesystem limits."""
    name = "a" * 200
    out = state_dir._sanitize_for_dirname(name)
    assert len(out) == 64


def test_sanitize_windows_reserved_basenames_prefixed() -> None:
    """Windows-reserved names (con/prn/aux/nul/comN/lptN) get `_` prefix."""
    for name in ("con", "CON", "Con", "prn", "aux", "nul",
                 "com1", "com9", "lpt1", "lpt9"):
        out = state_dir._sanitize_for_dirname(name)
        assert out.startswith("_"), f"{name!r} -> {out!r}; expected _ prefix"


def test_sanitize_non_reserved_unchanged() -> None:
    """Normal names are not prefixed."""
    for name in ("project", "my-app", "test_123", "myproject"):
        out = state_dir._sanitize_for_dirname(name)
        assert not out.startswith("_")
        assert out == name


def test_by_name_symlink_target_is_relative(tmp_path: Path, monkeypatch) -> None:
    """The symlink target is relative; copying/moving the state dir doesn't
    break it."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state-root"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    state_dir.ensure_project_dirs(
        paths, "55555555-5555-4555-8555-555555555555", project_name="relproj"
    )
    link = paths.state_dir / "by-name" / "relproj-55555555"
    target = os.readlink(link)
    assert not Path(target).is_absolute()
    assert target.endswith("55555555-5555-4555-8555-555555555555")
