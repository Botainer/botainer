"""`botainer list` — enumerate known projects on this host."""

from __future__ import annotations

import json

import click

from botainer.state import dir as state_dir


@click.command("list")
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit JSON for tooling (one object per project).",
)
@click.option(
    "--verbose", "-v", is_flag=True,
    help="Show extra columns: full UUID, session count, last-runtime, last-session timestamp.",
)
def list_(as_json: bool, verbose: bool) -> None:
    """List all botainer projects known to this host.

    Default: one line per project with short UUID, name, path, status.
    --verbose adds session count, last session runtime, last activity.
    --json emits machine-readable output.
    """
    projects = state_dir.list_projects()
    if as_json:
        click.echo(json.dumps([
            {
                "uuid": p.uuid,
                "display_name": p.display_name,
                "last_path": p.last_path,
                "path_exists": p.path_exists,
                "paths": list(p.paths),
                "sessions_dir_count": p.sessions_dir_count,
                "last_session_at": p.last_session_at,
                # Empty means UNKNOWN, not running — see state/dir.py.
                "last_session_ended_at": p.last_session_ended_at,
                "last_session_runtime": p.last_session_runtime,
            }
            for p in projects
        ], indent=2))
        return
    if not projects:
        click.echo("(no projects)")
        return
    if not verbose:
        # Compact, info-rich one-liner:
        #   <short-uuid>  <name-or-basename>  <path>  [(missing)] [sessions=N]
        for p in projects:
            short = p.uuid[:8]
            name = p.display_name or "(unnamed)"
            missing = "" if p.path_exists else "  (path missing)"
            sess = f"  [sessions={p.sessions_dir_count}]" if p.sessions_dir_count else ""
            click.echo(f"{short}  {name:24s}  {p.last_path}{missing}{sess}")
        click.echo("")
        click.secho(
            "    -v for sessions/runtime/last-active; --json for tooling.",
            fg="cyan",
        )
        return
    # Verbose: full UUID + activity columns.
    for p in projects:
        click.secho(p.uuid, bold=True, nl=False)
        click.echo(f"  {p.display_name or '(unnamed)'}")
        click.echo(f"  path:     {p.last_path}{'' if p.path_exists else '  (missing!)'}")
        if p.last_session_runtime:
            click.echo(f"  runtime:  {p.last_session_runtime}")
        if p.sessions_dir_count:
            click.echo(f"  sessions: {p.sessions_dir_count} record(s)")
        if p.last_session_at:
            click.echo(f"  last:     {p.last_session_at}")
        if len(p.paths) > 1:
            click.echo(f"  history:  {len(p.paths)} path(s)")
        click.echo("")
