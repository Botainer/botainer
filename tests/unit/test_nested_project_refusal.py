"""THREAT-MODEL audit (BS-5): a botainer project nested inside another
must be REFUSED, not silently preferred.

The null-bind mask that makes `.botainer/config.yaml` trusted input is structural
but covers exactly one path — `_MASK_REQUIRED_UNDER_RW = {"/workspace":
("/workspace/.botainer",)}`. Project discovery, its consumer, walks up to the
NEAREST `.botainer/project-id`. So the mask's domain is strictly smaller than its
consumer's: nothing masks `/workspace/anysubdir/.botainer/`, which a caged agent
(rw on its workspace) can create. A user who later runs any botainer command from
that subdirectory ON THE HOST would load an agent-authored config controlling
plugins_enabled, mounts.extra, network.mode, agent_permissions and image.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.cli._common import find_project_root
from botainer.core.refusal import Refused


def _make_project(root: Path) -> Path:
    (root / ".botainer").mkdir(parents=True, exist_ok=True)
    (root / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n", encoding="utf-8")
    return root


def test_ordinary_project_still_resolves(tmp_path: Path) -> None:
    proj = _make_project(tmp_path / "proj")
    (proj / "src").mkdir()
    assert find_project_root(proj / "src") == proj


def test_no_project_returns_none(tmp_path: Path) -> None:
    assert find_project_root(tmp_path) is None


def test_nested_project_is_refused(tmp_path: Path) -> None:
    """The agent-plantable shape: a project inside a project."""
    outer = _make_project(tmp_path / "outer")
    inner = _make_project(outer / "agent_made_this")
    with pytest.raises(Refused) as exc:
        find_project_root(inner)
    msg = str(exc.value)
    assert "NESTED" in msg
    assert str(inner) in msg and str(outer) in msg
    assert "agent-authored" in msg          # says WHY it matters
    assert "explicit project path" in msg   # says what to do


def test_refusal_fires_from_a_subdir_of_the_nested_project(tmp_path: Path) -> None:
    outer = _make_project(tmp_path / "outer")
    inner = _make_project(outer / "sub" / "planted")
    (inner / "deep").mkdir()
    with pytest.raises(Refused):
        find_project_root(inner / "deep")
