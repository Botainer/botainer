"""Regression test: every agent plugin renders a non-empty apptainer
command after the image.

real-host failure on Grace: `botainer-v0_1 start` with
agent-claude-shared blew up with `apptainer exec: requires at least 2
arg(s), only received 1`. Root cause: `agent-claude-shared` (and
`-proxy`) are mutually-exclusive with `agent-claude`, so when shared
or proxy mode is on, agent-claude is NOT instantiated and its
`entrypoint_wrap` declaration is NOT contributed to the composed
spec. On docker this works silently because the image's ENTRYPOINT
directive runs; on apptainer, `exec` requires an explicit command.

This test pins: every agent-* plugin contributes an entrypoint_wrap
(or otherwise produces a non-empty command after the image), so
neither adapter renders `apptainer exec <image>` with no command.

Same enforcement shape as
tests/integration/test_capability_surface_matches_inventory.py and
the umbrella-bind regression test: "principle in prose, nothing in
tests" reliably ships regressions; this exists so the prose doesn't
diverge from reality silently.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.adapters.apptainer import ApptainerAdapter
from botainer.core import composition, identity
from botainer.core import config as config_module
from botainer.plugins import builtin as plugin_builtin
from botainer.state import dir as state_dir
from tests.conftest import append_image_to_config

# Every agent plugin that can legally be `plugins_enabled[0]` on a
# fresh project. The bug appeared with shared/proxy variants (which
# are mutex with the base plugin); the base plugins are also included
# so the regression test exercises both code paths.
AGENT_PLUGINS = [
    ("agent-claude", "claude"),
    ("agent-claude-shared", "claude"),
    ("agent-claude-proxy", "claude"),
    ("agent-codex", "codex"),
    ("agent-codex-shared", "codex"),
]


@pytest.fixture
def installed_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()
    return state


def _make_project(tmp_path: Path, plugin_name: str, agent_short: str) -> Path:
    proj = tmp_path / f"proj_{plugin_name}"
    proj.mkdir()
    config_module.write_initial_config(proj, agent=agent_short, force=False)
    append_image_to_config(proj)
    cfg_path = proj / ".botainer" / "config.yaml"
    import yaml
    data = yaml.safe_load(cfg_path.read_text())
    enabled = [
        p for p in data.get("plugins_enabled", [])
        if not p.startswith("agent-")
    ]
    enabled.insert(0, plugin_name)
    data["plugins_enabled"] = enabled
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
    identity.init_project(proj, agent=agent_short, force=True, non_interactive=True)
    return proj


def _ensure_apptainer_sif_for(state_root: Path, agent_name: str) -> Path:
    """Create a placeholder .sif file for apptainer compose to find.
    The compose path only checks existence + returns the absolute path;
    apptainer is never actually invoked in these tests."""
    images = state_root / "images"
    images.mkdir(parents=True, exist_ok=True)
    sif = images / f"botainer-{agent_name}.sif"
    sif.touch()
    return sif


@pytest.mark.parametrize("plugin_name,agent_short", AGENT_PLUGINS)
def test_agent_plugin_produces_non_empty_apptainer_command(
    installed_state: Path,
    tmp_path: Path,
    plugin_name: str,
    agent_short: str,
) -> None:
    """The composed spec must produce a non-empty command after the
    image — either an entrypoint_wrap, an entrypoint, or a command.
    Without any of these, `apptainer exec <image>` fails with
    "requires at least 2 arg(s)" — the Grace bug."""
    proj = _make_project(tmp_path, plugin_name, agent_short)
    # Apptainer compose looks up the .sif on disk — create a placeholder
    # so resolution succeeds (we don't actually launch apptainer here).
    base_agent_name = f"agent-{agent_short}"
    _ensure_apptainer_sif_for(installed_state, base_agent_name)

    spec = composition.compose_session(
        proj, runtime_choice="apptainer", identity_accept=False
    )
    # At least one of entrypoint_wraps, entrypoint, command must be
    # non-empty so apptainer exec gets a command to run.
    assert spec.entrypoint_wraps or spec.entrypoint or spec.command, (
        f"Plugin {plugin_name!r} produced an empty command. "
        f"apptainer exec <image> with no command fails with "
        f"'requires at least 2 arg(s), only received 1'. "
        f"This is the 2026-05-19 Grace bug. Fix: declare an "
        f"entrypoint_wrap in plugins/{plugin_name}/botainer-plugin.yaml."
    )


@pytest.mark.parametrize("plugin_name,agent_short", AGENT_PLUGINS)
def test_apptainer_adapter_renders_command_after_image(
    installed_state: Path,
    tmp_path: Path,
    plugin_name: str,
    agent_short: str,
) -> None:
    """Render the spec through the apptainer adapter and assert the
    argv has at least one element AFTER spec.image. This is the
    invariant the runtime actually depends on."""
    proj = _make_project(tmp_path, plugin_name, agent_short)
    base_agent_name = f"agent-{agent_short}"
    _ensure_apptainer_sif_for(installed_state, base_agent_name)
    spec = composition.compose_session(
        proj, runtime_choice="apptainer", identity_accept=False
    )

    adapter = ApptainerAdapter()
    argv = adapter.render_argv(spec)

    # Find the image position
    image_idx = argv.index(spec.image)
    after_image = argv[image_idx + 1:]
    assert after_image, (
        f"Apptainer adapter rendered {argv!r} for plugin {plugin_name!r}. "
        f"There must be at least one argv element after the image "
        f"({spec.image!r}); apptainer exec needs a command. The 2026-05-19 "
        f"Grace bug."
    )
