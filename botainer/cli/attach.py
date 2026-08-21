"""`botainer attach` — reconnect to a detached running session.

When you've done `botainer start --detach` (or have an old session
still running from a prior shell), use `botainer attach` to bring its
stdio back into your terminal.

Behavior by runtime:

- **Docker**: `docker attach <container_id>`. Inherits stdin/stdout/
  stderr; Ctrl-C is forwarded to the container. The user can detach
  again with the Docker keystroke sequence (typically Ctrl-P Ctrl-Q
  if `-d` was used) but for the common case they just stay attached.

- **Apptainer + Slurm**: refuses with a hint. Apptainer doesn't have
  a docker-attach equivalent because the session runs as a foreground
  Slurm step in the user's terminal. If the user wants to reattach to
  a running step (e.g. they got disconnected), the right move is
  `sattach <jobid>` or `srun --overlap --jobid <jid> --pty bash`.

- **Mock**: refuses (no real container to attach to).
"""

from __future__ import annotations

import os
import shutil

import click

from botainer.cli import _common
from botainer.cli._refusal_handler import handle_refusals
from botainer.core import identity
from botainer.state import dir as state_dir
from botainer.state import liveness, session_record


@click.command("attach")
@click.argument("session_id", required=False)
@handle_refusals
def attach(session_id: str | None) -> None:
    """Reconnect stdio to a running session container."""
    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse(
            "attach",
            "not inside a botainer project",
            "cd into a project with .botainer/project-id",
        )

    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    uid, _ = identity.resolve_identity(project_root, identity_accept=True)
    proj_paths = state_dir.ensure_project_dirs(paths, uid)

    records = session_record.list_sessions(proj_paths.sessions_dir)
    if session_id:
        target = _common.find_session_by_prefix(records, session_id, cmd="attach")
        if target is None:
            _common.refuse(
                "attach",
                f"no session matches {session_id!r}",
                "check `botainer status` for available IDs",
            )
    else:
        # Task #147: previously this picked records[0] (newest) regardless
        # of liveness. If the newest session was already dead (container
        # exited; Slurm job finished), attach raced into a "no such
        # container" failure that looked like an attach bug. Filter to
        # alive sessions first; only attach to a confirmed-running one.
        alive = [r for r in records if liveness.is_session_alive(r)]
        if len(alive) > 1:
            ids = ", ".join(r.session_id[:12] for r in alive[:5])
            _common.refuse(
                "attach",
                f"{len(alive)} running sessions; specify a session-ID prefix",
                f"running: {ids}",
            )
        target = alive[0] if alive else None
        if target is None:
            _common.refuse(
                "attach",
                "no RUNNING sessions for this project "
                f"({len(records)} total records, none alive)",
                "start one with `botainer start --detach`",
            )

    if target.runtime == "docker":
        if target.docker is None or not target.docker.container_id:
            _common.refuse(
                "attach",
                f"session {target.session_id[:12]} has no docker container_id recorded",
                "the session record is incomplete; try `botainer status` for current state",
            )
        if not shutil.which("docker"):
            _common.refuse(
                "attach",
                "docker not on PATH; cannot attach",
                "install Docker (or run on a host where it's available)",
            )
        # exec docker attach in our terminal (replaces this process).
        # Use os.execvp so the user's Ctrl-C / detach sequences go straight to docker.
        # `--` separator defends against container_id starting with `-`
        # (session_record validates on load but argv hardening is cheap).
        argv = _build_docker_attach_argv(target.docker.container_id)
        click.echo(
            f"attaching to {target.session_id[:12]} "
            f"(container {target.docker.container_id[:12]}); "
            f"to detach without stopping, type Ctrl-P Ctrl-Q.",
            err=True,
        )
        os.execvp("docker", argv)
        # never returns
    elif target.runtime == "apptainer":
        slurm_jobid = (
            target.apptainer.slurm_jobid
            if target.apptainer
            else None
        )
        if slurm_jobid:
            _common.refuse(
                "attach",
                "botainer attach doesn't support Apptainer + Slurm at v0.1.0",
                f"use `botainer hpc attach --jobid {slurm_jobid}` (srun --overlap into "
                f"the running job); as a raw fallback: "
                f"`srun --overlap --jobid={slurm_jobid} --pty bash` or "
                f"`sattach {slurm_jobid}.0`",
            )
        _common.refuse(
            "attach",
            "Apptainer foreground sessions can't be reattached (no Slurm step recorded)",
            "the session is running in some terminal; switch back to that one",
        )
    elif target.runtime == "mock":
        _common.refuse(
            "attach",
            "mock runtime has nothing to attach to",
            "this session was composed but never actually launched",
        )
    else:
        _common.refuse(
            "attach",
            f"unknown runtime {target.runtime!r}",
            "this shouldn't happen; report this as a bug",
        )


def _build_docker_attach_argv(container_id: str) -> list[str]:
    """Build the argv for `docker attach`. Task #276: factored out so
    tests can assert the actual list structure, not grep source text.

    The `--` separator MUST appear before container_id so that a
    container_id starting with `-` is not parsed as a flag. This is
    belt-and-suspenders on top of session_record's validation.
    """
    return ["docker", "attach", "--", container_id]
