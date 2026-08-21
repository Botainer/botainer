"""Some plugins are unaffected by `plugins_enabled`; say so instead of lying.

User report: they ran cluster jobs successfully WITHOUT hpc-launcher
in `plugins_enabled`, never realised it was supposedly needed, and could not
tell whether something was wrong. It wasn't — `plugins_enabled` selects which
plugins COMPOSE (composition.py:368 intersects it with the installed set, then
runs hooks and applies binds/env/sidecars). hpc-launcher declares none of those,
so enabling it does nothing. Meanwhile `botainer plugin list` said "installed,
not enabled" — which reads as a switch left off — and `botainer init` generated
a template telling users to enable it.

The predicate is DERIVED from what a plugin declares, not from its name or its
`kind:` string, so a plugin that later grows a hook stops being inert on its own
rather than needing this list updated.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.plugins.manifest import load_manifest

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


def _man(name: str):
    return load_manifest(PLUGINS / name)


def test_hpc_launcher_is_inert_when_enabled() -> None:
    """The reported case: no hooks, no contributions, so enabling is a no-op."""
    man = _man("hpc-launcher")
    assert man.enabling_is_inert(), (
        "hpc-launcher declares something that composes — if that is now true, "
        "the config template and `plugin list` must go back to telling users "
        "to enable it")


def test_a_plugin_with_a_hook_is_never_inert() -> None:
    """hpc-modules sits beside hpc-launcher in the generated template and has a
    host_pre_launch hook, so it genuinely must be enabled. The two must not be
    treated alike — that was the original template's mistake."""
    man = _man("hpc-modules")
    assert man.hooks, "guard: hpc-modules should have a hook"
    assert not man.enabling_is_inert()


@pytest.mark.parametrize("name", [
    "agent-claude", "agent-claude-shared", "git", "nudge",
    "web-ports", "browser", "wolfram-sidecar",
])
def test_contributing_plugins_are_not_inert(name: str) -> None:
    """Fail-safe direction. Wrongly calling a plugin inert would tell users not
    to enable something they need — worse than the bug being fixed."""
    assert not _man(name).enabling_is_inert(), name


def test_a_mount_envelope_alone_does_not_count_as_contributing() -> None:
    """`mount_target_prefixes` is PERMISSION to bind under a prefix, not a
    contribution. hpc-launcher declares exactly that and nothing else, which is
    why it must still read as inert."""
    man = _man("hpc-launcher")
    assert man.contributes.mount_target_prefixes, "guard: it declares an envelope"
    assert man.enabling_is_inert()


def test_generated_hpc_template_does_not_tell_users_to_enable_it() -> None:
    """The template is where the confusion started."""
    import inspect

    from botainer.core import config as config_mod
    src = inspect.getsource(config_mod)
    block = src[src.index("hpc_plugins_enabled = ("):]
    block = block[:block.index(")")]
    assert "hpc-modules" in block, "hpc-modules DOES need enabling"
    assert "hpc-launcher" not in block, (
        "the generated config still tells users to enable hpc-launcher, which "
        "does nothing")


@pytest.mark.parametrize("field,value", [
    ("entrypoint_wrap", {"layer": "inner", "command": ["/usr/local/bin/wrap"]}),
    ("mcp_servers", [{"name": "x", "command": "y"}]),
    ("agent_hints_section", "extra text the agent is told"),
    ("capability_summary_block", "extra text the user is shown"),
    ("user_tips", ["a tip"]),
    ("sidecars", [{"name": "s", "runtime": "container"}]),
])
def test_each_contribution_kind_alone_defeats_inertness(field, value) -> None:
    """Every clause of the predicate must be load-bearing on its own.

    No BUNDLED plugin exercises these in isolation — each one that declares,
    say, an entrypoint_wrap also has hooks — so deleting a clause changes no
    current outcome and looks like dead code. It is not: a FUTURE plugin
    declaring only that one thing would be wrongly reported as "no enabling
    needed", and the user would not enable something they need. That is the
    fail-DANGEROUS direction, so each clause gets a synthetic case proving it
    matters rather than being trimmed.
    """
    from botainer.plugins.manifest import PluginManifest

    base = dict(apiVersion="botainer-plugin-v1", name="synthetic", version="0.0.1")
    inert = PluginManifest(**base)
    assert inert.enabling_is_inert(), "guard: a bare manifest is inert"

    man = PluginManifest(**base, contributes={field: value})
    assert not man.enabling_is_inert(), (
        f"a plugin contributing only `{field}` was reported as needing no "
        f"enabling; the user would not enable it and would lose that behaviour")
