"""Tests for the bundled-plugin trust store (Phase 3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.plugins import builtin, trust


def test_generate_trust_lock_returns_sha256_prefixed() -> None:
    """All entries in trust lock are sha256:-prefixed (prior-art F)."""
    lock = trust.generate_trust_lock()
    assert lock  # bundled plugins should be discoverable
    for name, hash_str in lock.items():
        assert hash_str.startswith("sha256:"), f"{name}: hash should start with sha256:"
        # 64 hex chars after sha256:
        suffix = hash_str.split(":", 1)[1]
        assert len(suffix) == 64
        assert all(c in "0123456789abcdef" for c in suffix)


def test_write_read_lock_roundtrip(tmp_path: Path) -> None:
    lock_path = tmp_path / "trusted_plugins.lock"
    entries = {
        "agent-claude": "sha256:" + "a" * 64,
        "git": "sha256:" + "b" * 64,
    }
    trust.write_trust_lock(lock_path, entries)
    assert lock_path.exists()
    loaded = trust.read_trust_lock(lock_path)
    assert loaded == entries


def test_verify_plugin_match_returns_first_party(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bundled plugin whose tree matches the trust lock is first-party."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    sources = builtin.discover_builtin_plugins()
    if not sources:
        pytest.skip("no bundled plugins available")
    src = sources[0]
    # Install + verify in the same process; trust lock will be generated.
    builtin.install_bundled(src)
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    installed = paths.plugins_dir / src.name
    tier, _detail = trust.verify_plugin(src.name, installed)
    assert tier == "first-party"


def test_verify_plugin_unknown_name_is_untrusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    tier, _detail = trust.verify_plugin("never-heard-of-this", tmp_path)
    assert tier == "untrusted"


def test_verify_plugin_modified_is_user_modified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the tree hash differs from the lock entry, tier is user-modified."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    sources = builtin.discover_builtin_plugins()
    if not sources:
        pytest.skip("no bundled plugins available")
    src = sources[0]
    builtin.install_bundled(src)
    # Tamper with the installed tree.
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    installed = paths.plugins_dir / src.name
    (installed / "TAMPER.txt").write_text("user-modified")
    tier, detail = trust.verify_plugin(src.name, installed)
    assert tier == "user-modified"
    assert "differs" in detail
