"""Which agent RUNS and whose credentials are PRESENT are two decisions. (#230)

They were one, because a single plugin object carries both the entrypoint wrap
and the credential delivery. Consequences of that weld, all observed:

  * `--agent` had to perform plugin surgery to switch agents, and the surgery
    is where #220 (mode inferred across families) and #228 (silent isolated
    fallback) came from.
  * `_drop_unselected_agent_plugins` had to talk to the user about CREDENTIALS
    when they had only chosen an AGENT — "not activating agent-codex-broker
    (its credentials are NOT bound and its entrypoint is NOT used)".
  * You could not have codex credentials available inside a claude session at
    all, so the main agent could never call the other one — which the design
    explicitly says should work: "multiple agents coexist ... the others are
    available inside via their binaries" (internal design note DN-030). The
    code said the opposite ("a menu; exactly one is activated per session") and
    nobody reconciled them.

Agent selection and credential injection are separate choices:

    agent: / --agent      WHICH AGENT STARTS. Never inferred, never blocked.
    inject_credentials:   WHOSE CREDENTIALS ARE BOUND, without their entrypoint
                          running.
    both naming the same family   a no-op, not a conflict.

NOT COVERED HERE, deliberately: whether the injected agent's credentials reach
the container end-to-end. Broker mode delivers over a socket whose daemon needs
a real credential and a container runtime, neither of which exists in a unit
test. What is asserted is the composition contract — the plugin is active and
contributes exactly one entrypoint.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from botainer.core.composition import compose_session


def _project(tmp_path, monkeypatch, *, agent: str, plugins: list[str],
             inject: list[str] | None = None) -> Path:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "bot"))
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(str(uuid.uuid4()))
    inject_line = f"inject_credentials: [{', '.join(inject)}]\n" if inject else ""
    (proj / ".botainer" / "config.yaml").write_text(
        f"version: config-v1\nagent: {agent}\nruntime: docker\n"
        "image: ubuntu:24.04\nnetwork:\n  mode: internet\n"
        f"{inject_line}"
        f"plugins_enabled: [{', '.join(plugins)}, git]\n")
    return proj


def _agents(spec) -> list[str]:
    return sorted(p for p in spec.plugins_enabled if p.startswith("agent-"))


BOTH = ["agent-claude", "agent-codex-broker"]


def test_without_injection_the_other_family_is_not_activated(
        tmp_path, monkeypatch) -> None:
    """The default stays as #120 left it: no unasked-for credential."""
    proj = _project(tmp_path, monkeypatch, agent="claude", plugins=BOTH)
    spec = compose_session(proj, runtime_choice="docker", identity_accept=True)
    assert _agents(spec) == ["agent-claude"]


def test_injection_keeps_the_other_family_active(tmp_path, monkeypatch) -> None:
    proj = _project(tmp_path, monkeypatch, agent="claude", plugins=BOTH,
                    inject=["codex"])
    spec = compose_session(proj, runtime_choice="docker", identity_accept=True)
    assert _agents(spec) == ["agent-claude", "agent-codex-broker"], (
        "inject_credentials: [codex] must keep the codex plugin active so its "
        "credential machinery contributes")


def test_the_injected_family_contributes_NO_entrypoint(
        tmp_path, monkeypatch) -> None:
    """The whole point of the split, and the thing #111 would otherwise refuse.

    Asserted on the rendered exec chain rather than on the plugin list: keeping
    the plugin active is only correct if its wrap stays out, otherwise two
    agents compete for the session.
    """
    proj = _project(tmp_path, monkeypatch, agent="claude", plugins=BOTH,
                    inject=["codex"])
    spec = compose_session(proj, runtime_choice="docker", identity_accept=True)
    chain = " ".join(str(x) for w in (spec.entrypoint_wraps or ()) for x in w)
    assert "agent-codex" not in chain, (
        f"the injected agent's entrypoint wrap is in the exec chain: {chain!r}")
    assert "agent-claude" in chain, (
        f"the session's own agent entrypoint wrap is missing: {chain!r}")


def test_naming_the_running_agent_is_a_no_op(tmp_path, monkeypatch) -> None:
    """`--agent codex` plus inject codex is trivial, so it must not error.

    Its credentials are bound because it is the running agent; the injection
    list has nothing left to do. A rule that refused this would be inventing a
    conflict.
    """
    proj = _project(tmp_path, monkeypatch, agent="claude",
                    plugins=["agent-claude"], inject=["claude"])
    spec = compose_session(proj, runtime_choice="docker", identity_accept=True)
    assert _agents(spec) == ["agent-claude"]


def test_injection_does_not_override_which_agent_runs(
        tmp_path, monkeypatch) -> None:
    """Injecting codex must not turn the session into a codex session.

    Guards the direction the weld used to make possible: a credential decision
    silently becoming an agent decision.
    """
    # Asserted on the EXEC CHAIN, not the image: the fixture pins `image:` to a
    # plain ubuntu so the test does not depend on which tag the plugin resolves
    # to, and an image assertion would then be measuring the fixture. The chain
    # is what actually decides which agent the session runs.
    proj = _project(tmp_path, monkeypatch, agent="claude", plugins=BOTH,
                    inject=["codex"])
    spec = compose_session(proj, runtime_choice="docker", identity_accept=True)
    chain = " ".join(str(x) for w in (spec.entrypoint_wraps or ()) for x in w)
    assert "agent-claude" in chain and "agent-codex" not in chain, (
        f"injecting codex changed which agent runs; exec chain: {chain!r}")


def test_the_not_activated_message_names_BOTH_choices(
        tmp_path, monkeypatch, capsys) -> None:
    """The old text named a flag and stopped.

    Someone whose config asks for two agents was told what was NOT happening,
    without being told which one-line edit picks which — and the flag it named
    was the one that, at the time, was silently choosing the wrong mode (#220).
    """
    proj = _project(tmp_path, monkeypatch, agent="claude", plugins=BOTH)
    compose_session(proj, runtime_choice="docker", identity_accept=True)
    err = capsys.readouterr().err
    assert "--agent" in err, f"no way to run the other agent given: {err!r}"
    assert "inject_credentials" in err, (
        f"no way to keep this agent but get the other's credentials: {err!r}")
