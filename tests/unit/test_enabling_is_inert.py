"""Plugin listings distinguish installation from compose-time enablement.

The hpc-launcher plugin provides a helper without compose-time contributions,
so toggling plugins_enabled does not affect it. Inertness is derived from the
manifest's contributions and hooks; adding a hook changes that classification.
The listing and generated configuration must explain this distinction."""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.plugins.manifest import load_manifest

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


def _man(name: str):
    return load_manifest(PLUGINS / name)


def test_hpc_launcher_is_inert_when_enabled() -> None:
    """No hooks or contributions means enabling has no compose-time effect."""
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


def test_generated_hpc_template_does_not_tell_users_to_enable_it(tmp_path) -> None:
    """The template is where the confusion started.

    WRITES A REAL CONFIG AND READS IT, rather than grepping the generator's
    source. The old version pulled the `hpc_plugins_enabled = (` block out of
    `inspect.getsource(config_mod)` and searched the text — so a comment
    mentioning hpc-launcher anywhere in that block would have failed it, and a
    generator that built the same string a different way would have passed
    while emitting the wrong thing. What matters is the FILE a user opens.
    """
    from botainer.core.config import write_initial_config

    write_initial_config(tmp_path, agent="agent-claude", force=True,
                         runtime="apptainer")
    generated = (tmp_path / ".botainer" / "config.yaml").read_text(encoding="utf-8")

    enabled = []
    in_block = False
    for line in generated.splitlines():
        if line.startswith("plugins_enabled:"):
            in_block = True
            continue
        if in_block:
            if line.startswith("  - "):
                enabled.append(line[4:].split("#")[0].strip())
                continue
            if line.strip() and not line.startswith(" "):
                break

    assert "hpc-modules" in enabled, (
        f"hpc-modules DOES need enabling and the generated config omits it; "
        f"plugins_enabled = {enabled}")
    assert "hpc-launcher" not in enabled, (
        f"the generated config still tells users to enable hpc-launcher, which "
        f"contributes nothing to the container and does not need it; "
        f"plugins_enabled = {enabled}")


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
