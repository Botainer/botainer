"""`botainer stop` — stop a running session.

Reads spec.json, identifies the runtime handle, and asks the runtime
to terminate the container/job. Updates spec.json with ended_at and
fires post_session hooks.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click

from botainer.cli import _common
from botainer.cli._refusal_handler import handle_refusals
from botainer.core import composition, identity
from botainer.state import dir as state_dir
from botainer.state import liveness, session_record


@click.command("stop")
@click.argument("session_id", required=False)
@click.option(
    "--all",
    "stop_all",
    is_flag=True,
    help="Stop all running sessions for this project.",
)
@click.option(
    "--force",
    is_flag=True,
    help="docker kill instead of docker stop (no graceful shutdown).",
)
@handle_refusals
def stop(session_id: str | None, stop_all: bool, force: bool) -> None:
    """Stop a running session container (or all with --all)."""
    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse(
            "stop",
            "not inside a botainer project",
            "cd into a project with .botainer/project-id",
        )

    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)

    records = session_record.list_sessions(proj_paths.sessions_dir)
    if not records:
        click.echo("(no sessions found in this project)")
        return

    targets: list[session_record.SessionRecord] = []
    if stop_all:
        targets = [r for r in records if liveness.is_session_alive(r)]
    elif session_id:
        match = _common.find_session_by_prefix(records, session_id, cmd="stop")
        if match is None:
            _common.refuse(
                "stop",
                f"no session matches {session_id!r}",
                "check `botainer status` for available IDs",
            )
        targets = [match]
    else:
        # No ID, no --all: pick most-recent running session.
        alive = [r for r in records if liveness.is_session_alive(r)]
        if not alive:
            click.echo("(no running sessions)")
            return
        if len(alive) > 1:
            ids = ", ".join(r.session_id[:12] for r in alive[:5])
            _common.refuse(
                "stop",
                f"{len(alive)} running sessions; specify --all or a session ID",
                f"running: {ids}",
            )
        targets = alive

    for rec in targets:
        _stop_one(rec, proj_paths.sessions_dir, force=force)


def _stop_one(
    rec: session_record.SessionRecord,
    sessions_dir: Path,
    *,
    force: bool,
) -> None:
    """Stop one session.

    Task #146: all early-return paths previously skipped ended_at +
    post_session. A docker kill failure left the session_record alive
    forever with no end timestamp; subsequent `botainer status` lied
    about it being running. Refactored to a try/finally so end-of-life
    bookkeeping runs even when the runtime stop fails or is impossible.

    Task #114: PENDING HPC jobs (queued but not running) now also get
    scancel'd via the apptainer branch (scancel works on PENDING jobs).
    """
    session_dir = sessions_dir / rec.session_id
    try:
        _do_runtime_stop(rec, force=force)
    finally:
        # End-of-life bookkeeping always runs.
        try:
            session_record.update_runtime(
                session_dir, ended_at=datetime.now(timezone.utc).isoformat()
            )
        except (FileNotFoundError, OSError):
            pass
        try:
            from botainer.core.spec import SessionSpec
            spec = SessionSpec.model_validate(rec.spec)
            composition.run_post_session_hooks(spec)
        except Exception:
            pass


def _do_runtime_stop(
    rec: session_record.SessionRecord, *, force: bool
) -> None:
    if rec.runtime == "docker":
        if rec.docker is None or not rec.docker.container_id:
            click.echo(
                f"{rec.session_id[:12]}: no container_id recorded; cannot stop"
            )
            return
        if not shutil.which("docker"):
            click.echo(
                f"{rec.session_id[:12]}: docker not on PATH; cannot stop"
            )
            return
        cmd = ["docker", "kill" if force else "stop", "--", rec.docker.container_id]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            click.secho(
                f"{rec.session_id[:12]}: stop timed out after 30s "
                "(try --force or `docker kill` manually)",
                fg="red", err=True,
            )
            return
        if result.returncode != 0:
            click.secho(
                f"{rec.session_id[:12]}: stop failed: {result.stderr.strip()}",
                fg="red",
                err=True,
            )
            return
        click.secho(f"{rec.session_id[:12]}: stopped", fg="green")
    elif rec.runtime == "apptainer":
        if rec.apptainer is None or not rec.apptainer.slurm_jobid:
            click.echo(
                f"{rec.session_id[:12]}: no slurm jobid; cannot stop "
                "(foreground apptainer must be Ctrl-C'd in its terminal)"
            )
            return
        if not shutil.which("scancel"):
            click.echo(
                f"{rec.session_id[:12]}: scancel not on PATH; cannot stop"
            )
            return
        try:
            result = subprocess.run(
                ["scancel", "--", rec.apptainer.slurm_jobid],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            click.secho(
                f"{rec.session_id[:12]}: scancel timed out (try manually)",
                fg="red", err=True,
            )
            return
        if result.returncode != 0:
            click.secho(
                f"{rec.session_id[:12]}: scancel failed: {result.stderr.strip()}",
                fg="red",
                err=True,
            )
            return
        click.secho(
            f"{rec.session_id[:12]}: scancel jobid={rec.apptainer.slurm_jobid} "
            f"(applies to PENDING + RUNNING)",
            fg="green",
        )
    elif rec.runtime == "mock":
        click.echo(f"{rec.session_id[:12]}: mock runtime; nothing to stop")
    else:
        click.echo(f"{rec.session_id[:12]}: unknown runtime {rec.runtime!r}")


