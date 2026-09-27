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

    # ALREADY ENDED IS NOT A REASON TO END IT AGAIN. Passing an explicit session
    # id skips the liveness filter, so `stop <id>` on a finished session used to
    # overwrite its historical ended_at with today's and run post_session a
    # SECOND time — re-signalling a recorded pid that may since belong to
    # someone else's process. (#229)
    if rec.ended_at:
        click.echo(f"{rec.session_id[:12]}: already ended at {rec.ended_at}; "
                   f"nothing to do")
        return

    stopped = _do_runtime_stop(rec, force=force)

    # BOOKKEEPING FOLLOWS THE VERDICT, NOT THE CONTROL FLOW. This used to be a
    # `finally`, so every "cannot stop" path still stamped ended_at and ran
    # post_session. Two consequences, both observed:
    #   * `stop` printed "docker not on PATH; cannot stop" and then recorded the
    #     session as ended, while `status` went on showing it as running — the
    #     record and the display disagreeing about the same session.
    #   * post_session ran against a LIVE container. In shared mode that sets
    #     the project's credential aside as `.credentials.json.pre-shared` and
    #     replaces it with a symlink, so a stop that did nothing still changed
    #     the login the running agent was using.
    #
    # #146 is why the bookkeeping exists at all — before it, a failed stop left
    # a session "running" forever with no end timestamp. That reasoning is right
    # and is preserved: a stop that SUCCEEDS, or a session with nothing to stop,
    # still records. What changes is that a stop which did not happen no longer
    # claims it did.
    if not stopped:
        click.secho(
            f"    → not marked as ended, and post-session cleanup was NOT run: "
            f"as far as botainer can tell this session is still running. "
            f"Fix the cause above and re-run, or stop it by hand.",
            fg="yellow", err=True)
        return

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
) -> bool:
    """True when the session is genuinely no longer running. (#229)

    It used to return None and signal failure with a bare `return`, and the
    caller's `finally` could not tell the two apart — so a stop that had just
    printed "docker not on PATH; cannot stop" still stamped ended_at and ran
    post_session, which for shared mode set the project's live credential aside
    and re-symlinked it, on a container that was still running.

    "Stopped" includes the cases where there is nothing to stop (mock runtime,
    no container id): the session is not running, which is what the bookkeeping
    records. It excludes every case where we tried and could not, and every case
    where we could not even try.
    """
    if rec.runtime == "docker":
        if rec.docker is None or not rec.docker.container_id:
            click.echo(
                f"{rec.session_id[:12]}: no container_id recorded; cannot stop"
            )
            return True  # nothing is running under this record
        if not shutil.which("docker"):
            click.echo(
                f"{rec.session_id[:12]}: docker not on PATH; cannot stop"
            )
            return False
        cmd = ["docker", "kill" if force else "stop", "--", rec.docker.container_id]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            click.secho(
                f"{rec.session_id[:12]}: stop timed out after 30s "
                "(try --force or `docker kill` manually)",
                fg="red", err=True,
            )
            return False
        if result.returncode != 0:
            click.secho(
                f"{rec.session_id[:12]}: stop failed: {result.stderr.strip()}",
                fg="red",
                err=True,
            )
            return False
        click.secho(f"{rec.session_id[:12]}: stopped", fg="green")
        return True
    elif rec.runtime == "apptainer":
        if rec.apptainer is None or not rec.apptainer.slurm_jobid:
            click.echo(
                f"{rec.session_id[:12]}: no slurm jobid; cannot stop "
                "(foreground apptainer must be Ctrl-C'd in its terminal)"
            )
            return False
        if not shutil.which("scancel"):
            click.echo(
                f"{rec.session_id[:12]}: scancel not on PATH; cannot stop"
            )
            return False
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
            return False
        if result.returncode != 0:
            click.secho(
                f"{rec.session_id[:12]}: scancel failed: {result.stderr.strip()}",
                fg="red",
                err=True,
            )
            return False
        click.secho(
            f"{rec.session_id[:12]}: scancel jobid={rec.apptainer.slurm_jobid} "
            f"(applies to PENDING + RUNNING)",
            fg="green",
        )
        return True
    elif rec.runtime == "mock":
        click.echo(f"{rec.session_id[:12]}: mock runtime; nothing to stop")
        return True
    else:
        # Cannot tell. Treated as NOT stopped, because the bookkeeping tears
        # down credentials and the safe answer to "is it still running" when we
        # do not know is "assume yes".
        click.echo(f"{rec.session_id[:12]}: unknown runtime {rec.runtime!r}")
        return False


