"""`is_executable` must not be fooled the way `os.access` is.

MEASURED, AND THE DIVERGENCE FOLLOWS THE MOUNT. A 0o644 file at euid 1000
returns **True** from `os.access(X_OK)` under `/workspace` (mounted
`fakeowner`) and **False** under `/tmp` (an `overlay`). So `/workspace` — where
this repo's files live, including every hook and dev check — is the lying side,
and a `tmp_path` fixture does NOT reproduce the hazard. That is precisely why
my first exec-bit test passed while its target was 644: it was checking a file
in the repo tree, where `os.access` is wrong.

Both are covered below: the `tmp_path` tests pin the helper's contract, and one
test probes the REPO's own filesystem, which is where the real risk is.

WHY IT MATTERS. Four production call sites use this check for one purpose:
refuse EARLY with a sentence the user can act on, instead of letting `exec`
fail with `bad interpreter: Permission denied` buried in hook output. When the
check says "fine" for a 0o644 script, the refusal never fires and the user gets
exactly the raw error it exists to prevent — the codex-broker-hooks defect
(#118), which recurred this session with `check-handover-commands.py`.

This project had already met the divergence twice — once in a hook-integrity
test that asserts the mode via git for exactly this reason, and once in a dev
check recording "reports 0644 while os.access(X_OK) says true". Both wrote it
down as a local comment, and I still wrote an exec-bit test with `os.access`
that passed while its target was 644. Hence a shared helper with its own test,
rather than a third comment saying "careful".
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from botainer.core import exec_bit


@pytest.fixture
def script(tmp_path) -> Path:
    p = tmp_path / "hook.sh"
    p.write_text("#!/bin/sh\necho hi\n")
    return p


def test_a_644_file_is_NOT_executable(script) -> None:
    """THE DEFECT, stated as the property that matters.

    This is the case that actually happens: a script committed without the
    bit, which git reproduces faithfully in every clone and in the export.
    """
    script.chmod(0o644)

    assert not exec_bit.is_executable(script), (
        f"a 0o644 file was reported executable "
        f"(os.access says {os.access(script, os.X_OK)} on this filesystem)")


def test_the_MODE_BIT_half_is_load_bearing_on_ANY_filesystem(
        script, monkeypatch) -> None:
    """Asserts the CONJUNCTION, so this is not vacuous off the lying mount.

    THE PREVIOUS VERSION WAS. It compared `is_executable` to `os.access` and
    SKIPPED when they agreed — which is always, on any filesystem that enforces
    the bit. The loop tzar measured it: with `is_executable` gutted to a plain
    `os.access` call, this file reported "7 passed, 1 skipped" on /tmp, byte
    for byte what correct code reports. Its docstring claimed it "stays honest
    on a developer's laptop or in CI". It did not; it stayed silent there.

    Forcing `os.access` to True reproduces the `fakeowner` behaviour ANYWHERE,
    so the mode-bit half must carry the answer alone — which is exactly the
    property the helper exists for, and it is now checked everywhere the suite
    runs rather than only where the filesystem happens to lie.
    """
    script.chmod(0o644)
    monkeypatch.setattr(exec_bit.os, "access", lambda *a, **k: True)

    assert not exec_bit.is_executable(script), (
        "with os.access forced True, is_executable still said yes for a 0o644 "
        "file — the mode-bit half is not being consulted, so the helper is a "
        "synonym for os.access")


def test_the_OS_ACCESS_half_is_also_load_bearing(script, monkeypatch) -> None:
    """The mirror. A 0o755 file the kernel refuses must still be refused.

    Without this the helper could drop `os.access` entirely and pass every
    other test here — losing the noexec-mount and ACL cases, which are the ones
    that actually occur on a cluster.
    """
    script.chmod(0o755)
    monkeypatch.setattr(exec_bit.os, "access", lambda *a, **k: False)

    assert not exec_bit.is_executable(script), (
        "is_executable ignored a kernel refusal on a mode-755 file — the "
        "os.access half is not being consulted")


def test_a_755_file_IS_executable(script) -> None:
    """The control. Without it the helper could return False always."""
    script.chmod(0o755)
    assert exec_bit.is_executable(script)


def test_a_missing_file_is_not_executable(tmp_path) -> None:
    """`stat` raises; the answer is False, not a traceback out of a refusal."""
    assert not exec_bit.is_executable(tmp_path / "nope")


def test_the_reason_names_chmod_for_a_missing_mode_bit(script) -> None:
    """The two failure modes need DIFFERENT advice, and naming the wrong fix
    wastes the reader's time as surely as saying nothing."""
    script.chmod(0o644)

    why = exec_bit.why_not_executable(script)

    assert "not marked executable" in why, why
    assert "chmod +x" in why, why
    assert "0o644" in why, why


def test_the_reason_is_EMPTY_when_the_file_is_fine(script) -> None:
    """Or callers would append a reason to a refusal that never fires."""
    script.chmod(0o755)
    assert exec_bit.why_not_executable(script) == ""


def test_the_reason_does_not_say_chmod_when_chmod_would_not_help(
        script, monkeypatch) -> None:
    """A marked-executable file the kernel still refuses is a noexec mount or
    an ACL. Telling someone to `chmod +x` a file that is already 0o755 sends
    them in a circle."""
    script.chmod(0o755)
    monkeypatch.setattr(exec_bit.os, "access", lambda *a, **k: False)

    why = exec_bit.why_not_executable(script)

    assert "noexec" in why, why
    assert "chmod +x" not in why, why


def test_on_the_REPO_filesystem_where_os_access_actually_lies() -> None:
    """The hazard lives under /workspace, not under tmp_path.

    Uses a file already in the tree (read-only; nothing is created or chmod'd)
    so the probe runs on the `fakeowner` mount where `os.access` is wrong. If
    it is wrong here, `is_executable` must still say False — that divergence is
    the entire reason this helper exists.

    SKIPS rather than fails when the repo filesystem behaves normally, so this
    stays honest on a developer's laptop or in CI.
    """
    repo = Path(__file__).resolve().parents[2]
    candidate = repo / "pyproject.toml"
    if not candidate.is_file():
        pytest.skip("no stable non-executable file to probe")

    mode = candidate.stat().st_mode & 0o777
    if mode & 0o111:
        pytest.skip(f"{candidate.name} is executable here (mode {oct(mode)})")

    if os.access(candidate, os.X_OK):
        assert not exec_bit.is_executable(candidate), (
            f"os.access lies about {candidate} on this mount and is_executable "
            f"repeats the lie — the helper adds nothing where it matters most")
    else:
        assert not exec_bit.is_executable(candidate)


# ── a DIRECTORY is not a runnable script ───────────────────────────────────

def test_a_directory_is_not_executable(tmp_path):
    """`0o40755 & 0o111` is truthy and `os.access(dir, X_OK)` means traversable.

    So a directory passed BOTH halves of this module's conjunction and was
    reported runnable. Measured before the fix:
    `adir lstat=0o40755 is_executable=True why=''`.

    For a non-`.py` hook path that became an uncaught PermissionError out of
    `run_hook` — a traceback from the module whose entire stated purpose is to
    replace `bad interpreter: Permission denied` with a sentence the user can
    act on.
    """
    from botainer.core.exec_bit import is_executable, why_not_executable
    d = tmp_path / "adir"
    d.mkdir(mode=0o755)

    assert is_executable(d) is False, (
        "a directory was reported runnable; both halves of the conjunction "
        "say yes for 0o40755")
    assert "directory" in why_not_executable(d), (
        f"and the reason was empty, so a caller had a refusal with nothing in "
        f"it: {why_not_executable(d)!r}")


def test_a_symlink_TO_a_directory_is_not_executable(tmp_path):
    """`is_file()` follows symlinks, which is the question the caller is asking.

    The caller is about to exec the TARGET, not the link, so resolving is
    correct here — and a symlink to a directory was the second spelling of the
    same false positive.
    """
    from botainer.core.exec_bit import is_executable, why_not_executable
    d = tmp_path / "adir"
    d.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(d, target_is_directory=True)

    assert is_executable(link) is False
    why = why_not_executable(link)
    assert "directory" in why, why
    assert "symlink" in why, (
        f"the reason does not say it is a symlink, so the reader cannot tell "
        f"WHICH path to look at: {why!r}")


def test_a_mode_ZERO_file_is_told_to_add_read_as_well(tmp_path):
    """`chmod +x` on a 0o000 file leaves it unreadable and failing again.

    A refusal that names an insufficient fix costs the reader a second
    round-trip, which is the cost this module exists to remove.
    """
    from botainer.core.exec_bit import why_not_executable
    f = tmp_path / "mode0"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o000)

    why = why_not_executable(f)

    assert "+rx" in why, (
        f"told the user `chmod +x` for a file that is not readable either: "
        f"{why!r}")
    assert "not readable either" in why, why


def test_an_unmarked_file_is_also_told_about_the_mount(tmp_path):
    """The mode branch is tested FIRST, so it used to hide the second cause.

    A file that is both unmarked AND on a `noexec` mount got only the chmod
    advice, followed it, and failed identically. The two cannot be
    distinguished while the bit is missing — so the message names both rather
    than asserting the one it happened to check first.
    """
    from botainer.core.exec_bit import why_not_executable
    f = tmp_path / "plain"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o644)

    why = why_not_executable(f)

    assert "chmod +x" in why, why
    assert "mount" in why, (
        f"names only the mode, so a reader on a noexec mount follows the "
        f"advice and hits the same wall: {why!r}")


def test_a_REAL_executable_still_passes_and_says_nothing(tmp_path):
    """THE CONTROL. Without it, every fix above could be "always refuse".

    `is_executable` gates four call sites that refuse a launch. A version that
    returns False for a genuinely runnable script would break every hook in the
    product, and each assertion above would still pass.
    """
    from botainer.core.exec_bit import is_executable, why_not_executable
    f = tmp_path / "ok"
    f.write_text("#!/bin/sh\necho hi\n")
    f.chmod(0o755)

    assert is_executable(f) is True
    assert why_not_executable(f) == "", why_not_executable(f)
