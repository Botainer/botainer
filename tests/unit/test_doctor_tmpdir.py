"""`doctor` measures whether $TMPDIR is node-local, instead of asking you.

The question was going to be put to the maintainer as "is $TMPDIR node-local on
your cluster?" — and that is not a per-site fact anyone should have to supply.
It is a property of the machine the command is standing on, and fs_kind already
knows how to ask.

The consequence is concrete rather than general HPC advice: when the state root
is too long for a unix socket path (sun_path is 108 bytes and an HPC $SCRATCH
eats most of it), agent-claude-broker falls back to $XDG_RUNTIME_DIR → $TMPDIR
→ /tmp. A unix socket on a NETWORK filesystem is the case that does not
reliably work.

THE CONSTANTS, NOT RE-SPELLED LITERALS. The first draft compared
`kind == "LOCAL"`; fs_kind's values are lowercase, so that branch could never
be taken — every machine reported "could not tell" while printing
`(overlay (local))` on the same line, contradicting itself. Reading the
function agreed it worked. Running it did not. These tests pass the same
constants the caller passes, so the two cannot drift apart again.
"""
from __future__ import annotations

import pytest

from botainer.cli.doctor import tmpdir_findings
from botainer.state.fs_kind import LOCAL, NETWORK, UNKNOWN


def _one(*args):
    found = tmpdir_findings(*args)
    assert len(found) == 1, found
    return found[0]


def test_a_node_local_tmpdir_is_reported_ok() -> None:
    f = _one("/scratch/local", LOCAL, "ext4 (local)")
    assert f.severity == "ok"
    assert f.check == "state.tmpdir"
    assert "/scratch/local" in f.detail and "node-local" in f.detail


def test_a_network_tmpdir_warns_and_says_what_breaks() -> None:
    """Not "this is unusual" — WHICH thing fails and what to do."""
    f = _one("/gpfs/tmp", NETWORK, "gpfs (network)")
    assert f.severity == "warn"
    assert "/gpfs/tmp" in f.detail and "NETWORK" in f.detail
    low = f.remediation.lower()
    assert "unix socket" in low, "does not name the mechanism that fails"
    assert "broker" in low, "does not say which feature is affected"
    assert "tmpdir" in low, "does not say what to change"


def test_an_unrecognised_filesystem_is_UNKNOWN_not_assumed_local() -> None:
    """fs_kind's whole design is that the ambiguous case takes the cheap
    mistake. A doctor line that quietly upgraded unknown to ok would undo it."""
    f = _one("/weird", UNKNOWN, "somefs (unknown)")
    assert f.severity == "info"
    assert "could not tell" in f.detail
    assert "node-local" not in f.detail


def test_an_unset_TMPDIR_says_which_path_it_actually_checked() -> None:
    """"(unset)" alone would leave the reader unable to act. /tmp is what gets
    used, so /tmp is what the line has to name."""
    f = _one("", LOCAL, "overlay (local)")
    assert "unset" in f.detail and "/tmp" in f.detail


@pytest.mark.parametrize("kind,expected", [
    (LOCAL, "ok"), (NETWORK, "warn"), (UNKNOWN, "info")])
def test_every_verdict_maps_to_exactly_one_status(kind, expected) -> None:
    """The literal-vs-constant bug made all three collapse to `info`. This is
    the shape that catches it: drive the same constants the caller passes and
    require the three to be DISTINGUISHED."""
    assert _one("/x", kind, "fs (x)").severity == expected
