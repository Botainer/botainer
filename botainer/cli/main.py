"""Top-level CLI entry point.

Subcommand modules are imported lazily so `botainer --version` and `botainer --help`
stay fast.

Per codex review 45 §8: top-level help groups commands by purpose so a
new user sees a guided path (getting-started → daily session → auth/config
→ inspection → HPC → advanced) rather than a flat 20-item toolbox.
"""

from __future__ import annotations

import sys

import click

from botainer import __version__

# Codex 45#8: commands grouped by user-facing purpose. The order
# within each group is the recommended order for that group; the
# order of groups is the recommended onboarding sequence.
_COMMAND_GROUPS: list[tuple[str, list[str]]] = [
    ("Getting started",      ["setup", "init", "start", "doctor"]),
    ("Daily session",        ["status", "attach", "stop", "list", "nudge"]),
    ("Auth and config",      ["auth", "config", "policy"]),
    ("Inspection",           ["inspect", "dry-run", "access", "selftest", "where", "tips"]),
    ("HPC",                  ["hpc"]),
    ("Advanced",             ["plugin", "image", "schema", "help"]),
]


class _GroupedHelpGroup(click.Group):
    """Click group that renders subcommands in named sections.

    Falls back to Click's default alphabetical list for any commands
    not assigned to a group, so a newly-added command isn't silently
    hidden — it shows up under "Other" until it's added to the table.
    """

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        commands_dict: dict[str, click.Command] = {}
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            commands_dict[name] = cmd
        seen: set[str] = set()
        for group_name, members in _COMMAND_GROUPS:
            rows = []
            for cmd_name in members:
                if cmd_name in commands_dict:
                    cmd = commands_dict[cmd_name]
                    seen.add(cmd_name)
                    short = cmd.get_short_help_str(limit=80)
                    rows.append((cmd_name, short))
            if rows:
                with formatter.section(group_name):
                    formatter.write_dl(rows)
        # Catch-all for any registered command not in a group.
        leftover_rows = []
        for cmd_name, cmd in sorted(commands_dict.items()):
            if cmd_name in seen:
                continue
            short = cmd.get_short_help_str(limit=80)
            leftover_rows.append((cmd_name, short))
        if leftover_rows:
            with formatter.section("Other"):
                formatter.write_dl(leftover_rows)


@click.group(
    cls=_GroupedHelpGroup,
    invoke_without_command=True,
)
@click.version_option(__version__, prog_name="botainer")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """botainer — run AI coding agents in inspectable containers.

    \b
    Common path on a new host:
      1. botainer setup                       # one-time: install plugins + user policy
      2. botainer image build agent-claude    # build the agent image (~8-12 min first time)
      3. cd <project> && botainer init        # per-project config
      4. botainer auth login --agent claude   # OAuth (after init, so isolated mode works)
      5. botainer start

    \b
    For HPC: botainer hpc setup → botainer hpc build agent-claude →
    botainer hpc submit --dry-run → botainer hpc submit.
    For all commands grouped by purpose, run `botainer --help`.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)


def _register_subcommands() -> None:
    """Register subcommands. Lazy-loaded so help/version stay fast."""
    from botainer.cli import (
        access,
        attach,
        doctor,
        dry_run,
        help_install,
        init,
        inspect,
        list_cmd,
        nudge,
        plugin,
        policy,
        setup,
        start,
        status,
        stop,
        where,
    )

    cli.add_command(init.init)
    cli.add_command(setup.setup)
    cli.add_command(start.start)
    cli.add_command(attach.attach)
    cli.add_command(stop.stop)
    cli.add_command(status.status)
    cli.add_command(list_cmd.list_)
    cli.add_command(inspect.inspect)
    cli.add_command(where.where)
    cli.add_command(dry_run.dry_run)
    cli.add_command(access.access)
    cli.add_command(doctor.doctor)
    cli.add_command(policy.policy)
    cli.add_command(plugin.plugin)
    cli.add_command(nudge.nudge)
    cli.add_command(help_install.help_group)
    from botainer.cli import image
    cli.add_command(image.image)
    from botainer.cli import schema
    cli.add_command(schema.schema)
    from botainer.cli import config_cmd
    cli.add_command(config_cmd.config)
    from botainer.cli import hpc
    cli.add_command(hpc.hpc)
    from botainer.cli import auth as auth_cmd
    cli.add_command(auth_cmd.auth)
    from botainer.cli import selftest as selftest_cmd
    cli.add_command(selftest_cmd.selftest)
    from botainer.cli import tips as tips_cmd
    cli.add_command(tips_cmd.tips)


def _maybe_tip_footer(exit_code: int, argv: list[str]) -> None:
    """Show one tip after a SUCCESSFUL command (never on error). Skipped for
    help/version, the bare invocation, and `tips` itself. Must never raise. Draws
    on core + every INSTALLED plugin's tips (plugin tips lead with an available
    capability + how to turn it on — useful whether or not it's enabled yet)."""
    try:
        if exit_code != 0:
            return
        if any(a in ("--help", "-h", "--version") for a in argv):
            return
        first = next((a for a in argv if not a.startswith("-")), None)
        if first in (None, "tips"):  # bare (help already shown) or the tips list
            return
        from botainer.tips import print_tip_footer

        print_tip_footer(enabled=None)  # None → core + all installed plugins
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    """Console entry point. Runs the click CLI, then prints a tip footer on
    success. Wrapping (rather than calling `cli` directly) is what lets the footer
    fire regardless of whether a subcommand returns or calls ctx.exit()."""
    args = list(sys.argv[1:] if argv is None else argv)
    code = 0
    try:
        cli.main(args=args, prog_name="botainer")
    except SystemExit as exc:  # click always exits this way in standalone mode
        code = exc.code if isinstance(exc.code, int) else (0 if not exc.code else 1)
    _maybe_tip_footer(code, args)
    return code


_register_subcommands()


if __name__ == "__main__":
    sys.exit(main())
