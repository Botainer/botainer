"""nudge targets THIS project, and refuses to guess between its sessions.

The behaviour was already correct — nudge resolves the project from
`.botainer/project-id` in the cwd and only ever reads that project's
`sessions/`. It cannot reach another project's agent. These tests pin that,
because it is the property a reader most doubts.

What was WRONG was what we told people about it, and one thing we did:

  * `--help` said "run it from your project dir, or pass --session",
    presenting those as alternatives. They are not: nudge refuses outside a
    project whether or not --session is given (OBSERVED).
  * the refusal offered `--project-root`, a flag that does not exist.
  * with two sessions alive in one project — a supported state, `--agent
    claude` and `--agent codex` (#112) — it silently picked the most recent.
    The same file refuses an ambiguous --session PREFIX three lines earlier.
    One ambiguity, two answers, and the silent one delivers your text to an
    agent you did not choose.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from botainer.cli import nudge as nudge_cli


def _project(tmp_path, uuid="11111111-1111-4111-8111-111111111111"):
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(uuid)
    return proj


def _session(root, uuid, sid, *, runtime="docker", alive=True):
    d = root / "state" / uuid / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.json").write_text(json.dumps({
        "session_id": sid, "project_uuid": uuid, "project_root": "/p",
        "runtime": runtime, "image": "img", "host": "h",
        "spec": {"plugins_enabled": ["nudge"]},
        "started_at": "2026-08-18T10:00:00Z", "ended_at": None,
        "runtime_handle": {"docker": {"container_id": sid * 2}},
        "schema_version": 1,
    }))
    return d


def test_outside_a_project_it_refuses_EVEN_WITH_session(tmp_path, monkeypatch):
    """The falsehood the help used to contain, as a test."""
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(nudge_cli.nudge,
                             ["--session", "abcdef0123456789", "hi"])
    assert res.exit_code != 0
    assert "not inside a botainer project" in res.output, res.output


def test_the_help_does_not_offer_session_as_an_escape_from_the_project_dir():
    """Pins the corrected wording against drift back to the false version."""
    help_text = nudge_cli.nudge.get_help(
        __import__("click").Context(nudge_cli.nudge))
    assert "or pass --session" not in help_text, (
        "help again presents --session as an ALTERNATIVE to standing in the "
        "project directory; it is not — nudge refuses outside a project "
        "either way")
    assert "--project-root" not in help_text, (
        "help/refusal names --project-root, which does not exist")


def test_two_live_sessions_refuse_instead_of_guessing(tmp_path, monkeypatch,
                                                      capsys):
    monkeypatch.setattr(nudge_cli, "_is_session_alive", lambda rec: True)
    root = tmp_path / "root"
    uuid = "11111111-1111-4111-8111-111111111111"
    _session(root, uuid, "aaaaaaaaaaaaaaaa")
    _session(root, uuid, "bbbbbbbbbbbbbbbb")

    with pytest.raises(SystemExit):
        nudge_cli._resolve_target_session(
            root / "state" / uuid / "sessions", None)

    # A bare pytest.raises would pass on ANY SystemExit, including one from an
    # unrelated failure. Assert it refused for THIS reason and told the user
    # how to resolve it — a refusal that does not name the choices is just a
    # different way to be stuck.
    out = capsys.readouterr()
    msg = out.err + out.out
    assert "refusing to guess" in msg, msg
    assert "aaaaaaaaaaaaaaaa" in msg and "bbbbbbbbbbbbbbbb" in msg, (
        f"refusal did not list the sessions to choose between: {msg}")
    assert "--session" in msg


def test_one_live_session_is_still_chosen_silently(tmp_path, monkeypatch):
    """The common case must not become chattier."""
    monkeypatch.setattr(nudge_cli, "_is_session_alive", lambda rec: True)
    root = tmp_path / "root"
    uuid = "11111111-1111-4111-8111-111111111111"
    _session(root, uuid, "aaaaaaaaaaaaaaaa")

    rec = nudge_cli._resolve_target_session(
        root / "state" / uuid / "sessions", None)
    assert rec is not None and rec.session_id == "aaaaaaaaaaaaaaaa"


def test_an_explicit_session_still_wins_over_the_ambiguity_refusal(
        tmp_path, monkeypatch):
    """--session is how you RESOLVE the ambiguity, so it must not trip it."""
    monkeypatch.setattr(nudge_cli, "_is_session_alive", lambda rec: True)
    root = tmp_path / "root"
    uuid = "11111111-1111-4111-8111-111111111111"
    _session(root, uuid, "aaaaaaaaaaaaaaaa")
    _session(root, uuid, "bbbbbbbbbbbbbbbb")

    rec = nudge_cli._resolve_target_session(
        root / "state" / uuid / "sessions", "bbbb")
    assert rec is not None and rec.session_id == "bbbbbbbbbbbbbbbb"
