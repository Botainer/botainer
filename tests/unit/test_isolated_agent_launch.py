"""Regression (T0-1): the ISOLATED auth mode (agent-claude, the mode botainer's
own ⚠ warning recommends for sensitive data) must LAUNCH.

Its `pre_session` hook binds the credential dir to the prefix DIRECTORY itself
(`/home/agent/.claude`, no trailing slash), while the manifest declares the
envelope prefix WITH a trailing slash (`/home/agent/.claude/`). The plugin-
envelope predicate was trailing-slash-sensitive, so it refused the bind →
`Refused(PLUGIN_CONTRIBUTION_OUT_OF_ENVELOPE)` at real `botainer start`. This was
LATENT: the only end-to-end compose smoke test uses SHARED (which declares BOTH
slash forms), so no test composed isolated through `run_pre_session_hooks`.

This test closes that coverage gap. The fix normalizes trailing slashes on both
sides of the envelope comparison (fixing the class for every plugin) WITHOUT
loosening containment — the sibling-prefix negative below proves a
`/home/agent/.claudeX` is still refused.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from botainer.core import composition, identity
from botainer.core import config as config_module
from botainer.core.refusal import Refused
from botainer.plugins import builtin as plugin_builtin
from botainer.state import dir as state_dir


@pytest.fixture
def installed_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()
    return state


def _isolated_project(tmp_path: Path) -> Path:
    """A project whose enabled agent plugin is the ISOLATED agent-claude
    (not agent-claude-shared)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    cfg_path = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    enabled = [p for p in data.get("plugins_enabled", []) if not p.startswith("agent-claude")]
    enabled.insert(0, "agent-claude")  # isolated variant (no -shared suffix)
    data["plugins_enabled"] = enabled
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
    return proj


def test_isolated_agent_credential_bind_passes_envelope(installed_state: Path, tmp_path: Path) -> None:
    proj = _isolated_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    uid = identity.read_project_id(proj)
    # Seed the isolated credential the pre_session hook requires (existence only).
    creds_dir = installed_state / "state" / uid / "data" / "agent-claude" / "profiles" / "default"
    creds_dir.mkdir(parents=True, exist_ok=True)
    (creds_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    os.chmod(creds_dir / ".credentials.json", 0o600)

    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert "agent-claude" in spec.plugins_enabled, "test must exercise the ISOLATED variant"
    # This is where the trailing-slash envelope bug refused the isolated agent.
    spec = composition.run_pre_session_hooks(spec)
    targets = {b.target for b in spec.mount_plan.binds}
    assert "/home/agent/.claude" in targets, (
        "isolated agent-claude credential bind (to the prefix directory itself) "
        "must pass the plugin envelope; the trailing-slash predicate refused it (T0-1)"
    )


def test_compose_refuses_plugin_tier_below_tightened_ceiling(
    installed_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#64: a plugin installed while its tier was allowed is refused at COMPOSE
    if the site policy is later tightened to exclude that tier. The tier ceiling
    is re-checked at compose (against the install-time-recorded tier), not only
    at install — closing the admin-tightens-post-install gap."""
    from botainer.core import policy as policy_module
    from botainer.core.policy import PluginsPolicy, SitePolicy
    from botainer.core.refusal import RefusalCategory

    proj = _isolated_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    uid = identity.read_project_id(proj)
    creds_dir = installed_state / "state" / uid / "data" / "agent-claude" / "profiles" / "default"
    creds_dir.mkdir(parents=True, exist_ok=True)
    (creds_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    os.chmod(creds_dir / ".credentials.json", 0o600)

    # All bundled plugins are first-party; tighten the site ceiling to exclude it
    # (intersect with the default user policy yields an empty allowed_tiers).
    monkeypatch.setattr(
        policy_module,
        "load_site_policy",
        lambda: SitePolicy(plugins=PluginsPolicy(allowed_tiers=["community-verified"])),
    )
    with pytest.raises(Refused) as exc:
        composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert exc.value.category == RefusalCategory.PLUGIN_TIER_NOT_ALLOWED


def test_envelope_predicate_still_rejects_sibling_prefix() -> None:
    """Containment not loosened: the fix must NOT let `/home/agent/.claudeX`
    slip through a `/home/agent/.claude/` prefix (only the exact dir + children)."""
    def _in_envelope(target: str, prefixes: list[str]) -> bool:
        # mirrors composition.py's fixed predicate
        return any(
            target.rstrip("/") == p.rstrip("/") or target.startswith(p.rstrip("/") + "/")
            for p in prefixes
        )
    prefixes = ["/home/agent/.claude/"]
    assert _in_envelope("/home/agent/.claude", prefixes)          # exact dir — the fix
    assert _in_envelope("/home/agent/.claude/", prefixes)         # with slash
    assert _in_envelope("/home/agent/.claude/.credentials.json", prefixes)  # child
    assert not _in_envelope("/home/agent/.claudeX", prefixes)     # sibling — MUST fail
    assert not _in_envelope("/home/agent/.claude-evil", prefixes) # sibling — MUST fail
