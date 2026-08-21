"""`botainer where` — show where botainer keeps its data, and what is big.

User question: *"is there an easy way to tell it where all the data
(especially large files) are stored? Is there an easy way to move them?"* — the
answer to both was no, and this answers the first half.

WHY THIS RATHER THAN CONFIGURABLE PLACEMENT. Relocatable storage needs a config
block, defaults resolution, a move command, and a migration for the absolute
`image:` paths in existing project configs. Being able to SEE what is big and
delete it costs one read-only command and, for a user who just wants their quota
back, is most of the value — because the two things that actually grow
(`packages/`, `scratch/`) are regenerable by construction and therefore safe to
delete. The user put the principle plainly: *"it should be relatively easy to
find packages folders to delete them manually."* See
DN-017 §4b.

Read-only. It never deletes anything — it prints the `rm -rf` for you to run,
because a command that reclaims GBs on your behalf should not be one keystroke
away from a command that shows you a table.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.state import dir as state_dir


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
@handle_refusals
def where(sizes: bool) -> None:
    """Show where botainer stores data on this host, and what is big.

    Nothing is deleted. Reclaimable directories print the exact `rm -rf` to run.
    """
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    root = paths.root
    src = ("MY_BOTAINER" if os.environ.get("MY_BOTAINER")
           else "BOTAINER_STATE_ROOT" if os.environ.get("BOTAINER_STATE_ROOT")
           else "")

    click.secho(f"State root: {root}", bold=True)
    click.echo(f"  set by:   {src or 'default (~/.botainer)'}")
    fs = _filesystem_of(root)
    if fs:
        click.echo(f"  filesystem: {fs}")
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

    if sizes and reclaimable:
        reclaimable.sort(reverse=True)
        total = sum(n for n, _, _ in reclaimable)
        click.echo()
        click.secho(f"♻ Reclaimable: {_human(total)} total", fg="green", bold=True)
        click.echo("  Everything marked ♻ regenerates on next use. Largest first:")
        for n, p, label in reclaimable[:8]:
            click.echo(f"    rm -rf {p}      # {_human(n)}  ({label})")
        if len(reclaimable) > 8:
            click.echo(f"    ... and {len(reclaimable) - 8} more")
        click.secho(
            "\n  Nothing here is deleted for you — run the lines you want.",
            fg="yellow")
