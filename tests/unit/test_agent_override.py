"""`botainer start --agent <name>` — enter the same project with either agent.

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
# Broker-mode regression: an agent override must identify the auth slot,
# including modes implemented by host-helper plugins:
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


# --------------------------------------------------------------------------
# #220. A project with agent-claude-shared and agent-codex-broker must
# preserve the selected Codex broker mode when overriding the agent.
#
# WHY EVERY TEST ABOVE MISSED IT, because that is the more useful finding.
# Each one puts the two families in the SAME mode:
#
#   test_the_auth_mode_survives_an_agent_swap        one family enabled
#   test_broker_mode_with_BOTH_families_enabled...   BOTH broker
#   test_swapping_back_to_claude_in_broker_mode...   BOTH broker
#
# The swap inferred the mode with a last-wins loop over ALL families, so a
# same-mode fixture gets the right answer no matter which plugin wins. The bug
# needs the families in DIFFERENT modes, and nothing constructed that. With the
# original logic restored, all 11 tests here passed — the file could not detect
# the defect it was written for.
#
# The fixture modelled what `auth use broker` leaves behind (it switches every
# family together) rather than what a person creates by hand, which is one
# plugin added to a project already in another mode. "A fixture must match a
# real install, not the minimum that makes the code path run."
#
# These go through compose_session — the REAL caller — not _Cfg. The stand-in
# was the second half of why this survived: it cannot show that the composed
# session mounts a credential the user did not ask for.
# --------------------------------------------------------------------------

def _project_with(tmp_path, monkeypatch, agent: str, plugins: list[str]) -> Path:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "bot"))
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(str(uuid.uuid4()))
    (proj / ".botainer" / "config.yaml").write_text(
        f"version: config-v1\nagent: {agent}\nruntime: docker\n"
        "image: ubuntu:24.04\nnetwork:\n  mode: internet\n"
        f"plugins_enabled: [{', '.join(plugins)}, git]\n")
    return proj


@pytest.mark.parametrize("order", [
    ["agent-codex-broker", "agent-claude-shared"],
    ["agent-claude-shared", "agent-codex-broker"],
])
def test_an_explicitly_enabled_plugin_is_never_replaced_by_an_inferred_one(
        tmp_path, monkeypatch, order) -> None:
    """#220: the mode the user WROTE DOWN wins over any inference.

    Parametrised on order because the original defect was order-dependent —
    listing the same two plugins the other way round produced the correct
    answer. A one-ordering test would have passed on the lucky arrangement and
    reported the feature working.
    """
    proj = _project_with(tmp_path, monkeypatch, "claude", order)
    spec = compose_session(proj, runtime_choice="docker",
                           identity_accept=True, agent_override="codex")
    assert _agents(spec) == ["agent-codex-broker"], (
        f"enabled={order}: --agent codex must use the agent-codex-broker that "
        f"is IN the config, not rebuild a name from another family's mode. "
        f"got {_agents(spec)}")


def test_the_swap_does_not_read_the_auth_mode_of_a_DIFFERENT_family(
        tmp_path, monkeypatch) -> None:
    """The anthropic plugin's mode must not decide the openai plugin's mode.

    Same defect `_shared_auth_family` (cli/start.py) fixed on 2026-09-04 for
    the concurrency prompt — "the old bool made every caller treat shared auth
    as one global thing". Fixed there, not here.

    Asserted as a SECURITY property, not a name: broker means the container is
    never handed the credential file, so if the wrong plugin were selected the
    session would mount one.
    """
    proj = _project_with(tmp_path, monkeypatch, "claude",
                         ["agent-claude-shared", "agent-codex-broker"])
    spec = compose_session(proj, runtime_choice="docker",
                           identity_accept=True, agent_override="codex")
    leaked = [b for b in spec.mount_plan.binds
              if "auth.json" in str(b.target) or "shared-auth" in str(b.source)]
    assert not leaked, (
        "broker was requested, so no credential file should be bound into the "
        f"container; found {[(str(b.source), str(b.target)) for b in leaked]}")


def test_the_mode_still_carries_when_the_target_family_has_no_plugin(
        tmp_path, monkeypatch) -> None:
    """The original FEATURE must survive the fix.

    With nothing enabled for the target family there is no explicit answer, so
    carrying the outgoing mode is right and #112 depends on it. Guards against
    "fix the bug by deleting the behaviour".
    """
    proj = _project_with(tmp_path, monkeypatch, "claude", ["agent-claude-shared"])
    spec = compose_session(proj, runtime_choice="docker",
                           identity_accept=True, agent_override="codex")
    assert _agents(spec) == ["agent-codex-shared"]


@pytest.mark.parametrize("order", [
    ["agent-codex-broker", "agent-codex"],
    ["agent-codex", "agent-codex-broker"],
])
def test_two_variants_of_one_family_REFUSE_instead_of_picking_by_order(
        tmp_path, monkeypatch, order) -> None:
    """The FIRST fix for #220 was wrong in the same way as the bug.

    It took `explicit[0]` — so with both agent-codex and agent-codex-broker
    enabled, LIST ORDER decided the auth mode, silently, and the message it
    printed claimed the choice came from plugins_enabled. `plugin enable` will
    happily create that state with no warning.

    Worse, it resolved the ambiguity BEFORE the mutually-exclusive guard could
    see it, so `--agent codex` proceeded where a plain `start` on the same
    config refuses. A flag that bypasses a refusal is not a convenience.

    Both orderings must now produce the SAME refusal the no-flag path gives.
    Parametrised because one ordering used to give the 'right' answer, which is
    exactly how the original defect stayed invisible.
    """
    from botainer.core.refusal import Refused
    proj = _project_with(tmp_path, monkeypatch, "claude", order)
    with pytest.raises(Refused) as exc:
        compose_session(proj, runtime_choice="docker",
                        identity_accept=True, agent_override="codex")
    msg = str(exc.value)
    assert "mutually exclusive" in msg, msg
    assert "agent-codex" in msg and "agent-codex-broker" in msg, msg


def test_the_ambiguous_case_refuses_the_SAME_way_with_and_without_the_flag(
        tmp_path, monkeypatch) -> None:
    """One rule, one place. The point of keeping both candidates rather than
    re-implementing the refusal here is that the two paths CANNOT drift."""
    from botainer.core.refusal import Refused
    plugins = ["agent-codex", "agent-codex-broker"]
    with pytest.raises(Refused) as with_flag:
        compose_session(_project_with(tmp_path / "a", monkeypatch, "claude", plugins),
                        runtime_choice="docker", identity_accept=True,
                        agent_override="codex")
    with pytest.raises(Refused) as no_flag:
        compose_session(_project_with(tmp_path / "b", monkeypatch, "codex", plugins),
                        runtime_choice="docker", identity_accept=True)
    assert str(with_flag.value) == str(no_flag.value), (
        f"--agent must refuse identically to a plain start:\n"
        f"  with flag: {with_flag.value}\n  no flag:   {no_flag.value}")


def test_the_swap_SAYS_which_mode_it_landed_on(tmp_path, monkeypatch, capsys) -> None:
    """Silence is what made #220 invisible until the launch banner.

    The two neighbouring functions (_drop_unselected_agent_plugins,
    _apply_auth_mode_override_in_memory) both announce themselves; this one
    never did. Asserted on the emitted text, and on the MODE appearing in it —
    a message that names the plugin but not the mode leaves the reader to know
    the suffix convention.
    """
    proj = _project_with(tmp_path, monkeypatch, "claude",
                         ["agent-claude-shared", "agent-codex-broker"])
    compose_session(proj, runtime_choice="docker",
                    identity_accept=True, agent_override="codex")
    err = capsys.readouterr().err
    assert "agent-codex-broker" in err, f"the swap was silent: {err!r}"
    assert "broker" in err, f"the resulting MODE is not stated: {err!r}"
