"""Switching auth mode moves the agent's history — say so.

User,: "I think there's something funny about session not being saved
after I exit… maybe when I make a config change?" then, a minute later, "maybe
I'm wrong about this - I'll keep an eye on it."

They were not wrong. Every auth mode binds the same container config dir
(/home/agent/.codex, /home/agent/.claude) from a DIFFERENT host directory:

    shared / isolated   …/data/agent-<x>/profiles/<profile>
    broker              …/data/agent-<x>/broker-state/<profile>

The CLI keeps session history in that dir, so crossing into or out of broker
relocates it and the next session looks amnesiac.

Broker's separate dir is LOAD-BEARING and must not be "tidied" away: its own
provenance says it "Holds NO credential", which is the entire point of broker
mode. Unifying the directories would put the real credential (shared mode keeps
auth.json there as a symlink into /shared-auth) inside a broker session — the
exact leak closed earlier the same day, reintroduced from the other end.

So the real fix is to stop partitioning HISTORY by auth mode (task #122), which
requires changing credential DELIVERY — the most fragile machinery here. That is
a designed change. What this file covers is the part that made it cost the user
an evening: it happened SILENTLY. A relocation you are told about is an
annoyance; a silent one is a bug report that starts "maybe I'm wrong".
"""
from __future__ import annotations

from pathlib import Path

from botainer.cli.auth import _history_dir_for


def test_broker_and_shared_resolve_to_DIFFERENT_dirs() -> None:
    """The mechanism, pinned. If these ever collapse to one path, the credential
    separation broker depends on has been lost — read the module docstring
    before "fixing" that."""
    root = Path("/S")
    shared = _history_dir_for(root, "uid-1", "agent-codex-shared", "shared")
    broker = _history_dir_for(root, "uid-1", "agent-codex-broker", "broker")
    assert shared != broker
    assert "profiles" in shared.parts
    assert "broker-state" in broker.parts


def test_shared_and_isolated_resolve_to_the_SAME_dir() -> None:
    """Which is why switching between those two must NOT warn — a warning that
    fires when nothing moved is the scenery CLAUDE.md forbids."""
    root = Path("/S")
    a = _history_dir_for(root, "uid-1", "agent-codex-shared", "shared")
    b = _history_dir_for(root, "uid-1", "agent-codex", "isolated")
    assert a == b


def test_the_agent_family_survives_every_mode_suffix() -> None:
    """All variants of a family must land under the same agent-<x> directory;
    a suffix leaking into the path would silently strand history per-mode even
    within one mode family."""
    root = Path("/S")
    # Only the SHIPPED modes: proxy is deferred, and naming it here would drag
    # a cut feature into the partition gate for no coverage gain.
    for plugin in ("agent-claude", "agent-claude-shared",
                   "agent-claude-broker"):
        d = _history_dir_for(root, "uid-1", plugin, "shared")
        assert "agent-claude" in d.parts, d
        assert not any(p.endswith(("-shared", "-broker"))
                       for p in d.parts), f"mode suffix leaked into {d}"
