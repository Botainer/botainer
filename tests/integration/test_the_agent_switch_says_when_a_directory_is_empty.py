"""`config set agent` names two directories; it must say when one holds nothing.

WHAT IT SAID. Switching agent prints, verbatim:

    ! Switching agent starts a FRESH history.
      … Nothing is deleted: 'claude''s history stays where it is
      and comes back if you switch back.
        claude: …/data/agent-claude/profiles/default
        codex:  …/data/agent-codex/profiles/default

MEASURED on a project that had never run a session: NEITHER directory existed.
So the reassurance — "stays where it is and comes back if you switch back" — was
a promise about a path that held nothing, and a user who went looking would find
no such directory and reasonably conclude something had been lost.

This is not the defect the task filed under this behaviour describes; that one
was a MODE mix-up (a claude project told its history lived under `broker-state/`)
and the fix for it is recorded in a comment beside this code. This is the
remaining half: the paths are right, and a bare path reads as "your history is
there".

THE SIBLING SAYS IT ALREADY. The disclosure added the same week for
`start --auth-mode` says "The one this session reads is EMPTY, so the agent
starts with no history", for exactly this reason. Two surfaces describing the
same fact should not disagree about whether it is worth mentioning.

AND THE POSSESSIVE. `{old_value!r}` rendered `'claude''s` — repr's quotes
colliding with the apostrophe. The agent name is a plain identifier and needs no
quoting.

BOTH DIRECTIONS ARE PINNED: a directory with history must NOT be annotated, or
the note becomes scenery and stops carrying information.
"""
from __future__ import annotations

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.main import cli


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    r = CliRunner()
    assert r.invoke(cli, ["setup"]).exit_code == 0
    assert r.invoke(cli, ["init"]).exit_code == 0

    def _history_dir_for(agent: str):
        from botainer.core import identity
        uid, _ = identity.resolve_identity(proj, identity_accept=True)
        return (tmp_path / "root" / "state" / uid / "data"
                / f"agent-{agent}" / "profiles" / "default")

    return r, _history_dir_for


def test_a_directory_that_holds_nothing_says_so(project):
    """The measured case: a project that has never run a session."""
    r, _ = project
    res = r.invoke(cli, ["config", "set", "agent", "codex", "--yes"])

    assert "empty — no history yet" in res.output, (
        f"two directories are named and neither exists, with no note saying "
        f"so:\n{res.output[:900]}")
    assert res.output.count("empty — no history yet") == 2, (
        f"only one of the two empty directories was annotated:\n"
        f"{res.output[:900]}")


def test_a_directory_WITH_history_is_not_annotated(project):
    """OPPOSITE DIRECTION, and the half that keeps the note meaningful.

    A note on every line is scenery; scenery is what trains people to stop
    reading the paragraph this one lives in.
    """
    r, history_dir_for = project
    live = history_dir_for("claude")
    live.mkdir(parents=True, exist_ok=True)
    (live / "history.jsonl").write_text('{"msg":"real work"}\n')

    res = r.invoke(cli, ["config", "set", "agent", "codex", "--yes"])

    claude_line = next((ln for ln in res.output.splitlines()
                        if ln.strip().startswith("claude:")), None)
    assert claude_line is not None, f"no claude line:\n{res.output[:900]}"
    assert "empty" not in claude_line, (
        f"a directory holding real history was called empty: {claude_line!r}")

    codex_line = next((ln for ln in res.output.splitlines()
                       if ln.strip().startswith("codex:")), None)
    assert codex_line is not None, f"no codex line:\n{res.output[:900]}"
    assert "empty — no history yet" in codex_line, (
        f"the genuinely empty directory lost its note: {codex_line!r}")


def test_the_possessive_is_not_double_quoted(project):
    """`{old_value!r}'s` produced "'claude''s"."""
    r, _ = project
    res = r.invoke(cli, ["config", "set", "agent", "codex", "--yes"])

    assert "''s" not in res.output, (
        f"repr's quotes collided with the apostrophe:\n{res.output[:900]}")
    assert "claude's history" in res.output, (
        f"the possessive does not read as a name:\n{res.output[:900]}")
