"""A query command must not decide whether this checkout is a copy. (#231)

`resolve_identity`'s ambiguous branch — reached only when the recorded path AND
the current path both exist — is, in its own words, "a real choice between move
and second copy". `identity_accept=True` means "the user passed
--accept-identity-change", takes the moved branch, and writes the new path into
path_history.

FOUR COMMANDS PASSED IT UNCONDITIONALLY: status.py, attach.py, stop.py,
nudge.py. None of them HAS that flag. Eight other call sites pass False. So
which answer you got to the clone question depended on which command you
happened to type.

AND IT WAS PERMANENT. The accept appends `here` to path_history, so afterwards
`last_known == here` and the branch is never reached again — by ANY command,
including `start`, which does honour the flag. One `cp -a` plus one
`botainer status` and the copy is attached to the original's credentials,
/packages and history, with the prompt disarmed for good.

docs/CAPABILITY-SURFACE.md §4ck asserts "Running from a copy while the original
still exists now prompts interactively, or refuses non-interactively". That was
false for those four.

WHY NOT JUST PASS False: the non-interactive refusal names
`--accept-identity-change` and `--fork`, which these commands do not have —
shipped output naming commands that do not exist. So the fix is to answer
NOTHING: return the uuid, write no meta, say what was seen and which command
decides.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from botainer.core import identity


@pytest.fixture
def cloned(tmp_path, monkeypatch):
    """An original and a copy, BOTH existing — the only state that is ambiguous.

    One path cannot express this: with the original gone it is an unambiguous
    move and the branch under test is never reached.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "bot"))
    uid = str(uuid.uuid4())
    orig = tmp_path / "orig"
    (orig / ".botainer").mkdir(parents=True)
    (orig / ".botainer" / "project-id").write_text(uid)
    identity.resolve_identity(orig)                    # records the original
    copy = tmp_path / "copy"
    (copy / ".botainer").mkdir(parents=True)
    (copy / ".botainer" / "project-id").write_text(uid)
    meta = tmp_path / "bot" / "state" / uid / "meta.json"
    return copy, meta, uid


def _paths(meta: Path) -> list[str]:
    d = json.loads(meta.read_text())
    return [r.get("path") if isinstance(r, dict) else r
            for r in d.get("path_history", [])]


def test_a_non_recording_resolve_writes_nothing(cloned) -> None:
    copy, meta, uid = cloned
    before = _paths(meta)
    got_uid, _ = identity.resolve_identity(copy, identity_accept=False, record=False)
    assert got_uid == uid, "the query still needs the project's uuid to find things"
    assert _paths(meta) == before, (
        f"a query wrote the copy's path into path_history: {_paths(meta)}")


def test_the_guard_still_fires_afterwards(cloned) -> None:
    """The whole point: a query must not disarm the prompt for `start`.

    Without this the fix could be satisfied by any non-writing shortcut that
    still leaves the launch path unable to see the ambiguity.
    """
    copy, meta, _ = cloned
    identity.resolve_identity(copy, identity_accept=False, record=False)
    with pytest.raises(identity.IdentityChangeRefused):
        identity.resolve_identity(copy, identity_accept=False)


def test_it_says_what_it_saw_and_which_command_decides(cloned, capsys) -> None:
    """Silence here would be its own defect — the user is in a copy and cannot tell.

    Asserted on both remedies being NAMED, because the failure mode this
    replaces is output that says what did not happen without saying what to do.
    """
    copy, _, _ = cloned
    identity.resolve_identity(copy, identity_accept=False, record=False)
    err = capsys.readouterr().err
    assert "--fork" in err and "--accept-identity-change" in err, err
    assert "botainer start" in err, (
        f"the remedies must name the command that HAS those flags: {err!r}")


def test_recording_is_still_the_default(cloned) -> None:
    """`start` must keep its ability to accept a move.

    Guards against fixing this by making the accept impossible everywhere.
    """
    copy, meta, _ = cloned
    identity.resolve_identity(copy, identity_accept=True)
    assert str(copy) in _paths(meta), (
        "an explicit --accept-identity-change no longer records the move")
