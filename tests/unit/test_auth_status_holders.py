"""`auth status` says WHO ELSE can redeem this login right now (#215).

Everything else `auth status` prints is about FILES — present, expired, which
path. None of it explains the failure that logged out six projects at once,
because every file involved looked fine: the cause was two live sessions both
able to redeem one rotating refresh token. `auth status` is where someone goes
when a login stops working, so the answer belongs there.

These cover the DISPLAY. The rule itself is in test_credential_holders.py.
A smoke test earns its place here for a specific reason: this block is wrapped
in `except Exception`, and auth.py already carries a comment (on a different
function) about a bare except once swallowing a NameError. A typo in a rarely-
hit display branch would otherwise surface as a polite "could not be checked".
"""
from __future__ import annotations

import json

from botainer.cli import auth
from botainer.core import credential_holders as ch


def _seed(tmp_path, monkeypatch, sessions):
    """A state root shaped like a real install (meta.json included)."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    from botainer.core.spec import SessionSpec
    from botainer.state import dir as state_dir
    from botainer.state import liveness
    from botainer.state import session_record as sr
    monkeypatch.setattr(liveness, "is_session_alive", lambda rec: True)
    paths = state_dir.ensure_user_state_dir()
    for uuid, name, plugin, sid in sessions:
        p = state_dir.ensure_project_dirs(paths, uuid)
        proj = tmp_path / name
        proj.mkdir(parents=True, exist_ok=True)
        (p.base / "meta.json").write_text(
            json.dumps({"display_name": name, "path_history": [str(proj)]}),
            encoding="utf-8")
        spec = SessionSpec(session_id=sid, project_uuid=uuid,
                           project_root=str(proj), state_dir=str(p.base),
                           runtime="mock", image="img", plugins_enabled=[plugin])
        d = p.sessions_dir / sid
        d.mkdir(parents=True, exist_ok=True)
        sr.write(d, sr.from_spec(spec))
    return paths


def test_no_running_sessions_says_so_positively(tmp_path, monkeypatch, capsys) -> None:
    """"none" must be stated, not implied by an absent section. A section that
    silently vanishes when the answer is good is a section nobody learns to
    trust when the answer is bad."""
    _seed(tmp_path, monkeypatch, [])
    auth._print_live_holders()
    out = capsys.readouterr().out
    assert "live credential holders: none" in out


def test_a_shared_and_a_broker_session_are_reported_as_clashing(
        tmp_path, monkeypatch, capsys) -> None:
    _seed(tmp_path, monkeypatch, [
        ("uuid-aaaa", "proj-a", "agent-claude-shared", "sesAAAA0001"),
        ("uuid-bbbb", "proj-b", "agent-claude-broker", "sesBBBB0001"),
    ])
    auth._print_live_holders()
    out = capsys.readouterr().out
    assert "proj-a" in out and "proj-b" in out
    assert "redeem the SAME refresh token" in out
    assert "sesAAAA0001"[:12] in out and "sesBBBB0001"[:12] in out
    # The pairing is DONE FOR the reader. A list of sessions is evidence;
    # whether any two will log each other out is the question they arrived with.
    assert "↔" in out
    # And it does not claim to have fixed anything.
    assert "not fixed" in out.lower()


def test_sessions_that_cannot_clash_are_said_to_be_fine(
        tmp_path, monkeypatch, capsys) -> None:
    """Two isolated projects, and a codex one. Reporting these as a conflict
    would train the reader to ignore the section."""
    _seed(tmp_path, monkeypatch, [
        ("uuid-aaaa", "proj-a", "agent-claude", "sesAAAA0001"),
        ("uuid-cccc", "proj-c", "agent-codex", "sesCCCC0001"),
    ])
    auth._print_live_holders()
    out = capsys.readouterr().out
    assert "proj-a" in out and "proj-c" in out
    assert "No two of these can invalidate each other" in out
    assert "redeem the SAME refresh token" not in out


def test_a_broken_check_says_it_is_broken_not_that_all_is_well(
        monkeypatch, capsys) -> None:
    """The important half. A detector whose failure looks like a clean result
    is worse than no detector: it converts "I don't know" into "you're fine"."""
    def _boom(*a, **k):
        raise RuntimeError("state tree unreadable")
    monkeypatch.setattr(ch, "live_holders", _boom)
    auth._print_live_holders()
    out = capsys.readouterr().out
    assert "could NOT be checked" in out
    assert "RuntimeError" in out and "state tree unreadable" in out
    assert "not an all-clear" in out
    assert "none" not in out.split("could NOT be checked")[0].split("\n")[-1]
