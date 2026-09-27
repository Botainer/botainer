"""Exactly ONE agent plugin is activated per session — the selected one.

`botainer auth use <mode>` switches EVERY installed family by design, so
plugins_enabled became [agent-claude-shared, agent-codex-shared]. Composition
honoured both, producing two failures from one cause:

1. SECURITY — the OpenAI credential was bound RW into a session running CLAUDE,
   with CODEX_HOME advertising where to find it. A prompt-injected Claude could
   read and exfiltrate a credential for an agent that was not even running.
   Each plugin was individually correct; nothing checked that only one should be
   active.

2. BROKEN LAUNCH — two entrypoint wraps stacked. Claude's wrap ends in
   `exec claude "$@"`, so codex's entrypoint became Claude's first positional
   (displayed to the user as a PROMPT) and codex's flags became Claude's flags.
   Dead in 1.2s.

The fix drops non-selected agent plugins BEFORE the contribution loop, so binds,
env, hooks and wraps are all consistent with one agent — rather than guarding
the wrap list, which would have fixed the crash and left the leak.

Listing several agent plugins in plugins_enabled stays legal: that is what lets
`--agent` pick either without editing config. The list is a menu; one is
activated.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from botainer.core.composition import compose_session

_CREDS = {
    "agent-claude": (".credentials.json",
                     {"claudeAiOauth": {"accessToken": "a" * 40,
                                        "refreshToken": "r" * 40,
                                        "expiresAt": 9_999_999_999_000}}),
    "agent-codex": ("auth.json",
                    {"tokens": {"access_token": "a" * 40,
                                "refresh_token": "r" * 40,
                                "account_id": "acct_1"}}),
}


@pytest.fixture()
def both_enabled(tmp_path, monkeypatch):
    """A project exactly as `botainer auth use shared` leaves it."""
    state = tmp_path / "bot"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    monkeypatch.setenv("BOTAINER_STATE_ROOT", str(state))
    for agent, (fname, payload) in _CREDS.items():
        d = state / "shared-auth" / agent
        d.mkdir(parents=True)
        f = d / fname
        f.write_text(json.dumps(payload))
        f.chmod(0o600)
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(str(uuid.uuid4()))
    return proj


def _write_cfg(proj: Path, agent: str, enabled: list[str]) -> None:
    (proj / ".botainer" / "config.yaml").write_text(
        "version: config-v1\n"
        f"agent: {agent}\n"
        "runtime: docker\n"
        "image: ubuntu:24.04\n"
        "network:\n  mode: internet\n"
        f"plugins_enabled: [{', '.join(enabled)}]\n")


BOTH = ["agent-claude-shared", "agent-codex-shared"]


def test_the_other_agents_credential_is_NOT_bound(both_enabled) -> None:
    """THE security regression.

    A Claude session must not carry the OpenAI credential, and vice versa.
    """
    _write_cfg(both_enabled, "claude", BOTH)
    spec = compose_session(both_enabled, runtime_choice="docker",
                           identity_accept=True)
    leaked = [b.target for b in spec.mount_plan.binds
              if "codex" in b.target or "openai" in b.target.lower()]
    assert leaked == [], (
        f"a CLAUDE session bound the codex credential at {leaked}. A "
        f"prompt-injected agent could exfiltrate a credential for an agent "
        f"that is not even running.")


def test_the_other_agents_config_dir_is_NOT_announced(both_enabled) -> None:
    """Binding is one half; telling the agent where to look is the other.

    CODEX_HOME in a Claude session points at the OpenAI credential.
    """
    _write_cfg(both_enabled, "claude", BOTH)
    spec = compose_session(both_enabled, runtime_choice="docker",
                           identity_accept=True)
    assert "CODEX_HOME" not in spec.env.values, (
        f"CODEX_HOME set in a claude session: {spec.env.values.get('CODEX_HOME')!r}")


def test_exactly_one_agent_entrypoint_wrap(both_enabled) -> None:
    """Two stacked wraps make the inner one an ARGUMENT to the outer agent.

    That is what produced `error: unknown option '--sandbox'` and showed the
    codex entrypoint path to the user as a prompt.
    """
    _write_cfg(both_enabled, "claude", BOTH)
    spec = compose_session(both_enabled, runtime_choice="docker",
                           identity_accept=True)
    agent_wraps = [w for w in spec.entrypoint_wraps
                   if any("agent-" in str(part) and "entrypoint" in str(part)
                          for part in w)]
    assert len(agent_wraps) == 1, (
        f"expected exactly one agent entrypoint wrap, got {agent_wraps}. The "
        f"outer agent receives the inner one's path as its first positional "
        f"argument, and the inner one's flags as its own.")
    assert any("claude" in str(part) for part in agent_wraps[0]), (
        f"the surviving wrap is not the selected agent's: {agent_wraps[0]}")


def test_it_works_the_other_way_round_too(both_enabled) -> None:
    """Symmetry matters: a codex session must not carry the Claude credential."""
    _write_cfg(both_enabled, "codex", BOTH)
    spec = compose_session(both_enabled, runtime_choice="docker",
                           identity_accept=True)
    leaked = [b.target for b in spec.mount_plan.binds
              if "claude" in b.target and "agent-claude" not in b.target]
    assert "CLAUDE_CONFIG_DIR" not in spec.env.values, (
        "CLAUDE_CONFIG_DIR set in a codex session")
    assert leaked == [], f"codex session bound claude paths: {leaked}"


def test_a_single_agent_project_is_unaffected(both_enabled) -> None:
    """The common case must not change: one agent plugin, everything kept."""
    _write_cfg(both_enabled, "claude", ["agent-claude-shared"])
    spec = compose_session(both_enabled, runtime_choice="docker",
                           identity_accept=True)
    assert "agent-claude-shared" in spec.plugins_enabled
    agent_wraps = [w for w in spec.entrypoint_wraps
                   if any("entrypoint" in str(p) for p in w)]
    assert len(agent_wraps) == 1


def test_non_agent_plugins_are_never_dropped(both_enabled) -> None:
    """The filter must target AUTH-SLOT plugins only. Dropping git, nudge or
    browser because they sat next to an agent plugin would silently remove
    capabilities the user configured."""
    _write_cfg(both_enabled, "claude", BOTH + ["git"])
    spec = compose_session(both_enabled, runtime_choice="docker",
                           identity_accept=True)
    assert "git" in spec.plugins_enabled
    assert "agent-codex-shared" not in spec.plugins_enabled
