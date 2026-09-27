"""`botainer shell` runs a command inside the composed container, without an agent.

It uses the same configuration, plugins, hooks, binds and environment as `start`.
An interactive invocation opens a shell; `-c` runs a command and exits. This
allows diagnostics to inspect the actual container view without reconstructing
an adapter command by hand.

Hooks run so their credential and software-root contributions are included.
Hook-created sidecars are cleaned up in a `finally` block. The command grants
no additional capabilities beyond the composed session."""
from __future__ import annotations

from pathlib import Path

import click


@click.command("shell")
@click.option("-c", "--command", "command", default=None,
              help="Run one command in the cage and exit, instead of an "
                   "interactive shell.")
@click.option("--shell", "shell_bin", default="/bin/bash", show_default=True,
              help="Shell to exec when no --command is given.")
@click.option("--runtime", type=click.Choice(["auto", "docker", "apptainer"]),
              default="auto", show_default=True)
@click.option("--agent", "agent_override", default=None,
              help="Compose as this agent would (its binds, its credential).")
@click.option("--print-argv", is_flag=True,
              help="Print the exact runtime command and the launch decisions "
                   "before running, then run it. Use this when the container "
                   "does something you cannot explain.")
@click.option("--no-hooks", is_flag=True,
              help="Skip pre_session/host_pre_launch hooks. Faster, but the "
                   "cage you get is the compose-time approximation and NOT "
                   "what a session sees — the hook-contributed binds and env "
                   "will be missing.")
def shell(command: str | None, shell_bin: str, runtime: str,
          agent_override: str | None, print_argv: bool, no_hooks: bool) -> None:
    """Open a shell INSIDE this project's container. The agent does not run.

    Same cage the agent gets — same binds, same env, same read-only overlays.
    Use it to answer "what can the agent actually see?" without guessing.

        botainer shell
        botainer shell -c 'ls -la $CODEX_HOME'
        botainer shell --agent codex -c 'env | grep CODEX'
    """
    import sys

    from botainer.cli import _common
    from botainer.core import composition

    project_root = _common.find_project_root() or Path.cwd()
    spec = composition.compose_session(
        project_root, runtime_choice=runtime, identity_accept=False,
        agent_override=agent_override,
        # This one LAUNCHES, so it owns the masking-dir reset. (#227)
        reset_null_anchor=True,
    )

    refusals: list = []
    if not no_hooks:
        # Collect rather than die: a hook that refuses (no credential yet, a
        # guarded-mode git config) must not cost you the diagnostic — being
        # unable to look is exactly when you most need to.
        spec = composition.run_host_pre_launch_hooks(
            spec, on_hook_error=refusals.append)
        spec = composition.run_pre_session_hooks(
            spec, on_hook_error=refusals.append)

    try:
        if refusals:
            click.secho(
                f"# {len(refusals)} hook(s) did not run, so this cage is "
                f"missing what they contribute:", fg="yellow", err=True)
            for r in refusals:
                first = (r.message or "").strip().splitlines()
                click.secho(f"#   {r.plugin} ({r.when}): "
                            f"{first[0] if first else ''}", fg="yellow", err=True)

        # Same agent-facing files a session gets, rendered from the POST-hook
        # spec, because the binds below reference them.
        composition.render_agent_files(spec)

        inner = ([shell_bin] if command is None
                 else [shell_bin, "-lc", command])

        # DROP `nudge` FROM THE RUN SPEC, and only from it.
        #
        # `nudge` makes the docker adapter wrap the run in `screen -dmS` — a
        # DETACHED screen — so `botainer nudge` can inject text into the
        # AGENT's stdin later. `attach()` then puts your terminal on that pty.
        # For a one-shot `-c`, the command finishes in milliseconds, screen
        # exits, and by the time attach runs there is no session left: the
        # command really ran and its output died with the pty. That is exactly
        # what `shell -c ls` did — launched cleanly, printed nothing.
        #
        # There is no agent here, so there is nothing to nudge. Stripping it
        # takes the adapter's bare `docker run -it` path, which inherits our
        # stdio.
        #
        # THE CAGE IS UNCHANGED, and that is checkable rather than asserted:
        # `plugins/nudge/` contains a manifest and a README and NO hooks, so it
        # contributes no bind and no env. `mount_plan` is carried over
        # untouched; test_the_cage_is_identical_with_nudge_stripped pins it.
        # `spec` keeps nudge, so teardown still sees the real session.
        run_spec = spec.model_copy(update={
            "entrypoint_wraps": (tuple(inner),),
            "plugins_enabled": tuple(
                p for p in spec.plugins_enabled if p != "nudge"),
        })

        click.secho(
            f"─── botainer shell ({run_spec.runtime}: {run_spec.image}) "
            f"— the agent is NOT running ───", fg="cyan", err=True)
        if command is None:
            click.secho("    Same binds and env as a real session. Ctrl-D to "
                        "leave.", fg="cyan", err=True)

        # THROUGH `launch`, NEVER render_argv + subprocess.run OF OUR OWN.
        # The first version of this command did the latter and broke instantly
        # on macOS: `launch` also calls `_prepare_nested_bind_placeholders`,
        # which creates the mountpoints inside the null-bind masking anchor
        # that Docker Desktop's virtiofs refuses to create on the fly. Skipping
        # it produced
        #     mountpoint ".../null-bind-anchor/AGENT_HINTS.md" is outside of
        #     rootfs
        # when those mountpoints have not been prepared.
        #
        # That is this project's recorded defect class — a second
        # implementation of the launch path that drifts from the first, the
        # same shape as the sbatch cage that mirrored the adapter until #52
        # made it BE the adapter. One chokepoint; the only thing `shell`
        # varies is WHAT RUNS INSIDE, which is a parameter, not a code path.
        # LAUNCH **AND ATTACH**, the pair `start` uses. Launch alone is not a
        # session: when the `nudge` plugin is enabled — it is by default — the
        # docker adapter wraps the whole invocation in `screen -dmS`, a
        # DETACHED screen, so nudge can inject into the agent's stdin later.
        # `attach` is what puts your terminal on that pty.
        #
        # Calling only `launch` is why the second version of this command ran
        # your command successfully and showed you NOTHING: `-c ls` executed
        # inside a detached screen and the output went to a pty nobody was
        # reading. Two bugs in a row from doing PART of what `start` does —
        # first skipping the placeholder prep, then skipping the attach.
        # On request, print the planned runtime command and screen wrapping
        # before launch so a failed or detached command can be diagnosed.
        if print_argv:
            from shlex import quote
            argv_preview = composition.render_argv(run_spec)
            click.secho("# runtime command:", fg="cyan", err=True)
            click.secho("  " + " ".join(quote(a) for a in argv_preview),
                        fg="cyan", err=True)
            click.secho(
                f"# screen wrap: "
                f"{'YES' if 'nudge' in run_spec.plugins_enabled else 'no'}"
                f"  (nudge in run spec: "
                f"{'nudge' in run_spec.plugins_enabled}); "
                f"entrypoint_wraps={run_spec.entrypoint_wraps}",
                fg="cyan", err=True)

        handle = composition.launch(run_spec)
        if print_argv:
            click.secho(f"# launched: id={handle.id} pid={handle.pid} "
                        f"extras={handle.extras}", fg="cyan", err=True)
        rc = composition.attach(handle)
        # ALWAYS report the exit code for a one-shot. Silence plus success is
        # indistinguishable from silence plus failure, and that ambiguity is
        # what made this command undebuggable.
        if command is not None:
            click.secho(f"# container exited {rc}", fg="cyan", err=True)
        sys.exit(rc)
    finally:
        if not no_hooks:
            # Stop anything a hook started. Errors are logged inside, not
            # raised — the same contract selftest relies on.
            composition.run_post_session_hooks(spec)
