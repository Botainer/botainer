"""`botainer dry-run` — print the exact runtime argv without launching."""

from __future__ import annotations

from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.core import composition
from botainer.core.spec import SessionSpec


@click.command("dry-run")
@click.option(
    "--include-hooks", is_flag=True,
    help=(
        "Run pre_session hooks (side-effects: file writes) so the argv "
        "reflects hook contributions. Off by default for hermetic dry-run."
    ),
)
@handle_refusals
def dry_run(include_hooks: bool) -> None:
    """Show the runtime command that would execute.

    Task #151: by default dry-run is HERMETIC — it does NOT run
    pre_session hooks, so the rendered argv omits any env / binds /
    sidecars those hooks would contribute. The argv shown may differ
    from what `botainer start` actually executes if any enabled plugin
    has a pre_session hook. Pass --include-hooks to run them (note:
    pre_session has side effects: writes files, spawns sidecars, etc.).
    """
    from botainer.cli import _common
    project_root = _common.find_project_root() or Path.cwd()
    spec = composition.compose_session(project_root, runtime_choice="auto", identity_accept=False)
    if include_hooks:
        # #160 (adversarial-review S2): run host_pre_launch too so the rendered
        # plan shows the hpc-modules software-root binds, not only pre_session
        # contributions. (Direct/docker path only; the sbatch OUTER-argv binds
        # are previewed via `botainer hpc submit --dry-run`.)
        spec = composition.run_host_pre_launch_hooks(spec)
        spec = composition.run_pre_session_hooks(spec)
    print_dry_run(spec, hooks_ran=include_hooks)


def print_dry_run(spec: SessionSpec, *, hooks_ran: bool = False) -> None:
    """Render argv as a shell-safe-quoted, copy-paste-friendly multi-line block."""
    from shlex import quote

    argv = composition.render_argv(spec)
    click.echo("# botainer dry-run — exact argv (would not exec):")
    for i, a in enumerate(argv):
        suffix = " \\" if i < len(argv) - 1 else ""
        click.echo(f"  {quote(a)}{suffix}")
    click.echo()
    click.echo(f"# Adapter: {spec.runtime}")
    click.echo(f"# Image:   {spec.image}")
    click.echo(f"# Binds:   {len(spec.mount_plan.binds)}")
    if not hooks_ran:
        # #160 (adversarial-review S2): include host_pre_launch (hpc-modules
        # software-root binds + module env) — not only pre_session — so the
        # warning names every start-time contributor the compose-time argv omits.
        any_start_hook = any(
            h.when in ("pre_session", "host_pre_launch") for h in spec.hooks
        )
        if any_start_hook:
            click.echo(
                "# WARNING: pre_session / host_pre_launch hooks were NOT run; "
                "actual argv at start time may include additional env / binds "
                "contributed by these hooks (e.g. hpc-modules software-root "
                "binds). Re-run with --include-hooks to see them.",
                err=True,
            )
