"""`botainer access` — print the agent-facing AGENT_ACCESS.txt view."""

from __future__ import annotations

from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.core import composition
from botainer.inspect import access as access_renderer


@click.command("access")
@handle_refusals
def access() -> None:
    """Show the AGENT_ACCESS.txt the agent will see inside the container."""
    from botainer.cli import _common
    project_root = _common.find_project_root() or Path.cwd()
    spec = composition.compose_session(project_root, runtime_choice="auto", identity_accept=False)
    click.echo(access_renderer.render(spec))
    # Readiness audit #16: this is the compose-time (pre-hook) view. Plugin
    # pre_session / host_pre_launch hooks can add binds (e.g. hpc-modules
    # software roots, the credential bind) the agent will actually see. Warn so
    # the previewed access file isn't mistaken for the complete one (matches
    # inspect.py / dry_run.py).
    if any(h.when in ("pre_session", "host_pre_launch") for h in spec.hooks):
        click.secho(
            "# NOTE: compose-time view; enabled pre_session/host_pre_launch hooks "
            "may add binds the agent sees. See `botainer dry-run --include-hooks`.",
            fg="yellow", err=True,
        )
