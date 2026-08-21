"""End-to-end compose_session for each auth mode (isolated/shared/proxy).

Pre-real-host sanity sweep: after editing the bundled plugin manifests
(strict envelope, OAuth READ, mount_target_prefixes additions), make
sure compose_session succeeds for each mode and produces the expected
binds. This is a smoke layer between the unit tests (which mock plugin
contributions) and real-host testing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import composition, identity
from botainer.core import config as config_module
from botainer.plugins import builtin as plugin_builtin
from botainer.state import dir as state_dir
from tests.conftest import append_image_to_config


@pytest.fixture
def installed_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """A state-dir with all bundled plugins installed.

    Mimics what `botainer setup` does for a fresh host.
    """
    state = tmp_path / "state"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()
    return state


def _make_project(tmp_path: Path, plugin_name: str) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    append_image_to_config(proj)
    # Rewrite plugins_enabled to the requested variant so we exercise
    # that mode's pre_session contribution.
    cfg_path = proj / ".botainer" / "config.yaml"
    import yaml
    data = yaml.safe_load(cfg_path.read_text())
    # Replace any agent-claude* entries with the requested one.
    enabled = [
        p for p in data.get("plugins_enabled", [])
        if not p.startswith("agent-claude")
    ]
    enabled.insert(0, plugin_name)
    data["plugins_enabled"] = enabled
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
    return proj


def test_compose_isolated_mode_with_mock(
    installed_state: Path, tmp_path: Path
) -> None:
    """Mode A (isolated): agent-claude bound at /home/agent/.claude."""
    proj = _make_project(tmp_path, "agent-claude")
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=False
    )
    # Composition should succeed and include the project mounts.
    targets = {b.target for b in spec.mount_plan.binds}
    assert "/packages" in targets
    assert "/scratch" in targets
    # The agent-claude plugin's pre_session hook would create the
    # /home/agent/.claude bind, but the hook only runs when start
    # invokes it. Compose itself doesn't trigger the bind; this test
    # just confirms compose doesn't refuse with the envelope check.


def test_compose_shared_mode_envelope_passes(
    installed_state: Path, tmp_path: Path
) -> None:
    """Mode B (shared): the agent-claude-shared manifest must declare
    BOTH /home/agent/.claude AND /shared-auth/agent-claude so the
    selective-bind contributions from hooks/pre_session.py pass the
    strict envelope check."""
    from botainer.plugins.lifecycle import list_installed
    from botainer.plugins.manifest import load_manifest
    inst = next(
        i for i in list_installed() if i.name == "agent-claude-shared"
    )
    m = load_manifest(inst.plugin_dir)
    prefixes = m.contributes.mount_target_prefixes
    # The hook's two bind targets, each must be coverable by SOME prefix.
    for target in ("/home/agent/.claude", "/shared-auth/agent-claude"):
        assert any(
            target == p or target.startswith(p.rstrip("/") + "/")
            for p in prefixes
        ), f"target {target!r} not covered by envelope {prefixes}"


def test_compose_proxy_mode_envelope_passes(
    installed_state: Path, tmp_path: Path
) -> None:
    """Mode C (proxy): the agent-claude-proxy manifest must declare
    the /run/anthropic-proxy.sock target the start_proxy.py hook
    contributes."""
    from botainer.plugins.lifecycle import list_installed
    from botainer.plugins.manifest import load_manifest
    inst = next(
        i for i in list_installed() if i.name == "agent-claude-proxy"
    )
    m = load_manifest(inst.plugin_dir)
    prefixes = m.contributes.mount_target_prefixes
    target = "/run/anthropic-proxy.sock"
    assert any(
        target == p or target.startswith(p.rstrip("/") + "/")
        for p in prefixes
    ), f"target {target!r} not covered by envelope {prefixes}"


def test_compose_codex_shared_envelope_passes(installed_state: Path) -> None:
    """Mode B (shared) for codex: same selective-bind story."""
    from botainer.plugins.lifecycle import list_installed
    from botainer.plugins.manifest import load_manifest
    inst = next(
        i for i in list_installed() if i.name == "agent-codex-shared"
    )
    m = load_manifest(inst.plugin_dir)
    prefixes = m.contributes.mount_target_prefixes
    for target in ("/home/agent/.codex", "/shared-auth/agent-codex"):
        assert any(
            target == p or target.startswith(p.rstrip("/") + "/")
            for p in prefixes
        ), f"target {target!r} not covered by envelope {prefixes}"


def test_compose_refuses_two_anthropic_variants(
    installed_state: Path, tmp_path: Path
) -> None:
    """Family-singleton enforcement (composition.py family_to_enabled
    check): enabling both agent-claude and agent-claude-shared refuses
    at compose time with a clear pointer to `botainer auth use`."""
    proj = _make_project(tmp_path, "agent-claude")
    cfg_path = proj / ".botainer" / "config.yaml"
    import yaml
    data = yaml.safe_load(cfg_path.read_text())
    data["plugins_enabled"] = ["agent-claude", "agent-claude-shared", "git"]
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    from botainer.core.refusal import Refused
    with pytest.raises(Refused, match="auth.*family|mutually exclusive"):
        composition.compose_session(
            proj, runtime_choice="mock", identity_accept=False
        )
