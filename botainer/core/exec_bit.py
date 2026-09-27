"""Is this script actually runnable?

`os.access(path, os.X_OK)` IS NOT A RELIABLE ANSWER, and this repo has now
learned that twice.

MEASURED, AND IT IS THE FILESYSTEM, NOT THE CONTAINER. A 0o644 file at euid
1000 returns **True** from `os.access(..., X_OK)` under `/workspace`, which is
mounted `fakeowner`; the same probe under `/tmp` (an `overlay` mount) correctly
returns False. So the divergence follows the MOUNT, and `/workspace` — where
this repo's own files live, including every hook and dev check — is the side
that lies. A test using `tmp_path` will not reproduce it; one touching the repo
tree will.

This project had already met the divergence twice before this helper existed —
once in a hook-integrity test that asserts the mode via git rather than via
`os.access` for exactly this reason, and once in a dev check that records
"reports 0644 while os.access(X_OK) says true". Both wrote it down as a local
comment. An exec-bit test was then written with `os.access` anyway, and it
passed while its target was 0o644 — vacuous, and caught only by mutating the
file on purpose. Hence one shared helper with its own test, rather than a third
comment saying "careful".

WHY THAT MATTERS HERE RATHER THAN BEING TRIVIA. Four call sites use this check
for one purpose: refuse EARLY with a sentence the user can act on, instead of
letting `exec` fail with `bad interpreter: Permission denied` buried in output.
If the check says "fine" for a 0o644 file, the refusal never fires and the user
gets exactly the raw error the check exists to prevent — which is the
codex-broker-hooks-shipped-non-executable defect (task #118), and which
happened again to `check-handover-commands.py` this session.

THE TWO QUESTIONS ARE DIFFERENT, so ask both:

  * `st_mode & 0o111` — "is this file MARKED executable". Catches the case that
    actually occurs: a script committed without the bit, which git faithfully
    reproduces in every clone and in the export.
  * `os.access(..., X_OK)` — "would the kernel let ME run it". Catches ACLs, a
    `noexec` mount, and being a different user than the owner. Real causes this
    repo will meet on a cluster.

A file must pass BOTH to be runnable. Neither alone is sufficient, and the
mode-bit half is the one that was missing.
"""
from __future__ import annotations

import os
from pathlib import Path


def is_executable(path: Path | str) -> bool:
    """True only if the file is marked executable AND this user may run it.

    Deliberately conjunctive — see the module docstring. `os.access` alone
    returns True for a 0o644 file on filesystems that do not enforce the bit,
    which is the blind spot that let non-executable hooks ship.
    """
    p = Path(path)
    try:
        # A DIRECTORY PASSES BOTH HALVES. `0o40755 & 0o111` is truthy and
        # `os.access(dir, X_OK)` means "traversable", so a directory — and a
        # symlink to one — was reported runnable. Measured:
        # `adir lstat=0o40755 is_executable=True why=''`. For a non-`.py` hook
        # path that became an uncaught PermissionError out of `run_hook`: a
        # traceback from the module whose entire purpose is an actionable
        # refusal, which is the defect class it was written to end.
        #
        # `is_file()` FOLLOWS symlinks, which is what the question needs: the
        # caller is about to exec the target, not the link.
        if not p.is_file():
            return False
        marked = bool(p.stat().st_mode & 0o111)
    except OSError:
        return False
    return marked and os.access(p, os.X_OK)


def why_not_executable(path: Path | str) -> str:
    """A short reason suitable for appending to a refusal, or "" if runnable.

    The two failure modes need DIFFERENT advice: a missing mode bit is fixed
    with `chmod +x` (and, in this repo, re-staging so git records 100755), while
    a kernel refusal on an already-marked file is usually a noexec mount or an
    ACL and `chmod` will not help. A refusal that names the wrong fix wastes
    the reader's time as surely as no refusal at all.
    """
    p = Path(path)
    try:
        mode = p.stat().st_mode
    except OSError as exc:
        return f"cannot stat it ({exc.strerror or exc})"
    # NOT A REGULAR FILE, FIRST. This returned "" for a directory — "runnable,
    # no complaint" — so `is_executable` saying False and this saying nothing
    # disagreed, and the caller had a refusal with no reason in it.
    if not p.is_file():
        kind = "a directory" if p.is_dir() else "not a regular file"
        extra = ""
        if p.is_symlink():
            try:
                extra = f" (symlink → {os.readlink(p)})"
            except OSError:
                extra = " (symlink)"
        return (f"it is {kind}{extra}, so there is nothing to execute. "
                f"Check the path: {p}")
    if not mode & 0o111:
        # NAME EVERY BIT THAT IS MISSING. A mode-0 file was told `chmod +x`,
        # which leaves it unreadable and failing again — the second round-trip
        # this module exists to avoid.
        need = "+x" if os.access(p, os.R_OK) else "+rx"
        hint = (f"Fix: chmod {need} {p}")
        if need == "+rx":
            hint += " (it is not readable either, so +x alone would fail again)"
        # AND DO NOT HIDE THE SECOND CAUSE. The mode branch is tested first, so
        # a file that is BOTH unmarked AND on a `noexec` mount used to get only
        # the chmod advice and then fail identically after following it. We
        # cannot distinguish the two while the bit is missing, so say both
        # rather than guessing.
        return (f"it is not marked executable (mode {oct(mode & 0o777)}). "
                f"{hint}. If it still refuses after that, the cause is the "
                f"mount or an ACL rather than the mode — see below.")
    if not os.access(p, os.X_OK):
        return ("it is marked executable but this user cannot run it — "
                "usually a `noexec` mount or an ACL, which `chmod` will not fix")
    return ""
