"""Tests for plugin install from file:// path + declarative-only enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.plugins import install as install_module
from botainer.plugins import provenance
from botainer.plugins.install import PluginInstallError, install
from botainer.state import dir as state_dir


def _allow_third_party_site(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUDIT (MEDIUM): install now uses intersect(site, user), so a
    third-party install requires the SITE ceiling to permit third-party (the
    default site allowed_tiers is ['first-party'] — a user policy alone can't
    widen above it, by design). Set a permissive site policy."""
    from botainer.core.policy import PluginsPolicy, SitePolicy
    monkeypatch.setattr(
        install_module, "load_site_policy",
        lambda: SitePolicy(plugins=PluginsPolicy(
            allowed_tiers=["first-party", "third-party"],
            third_party_must_be_declarative=True,
        )),
    )


def _make_third_party_plugin(
    root: Path, *, name: str = "tp-plugin", with_hooks: bool = False
) -> Path:
    pdir = root / name
    pdir.mkdir(parents=True)
    hook_section = ""
    if with_hooks:
        hooks_dir = pdir / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "pre_session.py").write_text("#!/usr/bin/env python3\nprint('{}')\n")
        (hooks_dir / "pre_session.py").chmod(0o755)
        hook_section = (
            "hooks:\n"
            "  - when: pre_session\n"
            "    script: hooks/pre_session.py\n"
            "    timeout_seconds: 10\n"
        )
    (pdir / "botainer-plugin.yaml").write_text(
        "apiVersion: botainer-plugin-v1\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "tier: third-party\n"
        "trust_required: declarative\n" + hook_section
    )
    return pdir


def test_install_third_party_declarative_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    _allow_third_party_site(monkeypatch)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    state_dir.write_default_policy(
        paths.root,
        allow_tiers=["first-party", "third-party"],
        force=True,
    )
    src = _make_third_party_plugin(tmp_path / "src", with_hooks=False)
    info = install(f"file://{src}", consent=False)
    assert info.name == "tp-plugin"
    assert info.tree_sha
    lock = provenance.read_lock(paths.installed_lock_path)
    assert any(e.name == "tp-plugin" for e in lock)


def test_install_third_party_hooked_without_consent_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    _allow_third_party_site(monkeypatch)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    state_dir.write_default_policy(
        paths.root,
        allow_tiers=["first-party", "third-party"],
        force=True,
    )
    src = _make_third_party_plugin(tmp_path / "src", with_hooks=True)
    with pytest.raises(PluginInstallError) as exc:
        install(f"file://{src}", consent=False)
    assert "declarative" in str(exc.value).lower() or "consent" in str(exc.value).lower()


def test_install_third_party_hooked_with_consent_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    _allow_third_party_site(monkeypatch)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    state_dir.write_default_policy(
        paths.root,
        allow_tiers=["first-party", "third-party"],
        force=True,
    )
    src = _make_third_party_plugin(tmp_path / "src", with_hooks=True)
    info = install(f"file://{src}", consent=True)
    assert info.name == "tp-plugin"


def test_install_self_declared_higher_tier_cannot_skip_consent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AUDIT (MEDIUM): tier is launcher-determined, NOT
    manifest-declared. A hooked plugin from a non-first-party source that
    SELF-DECLARES a non-third-party tier (e.g. community-verified) must still be
    treated as third-party → the declarative-consent gate fires. Before the fix
    it gated on manifest.tier == "third-party", so the self-declared tier
    skipped consent and the hooked plugin installed silently."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    # Permissive site: allow community-verified + third-party so the tier check
    # itself passes and we isolate the consent-gate behavior.
    from botainer.core.policy import PluginsPolicy, SitePolicy
    monkeypatch.setattr(
        install_module, "load_site_policy",
        lambda: SitePolicy(plugins=PluginsPolicy(
            allowed_tiers=["first-party", "community-verified", "third-party"],
            third_party_must_be_declarative=True,
        )),
    )
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    state_dir.write_default_policy(
        paths.root,
        allow_tiers=["first-party", "community-verified", "third-party"],
        force=True,
    )
    pdir = tmp_path / "src" / "cv-plugin"
    (pdir / "hooks").mkdir(parents=True)
    (pdir / "hooks" / "pre_session.py").write_text("#!/usr/bin/env python3\nprint('{}')\n")
    (pdir / "hooks" / "pre_session.py").chmod(0o755)
    (pdir / "botainer-plugin.yaml").write_text(
        "apiVersion: botainer-plugin-v1\nname: cv-plugin\nversion: 0.1.0\n"
        "tier: community-verified\ntrust_required: declarative\n"
        "hooks:\n  - when: pre_session\n    script: hooks/pre_session.py\n"
        "    timeout_seconds: 10\n"
    )
    with pytest.raises(PluginInstallError) as exc:
        install(f"file://{pdir}", consent=False)  # no consent
    assert "declarative" in str(exc.value).lower() or "consent" in str(exc.value).lower()


def test_install_reserved_name_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    state_dir.write_default_policy(
        paths.root,
        allow_tiers=["first-party", "third-party"],
        force=True,
    )
    src = tmp_path / "src" / "agent-claude"
    src.mkdir(parents=True)
    # Mark this as NOT-first-party by giving it a real third-party-looking path.
    (src / "botainer-plugin.yaml").write_text(
        "apiVersion: botainer-plugin-v1\n"
        "name: agent-claude\n"
        "version: 0.0.1\n"
        "tier: third-party\n"
        "trust_required: declarative\n"
    )
    with pytest.raises(PluginInstallError) as exc:
        install(f"file://{src}", consent=False)
    assert "reserved" in str(exc.value).lower()


def test_tree_sha_is_deterministic(tmp_path: Path) -> None:
    src = _make_third_party_plugin(tmp_path / "src")
    sha1 = provenance.compute_tree_sha(src)
    sha2 = provenance.compute_tree_sha(src)
    assert sha1 == sha2
    # Modify a file → sha changes.
    (src / "botainer-plugin.yaml").write_text(
        (src / "botainer-plugin.yaml").read_text() + "# extra\n"
    )
    sha3 = provenance.compute_tree_sha(src)
    assert sha3 != sha1
