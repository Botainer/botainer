"""Switching auth mode moves the agent's history — say so, and offer to bring it.

Auth modes bind the same container config directory
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

WHAT CHANGED (#159). This file used to end by saying the real fix "requires
changing credential DELIVERY — the most fragile machinery here", and deferring
it. That is no longer the plan. The directories STAY separate, and the history
is CARRIED between them instead, without changing credential delivery.
This file covers both halves — the switch says WHERE,
and it offers to bring the files.
"""
from __future__ import annotations

from pathlib import Path

import click
from click.testing import CliRunner

from botainer.cli._history_prompt import (
    history_dir_for,
    offer_carry,
    warn_history_will_move,
)
from botainer.cli.auth import _history_moves_for


def test_broker_and_shared_resolve_to_DIFFERENT_dirs() -> None:
    """The mechanism, pinned. If these ever collapse to one path, the credential
    separation broker depends on has been lost — read the module docstring
    before "fixing" that."""
    root = Path("/S")
    shared = history_dir_for(root, "uid-1", "agent-codex-shared", "shared")
    broker = history_dir_for(root, "uid-1", "agent-codex-broker", "broker")
    assert shared != broker
    assert "profiles" in shared.parts
    assert "broker-state" in broker.parts


def test_shared_and_isolated_resolve_to_the_SAME_dir() -> None:
    """Which is why switching between those two must NOT warn — a warning that
    fires when nothing moved is the scenery CLAUDE.md forbids."""
    root = Path("/S")
    a = history_dir_for(root, "uid-1", "agent-codex-shared", "shared")
    b = history_dir_for(root, "uid-1", "agent-codex", "isolated")
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
        d = history_dir_for(root, "uid-1", plugin, "shared")
        assert "agent-claude" in d.parts, d
        assert not any(p.endswith(("-shared", "-broker"))
                       for p in d.parts), f"mode suffix leaked into {d}"


# ── the warning must say WHERE, not just THAT ──────────────────────────────
#
# Everything above drives the pure path function. Nothing drove the thing the
# user actually sees — and that gap hid a real defect for as long as the warning
# has existed:
#
#     state_root = _state_dir.state_root()      # no such attribute, ever
#
# wrapped in `except Exception: state_root = uid = None`. So the guard
# `if state_root and uid:` never fired and the warning printed "your history
# MOVED" while never printing where it moved to — precisely the half the module
# docstring calls the point. A bare `except` around a name lookup is how a typo
# survives as a silent feature loss, and a test that only exercises the helper
# can never see it. Drive the real function.


def _project_with_history(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A project whose SHARED history dir exists on disk AND holds history.

    Holding history matters, not just existing: a directory containing only a
    credential is the state right after a login, and treating that as history
    would make the warning fire on a first switch with nothing to move.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    uid = "2833fc11-6d6c-4a62-b162-d8f3dae4d09d"
    (proj / ".botainer" / "project-id").write_text(uid)

    from botainer.state import dir as _state_dir
    root = _state_dir.ensure_user_state_dir(create_if_missing=True).root
    shared = history_dir_for(root, uid, "agent-claude", "shared")
    shared.mkdir(parents=True, exist_ok=True)
    (shared / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    (shared / "history.jsonl").write_text('{"display":"earlier work"}\n')
    (shared / "todos").mkdir(exist_ok=True)
    (shared / "todos" / "t.json").write_text("[]")
    return proj, shared


def _run(fn):
    """Drive a prompt function inside a real command so click I/O is real."""
    @click.command()
    def cmd() -> None:
        fn()

    result = CliRunner().invoke(cmd, input="", catch_exceptions=False)
    text = result.output or ""
    try:
        if result.stderr:
            text += result.stderr
    except ValueError:
        pass
    return text


def test_the_mode_switch_warning_prints_the_actual_directory(
        tmp_path, monkeypatch) -> None:
    proj, shared = _project_with_history(tmp_path, monkeypatch)

    moves = _history_moves_for(proj, disabled=["agent-claude-shared"],
                               enabled=["agent-claude-broker"], mode="broker")
    assert moves, "a broker switch moves history and must be detected"
    err = _run(lambda: [warn_history_will_move(s, d, what_changed="auth mode")
                        for _a, s, d in moves])

    assert str(shared) in err, (
        "the warning says history moved but never says WHERE. That is the "
        "half that cost the user an evening — a relocation you are told "
        "about is an annoyance, a silent one is a bug report that starts "
        "'maybe I'm wrong'."
    )
    assert "broker-state" in err, "and where it is going"


def test_a_same_dir_switch_stays_quiet(tmp_path, monkeypatch) -> None:
    """shared <-> isolated share a directory, so there is nothing to say.

    Pinned because the cheap way to 'fix' the test above is to print the path
    unconditionally, which would fire this warning on switches that move
    nothing — and a warning that fires when nothing happened is the scenery
    problem this project has already been bitten by.
    """
    proj, _ = _project_with_history(tmp_path, monkeypatch)

    moves = _history_moves_for(proj, disabled=["agent-claude-shared"],
                               enabled=["agent-claude"], mode="isolated")

    assert moves == []
    assert _run(lambda: [warn_history_will_move(s, d, what_changed="auth mode")
                         for _a, s, d in moves]) == ""


def test_one_pair_per_agent_even_when_the_diff_names_both_variants(
        tmp_path, monkeypatch) -> None:
    """`agent-claude` and `agent-claude-broker` are one agent, not two.

    A diff naming both would otherwise derive the same (from, to) pair twice
    and warn the user about their history twice in one command.
    """
    proj, _ = _project_with_history(tmp_path, monkeypatch)

    moves = _history_moves_for(
        proj, disabled=["agent-claude", "agent-claude-shared"],
        enabled=["agent-claude-broker"], mode="broker")

    assert len(moves) == 1, moves


def test_the_switch_actually_brings_the_history_across(
        tmp_path, monkeypatch) -> None:
    """The half that did not exist: the files arrive in the new mode's dir.

    Told-but-not-moved was the shipped state after the warning was fixed. This
    is what makes the answer to "did we do the thing that moves history" yes.
    """
    proj, shared = _project_with_history(tmp_path, monkeypatch)
    moves = _history_moves_for(proj, disabled=["agent-claude-shared"],
                               enabled=["agent-claude-broker"], mode="broker")
    _agent, src, dst = moves[0]

    _run(lambda: offer_carry(src, dst, what_changed="auth mode",
                             assume_yes=True))

    assert (dst / "history.jsonl").read_text() == '{"display":"earlier work"}\n'
    assert (dst / "todos" / "t.json").exists()
    assert not (dst / ".credentials.json").exists(), (
        "broker-state must hold NO credential — that separation is the whole "
        "point of broker mode, and a carry is not allowed to undo it"
    )
    assert (shared / ".credentials.json").exists(), (
        "the credential stays behind, which is what lets a user switch away "
        "and back without signing in again"
    )
    assert not (shared / "history.jsonl").exists(), (
        "the history MOVED — leaving a second live copy is what makes "
        "'which of these is current?' a question the user has to answer"
    )
