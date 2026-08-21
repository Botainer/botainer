"""Copying a project must not silently reuse the original's state dir.

`resolve_identity` did `[str(x) for x in raw_hist]` on a `path_history` whose
entries are DICTS. Each record stringified to `"{'first_seen': ..., 'path':
...}"`, which is never equal to the current path and is not a path that exists.
So the two branches that decide "same place" and "prior copy still alive" were
both unreachable, and every invocation fell into "prior path is gone -> moved,
record silently".

The consequence was not cosmetic: copy a project, run from the copy, and it
silently attached to the ORIGINAL's state dir — same /packages, same scratch,
same per-project credentials — which is exactly what the clone/fork prompt
exists to prevent. The prompt had never been reachable.

Two readers of one field, updated at different times, with nothing comparing
them: `list_projects` was taught the dict shape, `resolve_identity` was not.
Both now go through `state_dir.path_history_records`.
"""

from __future__ import annotations

import socket

import pytest

from botainer.core import identity
from botainer.core.refusal import Refused
from botainer.state import dir as state_dir


def _meta_with(paths_and_hosts):
    return {
        "path_history": [
            {"path": p, "host": h, "first_seen": "x", "last_seen": "y"}
            for p, h in paths_and_hosts
        ]
    }


def test_records_are_read_as_paths_not_stringified_dicts():
    meta = _meta_with([("/a/b", "host1"), ("/c/d", "host1")])
    assert [r["path"] for r in state_dir.path_history_records(meta)] == ["/a/b", "/c/d"]
    # The old code produced this instead — pin that we are not back there.
    assert not any(
        p.startswith("{") for p in
        (r["path"] for r in state_dir.path_history_records(meta))
    )


def test_old_string_shape_still_reads():
    # meta.json written before the record migration.
    meta = {"path_history": ["/a/b", "/c/d"]}
    assert [r["path"] for r in state_dir.path_history_records(meta)] == ["/a/b", "/c/d"]


def test_last_known_prefers_this_host():
    me = socket.gethostname()
    meta = _meta_with([("/on/mine", me), ("/on/theirs", "other-machine")])
    # Records are host-keyed so a laptop path and a cluster path do not read as
    # a move; the last entry overall is the other host's.
    assert state_dir.last_known_path(meta) == "/on/mine"


def test_last_known_falls_back_when_no_host_matches():
    meta = _meta_with([("/a", "someone"), ("/b", "else")])
    assert state_dir.last_known_path(meta) == "/b"


def test_last_known_is_none_for_empty_history():
    assert state_dir.last_known_path({}) is None
    assert state_dir.last_known_path({"path_history": []}) is None


def _init(root):
    """Just the project-id file — init_project also writes config/policy we
    do not need here, and its signature is not what this test is about."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".botainer").mkdir(exist_ok=True)
    identity.write_project_id(root, identity.generate_new())


def test_copy_of_a_live_project_is_refused_not_silently_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    orig = tmp_path / "orig"
    _init(orig)
    uid_a, _ = identity.resolve_identity(orig)

    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / ".botainer").mkdir()
    (copy / ".botainer" / "project-id").write_text(
        (orig / ".botainer" / "project-id").read_text())

    # Non-interactive: must refuse rather than attach to orig's state dir.
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    with pytest.raises(Refused) as exc:
        identity.resolve_identity(copy)
    assert "still exists" in str(exc.value)
    assert str(orig) in str(exc.value)
    assert uid_a  # the original is untouched


def test_a_genuine_move_still_proceeds_silently(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    orig = tmp_path / "orig"
    _init(orig)
    uid_a, _ = identity.resolve_identity(orig)

    moved = tmp_path / "moved"
    orig.rename(moved)
    uid_b, _ = identity.resolve_identity(moved)
    assert uid_b == uid_a, "a move must keep the project's identity"


def test_rerunning_in_place_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    _init(proj)
    first, _ = identity.resolve_identity(proj)
    for _ in range(3):
        again, _ = identity.resolve_identity(proj)
        assert again == first


def test_fork_flag_mints_a_new_uuid_and_leaves_the_original(tmp_path, monkeypatch):
    """`--fork` is the non-interactive counterpart of answering "[f] fork".

    The refusal message has named `--fork` since v0.0.13 and the flag did not
    exist. Nobody noticed because the refusal itself was unreachable until the
    path_history reader was fixed — so the moment that fix landed, botainer
    started telling people to pass a flag it would reject.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    orig = tmp_path / "orig"
    _init(orig)
    uid_orig, _ = identity.resolve_identity(orig)

    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / ".botainer").mkdir()
    (copy / ".botainer" / "project-id").write_text(
        (orig / ".botainer" / "project-id").read_text())

    uid_fork, state = identity.resolve_identity(copy, fork=True)
    assert uid_fork != uid_orig
    # The original is untouched — it keeps its uuid and its state dir.
    assert identity.read_project_id(orig) == uid_orig
    # The copy's own project-id was rewritten to the new uuid.
    assert identity.read_project_id(copy) == uid_fork
    # Provenance is recorded so the fork can be traced back.
    import json
    meta = json.loads((state / "meta.json").read_text())
    assert meta["forked_from"] == uid_orig


def test_fork_and_accept_together_are_refused(tmp_path, monkeypatch):
    # They say opposite things; picking one silently would be a coin flip on
    # whether the user keeps or abandons the original's state dir.
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    orig = tmp_path / "orig"
    _init(orig)
    identity.resolve_identity(orig)
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / ".botainer").mkdir()
    (copy / ".botainer" / "project-id").write_text(
        (orig / ".botainer" / "project-id").read_text())

    with pytest.raises(Refused) as exc:
        identity.resolve_identity(copy, fork=True, identity_accept=True)
    assert "opposite" in str(exc.value)


def test_every_flag_the_refusal_names_actually_exists():
    """The refusal text is a promise; keep it checkable.

    This is the #130 class — shipped output naming a command or flag that does
    not exist — applied to the one message that had been hiding behind dead
    code.
    """
    import re
    from botainer.cli.start import start as start_cmd
    src = (identity.__file__)
    text = open(src).read()
    named = set(re.findall(r"`?(--[a-z][a-z-]+)`?", text))
    real = {opt for p in start_cmd.params for opt in getattr(p, "opts", [])}
    missing = {f for f in named if f not in real}
    assert not missing, (
        f"identity.py names flag(s) {sorted(missing)} that `botainer start` "
        f"does not define"
    )
