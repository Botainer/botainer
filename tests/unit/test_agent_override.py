"""`botainer start --agent <name>` — enter the same project with either agent.

User,, after logging codex in successfully: "I start the project, I
get claude, not codex! and start --agent codex not recognized."

`agent:` lived only in .botainer/config.yaml, so using the other agent in a
project meant hand-editing config and editing it back. Meanwhile `start`
already carried one-shot overrides for the NEIGHBOURING concept (--auth-mode,
--auth-profile), which makes this an inconsistency rather than a decision.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from botainer.core.composition import compose_session


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "bot"))
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(str(uuid.uuid4()))
    (proj / ".botainer" / "config.yaml").write_text(
        "version: config-v1\nagent: claude\nruntime: docker\n"
        "image: ubuntu:24.04\nnetwork:\n  mode: internet\n"
        "plugins_enabled: [agent-claude-shared, git]\n")
    return proj


def _agents(spec) -> list[str]:
    return sorted(p for p in spec.plugins_enabled if p.startswith("agent-"))


def test_without_the_flag_the_config_agent_is_used(project) -> None:
    spec = compose_session(project, runtime_choice="docker", identity_accept=True)
    assert _agents(spec) == ["agent-claude-shared"]


def test_the_flag_swaps_the_agent(project) -> None:
    spec = compose_session(project, runtime_choice="docker",
                           identity_accept=True, agent_override="codex")
    assert _agents(spec) == ["agent-codex-shared"]


def test_the_auth_mode_survives_the_swap(project) -> None:
    """Someone in SHARED mode must land on agent-codex-shared, not the bare
    agent-codex. Dropping the suffix would silently move them to per-project
    credentials — a different security posture than the one they configured,
    arrived at by asking for a different agent."""
    spec = compose_session(project, runtime_choice="docker",
                           identity_accept=True, agent_override="codex")
    assert _agents(spec) == ["agent-codex-shared"]
    assert not any(p == "agent-codex" for p in spec.plugins_enabled)


def test_exactly_one_agent_plugin_remains_after_the_swap(project) -> None:
    """The old one must be REMOVED, not joined. Two enabled agent plugins
    compose into a broken chained entrypoint (#111) — so a swap that only
    appended would produce exactly that."""
    spec = compose_session(project, runtime_choice="docker",
                           identity_accept=True, agent_override="codex")
    assert len(_agents(spec)) == 1, _agents(spec)


def test_the_override_does_NOT_touch_config_on_disk(project) -> None:
    """THE load-bearing property.

    An earlier auth-mode implementation edited config.yaml and relied on atexit
    to restore it — which does not fire on SIGKILL/OOM/exec, so users could end
    up silently switched. A one-shot override that persists is worse than no
    override at all, because the NEXT launch would quietly use the other agent.
    """
    cfg = project / ".botainer" / "config.yaml"
    before = cfg.read_text()
    compose_session(project, runtime_choice="docker",
                    identity_accept=True, agent_override="codex")
    assert cfg.read_text() == before, "the one-shot override persisted to disk"
    # and the next launch must be back to the configured agent
    spec = compose_session(project, runtime_choice="docker", identity_accept=True)
    assert _agents(spec) == ["agent-claude-shared"]


def test_an_unknown_agent_refuses_and_lists_what_exists(project) -> None:
    """Refusing is only useful if it says what IS available — otherwise the
    user is sent to the source to find out."""
    from botainer.core.refusal import Refused
    with pytest.raises(Refused) as exc:
        compose_session(project, runtime_choice="docker",
                        identity_accept=True, agent_override="nope")
    msg = str(exc.value)
    assert "agent-nope" in msg
    assert "agent-claude" in msg, "did not list the installed agent plugins"


# --------------------------------------------------------------------------
# Broker mode. Found on Grace,, by running the real thing:
#
#   $ botainer auth use broker      -> enables agent-claude-broker AND
#                                       agent-codex-broker
#   $ botainer start --agent codex
#   refused: plugins 'agent-codex-broker' and 'agent-codex' are mutually
#            exclusive.
#
# The swap selected candidate plugins with `man.kind == "agent"`. But broker and
# proxy plugins are kind='host_helper' — they run a host-side process — and only
# the isolated/shared ones are kind='agent'. So the swap saw NO current agent,
# defaulted the mode to isolated, and appended bare agent-codex next to the
# agent-codex-broker already enabled.
#
# `kind` describes HOW a plugin is implemented; the swap needed to know WHICH
# AUTH SLOT it fills. Two of the four modes were silently invisible.
# --------------------------------------------------------------------------

import pytest

from botainer.core.composition import _apply_agent_override_in_memory


class _Cfg:
    """Minimal stand-in with the two fields the swap touches."""

    def __init__(self, agent, plugins_enabled):
        self.agent = agent
        self.plugins_enabled = plugins_enabled

    def model_copy(self, *, update):
        return _Cfg(update.get("agent", self.agent),
                    update.get("plugins_enabled", self.plugins_enabled))


@pytest.mark.parametrize("mode,suffix", [
    ("broker", "-broker"),
    ("shared", "-shared"),
    ("isolated", ""),
])
def test_the_auth_mode_survives_an_agent_swap(mode, suffix) -> None:
    """Switching AGENT must not switch AUTH MODE.

    The mode is a security and billing decision — broker never gives the
    container a refresh token, shared does — so silently landing a broker user
    on isolated (which is what happened on Grace) changes the trust model
    without asking.
    """
    enabled = [f"agent-claude{suffix}"] if suffix else ["agent-claude"]
    out = _apply_agent_override_in_memory(_Cfg("claude", enabled), "codex")
    assert out.plugins_enabled == [f"agent-codex{suffix}"], (
        f"{mode}: expected agent-codex{suffix}, got {out.plugins_enabled}")
    assert out.agent == "codex"


def test_broker_mode_with_BOTH_families_enabled_swaps_cleanly() -> None:
    """THE Grace regression, exactly as `auth use broker` leaves the config.

    `auth use broker` switches every installed family, so both broker plugins
    are enabled at once. Selecting codex must leave ONE codex plugin, not add a
    second one that collides with it.
    """
    cfg = _Cfg("claude", ["agent-claude-broker", "agent-codex-broker"])
    out = _apply_agent_override_in_memory(cfg, "codex")
    assert out.plugins_enabled == ["agent-codex-broker"], (
        f"got {out.plugins_enabled}; bare agent-codex here is the bug — it is "
        f"mutually exclusive with agent-codex-broker and start refuses")


def test_swapping_back_to_claude_in_broker_mode_also_works() -> None:
    cfg = _Cfg("codex", ["agent-claude-broker", "agent-codex-broker"])
    out = _apply_agent_override_in_memory(cfg, "claude")
    assert out.plugins_enabled == ["agent-claude-broker"]
    assert out.agent == "claude"
