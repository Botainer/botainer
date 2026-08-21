"""S1 + S2 (security audit): plugin trust must come from HOW a plugin
was installed, never from what its own tree says about itself.

trust.py's own docstring states the rule: "Trust comes from a launcher-shipped
allowlist, not a self-declared manifest field." These pin it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.plugins import install as install_mod
from botainer.plugins import lifecycle


def _plugin_tree(root: Path, name: str, tier: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "botainer-plugin.yaml").write_text(
        "apiVersion: botainer-plugin-v1\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "kind: tool\n"
        "trust_required: declarative\n"
        f"tier: {tier}\n",
        encoding="utf-8",
    )
    return d


# ── S1: a URL-shaped source must never become a local path ──

def test_url_shaped_source_is_refused_not_path_coerced(tmp_path: Path, monkeypatch) -> None:
    """`Path("https://github.com/botainer/x")` is the RELATIVE path
    `https:/github.com/botainer/x`. A directory of that shape — which the caged
    agent can create inside /workspace — used to install AS IF it were the URL,
    which granted first-party tier and a reserved plugin name."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    spoof = tmp_path / "https:" / "github.com" / "botainer" / "evil"
    _plugin_tree(spoof.parent, "evil", "first-party")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(install_mod.PluginInstallError) as exc:
        install_mod.install("https://github.com/botainer/evil", consent=True)
    assert "looks like a URL" in str(exc.value)


def test_install_from_source_can_never_grant_first_party() -> None:
    """First-party is a property of the install MECHANISM (install_bundled),
    not of any user-supplied string."""
    for candidate in (
        "https://github.com/botainer/agent-claude",
        "https://github.com/anthropics/botainer",
        "file:///tmp/whatever",
        "/home/user/plugins/agent-claude",
        "./plugins/agent-claude",
    ):
        assert install_mod._source_is_first_party(candidate) is False, candidate


# ── S2: a copied-in directory does not get to pick its own tier ──

def test_copied_in_plugin_does_not_inherit_its_self_declared_tier(
    tmp_path: Path, monkeypatch
) -> None:
    """`cp -r` a tree declaring `tier: first-party` into ~/.botainer/plugins/ and
    it used to be treated as first-party — bypassing the whole install gate
    stack. With no lock entry there is no evidence, so it must get the lowest
    tier (which the default ceiling allowed_tiers=["first-party"] then refuses).
    """
    state = tmp_path / "state"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    _plugin_tree(paths.plugins_dir, "sneaky", "first-party")

    # No editable overlay in this environment for a name that isn't in the clone.
    got = {p.name: p for p in lifecycle.list_installed()}
    assert "sneaky" in got, "the plugin should still be LISTED"
    assert got["sneaky"].tier == "third-party", (
        "a self-declared tier must not be honored without a lock entry")


def test_lock_entry_is_what_grants_the_tier(tmp_path: Path, monkeypatch) -> None:
    """The mechanism that DOES grant tier: an installed.lock entry, written by
    the launcher at install time."""
    state = tmp_path / "state"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    from botainer.plugins import provenance as prov
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    _plugin_tree(paths.plugins_dir, "legit", "first-party")
    prov.append_lock(paths.installed_lock_path, prov.ProvenanceEntry(
        name="legit", version="0.1.0", source="test", tree_sha="sha256:0",
        image_digest=None, installed_at="t", tier="first-party"))

    got = {p.name: p for p in lifecycle.list_installed()}
    assert got["legit"].tier == "first-party"
