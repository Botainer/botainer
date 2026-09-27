"""Detect single-file bind mounts severed by host-side file replacement.

Atomic replacement can leave a running container bound to the old inode.
The path may fail stat/read operations while mountinfo still lists the bind
with a //deleted suffix. The parser accepts supplied text so this condition
can be tested without replacing real host configuration.
"""
from __future__ import annotations

from botainer.cli.doctor import stale_bind_findings
from botainer.state.fs_kind import read_stale_binds, stale_binds

# Anonymized mountinfo fixtures preserve the kernel's suffix and field layout.
# Account paths and filesystem labels carry no private values.
OBSERVED = (
    "376 353 0:46 /hostuser/.gitconfig//deleted /home/user/.gitconfig "
    "ro,nosuid,nodev,relatime - fakeowner /run/host_mark/Users rw"
)
HEALTHY = (
    "375 353 0:46 /hostuser/.gitconfig /home/user/.gitconfig "
    "ro,nosuid,nodev,relatime - fakeowner /run/host_mark/Users rw"
)


def test_the_line_that_actually_broke_git_is_detected() -> None:
    assert stale_binds(OBSERVED) == [("/hostuser/.gitconfig", "/home/user/.gitconfig")]


def test_the_same_bind_when_healthy_is_not_flagged() -> None:
    """One character of difference decides it. A check that flagged both would
    fire on every session and be ignored within a day."""
    assert stale_binds(HEALTHY) == []


def test_a_path_that_merely_CONTAINS_deleted_is_not_flagged() -> None:
    """`//deleted` is a kernel-appended SUFFIX, not a word to search for. Found
    by mutation: relaxing the check to `"deleted" in root` left every test
    green, and would have flagged anyone whose directory is called `deleted/`
    or `deleted-backups/`. A false ERROR on a healthy session is worse than no
    check — it is the warn-that-always-fires shape."""
    innocent = "\n".join([
        "1 2 0:1 /home/user/deleted /mnt/a rw - ext4 /dev/sda rw",
        "2 2 0:1 /home/user/deleted-files/config /mnt/b rw - ext4 /dev/sda rw",
        "3 2 0:1 /srv/deleted//kept /mnt/c rw - ext4 /dev/sda rw",
    ])
    assert stale_binds(innocent) == []


def test_optional_fields_do_not_shift_the_parse() -> None:
    """mountinfo has a VARIABLE number of optional fields before the ` - `
    separator. Counting from the right, or splitting naively, breaks on any
    mount that has propagation flags — which shared mounts do."""
    with_opts = (
        "376 353 0:46 /src//deleted /dst rw,relatime shared:12 master:9 "
        "- ext4 /dev/sda1 rw"
    )
    assert stale_binds(with_opts) == [("/src", "/dst")]


def test_a_deleted_ordinary_mount_source_is_reported_too() -> None:
    """Not only file binds. If the kernel says //deleted, something is severed;
    narrowing to paths that look like files would be guessing at the cause."""
    many = "\n".join([HEALTHY, OBSERVED,
                      "377 353 0:46 /a/dir//deleted /mnt rw - ext4 /dev/sdb rw"])
    assert stale_binds(many) == [
        ("/hostuser/.gitconfig", "/home/user/.gitconfig"), ("/a/dir", "/mnt")]


def test_junk_lines_are_skipped_not_crashed_on() -> None:
    """This runs inside `doctor`. A malformed line must not take the command
    down — doctor's whole job is to still report when something is wrong."""
    assert stale_binds("") == []
    assert stale_binds("garbage\n\n1 2 3\n" + OBSERVED) == [
        ("/hostuser/.gitconfig", "/home/user/.gitconfig")]


def test_read_stale_binds_returns_None_off_linux_not_an_empty_list(
        monkeypatch, tmp_path) -> None:
    """THREE STATES. "cannot tell" must never render as "clean" — the defect
    this repo keeps finding. On a Mac there is no /proc/self/mountinfo."""
    import botainer.state.fs_kind as fk
    monkeypatch.setattr(fk, "Path", lambda *a, **k: tmp_path / "nope")
    assert read_stale_binds() is None


# ── what the user is actually shown ───────────────────────────────────────

def test_a_severed_bind_is_an_ERROR_that_names_both_paths() -> None:
    (f,) = stale_bind_findings([("/hostuser/.gitconfig", "/home/user/.gitconfig")])
    assert f.severity == "err"
    assert "/hostuser/.gitconfig" in f.detail and "/home/user/.gitconfig" in f.detail


def test_the_remedy_says_it_is_HOST_side_and_that_restarting_is_the_fix() -> None:
    """The symptom names no file and no mount; the remedy has to do all the
    work. It must also say WHERE — there is no in-container repair, and the
    reader is by definition looking at a broken shell."""
    (f,) = stale_bind_findings([("/a", "/b")])
    low = f.remediation.lower()
    # NOT just `"host" in low` — that was the first version, and mutation showed
    # it passes on the DIAGNOSIS ("something on the host replaced the file")
    # while the instruction loses its WHERE. The maintainer's standing rule is
    # that a command without a place to run it is useless, and here the reader
    # is by definition sitting in the broken shell.
    assert "restart" in low, "does not name the actual fix"
    # THE SENTENCE THAT GIVES THE INSTRUCTION must name the side. Two weaker
    # versions of this assertion passed under mutation: `"host" in low` and
    # `"on the host" in low` both match the DIAGNOSIS ("something on the host
    # replaced the file") while the instruction loses its WHERE entirely.
    # Locating the sentence is what makes it test the property instead of the
    # vocabulary.
    fix_sentence = next(
        (part for part in low.replace("\n", " ").split(".") if "restart" in part),
        "")
    assert "host" in fix_sentence, (
        f"the instruction does not say WHICH SIDE to run it on: {fix_sentence!r}"
        " — a command without a place to run it is useless, and the reader is "
        "by definition sitting in the broken shell")
    assert "no in-container repair" in low or "not in here" in low


def test_a_clean_mount_table_says_so_positively() -> None:
    (f,) = stale_bind_findings([])
    assert f.severity == "ok" and "no bind" in f.detail


def test_off_linux_reports_NOT_CHECKED_rather_than_ok() -> None:
    (f,) = stale_bind_findings(None)
    assert f.severity == "info"
    assert "not checked" in f.detail
    assert f.severity != "ok", "cannot-tell must not render as an all-clear"
