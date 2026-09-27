"""`botainer dry-run` — print the exact runtime argv without launching."""

from __future__ import annotations

from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.core import composition
from botainer.auth_modes import AUTH_MODES, ONE_SHOT_AXIS_HELP
from botainer.core.spec import SessionSpec


@click.command("dry-run")
@click.option(
    "--agent",
    "agent_override",
    default=None,
    help=(
        "Render the argv for a DIFFERENT agent (e.g. `codex`) without editing "
        ".botainer/config.yaml — the same one-shot, in-memory swap "
        "`botainer start --agent` performs."
    ),
)
@click.option(
    "--include-hooks", is_flag=True,
    help=(
        "Run pre_session hooks (side-effects: file writes) so the argv "
        "reflects hook contributions. Off by default for hermetic dry-run."
    ),
)
@click.option(
    "--runtime",
    type=click.Choice(["docker", "apptainer", "auto"]),
    default="auto",
    help=(
        "Preview for a specific runtime. docker and apptainer render "
        "COMPLETELY different argv, so a preview that cannot be told which "
        "one you mean is a preview of something else."
    ),
)
@click.option(
    "--auth-mode",
    type=click.Choice(AUTH_MODES),
    default=None,
    help=(
        "Preview a different auth mode. This decides WHICH CREDENTIAL is "
        "bound, so it is the option whose absence mattered most here."
    ),
)
@click.option(
    "--auth-profile",
    default=None,
    help="Preview a different auth profile. " + ONE_SHOT_AXIS_HELP,
)
@handle_refusals
def dry_run(include_hooks: bool, agent_override: str | None,
            runtime: str, auth_mode: str | None,
            auth_profile: str | None) -> None:
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
    # --agent added alongside inspect's: `start` has had the one-shot swap
    # since #112, and the two commands whose entire job is "show me what will
    # happen" could not show it. A project inited for claude had no way to
    # preview codex short of launching it.
    # #204: these four now come from the caller instead of being hardcoded.
    #
    # `start` forwards six things to the composer; this forwarded ONE and
    # hardcoded two as constants, which LOOKS like coverage in a diff and is
    # not. So a preview whose entire job is "show me what will happen" could
    # not be told the runtime (docker and apptainer render completely
    # different argv) or the auth mode (which decides WHICH CREDENTIAL is
    # bound). It was structurally capable of previewing a different session
    # than the one that runs.
    #
    # DELIBERATELY STILL FIXED: `identity_accept=False` and no `--fork`. Both
    # of those WRITE — fork calls `write_project_id(..., overwrite=True)` and
    # identity_accept appends to path history. A command that promises to
    # change nothing must not offer them. The gap is recorded in
    # tests/unit/test_preview_matches_start.py rather than left to be
    # rediscovered as an oversight.
    spec = composition.compose_session(
        project_root,
        runtime_choice=runtime,
        identity_accept=False,
        agent_override=agent_override,
        auth_mode_override=auth_mode,
        auth_profile_override=auth_profile,
    )
    refusals: list[composition.HookRefusal] = []
    if include_hooks:
        # #160 (adversarial-review S2): run host_pre_launch too so the rendered
        # plan shows the hpc-modules software-root binds, not only pre_session
        # contributions. (Direct/docker path only; the sbatch OUTER-argv binds
        # are previewed via `botainer hpc submit --dry-run`.)
        #
        # A PREVIEW SURVIVES A HOOK THAT REFUSES TO RUN. `start` cannot — it
        # would launch a session missing that plugin's binds — but a preview
        # printing NOTHING is the worse failure, and it is reachable from
        # outside: the git plugin correctly refuses a repo whose .git/config
        # carries `core.sshCommand`, so before this, one line in a cloned repo
        # made `dry-run --include-hooks` print no plan at all. A repo you are
        # inspecting BECAUSE you do not trust it could suppress its own
        # inspection. Collect, report, and render what we have.
        #
        # Only "the hook did not run" is collected here; a REFUSED CONTRIBUTION
        # still ends the command. See composition._hook_failure_is_recoverable.
        spec = composition.run_host_pre_launch_hooks(
            spec, on_hook_error=refusals.append
        )
        spec = composition.run_pre_session_hooks(
            spec, on_hook_error=refusals.append
        )
    print_dry_run(spec, hooks_ran=include_hooks, refusals=refusals)


def print_dry_run(
    spec: SessionSpec,
    *,
    hooks_ran: bool = False,
    refusals: "list | None" = None,
) -> None:
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
    if refusals:
        # NAME THE HOOKS, NOT JUST THE FACT. "some hooks failed" sends you
        # hunting; the plugin, the phase and the hook's own message are what
        # let you decide whether the gap matters for what you were checking.
        click.echo(
            f"# INCOMPLETE: {len(refusals)} hook(s) did not run, so any env "
            f"or binds they contribute are MISSING from the plan above:",
            err=True,
        )
        for r in refusals:
            # EVERY LINE, NOT JUST THE HEADLINE. This printed `lines[0]` only,
            # so a hook whose message spans lines lost everything below the
            # first — and what a hook puts below the first line is the REMEDY.
            # Measured on the shipped default-path failure: agent-claude-shared
            # with no shared credential yet renders here as
            #
            #   agent-claude-shared (pre_session): … no shared credential at …
            #
            # while `Run: botainer auth login --shared --agent claude` was
            # dropped. `start` prints the whole thing — _refusal_handler renders
            # all of `exc.args[0]` — so the two commands described one failure
            # differently and the PREVIEW was the one that lost the fix.
            #
            # `#`-PREFIX EVERY CONTINUATION. This block is rendered to be
            # copy-pasteable; an unprefixed line would be a shell command rather
            # than a comment, and the text is plugin-authored.
            #
            # NO TRUNCATION HERE, deliberately: `_hook_stderr_excerpt` already
            # bounds the tail, and the remaining PLUGIN_HOOK_FAILED messages are
            # one-liners. A second cap would re-implement someone else's policy.
            lines = (r.message or "").strip().splitlines() or ["no message"]
            click.echo(f"#   {r.plugin} ({r.when}): {lines[0]}", err=True)
            for cont in lines[1:]:
                click.echo(f"#     {cont}", err=True)
        click.echo(
            "# `start` would REFUSE on the same failure rather than launch a "
            "session missing those contributions.",
            err=True,
        )
