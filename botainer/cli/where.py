"""Locate Botainer data and report storage use.

The command is read-only. It lists state locations, can measure their sizes and
prints deletion commands for the user to inspect and run separately. Package and
scratch directories can grow independently of the project checkout; their
locations should be discoverable without guessing the configured state root.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.core.history_carry import (ARCHIVED_MARKER, SET_ASIDE_MARKERS,
                                         SUPERSEDED_MARKER)
from botainer.state import dir as state_dir
from botainer.state import fs_kind


def _set_aside_history_dirs(data_dir, markers):
    """Timestamped history copies left under `agent-*/{profiles,broker-state}/`.

    THE TWO MARKERS DO NOT MEAN THE SAME THING, and this function used to match
    both and hand the union to the reclaim list. Its docstring asserted the
    justification — "Both are made by RENAME after the live copy was written and
    verified, so every byte in them also exists at the live location" — and that
    is true of exactly one of them:

      `.superseded-<ts>`  written by `supersede_carried`, which runs only AFTER
                          a carry copied and verified every file. A second copy
                          exists. Safe to delete.
      `.archived-<ts>`    written by `archive_dir`, whose own docstring says it
                          is "the way out of a BLOCKED carry". A blocked carry
                          copied NOTHING. This is the ONLY copy of that history.

    So `botainer where` labelled an irreplaceable directory "already copied
    across" and printed the `rm -rf` for it — to a user who, by construction,
    reached for this command because they were looking for history they had
    lost. (#226)

    Deliberately narrow otherwise: only our own suffixes, only under the two
    known parents, so a directory a user happened to name `.superseded-anything`
    elsewhere is never offered up.
    """
    from pathlib import Path as _P
    out: list[_P] = []
    if not data_dir.is_dir():
        return out
    for agent_dir in sorted(data_dir.glob("agent-*")):
        for kind in ("profiles", "broker-state"):
            parent = agent_dir / kind
            if not parent.is_dir():
                continue
            for child in sorted(parent.iterdir()):
                if not child.is_dir() or child.is_symlink():
                    continue
                if any(m in child.name for m in markers):
                    out.append(child)
    return out


def _superseded_history_dirs(data_dir):
    """Carried-and-verified copies. A live copy exists; safe to reclaim."""
    return _set_aside_history_dirs(data_dir, (SUPERSEDED_MARKER,))


def _archived_history_dirs(data_dir):
    """Blocked-carry copies. THE ONLY copy — never offered for deletion."""
    return _set_aside_history_dirs(data_dir, (ARCHIVED_MARKER,))


def live_profile_dir_for(superseded: Path) -> Path:
    """The live profile directory a `.superseded-`/`.archived-` copy came from.

    `profiles/work.superseded-20260828T010203Z` -> `profiles/work`. Both
    suffixes are stripped by NAME rather than by splitting on the last dot,
    because a profile name can contain neither (the charset is
    `^[a-z][a-z0-9_-]{0,31}$` — no dots, since the name becomes a bind source
    and an mkdir). So the first occurrence of either marker is unambiguous.
    """
    name = superseded.name
    for marker in SET_ASIDE_MARKERS:
        if marker in name:
            return superseded.with_name(name.split(marker, 1)[0])
    return superseded


def _dir_size(path: Path) -> int:
    """Bytes under `path`. Best-effort: unreadable subtrees are skipped rather
    than aborting the whole listing — a partial number beats no table."""
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for f in files:
            try:
                fp = Path(root) / f
                if not fp.is_symlink():
                    total += fp.stat().st_size
            except OSError:
                continue
    return total


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}T"


def _filesystem_of(path: Path) -> str:
    """Which mount point backs this path — the thing that decides quota and
    purge policy. Empty when it cannot be determined."""
    try:
        p = path.resolve()
        dev = p.stat().st_dev
        while p != p.parent and p.parent.stat().st_dev == dev:
            p = p.parent
        return str(p)
    except OSError:
        return ""


@click.command("where")
@click.option("--sizes/--no-sizes", default=True,
              help="Measure directory sizes (walks the tree; slow on network "
                   "filesystems).")
@click.option("--state-root", "state_root_only", is_flag=True, default=False,
              help="Print ONLY the state-root path, one bare line, and exit. "
                   "For scripts and for diagnostics we hand someone: "
                   "`STATE=$(botainer where --state-root)`. Skips the size "
                   "walk. Exits 3 if the root does not exist yet.")
@handle_refusals
def where(sizes: bool, state_root_only: bool) -> None:
    """Show where botainer stores data on this host, and what is big.

    Nothing is deleted. Reclaimable directories print the exact `rm -rf` to run.
    """
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    root = paths.root

    if state_root_only:
        # ONE BARE LINE ON STDOUT, so `$(…)` captures a usable path and nothing
        # else. Everything explanatory goes to stderr, which command
        # substitution does not capture.
        #
        # Automation needs a machine-readable state-root query that honors
        # configured roots. A missing directory must not be reported as an
        # empty inspected one. Exit 3 when absent, but still print the path so
        # scripts can locate the configured destination.
        click.echo(str(root))
        if not root.exists():
            click.secho(
                f"note: {root} does not exist yet — run `botainer setup`. "
                f"Anything that searches this path will find nothing, which is "
                f"NOT the same as finding nothing there.",
                fg="yellow", err=True)
            raise SystemExit(3)
        return
    # ONLY MY_BOTAINER. This used to fall back to reporting
    # "set by: BOTAINER_STATE_ROOT", which is false: `state_dir`'s
    # `_resolve_state_root` reads MY_BOTAINER and nothing else, and
    # BOTAINER_STATE_ROOT is what the launcher EXPORTS to plugin subprocesses
    # (see `subprocess_state_env`). Inside a session that variable is always
    # set, so `where` would confidently attribute the root to a variable that
    # had no part in choosing it — and a user who then edited it would see no
    # effect. Found while fixing the state-dir refusal (queue rows 71+72),
    # which had copied the same wrong pair.
    src = "MY_BOTAINER" if os.environ.get("MY_BOTAINER") else ""

    click.secho(f"State root: {root}", bold=True)
    click.echo(f"  set by:   {src or 'default (~/.botainer)'}")
    fs = _filesystem_of(root)
    if fs:
        # The TYPE, not just the mount point. Until 2026-08-31 this printed the
        # mount point alone, so a user on GPFS and a user on an SSD saw
        # identical output — and only one of them was heading for #146, where
        # codex's four WAL-mode SQLite databases cannot work on a network
        # filesystem. Stating the kind is the cheapest half of that: it does not
        # fix anything, it stops the situation being invisible.
        kind = fs_kind.describe(root)
        click.echo(f"  filesystem: {fs}  [{kind}]")
        if fs_kind.classify(root) == fs_kind.NETWORK:
            click.secho(
                "    ! This is a NETWORK filesystem. SQLite databases in WAL "
                "mode do not\n"
                "      work here — codex keeps four. See `botainer doctor`.",
                fg="yellow")
    if not root.exists():
        click.secho("\n  (does not exist yet — run `botainer setup`)", fg="yellow")
        return
    try:
        du = shutil.disk_usage(root)
        click.echo(f"  free space: {_human(du.free)} of {_human(du.total)}")
    except OSError:
        pass
    click.echo()

    # Host-wide, keyed by nothing / by agent.
    click.secho("Host-wide", fg="cyan", bold=True)
    for name, path, note in (
        ("images", root / "images", "one .sif per AGENT, reused by every project"),
        ("plugins", root / "plugins", "installed plugins"),
        ("shared-auth", root / "shared-auth", "host-wide credentials — do NOT delete"),
        ("policy.yaml", root / "policy.yaml", "your user policy — do NOT delete"),
    ):
        if not path.exists():
            continue
        size = f"{_human(_dir_size(path)):>8}" if sizes else "       -"
        click.echo(f"  {size}  {name:<14} {path}")
        click.echo(f"            {' ' * 14} {note}")
    click.echo()

    # Per botainer project. This is where the space actually goes.
    projects = state_dir.list_projects()
    if not projects:
        click.echo("No projects yet.")
        return
    click.secho(f"Per project ({len(projects)})", fg="cyan", bold=True)
    reclaimable: list[tuple[int, Path, str]] = []
    for entry in projects:
        proj_dir = root / "state" / entry.uuid
        label = entry.display_name or Path(entry.last_path).name or entry.uuid[:8]
        gone = "" if entry.path_exists else "   [project directory is GONE]"
        click.echo(f"\n  {label}{gone}")
        click.echo(f"    {entry.last_path}")
        click.echo(f"    state: {proj_dir}")
        for sub, note, safe in (
            ("packages", "installed pip/conda/npm — regenerable, slow to rebuild", True),
            ("scratch", "disposable by design — safe to delete any time", True),
            ("home", "tool caches (.npm, pip) — regenerable", True),
            ("sessions", "session records — small, historical", False),
            ("data", "plugin data incl. credentials — do NOT delete", False),
        ):
            p = proj_dir / sub
            if not p.exists():
                continue
            n = _dir_size(p) if sizes else 0
            size = f"{_human(n):>8}" if sizes else "       -"
            mark = " ♻" if safe else "  "
            click.echo(f"    {size}{mark} {sub:<10} {note}")
            if safe and n > 0:
                reclaimable.append((n, p, f"{label}/{sub}"))

        # `data` is marked do-NOT-delete because it holds credentials — and
        # that is right for the directory as a whole. But the history carry
        # leaves timestamped copies INSIDE it, and those are the one thing in
        # there that is definitely safe to remove: every file in them was
        # copied to the live directory and verified before being renamed aside.
        #
        # Without this the storage surface points a user AWAY from the only
        # part of `data` they should ever reclaim, and the carry grows the
        # state root with nothing offering to shrink it again.
        superseded = _superseded_history_dirs(proj_dir / "data")
        for old in superseded:
            n = _dir_size(old) if sizes else 0
            size = f"{_human(n):>8}" if sizes else "       -"
            click.echo(
                f"    {size} ♻ {'  └ ' + old.name[:36]:<10} "
                f"superseded by a profile/mode switch — already copied across")
            if n > 0:
                reclaimable.append((n, old, f"{label}/data/{old.name}"))

        # ARCHIVED IS NOT RECLAIMABLE. `archive_dir` runs when a carry was
        # BLOCKED, so nothing was copied and this directory is the only copy of
        # that history. It is listed — hiding it would be worse, since finding
        # stranded history is the reason people run this command — but it never
        # enters `reclaimable`, so no `rm -rf` is ever printed for it. (#226)
        archived = _archived_history_dirs(proj_dir / "data")
        for old in archived:
            n = _dir_size(old) if sizes else 0
            size = f"{_human(n):>8}" if sizes else "       -"
            click.secho(
                f"    {size} ⚠ {'  └ ' + old.name[:36]:<10} "
                f"THE ONLY COPY — a carry was blocked, nothing was copied out",
                fg="yellow")
        if archived:
            live = live_profile_dir_for(archived[0])
            click.secho(
                "         Not reclaimable and not deleted by anything. To put "
                "one back, on the HOST, with no session running:", fg="cyan")
            click.secho(f"           mv {live} {live}.replaced", fg="cyan")
            click.secho(f"           mv {archived[0]} {live}", fg="cyan")
        if superseded:
            # Listing these as reclaimable and nothing else would leave the one
            # command that FINDS them offering only to delete them. They are the
            # old copy of somebody's history; wanting it back is at least as
            # likely as wanting the disk space. No command restores one — the
            # profile charset forbids a dotted name, so you cannot switch INTO
            # one — but it is two renames, so print them the way this command
            # already prints the `rm -rf`.
            old = superseded[0]
            live = live_profile_dir_for(old)
            click.secho(
                "         These are the only ♻ entries that do NOT come back "
                "on their own.", fg="cyan")
            click.secho(
                "         To restore one instead of deleting it, on the HOST, "
                "with no session running:", fg="cyan")
            # `{live}.replaced`, NOT `{old}.replaced`. The archived branch 18
            # lines up had this right and this one did not, and the difference
            # is not cosmetic: `old` already carries `.superseded-<ts>`, so
            # parking the LIVE directory under a name derived from it hands
            # `_set_aside_history_dirs` a match, and the next `where --sizes`
            # lists the user's current history as reclaimable and offers to
            # `rm -rf` it. A printed remedy that feeds the delete list is worse
            # than printing nothing.
            click.secho(f"           mv {live} {live}.replaced", fg="cyan")
            click.secho(f"           mv {old} {live}", fg="cyan")

    if sizes and reclaimable:
        reclaimable.sort(reverse=True)
        total = sum(n for n, _, _ in reclaimable)
        click.echo()
        click.secho(f"♻ Reclaimable: {_human(total)} total", fg="green", bold=True)
        # Was "Everything marked ♻ regenerates on next use", which stopped
        # being true the moment superseded history joined this list: packages
        # and caches rebuild themselves, an old transcript does not. A blanket
        # reassurance over a list that now contains one irreplaceable member is
        # the kind of sentence someone deletes a directory on.
        click.echo("  Largest first. Packages, scratch and caches rebuild "
                   "themselves;")
        click.echo("  superseded history does NOT — see the restore commands "
                   "above before deleting one.")
        for n, p, label in reclaimable[:8]:
            click.echo(f"    rm -rf {p}      # {_human(n)}  ({label})")
        if len(reclaimable) > 8:
            click.echo(f"    ... and {len(reclaimable) - 8} more")
        click.secho(
            "\n  Nothing here is deleted for you — run the lines you want.",
            fg="yellow")
