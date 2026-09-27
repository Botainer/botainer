"""botainer's memory of what config.yaml DECLARED at the last start.

THE PROPERTY THIS DEFENDS (#192): a one-shot `--auth-profile` must be
structurally unable to look like a config change. `SessionRecord.spec` holds the
EFFECTIVE value after overrides are folded in, so comparing sessions would fire
on the flag AND on the next plain start — two carries and two set-aside copies
from one temporary flag. Comparing DECLARED values cannot see a flag at all,
because flags never touch config.yaml.

The second property, equally deliberate: ABSENCE IS BENIGN. A missing, corrupt
or future-versioned file all mean "I do not know", and the safe response to all
three is identical. A user who deletes this loses one carry OFFER, never data —
which is what makes the file's own "safe to delete" note true rather than a
threat.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from botainer.state import declared


# ── the core property: declared, not effective ───────────────────────────────

def test_a_one_shot_flag_is_invisible(tmp_path) -> None:
    """The whole reason this module exists.

    The session RAN with profile=work (a flag); the CONFIG still says personal.
    Recording the declared value means the next start sees no change.
    """
    declared.write_if_changed(tmp_path, {"profile": "personal", "agent": "claude"})

    # next start: config unchanged, whatever any flag did during the last one
    rec = declared.read(tmp_path)
    assert rec.differs_from({"profile": "personal", "agent": "claude"}) == {}


def test_a_hand_edit_is_visible(tmp_path) -> None:
    """The defect #192 is about: the route botainer's own help text recommends."""
    declared.write_if_changed(tmp_path, {"profile": "personal", "agent": "claude"})

    moved = declared.read(tmp_path).differs_from(
        {"profile": "work", "agent": "claude"})

    assert moved == {"profile": ("personal", "work")}


def test_several_axes_move_together(tmp_path) -> None:
    declared.write_if_changed(
        tmp_path, {"profile": "personal", "agent": "claude", "auth_mode": "shared"})
    moved = declared.read(tmp_path).differs_from(
        {"profile": "work", "agent": "codex", "auth_mode": "shared"})
    assert set(moved) == {"profile", "agent"}


# ── absence is benign, in every flavour ──────────────────────────────────────

@pytest.mark.parametrize("corrupt", [
    "",                                   # empty
    "not json at all",                    # unparseable
    "[]",                                 # wrong top-level type
    '{"version": 999, "declared": {}}',   # a schema this build cannot read
    '{"version": 1}',                     # missing the payload
    '{"version": 1, "declared": "no"}',   # payload wrong type
])
def test_every_broken_shape_reads_as_unknown(tmp_path, corrupt) -> None:
    """All of these mean "I do not know what the config said", and the caller's
    safe response to all of them is the same — so they must not be
    distinguished. A corrupt file must never be MORE dangerous than a missing
    one."""
    declared.path_for(tmp_path).write_text(corrupt)
    assert declared.read(tmp_path) is None


def test_a_missing_file_is_none_not_an_error(tmp_path) -> None:
    assert declared.read(tmp_path) is None


def test_deleting_it_is_safe(tmp_path) -> None:
    """Pins the promise the file makes about itself, in its own first key."""
    declared.write_if_changed(tmp_path, {"profile": "work"})
    note = json.loads(declared.path_for(tmp_path).read_text())["_"]
    assert "Deleting it is SAFE" in note

    declared.path_for(tmp_path).unlink()
    assert declared.read(tmp_path) is None          # no exception
    assert declared.write_if_changed(tmp_path, {"profile": "work"}) is True


# ── write-on-change: the inode-quota lesson ──────────────────────────────────

def test_an_unchanged_config_writes_nothing(tmp_path) -> None:
    """state/dir.py records a cluster with a hard 500,000-INODE cap on $HOME
    where every session died writing `.meta.json.tmp` with Errno 122. A file
    rewritten on every start would add that failure point back. The common path
    must be ZERO writes."""
    vals = {"profile": "personal", "agent": "claude"}
    assert declared.write_if_changed(tmp_path, vals) is True     # first time
    before = declared.path_for(tmp_path).stat().st_mtime_ns

    assert declared.write_if_changed(tmp_path, vals) is False    # no-op
    assert declared.path_for(tmp_path).stat().st_mtime_ns == before, (
        "an unchanged config must not touch the file at all"
    )


def test_a_write_failure_never_raises(tmp_path, monkeypatch) -> None:
    """Failing to record must not fail the SESSION. The cost of silence is one
    missed carry offer; the cost of raising is `start` dying on a full disk —
    which is the exact failure this module's design exists to avoid."""
    def boom(*a, **k):
        raise OSError("Errno 122 Disk quota exceeded")
    monkeypatch.setattr(declared, "write_secure", boom)

    assert declared.write_if_changed(tmp_path, {"profile": "work"}) is False


# ── shape and permissions ────────────────────────────────────────────────────

def test_it_is_written_private(tmp_path) -> None:
    """Host-private by placement, and 0600 by construction — the agent must
    never be able to lie to the next session about what the config said."""
    declared.write_if_changed(tmp_path, {"profile": "work"})
    mode = stat.S_IMODE(declared.path_for(tmp_path).stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_only_tracked_fields_are_recorded(tmp_path) -> None:
    """Not a general config dump. Recording more would make this a second,
    drifting copy of the config rather than a note about three axes."""
    declared.write_if_changed(tmp_path, {
        "profile": "work", "agent": "claude",
        "image": "ubuntu:24.04", "runtime": "docker",   # not tracked
    })
    stored = json.loads(declared.path_for(tmp_path).read_text())["declared"]
    assert set(stored) == {"profile", "agent"}


def test_a_field_added_to_TRACKED_later_is_not_a_change(tmp_path) -> None:
    """Forward-compat, and it matters: without this, the first start after a new
    axis ships would report a change on EVERY project and offer a carry nobody
    asked for."""
    declared.path_for(tmp_path).write_text(json.dumps(
        {"version": declared.SCHEMA_VERSION, "declared": {"profile": "work"}}))

    moved = declared.read(tmp_path).differs_from(
        {"profile": "work", "auth_mode": "broker"})   # auth_mode unknown to the record

    assert moved == {}
