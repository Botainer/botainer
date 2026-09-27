"""Carry an agent's session history across a mode or profile switch.

WHY THIS EXISTS
---------------
Every auth mode binds the same container path (``/home/agent/.claude``,
``/home/agent/.codex``) from a DIFFERENT host directory, and the profile name is
a path component in all of them:

    isolated / shared   state/<uid>/data/agent-claude/profiles/<profile>/
    broker              state/<uid>/data/agent-claude/broker-state/<profile>/

That directory is not just the credential. It is also every transcript, todo
list, user-level subagent, MCP server entry and preference the agent has
accumulated. So switching auth mode, or switching profile, silently swapped the
agent's history directory. Without disclosure, an empty destination can be
mistaken for missing or lost transcripts.

The separation itself is deliberate and stays: broker mode's directory must hold
NO credential, which is the entire point of broker mode. What was wrong is that
HISTORY followed a SECURITY setting. This module keeps directories separate and provides a carry operation when
the user makes a persistent selection change.

WHAT TRAVELS, AND WHAT DOES NOT
-------------------------------
The directory holds three different kinds of thing, and only the split between
them is interesting — the "history vs config" question answers itself once they
are named:

===============  ==========================================================
credential       never carried
account state    never carried
everything else  carried: transcripts, todos, the user's own subagents,
                 ``settings.json``, per-project MCP servers, permissions
===============  ==========================================================

**Config travels with history, and that is deliberate.** The state directory is
already per-PROJECT, so a switch moves between two slots of the same project.
Nothing in ``settings.json``, ``agents/`` or the MCP entries is a function of how
the credential is delivered, so leaving them behind would be arbitrary — the user
would keep their transcripts and lose their tools.

WHAT IT WILL NOT DO
-------------------
Four properties, each structural rather than a check that has to stay correct:

0. **Account state is never carried.** ``claude.json`` mixes account identity
   (``oauthAccount``, ``userID``, an org-level flag and six entitlement caches)
   with the project config worth keeping, so that file is REDUCED on the way:
   :func:`reduce_account_state` selects the project keys rather than removing
   the account ones, which makes an identity key unrepresentable in the output
   instead of merely absent from a list someone has to maintain.

   This is not tidiness. The credential is not carried either, so the
   destination must log in regardless; copying an account descriptor without its
   token would produce a directory that NAMES an account it cannot authenticate
   as. ``docs/CAPABILITY-SURFACE.md`` §4cm settles the rest: a profile IS an
   account boundary, and there is no sound same-account signal on disk — not
   even between two directories with the same profile name.

1. **A credential is never carried.** Not across modes, not across profiles.
   Carrying one into ``broker-state/`` would put a credential in the one
   directory whose contract says it holds none; carrying one out of shared mode
   would fork a token that rotates. The names come from
   :data:`CREDENTIAL_FILENAMES`, pinned against the auth CLI's own registry by
   ``tests/unit/test_history_carry.py`` so the two cannot drift apart.

2. **A symlink is never carried, at any depth.** This is load-bearing, not
   tidiness. Shared mode works by placing a ``.credentials.json`` SYMLINK in the
   per-project dir pointing into the host-wide store; copying that link into an
   isolated or broker directory would silently re-share the credential while
   every surface still called the mode isolated. Refusing symlinks outright
   means the shared-mode mechanism cannot be smuggled anywhere by a copy, and it
   also means no copy can be redirected outside the destination tree. Nothing in
   the genuine history set is a symlink.

3. **Nothing is ever deleted or overwritten.** The carry only ever ADDS files
   that are not already at the destination. When both sides hold history it
   refuses and hands the decision back, and the way out is :func:`archive_dir`,
   which renames — the bytes stay on disk under a name that says when they were
   set aside.

   This holds even though the carry is a MOVE. :func:`supersede_carried` takes
   the source's copies out of the way by RENAMING them into a timestamped
   sibling, never by unlinking, and only ever the files that verifiably landed
   at the destination.

4. **The carry is a move, and that is a correctness property rather than
   tidiness.** With a copy, switching A->B, working in B and switching back
   leaves two populated directories and no sound way to say which is current —
   only a modification time and the first line of a transcript, i.e. a
   judgement the product would be making the user perform. Moving leaves one
   live copy, so the question cannot be asked. The CREDENTIAL stays behind,
   which is what lets someone switch away and back without signing in again.

The cost of (0), (1) and (2) is that the destination needs its own sign-in,
which is correct: a switch is a credential change. The user signs in once on the
other side and their history is already there.

WHAT THIS IS NOT
----------------
This module exists because the auth mode and the profile are PATH COMPONENTS of
the agent's config directory. v0.0.x had one directory per project and no mode
in the path, so nothing ever had to be carried. Restoring that shape — one
config dir per project, with the mode deciding only how the credential gets into
it — would delete this module outright. That is recorded as the better fix and
has not been chosen; see the internal design note on the carry's open questions.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Credential filenames, per agent family. NEVER carried.
#
# This duplicates knowledge that `botainer.cli.auth` already has
# (`_family_creds_filename` / `_family_isolated_creds_filenames`). The
# duplication is deliberate and guarded: importing the CLI from core would
# invert the layering, so instead the union is pinned by a test that reads the
# auth module's own registries. If auth learns a new credential name and this
# set does not, the test fails — which is the failure we want, because the
# alternative is a credential quietly riding along in a carry.
CREDENTIAL_FILENAMES: frozenset[str] = frozenset({
    ".credentials.json",   # Claude OAuth
    "auth.json",           # codex OAuth (shared mode)
    "api_key",             # codex key-paste (isolated mode)
})


def credential_files_under(root):
    """Every credential-NAMED file anywhere under `root`, recursively.

    ONE implementation, because two callers want this and two copies drift.
    `auth status` and `auth doctor` both scan `broker-state/` for pollution;
    the first version of each walked one level deep and matched
    :data:`CREDENTIAL_FILENAMES` exactly. A reviewer found both misses:

      * ``broker-state/<profile>/sub/.credentials.json`` — the whole profile
        directory is bound, so nesting changes nothing about reachability.
      * ``.credentials.json.pre-shared`` — this module's own docs already say
        the ``.pre-shared`` backups ARE credentials.

    NOT the full :func:`_is_credential_name` predicate. That one also has a
    prefix rule matching lock-file neighbours, which would make a
    ``.credentials.json.lock`` read as a leaked secret. Names only, exactly the
    set plus its backup spelling — and the caller is expected to say out loud
    that it matched a NAME and did not read the file.

    Follows symlinked directories on purpose: a symlinked profile dir is bound
    into the container exactly like a real one, so skipping it would be a blind
    spot rather than a safety measure. Unreadable directories are skipped
    silently — this is a reporting helper, not a gate, and it must not turn a
    permissions problem into a traceback in the middle of `auth status`.
    """
    wanted = set(CREDENTIAL_FILENAMES) | {
        f"{n}.pre-shared" for n in CREDENTIAL_FILENAMES}
    found = []
    stack = [root]
    seen: set = set()
    while stack:
        d = stack.pop()
        try:
            real = d.resolve()
        except OSError:
            continue
        if real in seen:       # a symlink loop would otherwise never end
            continue
        seen.add(real)
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir():
                    stack.append(e)
                elif e.name in wanted:
                    found.append(e)
            except OSError:
                continue
    return sorted(found)


# Backups and locks derived from a credential. Withheld for the same reason as
# the credential itself: `.pre-shared` backups ARE credentials, and a refresh
# lock carried into another directory would guard the wrong file.
_CREDENTIAL_SUFFIXES: tuple[str, ...] = (
    ".pre-shared",
    ".refresh.lock",
)

# Files botainer itself drops in a mode's directory to explain that mode. They
# describe where they are, so carrying them into a different mode would make
# them lies. Cheap to regenerate; not history.
_MODE_LOCAL_FILENAMES: frozenset[str] = frozenset({
    "README.shared-mode.txt",
    # codex's config.toml — and here the "would make them lies" is not a
    # tidiness argument, it is a CREDENTIAL LEAK.
    #
    # agent-codex-broker writes this file into `broker-state/<profile>/` to
    # route codex at the per-session broker: a `model_providers` entry with
    # `base_url = http://127.0.0.1:<ephemeral port>/v1` and
    # `env_key = "OPENAI_API_KEY"`. In broker mode that variable holds a
    # SENTINEL, so pointing it anywhere is harmless.
    #
    # Carry it into `profiles/<profile>/` — which is exactly what a
    # broker -> isolated/shared switch does, since both directories bind to
    # /home/agent/.codex — and every term flips. The port died with the broker
    # and is free for anyone to bind. `OPENAI_API_KEY` in mount mode is the
    # REAL key (agent-codex/entrypoint_wrap.sh sets it from the key file).
    # Nothing in mount mode rewrites or removes this file. So the next session
    # sends the real credential to whoever now owns that port — on an apptainer
    # login node, any co-tenant who binds it.
    #
    # Worse with a hostile agent and no luck required: `broker-state/` is bound
    # rw, and the plugin documents the file as agent-editable BECAUSE in broker
    # mode an edit can only redirect a sentinel. The carry is what breaks that
    # reasoning — it delivers an agent-authored `base_url` into the directory
    # holding the real key. Found by review 2026-09-04, same day the file was
    # introduced.
    #
    # It is regenerated by the broker hook on every session, so withholding it
    # costs nothing.
    "config.toml",
})

# The one file that mixes all three categories: ACCOUNT state, PROJECT config
# and UI preferences. Claude Code writes it inside `CLAUDE_CONFIG_DIR`; the
# leading dot varies by version, so both names are handled.
ACCOUNT_STATE_FILENAMES: frozenset[str] = frozenset({
    "claude.json", ".claude.json",
})

# What survives from that file. An ALLOWLIST, deliberately, and the reasoning is
# worth stating because the opposite choice looks equally reasonable:
#
#   - allowlist too narrow  -> the user loses a preference on switch. Visible,
#     recoverable, annoying.
#   - denylist too narrow   -> the user's new profile carries another account's
#     identity or entitlements across a boundary `docs/CAPABILITY-SURFACE.md`
#     §4cm calls an ACCOUNT boundary. A broker "self-heal" that crossed it was
#     reverted within the hour.
#
# The consequences are not symmetric, so the safe direction is to name what
# travels rather than what does not. A key upstream adds later simply does not
# travel, which reads to the user as a reset preference and never as a leak.
#
# `projects` is the substance — per-project MCP servers, allowed tools, the
# trust-dialog answer and the prompt history behind the up-arrow. It is keyed by
# path, and both sides of a carry are the same project, so the key matches.
_CARRIED_ACCOUNT_FILE_KEYS: frozenset[str] = frozenset({
    "projects",
    # UI and onboarding flags. No account information, and dropping them makes
    # the agent re-run onboarding and re-show every tip the user has dismissed.
    "hasCompletedOnboarding",
    "autoUpdates",
    "showExpandedTodos",
    "tipsHistory",
    "tipLifetimeShownCounts",
    "announcementImpressions",
    "hasUsedBackslashReturn",
    "hasSeenTasksHint",
    "hasOpenedAgentsView",
})


def reduce_account_state(text: str) -> str | None:
    """Keep the project config out of a config file; leave the account behind.

    Returns the reduced JSON, or None when the input cannot be parsed — in which
    case the caller carries NOTHING rather than the original, because a file we
    cannot read is a file whose account keys we cannot find.

    Measured against a real install (2026-08-27): nine account/entitlement keys
    live here alongside the project config — `oauthAccount`, `userID`,
    `cachedExtraUsageDisabledReason` (an ORG-level flag), and six entitlement
    caches. None of them may cross a profile boundary.

    Note this is not merely tidiness. The carry does not copy the CREDENTIAL, so
    the destination has to log in either way. Copying `oauthAccount` without its
    token would produce a directory that NAMES an account it cannot authenticate
    as — worse than carrying neither, and unjustifiable given §4cm's finding that
    there is no sound same-account signal on disk.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    kept = {k: v for k, v in data.items() if k in _CARRIED_ACCOUNT_FILE_KEYS}
    # Compact, like the file upstream writes. Pretty-printing it would inflate
    # a large `projects` map and make the size botainer quotes to the user
    # smaller than what lands on disk.
    return json.dumps(kept, separators=(",", ":"))


def _is_account_state_name(name: str) -> bool:
    return name in ACCOUNT_STATE_FILENAMES


def _is_credential_name(name: str) -> bool:
    """True for a credential, a credential backup, or a credential's lock.

    Matched on the FULL name and on the credential-derived suffixes, so
    `.credentials.json`, `.credentials.json.pre-shared` and
    `..credentials.json.refresh.lock` are all caught without listing each
    variant — the variants are generated from the credential name elsewhere in
    the codebase and would drift out of any literal list.
    """
    if name in CREDENTIAL_FILENAMES:
        return True
    if any(name.endswith(sfx) for sfx in _CREDENTIAL_SUFFIXES):
        return True
    # `.credentials.json.pre-shared` and friends: any name that starts with a
    # credential name is credential-derived.
    return any(
        name.startswith(cred) and name != cred for cred in CREDENTIAL_FILENAMES
    )


@dataclass(frozen=True)
class Withheld:
    """One thing the carry declined to take, and the reason a human needs."""

    path: Path
    reason: str


@dataclass
class CarryPlan:
    """What a carry would do. Computed without touching the destination.

    `blocked` is the both-sides-have-history case. It is not an error: it is the
    case where botainer must not choose, because either directory could be the
    one the user wants and the wrong guess loses work.
    """

    source: Path
    destination: Path
    carry: list[Path] = field(default_factory=list)          # relative to source
    # Carried, but REDUCED on the way: the config file that mixes account state
    # with project config. Kept as a separate list so the plan can say so rather
    # than presenting a partial copy as a whole one.
    carry_reduced: list[Path] = field(default_factory=list)
    withheld: list[Withheld] = field(default_factory=list)
    blocked: bool = False
    blocked_reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.carry and not self.carry_reduced

    @property
    def file_count(self) -> int:
        return len(self.carry) + len(self.carry_reduced)

    @property
    def bytes_to_carry(self) -> int:
        # Includes the reduced file at its SOURCE size — an over-estimate,
        # since reducing only removes keys. Excluding it made the figure quoted
        # before the copy smaller than the one reported after it, which reads
        # like something unaccounted-for was written.
        total = 0
        for rel in list(self.carry) + list(self.carry_reduced):
            try:
                total += (self.source / rel).stat().st_size
            except OSError:
                pass
        return total


def _walk_regular_files(
    root: Path, *, strict: bool = False,
) -> tuple[list[Path], list[Withheld]]:
    """Every regular file under `root`, as paths relative to it.

    Symlinks are refused at every depth and returned as withheld (see the module
    docstring, property 2). `os.walk(followlinks=False)` does not descend into a
    symlinked directory, but it still LISTS it, so directory symlinks are caught
    here rather than silently dropped — a user whose history dir contains a link
    should be told, not quietly given less than they asked for.

    Strict observation propagates filesystem errors and refuses unassessed
    symlinked subtrees. Existing carry callers retain their original best-effort
    behavior unless they explicitly request it.
    """
    def scan_error(error: OSError) -> None:
        raise error

    files: list[Path] = []
    withheld: list[Withheld] = []
    walk = (os.walk(root, followlinks=False, onerror=scan_error) if strict
            else os.walk(root, followlinks=False))
    for dirpath, dirnames, filenames in walk:
        here = Path(dirpath)
        for d in list(dirnames):
            is_link = (stat.S_ISLNK((here / d).lstat().st_mode) if strict
                       else (here / d).is_symlink())
            if is_link:
                if strict:
                    raise OSError("History contains an unassessed directory link")
                dirnames.remove(d)
                withheld.append(Withheld(
                    (here / d).relative_to(root),
                    "symlinked directory — a copy could redirect writes "
                    "outside the new mode's directory",
                ))
        for f in filenames:
            p = here / f
            rel = p.relative_to(root)
            is_link = stat.S_ISLNK(p.lstat().st_mode) if strict else p.is_symlink()
            if is_link:
                withheld.append(Withheld(
                    rel,
                    "symlink — shared mode delivers its credential as one, so "
                    "copying links between modes could silently re-share it",
                ))
                continue
            is_file = stat.S_ISREG(p.stat().st_mode) if strict else p.is_file()
            if not is_file:
                withheld.append(Withheld(rel, "not a regular file"))
                continue
            files.append(rel)
    return files, withheld


def has_history(directory: Path, *, strict: bool = False) -> bool:
    """True when `directory` holds anything worth carrying.

    A directory that contains only a credential (and the lock/backup files that
    come with one) has no history — that is exactly the state right after a
    login, and treating it as "has history" would make every first switch look
    like a conflict.

    With strict=True, failed scans, metadata lookups and unassessed links or
    special entries raise OSError; availability callers must report unknown.
    Known credential/mode-local links are excluded only when their target is a
    regular file. No file contents are read or linked subtrees traversed. A
    missing directory is still absent. Default callers keep the existing
    best-effort behavior; this option does not change which files can be carried.
    """
    if strict:
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode):
            raise OSError("History location is an unassessed directory link")
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError("History location is not a directory")
    elif not directory.is_dir():
        return False
    files, withheld = _walk_regular_files(directory, strict=strict)
    if strict:
        for item in withheld:
            excluded_name = (_is_credential_name(item.path.name)
                             or item.path.name in _MODE_LOCAL_FILENAMES)
            if excluded_name and stat.S_ISREG((directory / item.path).stat().st_mode):
                continue
            raise OSError("History contains an unassessed link or special entry")
    return any(
        not _is_credential_name(rel.name) and rel.name not in _MODE_LOCAL_FILENAMES
        for rel in files
    )


def plan_carry(source: Path, destination: Path) -> CarryPlan:
    """Work out what would move from `source` to `destination`. Reads only.

    Returns a plan with `blocked=True` rather than raising when the destination
    already holds history: the caller has to show the user both directories and
    let them decide, and a plan carries the information needed to do that.
    """
    plan = CarryPlan(source=source, destination=destination)

    if source == destination:
        plan.blocked = True
        plan.blocked_reason = "source and destination are the same directory"
        return plan
    if not source.is_dir():
        plan.blocked_reason = "no previous directory to carry from"
        return plan

    files, withheld = _walk_regular_files(source)
    plan.withheld.extend(withheld)

    if contains_symlink(destination):
        # Before the has_history check, deliberately: a destination holding
        # ONLY a planted link reports no history (symlinks are skipped), so
        # ordering this second would let the dangerous case through the
        # cheerful path.
        plan.blocked = True
        plan.blocked_reason = (
            "the destination contains a symbolic link, and botainer will not "
            "write through one — the agent can write to that directory, so a "
            "link there could redirect the copy outside it"
        )
        return plan

    if has_history(destination):
        plan.blocked = True
        plan.blocked_reason = (
            "the destination already holds history — carrying now would mix two "
            "separate sets of transcripts"
        )
        return plan

    for rel in files:
        if _is_credential_name(rel.name):
            plan.withheld.append(Withheld(
                rel,
                "credential material — a mode switch is a credential change, "
                "so log in again in the new mode",
            ))
            continue
        if rel.name in _MODE_LOCAL_FILENAMES:
            plan.withheld.append(Withheld(
                rel, "describes the mode it lives in; would be wrong here"))
            continue
        if (destination / rel).exists():
            plan.withheld.append(Withheld(
                rel, "already present at the destination — not overwritten"))
            continue
        if _is_account_state_name(rel.name):
            if reduce_account_state(_read_text(source / rel)) is None:
                plan.withheld.append(Withheld(
                    rel, "could not be parsed, so the account keys inside it "
                         "could not be located — carried nothing rather than "
                         "carrying all of it"))
                continue
            plan.carry_reduced.append(rel)
            continue
        plan.carry.append(rel)

    plan.carry.sort()
    plan.carry_reduced.sort()
    return plan


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@dataclass
class CarryReport:
    """What a carry actually did. Distinct from the plan: files can fail."""

    copied: list[Path] = field(default_factory=list)
    # Carried with the account state removed. Separate from `copied` so a caller
    # can say "and your project settings, without the account details" rather
    # than reporting a reduced file as a faithful copy.
    reduced: list[Path] = field(default_factory=list)
    failed: list[Withheld] = field(default_factory=list)
    bytes_copied: int = 0

    @property
    def file_count(self) -> int:
        return len(self.copied) + len(self.reduced)


def execute_carry(plan: CarryPlan) -> CarryReport:
    """Copy the planned files. Adds only; never deletes, never overwrites.

    A per-file failure is recorded and the rest proceeds. A carry that half
    works is better than one that aborts at file 400 of 900 and leaves the user
    to work out which half they have — and because nothing is deleted, a partial
    carry can simply be run again.
    """
    report = CarryReport()
    if plan.blocked or plan.is_empty:
        return report

    # The destination ROOT has to exist before anything can be opened relative
    # to it. `_exclusive_create_under` deliberately does not create it: that
    # function's whole job is to refuse to invent path components, and the root
    # is botainer's own directory rather than anything derived from the carry.
    # (Caught by a test, after the switch to dir_fd removed the implicit
    # `mkdir(parents=True)` that used to create it as a side effect.)
    try:
        plan.destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.failed.append(Withheld(Path("."), str(exc)))
        return report

    for rel in plan.carry:
        src = plan.source / rel
        dst = plan.destination / rel
        try:
            with _exclusive_create_under(plan.destination, rel) as out:
                with open(src, "rb") as inp:
                    shutil.copyfileobj(inp, out)
            shutil.copystat(src, dst, follow_symlinks=False)
            report.copied.append(rel)
            report.bytes_copied += dst.stat().st_size
        except OSError as exc:
            report.failed.append(Withheld(rel, str(exc)))

    for rel in plan.carry_reduced:
        dst = plan.destination / rel
        try:
            reduced = reduce_account_state(_read_text(plan.source / rel))
            if reduced is None:
                # Only reachable if the file changed between plan and execute.
                report.failed.append(Withheld(
                    rel, "became unparseable after planning; carried nothing"))
                continue
            with _exclusive_create_under(plan.destination, rel) as out:
                out.write(reduced.encode("utf-8"))
            report.reduced.append(rel)
            report.bytes_copied += dst.stat().st_size
        except OSError as exc:
            report.failed.append(Withheld(rel, str(exc)))
    return report


@contextmanager
def _exclusive_create_under(root: Path, rel: Path):
    """Create ``root/rel`` for writing. Cannot be redirected out of ``root``.

    Two properties, and neither is a check that runs before the write:

    **It never truncates.** ``O_EXCL`` rather than a pre-check, because that is
    what makes "never overwrites" TRUE rather than true-unless-something-raced:
    a session can start between the plan and the copy and write the very file
    about to be written.

    **It never traverses a symlink.** Each path component is opened RELATIVE to
    a descriptor for the one above it, with ``O_NOFOLLOW``, so a symlink
    anywhere in the chain fails with ``ELOOP`` instead of redirecting the write.

    That second one is not hypothetical, and the naive version of this function
    shipped without it. The destination is the config directory bound at
    ``/home/agent/.claude`` — the agent WRITES there. A session could leave

        <destination>/projects  ->  /somewhere/else

    behind, and ``mkdir(parents=True, exist_ok=True)`` accepts an existing
    symlink happily, so the carry wrote agent-controlled content to an
    agent-chosen path outside the destination. Demonstrated, not theorised.

    Worse, two rules combined to hide it: :func:`_walk_regular_files` skips
    symlinks — correct for the SOURCE — which made a destination holding nothing
    but a planted link look EMPTY, so :func:`has_history` reported no conflict
    and the carry proceeded. The rule that should have stopped it waved it
    through.

    A ``lstat``-then-open check would be a TOCTOU race. Opening relative to a
    held descriptor is not a check at all; there is no window.
    """
    fds: list[int] = []
    try:
        # `root` itself is botainer's own path and MAY legitimately be reached
        # through a symlink (a state root the user pointed elsewhere), so it is
        # opened normally. Everything below it is agent-writable and is not.
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        fds.append(current)
        for part in rel.parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            current = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current)
            fds.append(current)

        name = rel.name
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=current)
        try:
            with open(fd, "wb", closefd=True) as handle:
                yield handle
        except BaseException:
            try:
                os.unlink(name, dir_fd=current)
            except OSError:
                pass
            raise
    finally:
        for handle_fd in fds:
            try:
                os.close(handle_fd)
            except OSError:
                pass


def contains_symlink(directory: Path) -> bool:
    """True if anything under `directory` is a symlink, at any depth.

    Used to REFUSE a carry with a legible reason. The refusal is a courtesy on
    top of :func:`_exclusive_create_under`, which makes the write impossible
    regardless — without it the user would get a bare ``ELOOP`` from a command
    that was trying to help them, and "Too many levels of symbolic links" is
    not an explanation of anything.
    """
    if not directory.is_dir():
        return False
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames) + list(filenames):
            if (here / name).is_symlink():
                return True
    return False


# The two markers that name a history copy botainer renamed aside: one from a
# carry, one from resolving a two-sided conflict. Defined HERE, beside the code
# that creates them, and imported by everything that recognises one.
#
# They were string literals in two files — created in this module, recognised in
# `botainer where`, and about to be counted in a third place. That is how a
# lister and a creator drift apart, and the failure is silent in the worst
# direction: a directory nothing offers to reclaim and nothing offers to
# restore, holding the only copy of someone's history.
SUPERSEDED_MARKER = ".superseded-"   # left by a carry
ARCHIVED_MARKER = ".archived-"       # left by resolving a two-sided conflict
SET_ASIDE_MARKERS = (SUPERSEDED_MARKER, ARCHIVED_MARKER)

# Warn once the copies of ONE profile reach this many. The number is a judgement
# and not a limit: nothing is refused, nothing is deleted, and the count keeps
# rising if the user ignores it. Five is where "botainer left something behind"
# stops being a detail and starts being an amount of disk — and, more to the
# point, where the user is unlikely to remember what is in the older ones.
SET_ASIDE_NUDGE_AT = 5


def set_aside_siblings(directory: Path) -> list[Path]:
    """Every set-aside copy of `directory`, by name, oldest stamp first.

    Matches on the name BEFORE the marker being exactly `directory.name`, not on
    a prefix: `work-2.superseded-…` starts with `work` and is a different
    profile's history. A count that quietly folded in a neighbour's copies would
    nag about a directory the user cannot find.

    Symlinks are skipped — a link named like a set-aside copy is not one, and
    following it to measure or offer it would be reading somewhere else.
    """
    parent = directory.parent
    out: list[Path] = []
    try:
        children = sorted(parent.iterdir())
    except OSError:
        return out
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        for marker in SET_ASIDE_MARKERS:
            if marker in child.name and \
                    child.name.split(marker, 1)[0] == directory.name:
                out.append(child)
                break
    return out


def supersede_carried(plan: CarryPlan, report: CarryReport, *,
                      now: datetime | None = None) -> Path | None:
    """Move the carried files out of the source, leaving its credential behind.

    This is what makes the carry a MOVE rather than a COPY, and the reason to
    want that is not tidiness:

        A -> B   copy            both hold the same history
        work in B                B advances, A goes stale
        B -> A                   BOTH populated; which one is current?

    With a copy, the only evidence for "which is latest" is a modification time
    and the first line of a transcript — a judgement the product would be making
    the user perform. With a move there is one live copy and the question cannot
    be asked.

    Nothing is deleted. The carried files are RENAMED into a timestamped
    sibling, so a move that goes wrong is recoverable and the project's standing
    "nothing is deleted" property holds. The credential and anything withheld
    stay exactly where they are — which is what lets a user switch away and back
    and still be logged in.

    Only files this carry actually landed are moved. A file that failed to copy
    stays put, because moving it would be losing it.
    """
    if not report.copied and not report.reduced:
        return None
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    base = f"{plan.source.name}{SUPERSEDED_MARKER}{stamp}"
    superseded = plan.source.with_name(base)
    n = 1
    while superseded.exists():
        n += 1
        superseded = plan.source.with_name(f"{base}-{n}")

    moved_any = False
    for rel in list(report.copied) + list(report.reduced):
        src = plan.source / rel
        if not src.is_file() or src.is_symlink():
            continue
        dst = superseded / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            moved_any = True
        except OSError:
            # Leave it. A file we cannot move is a file we keep, never one we
            # lose — the destination already has its copy either way.
            continue
    _prune_empty_dirs(plan.source)
    return superseded if moved_any else None


def _prune_empty_dirs(root: Path) -> None:
    """Remove directories left empty by the move. Never removes `root` itself.

    `rmdir` only — it refuses a non-empty directory, so this cannot take
    anything with it. Without it the source keeps an empty `projects/`,
    `todos/` and `agents/` skeleton that reads as "there is something here".
    """
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        here = Path(dirpath)
        if here == root or here.is_symlink():
            continue
        try:
            here.rmdir()
        except OSError:
            pass


def archive_dir(directory: Path, *, now: datetime | None = None) -> Path:
    """Rename `directory` aside, returning the new path. Nothing is deleted.

    The way out of a blocked carry. The timestamp is UTC and in the name so the
    user can tell two archives apart without stat-ing them, and so the name
    sorts chronologically.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    base = f"{directory.name}{ARCHIVED_MARKER}{stamp}"
    target = directory.with_name(base)
    n = 1
    while target.exists():
        n += 1
        target = directory.with_name(f"{base}-{n}")
    directory.rename(target)
    return target


def describe_dir(directory: Path) -> dict[str, object]:
    """Facts about a history directory, for showing the user a blocked carry.

    Deliberately facts and not a verdict: file count, total bytes, newest
    modification time, and the newest transcript's first prompt if one can be
    read. Which directory to keep is the user's call, and the thing that lets
    them make it is recognising the conversation — not a number.
    """
    info: dict[str, object] = {
        "path": str(directory),
        "exists": directory.is_dir(),
        "files": 0,
        "bytes": 0,
        "modified": None,
        "last_prompt": None,
    }
    if not directory.is_dir():
        return info

    # Describe the HISTORY, not the directory. Credentials and the files
    # botainer drops in a mode's dir are never carried, so counting them
    # answers a question nobody asked — and the mtime is worse than useless:
    # a credential rewritten seconds ago dominates the "last written" of a
    # directory whose transcripts are months old, which is exactly backwards
    # for a prompt asking "which of these two is current?". Observed: two
    # profiles with transcripts a month apart both reported the same instant.
    files, _ = _walk_regular_files(directory)
    newest_mtime = 0.0
    for rel in files:
        if _is_credential_name(rel.name) or rel.name in _MODE_LOCAL_FILENAMES:
            continue
        try:
            st = (directory / rel).stat()
        except OSError:
            continue
        info["files"] = int(info["files"]) + 1
        info["bytes"] = int(info["bytes"]) + st.st_size
        newest_mtime = max(newest_mtime, st.st_mtime)
    if newest_mtime:
        info["modified"] = datetime.fromtimestamp(
            newest_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    info["last_prompt"] = _newest_first_prompt(directory)
    return info


def _newest_first_prompt(directory: Path) -> str | None:
    """The opening prompt of the most recently touched transcript, if any.

    Best-effort by design. Transcripts are JSONL written by the agent, i.e.
    UNTRUSTED input from botainer's point of view, so this reads a bounded
    prefix, never evaluates, and returns None on anything unexpected rather
    than propagating. The return value is truncated by the caller for display.
    """
    candidates: list[tuple[float, Path]] = []
    for sub in ("projects", "sessions"):
        root = directory / sub
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for d in list(dirnames):
                if (Path(dirpath) / d).is_symlink():
                    dirnames.remove(d)
            for f in filenames:
                if not f.endswith(".jsonl"):
                    continue
                p = Path(dirpath) / f
                if p.is_symlink():
                    continue
                try:
                    candidates.append((p.stat().st_mtime, p))
                except OSError:
                    continue
    if not candidates:
        return None

    for _, path in sorted(candidates, reverse=True)[:5]:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for _ in range(50):
                    line = fh.readline()
                    if not line:
                        break
                    if len(line) > 200_000:
                        continue
                    try:
                        rec = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    text = _user_text(rec)
                    if text:
                        return " ".join(text.split())
        except OSError:
            continue
    return None


def _user_text(rec: object) -> str | None:
    """Pull the first user prompt out of one transcript record, or None.

    Tolerates both the flat (`{"type": "user", "message": {"content": "..."}}`)
    and block-list content shapes, because the transcript format is upstream's
    and has changed before.
    """
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return None
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                if block["text"].strip():
                    return block["text"].strip()
    return None
