"""Tests for bundled-plugin discovery and installation (Phase 0)."""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.plugins import builtin, provenance
from botainer.state import dir as state_dir


def test_discover_finds_bundled_plugins() -> None:
    """In dev / editable install, plugins are under <repo-root>/plugins/."""
    paths = builtin.discover_builtin_plugins()
    assert paths, "expected to find bundled plugins"
    names = {p.name for p in paths}
    assert "agent-claude" in names
    assert "git" in names


def test_find_root_returns_path() -> None:
    root = builtin.find_builtin_plugins_root()
    assert root is not None
    assert root.exists()


def test_install_bundled_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    sources = builtin.discover_builtin_plugins()
    if not sources:
        pytest.skip("no bundled plugins available")
    src = next(p for p in sources if p.name == "agent-claude")
    entry1 = builtin.install_bundled(src)
    entry2 = builtin.install_bundled(src)
    # Tree hash should be deterministic
    assert entry1.tree_sha == entry2.tree_sha
    assert entry1.name == "agent-claude"
    assert entry1.tier == "first-party"
    # Algorithm-prefixed hash per prior-art review
    assert entry1.tree_sha.startswith("sha256:")


def test_install_all_builtin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    entries = builtin.install_all_builtin()
    assert entries, "expected to install at least one bundled plugin"
    names = {e.name for e in entries}
    assert "agent-claude" in names
    # All entries should be first-party with algo-prefixed hash
    for e in entries:
        assert e.tier == "first-party"
        assert e.tree_sha.startswith("sha256:")


def test_install_records_in_installed_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    sources = builtin.discover_builtin_plugins()
    if not sources:
        pytest.skip("no bundled plugins available")
    src = next(p for p in sources if p.name == "agent-claude")
    builtin.install_bundled(src)
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    locks = provenance.read_lock(paths.installed_lock_path)
    assert any(e.name == "agent-claude" for e in locks)
