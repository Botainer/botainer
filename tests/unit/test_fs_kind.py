"""Filesystem classification, and the tri-state that is the whole point.

BACKGROUND (#146). codex keeps four SQLite databases in WAL mode and re-sets WAL
on every open, so where those files LIVE decides whether they work. Nothing in
botainer knew what a path was sitting on: `botainer where` walked st_dev for the
mount POINT and never asked its TYPE, so a user on GPFS and a user on an SSD got
identical output and only one of them was heading for corruption.

THE PROPERTY THESE TESTS DEFEND is the tri-state. An unrecognised filesystem must
classify as UNKNOWN and never as LOCAL, because the two mistakes cost different
amounts: wrongly "local" means we stay silent while a database corrupts; wrongly
"unknown" means one extra line of output. A future contributor tidying this into
a bool is the regression to catch.
"""
from __future__ import annotations

import pytest

from botainer.state import fs_kind


# ── the tri-state, which is the design ───────────────────────────────────────

def test_an_unrecognised_filesystem_is_unknown_not_local(monkeypatch) -> None:
    """The load-bearing one. Sites run parallel filesystems this module has
    never heard of; guessing "local" for them is the silent-corruption path."""
    monkeypatch.setattr(fs_kind, "_mount_table",
                        lambda: [("/", "somefsnobodyhasheardof")])
    assert fs_kind.classify("/anything") == fs_kind.UNKNOWN
    # The WAL assertion that used to sit here has moved to the probe tests
    # above. It passed for a coincidental reason once the probe landed —
    # `/anything` does not exist, so the answer was None because there was
    # nowhere to write, not because the filesystem was unrecognised. A test
    # that would pass with the feature deleted is not evidence for it.


def test_wal_safety_is_probed_not_inferred_from_the_mount_type(monkeypatch, tmp_path) -> None:
    """The answer must NOT move when the filesystem type does.

    This function used to classify the type and answer from it — network means
    unsafe, local means safe. That was a guess about the thing a wrong guess
    corrupts: whether WAL works depends on the server, the mount options and the
    client build, not on the string in /proc/mounts. Now it opens a database and
    finds out, so lying about the type changes nothing.
    """
    answers = set()
    for fstype in ("ext4", "lustre", "gpfs", "weird"):
        monkeypatch.setattr(fs_kind, "_mount_table", lambda t=fstype: [("/", t)])
        answers.add(fs_kind.sqlite_wal_is_safe(tmp_path))
    assert len(answers) == 1, (
        f"the probe's answer moved with the faked mount type: {answers} — "
        "it is inferring again"
    )


def test_wal_probe_says_yes_on_a_filesystem_that_really_supports_it(tmp_path) -> None:
    assert fs_kind.sqlite_wal_is_safe(tmp_path) is True


def test_wal_probe_leaves_nothing_behind(tmp_path) -> None:
    fs_kind.sqlite_wal_is_safe(tmp_path)
    assert list(tmp_path.iterdir()) == [], "a diagnostic must not litter the state root"


def test_wal_probe_is_tri_state_and_unknown_is_not_optimism(tmp_path) -> None:
    # None means "could not run the probe", never "probably fine". A caller that
    # refuses on False must not be silently handed True for an untested path.
    assert fs_kind.sqlite_wal_is_safe(tmp_path / "does-not-exist") is None

    unwritable = tmp_path / "ro"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    try:
        assert fs_kind.sqlite_wal_is_safe(unwritable) is None
    finally:
        unwritable.chmod(0o700)


def test_wal_probe_never_raises(monkeypatch, tmp_path) -> None:
    import sqlite3

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sqlite3, "connect", boom)
    # An I/O error IS the finding — SQLITE_IOERR_SHMOPEN is exactly what a
    # filesystem with no shared-memory support raises — so it reports False
    # rather than propagating and taking `doctor` down with it.
    assert fs_kind.sqlite_wal_is_safe(tmp_path) is False


# ── classification ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("fstype", [
    "nfs", "nfs4", "lustre", "gpfs", "beegfs", "cephfs", "smb3", "afs", "9p",
])
def test_known_network_filesystems_are_network(monkeypatch, fstype) -> None:
    monkeypatch.setattr(fs_kind, "_mount_table", lambda: [("/", fstype)])
    assert fs_kind.classify("/x") == fs_kind.NETWORK
    # NB no WAL assertion here any more. Classification still describes the
    # filesystem for `botainer where`; it no longer DECIDES whether WAL works,
    # because that is probed. Keeping the two coupled is what made the old
    # answer a guess.


@pytest.mark.parametrize("fstype", ["ext4", "xfs", "btrfs", "apfs", "tmpfs", "overlay"])
def test_known_local_filesystems_are_local(monkeypatch, fstype) -> None:
    monkeypatch.setattr(fs_kind, "_mount_table", lambda: [("/", fstype)])
    assert fs_kind.classify("/x") == fs_kind.LOCAL


def test_fuse_transports_are_network_but_fuseblk_is_not(monkeypatch) -> None:
    """sshfs/s3fs are network wearing a local-looking name; `fuseblk` is a
    genuinely local NTFS/exFAT mount and must not be swept up with them."""
    monkeypatch.setattr(fs_kind, "_mount_table", lambda: [("/", "fuse.sshfs")])
    assert fs_kind.classify("/x") == fs_kind.NETWORK
    monkeypatch.setattr(fs_kind, "_mount_table", lambda: [("/", "fuseblk")])
    assert fs_kind.classify("/x") == fs_kind.LOCAL


def test_case_is_not_significant(monkeypatch) -> None:
    monkeypatch.setattr(fs_kind, "_mount_table", lambda: [("/", "LUSTRE")])
    assert fs_kind.classify("/x") == fs_kind.NETWORK


# ── mount-point resolution ───────────────────────────────────────────────────

def _table(*rows):
    """Mount table in the order the real one is produced: longest point first."""
    return sorted(rows, key=lambda r: len(r[0]), reverse=True)


def test_the_longest_matching_mount_point_wins(monkeypatch, tmp_path) -> None:
    """`/` and `<tmp>/scratch` both contain `<tmp>/scratch/x`; only the longer
    is the filesystem it is actually on. Backwards, every path reports as
    whatever `/` is.

    The mount points here EXIST on disk. An earlier version of this test
    declared a mount at `/scratch` on a machine with no `/scratch`, and
    filesystem_type correctly walked up to the nearest existing ancestor and
    answered for `/` — so the test failed for a reason that had nothing to do
    with longest-match. A real mount point is always a real directory; the
    fixture has to be too.
    """
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(fs_kind, "_mount_table",
                        lambda: _table(("/", "ext4"), (str(scratch), "lustre")))
    assert fs_kind.filesystem_type(scratch) == "lustre"
    assert fs_kind.filesystem_type(tmp_path) == "ext4"


def test_a_prefix_that_is_not_a_path_component_does_not_match(
        monkeypatch, tmp_path) -> None:
    """`<tmp>/scratchpad` must NOT match the `<tmp>/scratch` mount. A naive
    startswith puts it on lustre and reports the wrong answer confidently —
    and for the codex guard that is the difference between a warning and
    silence."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    pad = tmp_path / "scratchpad"
    pad.mkdir()
    monkeypatch.setattr(fs_kind, "_mount_table",
                        lambda: _table(("/", "ext4"), (str(scratch), "lustre")))
    assert fs_kind.filesystem_type(pad) == "ext4"


def test_a_path_that_does_not_exist_yet_uses_its_nearest_ancestor(tmp_path) -> None:
    """Callers ask about directories botainer is ABOUT to create. "Does not
    exist yet" must not degrade into "unknown filesystem"."""
    future = tmp_path / "not" / "yet" / "created"
    assert fs_kind.filesystem_type(future) == fs_kind.filesystem_type(tmp_path)
    assert fs_kind.classify(future) == fs_kind.classify(tmp_path)


# ── against the real host, whatever it is ────────────────────────────────────

def test_it_answers_for_this_machine_without_raising(tmp_path) -> None:
    """No assertion about WHICH filesystem — this runs on a dev container, a
    Mac and CI. The claim is only that it returns a legal value and does not
    explode, which is what a start-time check needs."""
    assert fs_kind.classify(tmp_path) in (
        fs_kind.NETWORK, fs_kind.LOCAL, fs_kind.UNKNOWN)
    assert isinstance(fs_kind.describe(tmp_path), str)
    assert fs_kind.describe(tmp_path)


def test_describe_names_both_the_type_and_the_verdict(monkeypatch) -> None:
    monkeypatch.setattr(fs_kind, "_mount_table", lambda: [("/", "gpfs")])
    assert fs_kind.describe("/x") == "gpfs (network)"


# ── case sensitivity: probed, because the type cannot answer it ──────────────
#
# MEASURED on this dev container's Mac-hosted bind, 2026-08-31:
#     write Foo.py "first", then foo.py "second"
#     -> ONE file, named Foo.py, containing "second"
# A silent clobber, not an error. /packages sits on the same mount, so conda,
# pip and npm installs land there. The same tree on a cluster (ext4/GPFS) keeps
# both files — the dev scaffold and the production platform disagree in silence.


def test_the_probe_answers_for_this_machine(tmp_path) -> None:
    """No assertion about WHICH — this runs on Linux CI, a Mac, and a bind. The
    claim is that it returns a real answer rather than raising or guessing."""
    assert fs_kind.is_case_insensitive(tmp_path) in (True, False)


def test_the_probe_cleans_up_after_itself(tmp_path) -> None:
    """It WRITES. A detection function that leaves litter in a user's package
    root would be its own defect."""
    before = sorted(p.name for p in tmp_path.iterdir())
    fs_kind.is_case_insensitive(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_an_unwritable_path_is_none_not_false(tmp_path) -> None:
    """Same tri-state discipline as the network/local classification: "I could
    not test" must not collapse into "it is fine". A False here would tell a
    caller the path is safe for case-colliding installs on no evidence."""
    assert fs_kind.is_case_insensitive("/proc/sys") is None
    assert fs_kind.is_case_insensitive(tmp_path / "does-not-exist") is None


def test_it_is_probed_not_inferred_from_the_type(monkeypatch, tmp_path) -> None:
    """The filesystem TYPE must not drive this answer.

    APFS ships case-insensitive by default but can be formatted case-sensitive,
    and a Docker bind inherits the host volume's behaviour — so any answer
    derived from the type name is a guess about the exact thing a wrong guess
    corrupts. Faking the mount table to something absurd must not move it.
    """
    real = fs_kind.is_case_insensitive(tmp_path)
    monkeypatch.setattr(fs_kind, "_mount_table", lambda: [("/", "apfs")])
    assert fs_kind.is_case_insensitive(tmp_path) is real
    monkeypatch.setattr(fs_kind, "_mount_table", lambda: [("/", "lustre")])
    assert fs_kind.is_case_insensitive(tmp_path) is real
