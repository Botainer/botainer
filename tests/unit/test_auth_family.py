"""Tests for auth-family plugin model.

Per AUTH-PRODUCT-PLAN.md §1: each agent plugin declares auth_family +
auth_mode + mutually_exclusive_with. Launcher enforces single-mode-
per-family at compose time. Subcommand `botainer auth use <mode>`
flips by enable/disable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.plugins.manifest import PluginManifest


# Derived from __file__, never hardcoded: /workspace is this dev
# container's checkout path. On CI (and any contributor's machine) the
# repo lives elsewhere, and the absolute form failed there — 5 tests.
_PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


def test_manifest_auth_family_defaults_empty() -> None:
    m = PluginManifest(name="x", version="0.1.0")
    assert m.auth_family == ""
    assert m.auth_mode == ""


def test_manifest_auth_mode_accepts_known_values() -> None:
    for mode in ("isolated", "shared", "proxy"):
        m = PluginManifest(name="x", version="0.1.0",
                           auth_family="anthropic", auth_mode=mode)
        assert m.auth_mode == mode


def test_manifest_auth_mode_refuses_unknown() -> None:
    with pytest.raises(Exception, match="auth_mode"):
        PluginManifest(name="x", version="0.1.0",
                       auth_family="anthropic", auth_mode="bogus")


def test_manifest_mutually_exclusive_with_defaults_empty() -> None:
    m = PluginManifest(name="x", version="0.1.0")
    assert m.mutually_exclusive_with == []


def test_bundled_agent_claude_declares_family() -> None:
    """The bundled agent-claude plugin must declare auth_family=anthropic
    + auth_mode=isolated + mutually_exclusive_with the two siblings."""
    from botainer.plugins.manifest import load_manifest
    m = load_manifest(_PLUGINS / "agent-claude")
    assert m.auth_family == "anthropic"
    assert m.auth_mode == "isolated"
    assert "agent-claude-shared" in m.mutually_exclusive_with
    assert "agent-claude-proxy" in m.mutually_exclusive_with


def test_bundled_agent_claude_shared_declares_family() -> None:
    from botainer.plugins.manifest import load_manifest
    m = load_manifest(_PLUGINS / "agent-claude-shared")
    assert m.auth_family == "anthropic"
    assert m.auth_mode == "shared"
    assert "agent-claude" in m.mutually_exclusive_with
    assert "agent-claude-proxy" in m.mutually_exclusive_with


def test_bundled_agent_claude_proxy_declares_family() -> None:
    from botainer.plugins.manifest import load_manifest
    m = load_manifest(_PLUGINS / "agent-claude-proxy")
    assert m.auth_family == "anthropic"
    assert m.auth_mode == "proxy"
    assert "agent-claude" in m.mutually_exclusive_with
    assert "agent-claude-shared" in m.mutually_exclusive_with


def test_bundled_agent_codex_declares_family() -> None:
    from botainer.plugins.manifest import load_manifest
    m = load_manifest(_PLUGINS / "agent-codex")
    assert m.auth_family == "openai"
    assert m.auth_mode == "isolated"


def test_auth_discover_families_finds_anthropic_modes() -> None:
    """`botainer auth status` discovers installed plugin variants by family.

    All three anthropic variants are bundled, so we should see isolated,
    shared, and proxy for anthropic.
    """
    from botainer.cli.auth import _discover_families
    families = _discover_families()
    assert "anthropic" in families
    anthropic = families["anthropic"]
    assert "isolated" in anthropic, anthropic
    assert "shared" in anthropic, anthropic
    assert "proxy" in anthropic, anthropic
    assert anthropic["isolated"] == "agent-claude"
    assert anthropic["shared"] == "agent-claude-shared"
    assert anthropic["proxy"] == "agent-claude-proxy"


def test_auth_discover_families_finds_openai() -> None:
    from botainer.cli.auth import _discover_families
    families = _discover_families()
    assert "openai" in families
    assert "isolated" in families["openai"]
    assert families["openai"]["isolated"] == "agent-codex"
