"""`botainer status` — show status of sessions for this project.

Reads spec.json records from `${state_dir}/projects/<uuid>/sessions/`,
queries the runtime to determine if each is still running, and prints
a one-row-per-session summary.
"""

from __future__ import annotations

import json
import os

import click

from botainer.cli import _common
from botainer.cli._refusal_handler import handle_refusals
from botainer.core import identity
from botainer.state import dir as state_dir
from botainer.state import liveness, session_record


@click.command("status")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Include exited / stopped sessions, not just running.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit JSON for machine consumption.",
)
@click.option(
    "--global", "-g", "show_global",
    is_flag=True,
    help="Audit T11: aggregate sessions across ALL projects on this host "
         "(not just the current one) — find a detached session you started "
         "elsewhere. Works outside a project.",
)
@handle_refusals
def status(show_all: bool, as_json: bool, show_global: bool) -> None:
    """Show status of sessions for the current botainer project (or, with
    --global, every project on this host)."""
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)

    if show_global:
        rows: list[dict[str, object]] = []
        for entry in state_dir.list_projects():
            proj_paths = paths.for_project(entry.uuid)
            for row in _build_rows(proj_paths, show_all=show_all, as_json=as_json):
                row["project_uuid"] = entry.uuid
                row["project"] = entry.display_name or entry.last_path
                rows.append(row)
        _emit_rows(rows, as_json=as_json, show_global=True)
        return

    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse(
            "status",
            "not inside a botainer project",
            "cd into a project with .botainer/project-id, run "
            "`botainer init` to set one up, or pass --global to see sessions "
            "across all projects on this host",
        )

    uid, _ = identity.resolve_identity(
        project_root,
        # A QUERY DOES NOT DECIDE THE CLONE QUESTION, and does not write
        # meta.json. This used to pass identity_accept=True — the flag
        # this command does not have — which silently answered it and
        # disarmed the guard for `start` too. (#231)
        identity_accept=False,
        record=False,
    )
    proj_paths = state_dir.ensure_project_dirs(paths, uid)
    _emit_rows(_build_rows(proj_paths, show_all=show_all, as_json=as_json),
               as_json=as_json, show_global=False)


def _broker_health(rec) -> bool | None:
    """Is this session's host-side credential broker still running?

    Returns None when the session has no broker (not broker mode, or an older
    record), True when the recorded pid still exists, False when it is
    definitely gone.

    Why this exists: broker START failure is already fail-closed and surfaced
    (the pre_session hook returns nonzero and `botainer/plugins/hooks.py`
    refuses the launch). A broker that dies MID-session was invisible — the
    caged agent just starts getting "can't connect to api" with no pointer to
    the cause. See the host finding + TROUBLESHOOTING "Auth".

    Deliberately conservative: only `ProcessLookupError` (the pid is gone) is
    reported as DOWN. A live-but-not-ours pid (PID reuse) reports healthy —
    a missed warning is far better than crying wolf on a working session.
    """
    handle = rec.extra_runtime_handle.get("broker")
    if not isinstance(handle, dict):
        return None
    pid = handle.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by another uid
    except OSError:
        return None          # can't tell — don't guess
    return True


def _broker_err_name(session_dir) -> str | None:
    """The broker stderr file(s) in this session dir, or None.

    Per-plugin since 2026-09-04: `agent-<family>-broker-daemon.err`. Both
    brokers previously wrote one `broker-daemon.err`, so a failure could not be
    attributed without opening the session record. Globbing means this keeps
    working when a session runs two brokers, and degrades to None rather than
    naming a file that is not there.
    """
    try:
        names = sorted(p.name for p in session_dir.glob("*-daemon.err")
                       if p.stat().st_size > 0)
    except OSError:
        return None
    return ", ".join(names) if names else None


def _build_rows(proj_paths, *, show_all: bool, as_json: bool) -> list[dict[str, object]]:
    records = session_record.list_sessions(proj_paths.sessions_dir)
    rows: list[dict[str, object]] = []
    for rec in records:
        alive = liveness.is_session_alive(rec)
        # HPC review F9: reconcile stale "running" records.
        # If a Slurm job times out, squeue stops reporting it but the
        # session record still has ended_at empty. Update the record on
        # disk so `status --all`, `list`, and `nudge` all see the truth.
        #
        # Task #152: --json is a READ-ONLY rendering mode (machine
        # automation, CI, downstream tools). Mutating disk state during
        # a read surprises every consumer that expects 'status --json'
        # to be safe to run from cron. Skip the reconcile write in
        # --json mode; the in-memory `alive=False` still reaches the
        # output dict so callers see the truth.
        if (
            not alive and not rec.ended_at
            and rec.runtime != "mock" and not as_json
        ):
            try:
                from datetime import datetime, timezone
                ended = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                session_record.update_runtime(
                    proj_paths.sessions_dir / rec.session_id,
                    ended_at=ended,
                )
            except Exception:
                pass  # best-effort; don't fail status on reconcile errors
        if not show_all and not alive:
            continue
        rows.append(
            {
                "session_id": rec.session_id,
                "runtime": rec.runtime,
                "image": rec.image,
                "container_id": (rec.docker.container_id if rec.docker else None),
                "slurm_jobid": (
                    rec.apptainer.slurm_jobid if rec.apptainer else None
                ),
                "node": rec.apptainer.node if rec.apptainer else None,
                "started_at": rec.started_at,
                "ended_at": rec.ended_at,
                "host": rec.host,
                "alive": alive,
                "screen_session_id": rec.screen_session_id,
                # None = no broker for this session; False = broker died.
                "broker_alive": _broker_health(rec),
                # The per-plugin stderr file(s) this session actually has, so
                # the "why:" line can name one instead of guessing.
                "broker_err": _broker_err_name(
                    proj_paths.sessions_dir / rec.session_id),
            }
        )
    return rows


def _emit_rows(rows: list[dict[str, object]], *, as_json: bool, show_global: bool) -> None:
    if as_json:
        click.echo(json.dumps({"sessions": rows}, indent=2, sort_keys=True))
        return

    if not rows:
        scope = "across any project" if show_global else "for this project"
        click.echo(f"(no running sessions {scope}; pass --all to include stopped)")
        return

    # Human-readable: one line per session.
    for row in rows:
        sid_full = str(row["session_id"])
        sid = sid_full[:12]
        status_indicator = "▶" if row["alive"] else "■"
        runtime = str(row["runtime"])
        if runtime == "docker":
            handle = str(row["container_id"] or "")[:12]
        elif runtime == "apptainer":
            handle = str(row["slurm_jobid"] or "(no slurm)")
        else:
            handle = ""
        started = str(row["started_at"] or "")[:19]
        proj = f"  [{row['project']}]" if show_global and row.get("project") else ""
        click.echo(
            f"{status_indicator} {sid}  {runtime:9s}  {handle:14s}  "
            f"{started}  {row['image']}{proj}"
        )
        if row["screen_session_id"] and row["alive"]:
            click.secho(
                f"    nudge: `botainer nudge --session {sid} \"<text>\"`",
                fg="cyan",
            )
        # The broker died under a still-running session: the agent inside is
        # getting API errors with no way to know why. Say so, loudly.
        if row["alive"] and row["broker_alive"] is False:
            click.secho(
                "    ⚠ credential broker for this session is NOT running — "
                "the agent's API calls will fail.",
                fg="red",
                bold=True,
            )
            # Broker log filenames include the plugin name so errors can be
            # attributed when multiple plugins use the same session directory.
            # Diagnostics should point to a file that actually exists.
            _which = str(row.get("broker_err") or "*-daemon.err")
            click.secho(
                f"      why: see sessions/{sid_full}/{_which}  "
                "(often an expired/rotated login)\n"
                "      fix: `botainer auth login`, then restart the session.",
                fg="red",
            )


