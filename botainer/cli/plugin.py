"""`botainer plugin <subcmd>` — plugin lifecycle and per-plugin subcommands."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.plugins import install as install_module
from botainer.plugins import lifecycle as lifecycle_module
from botainer.plugins import manifest as manifest_module


@click.group("plugin", invoke_without_command=True)
@click.pass_context
def plugin(ctx: click.Context) -> None:
    """Manage plugins and invoke plugin subcommands."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@plugin.command("add")
@click.argument("source")
@click.option("--yes", is_flag=True, help="Skip interactive consent prompt.")
@handle_refusals
def add(source: str, yes: bool) -> None:
    """Install a plugin from a file:// URL, git URL, or tarball."""
    try:
        info = install_module.install(source, consent=yes)
    except install_module.PluginInstallError as exc:
        click.secho(f"refused: {exc.category}: {exc}", fg="red", err=True)
        sys.exit(5)
    click.echo(f"Installed plugin '{info.name}' v{info.version} (tree-sha: {info.tree_sha[:12]}).")


@plugin.command("enable")
@click.argument("name")
@handle_refusals
def enable(name: str) -> None:
    """Enable a plugin for this project."""
    # Task #235: was silent on typos — enable('age-claud') succeeded
    # and the plugin then "wasn't loaded" at start time with no hint
    # at WHY. Now check installed set first; suggest closest match on miss.
    installed = lifecycle_module.list_installed()
    installed_names = [p.name for p in installed]
    if name not in installed_names:
        import difflib
        suggestion = ""
        close = difflib.get_close_matches(name, installed_names, n=1, cutoff=0.6)
        if close:
            suggestion = f" Did you mean '{close[0]}'?"
        click.secho(
            f"refused: plugin '{name}' not installed.{suggestion}",
            fg="red", err=True,
        )
        click.echo("Installed plugins:")
        for n in sorted(installed_names):
            click.echo(f"  {n}")
        import sys
        sys.exit(4)
    lifecycle_module.enable(Path.cwd(), name)
    click.echo(f"Enabled '{name}' for this project.")


@plugin.command("disable")
@click.argument("name")
@handle_refusals
def disable(name: str) -> None:
    """Disable a plugin for this project."""
    lifecycle_module.disable(Path.cwd(), name)
    click.echo(f"Disabled '{name}' for this project.")


@plugin.command("list")
@click.option(
    "--available",
    "show_available",
    is_flag=True,
    help="Show all bundled first-party plugins (installed or not).",
)
def list_(show_available: bool) -> None:
    """List installed plugins on this host (or available with --available)."""
    installed = lifecycle_module.list_installed()
    installed_names = {p.name for p in installed}

    # Task #234: also read the per-project enabled set so users can see
    # which installed plugins are actually active for this project.
    enabled_in_project: set[str] = set()
    try:
        import yaml as _yaml
        cfg_path = Path.cwd() / ".botainer" / "config.yaml"
        if cfg_path.exists():
            _data = _yaml.safe_load(cfg_path.read_text()) or {}
            enabled_in_project = set(_data.get("plugins_enabled") or [])
    except Exception:
        pass

    # Legend up front so the markers are self-explanatory (UX: the old
    # output gave a "✓" to installed-but-INACTIVE plugins and a "·" to ENABLED
    # ones — backwards, and the description was truncated to 60 chars).
    click.echo("Legend:  ● enabled for this project    "
               "○ installed (not enabled here)    · available (not installed)")
    click.echo("")

    def _full_desc(man) -> str:
        # The FULL description (all lines joined into a paragraph), not a
        # 60-char first-line snippet.
        return " ".join((man.description or "").split()) if man else ""

    rows: list[tuple[str, str, str, str, str]] = []  # (name, version, tier, status, desc)
    for p in installed:
        try:
            desc = _full_desc(manifest_module.load_manifest(p.plugin_dir))
        except Exception:
            desc = ""
        status = "enabled" if p.name in enabled_in_project else "installed"
        rows.append((p.name, p.version, p.tier, status, desc))

    if show_available:
        from botainer.plugins.builtin import (
            BUILTIN_PLUGIN_NAMES,
            find_builtin_plugins_root,
        )
        root = find_builtin_plugins_root()
        for name in BUILTIN_PLUGIN_NAMES:
            if name in installed_names:
                continue
            desc, version = "", "?"
            if root is not None:
                try:
                    man = manifest_module.load_manifest(root / name)
                    desc, version = _full_desc(man), man.version
                except Exception:
                    pass
            rows.append((name, version, "first-party", "available", desc))

    import textwrap as _tw
    # Which installed plugins are inert when enabled (see
    # PluginManifest.enabling_is_inert). Best-effort: a manifest we cannot load
    # simply falls back to the normal label rather than failing the listing.
    _inert: dict[str, bool] = {}
    for _inst in lifecycle_module.list_installed():
        try:
            _inert[_inst.name] = manifest_module.load_manifest(
                _inst.plugin_dir).enabling_is_inert()
        except Exception:
            _inert[_inst.name] = False
    _marker = {"enabled": "●", "installed": "○", "available": "·"}
    for name, version, tier, status, desc in rows:
        # A plugin that contributes nothing at compose is unaffected by
        # `plugins_enabled`, so "installed, not enabled" misreads as a switch
        # left off (user report: they ran cluster jobs fine without
        # hpc-launcher enabled and could not tell whether that was a mistake).
        label = ("ENABLED" if status == "enabled"
                 else "installed (no enabling needed)"
                      if status == "installed" and _inert.get(name)
                 else "installed, not enabled" if status == "installed"
                 else "available")
        click.echo(f"{_marker[status]} {name}  v{version}  [{tier}]  {label}")
        if desc:
            # Full description, wrapped + indented under the plugin line.
            for line in _tw.wrap(desc, width=76):
                click.echo(f"      {line}")
        click.echo("")


@plugin.command("info")
@click.argument("name")
def info(name: str) -> None:
    """Show detailed information for a single plugin (manifest contents,
    config schema, hooks, capabilities)."""
    import json
    plugin_dir = None
    # First: check installed.
    for p in lifecycle_module.list_installed():
        if p.name == name:
            plugin_dir = p.plugin_dir
            break
    # Second: check bundled (available but not installed).
    if plugin_dir is None:
        from botainer.plugins.builtin import find_builtin_plugins_root
        root = find_builtin_plugins_root()
        if root is not None and (root / name).exists():
            plugin_dir = root / name
    if plugin_dir is None:
        click.secho(f"refused: plugin {name!r} not found (installed or bundled)", fg="red", err=True)
        click.echo("Try `botainer plugin list --available` to see what's available.")
        sys.exit(2)
    try:
        man = manifest_module.load_manifest(plugin_dir)
    except Exception as exc:
        click.secho(f"refused: manifest at {plugin_dir} failed to load: {exc}", fg="red", err=True)
        sys.exit(2)
    click.secho(f"Plugin: {man.name}", fg="cyan", bold=True)
    click.echo(f"  Version:     {man.version}")
    click.echo(f"  Tier:        {man.tier}")
    click.echo(f"  Kind:        {man.kind}")
    click.echo(f"  Runtimes:    {', '.join(man.runtimes)}")
    if man.license:
        click.echo(f"  License:     {man.license}")
    if man.maintainer:
        click.echo(f"  Maintainer:  {man.maintainer}")
    if man.depends_on:
        click.echo(f"  Depends on:  {', '.join(man.depends_on)}")
    if man.capabilities:
        click.echo(f"  Caps needed: {', '.join(man.capabilities)}")
    click.echo(f"  Trust mode:  {man.trust_required}")
    if man.description:
        click.echo()
        click.echo("Description:")
        for line in man.description.strip().splitlines():
            click.echo(f"  {line}")
    if man.hooks:
        click.echo()
        click.echo("Hooks:")
        for h in man.hooks:
            click.echo(f"  - {h.when:20s} {h.script}")
    if man.commands:
        click.echo()
        click.echo("CLI subcommands (run via `botainer plugin " + name + " <verb>`):")
        for cmd in man.commands:
            help_text = cmd.help or "(no help)"
            click.echo(f"  - {cmd.name:15s} {help_text}")
    if man.config_schema:
        click.echo()
        click.echo("Config schema (JSON):")
        for line in json.dumps(man.config_schema, indent=2).splitlines():
            click.echo(f"  {line}")


def state_dir_lookup() -> Any:
    from botainer.state import dir as _state_dir
    return _state_dir.ensure_user_state_dir(create_if_missing=False)


def _wire_per_plugin_subcommands() -> None:
    """For each installed plugin with declared commands, attach them under `plugin <name>`."""
    try:
        installed = lifecycle_module.list_installed()
    except Exception:
        return  # state-dir not initialized yet — fine; no plugin subcommands available
    for p in installed:
        cmd_map = manifest_module.declared_commands(p)
        if not cmd_map:
            continue
        group = click.Group(p.name, help=f"Subcommands for plugin '{p.name}'.")
        for verb, handler in cmd_map.items():
            group.add_command(handler, name=verb)
        plugin.add_command(group, name=p.name)


_wire_per_plugin_subcommands()
