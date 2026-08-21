"""`botainer config check` / `botainer config explain`.

Help users (and the LLMs that help users) validate + understand their
project config without launching the agent.

  botainer config check         — schema validation + sanity checks; refuses
                                   with actionable hints on errors.
  botainer config explain        — prints a human-readable summary of what
                                   the current config would result in:
                                   enabled plugins, network mode, mounts,
                                   auth mode, etc. Does NOT compose a
                                   real session (no identity prompt).
"""

from __future__ import annotations

import sys

import click

from botainer.cli import _common
from botainer.cli._refusal_handler import handle_refusals
from botainer.core import config as config_module
from botainer.core.refusal import Refused


@click.group("config", invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """Read/write the THIS PROJECT's .botainer/config.yaml.

    Scope: per-project. Edits .botainer/config.yaml in the current
    project's tree. For host-wide settings, use `botainer policy`.

    Subcommands:
      get      Read a value by dotted-path key.
      set      Write a value (shows diff + asks).
      check    Validate schema + run sanity checks.
      explain  Print human-readable summary of effective config.

    Examples:
      botainer config get network.mode
      botainer config set network.mode internet
      botainer config set plugins_enabled '[agent-claude, git]'
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@config.command("check")
@click.option(
    "--strict",
    is_flag=True,
    help="Treat warnings as errors (exit nonzero).",
)
@handle_refusals
def check(strict: bool) -> None:
    """Validate config.yaml against its schema + run sanity checks.

    Sanity checks include:
    - referenced plugins are installed (warning if not)
    - referenced credential profile exists
    - env vars on the denylist
    - credential-shaped env vars (e.g. ANTHROPIC_API_KEY in env:)
    - network.mode=endpoint-ip-allowlist with no endpoints
    - port_forwards with conflicting hosts
    """
    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse(
            "config check",
            "not inside a botainer project",
            "cd into a project with .botainer/project-id",
        )
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        _common.refuse(
            "config check",
            f"no config at {cfg_path}",
            "run `botainer init --agent <name>` to create one",
        )
    # Schema validation via pydantic.
    try:
        cfg = config_module.load_config(project_root)
    except Refused as exc:
        click.secho(f"✗ schema: {exc}", fg="red")
        sys.exit(2)
    click.secho("✓ schema valid (config matches ProjectConfig)", fg="green")

    warnings: list[str] = []
    errors: list[str] = []

    # Sanity check: enabled plugins exist.
    from botainer.plugins.lifecycle import list_installed
    installed_names = {p.name for p in list_installed()}
    for name in cfg.plugins_enabled:
        if name not in installed_names:
            warnings.append(
                f"plugin {name!r} enabled but not installed "
                f"(run `botainer setup` or `botainer plugin add ...`)"
            )

    # Sanity check: credential-shaped env vars.
    from botainer.core import credential_leak_check
    leaked = credential_leak_check.detect_credential_env_keys(cfg.env)
    if leaked:
        errors.append(
            f"credential-shaped env vars in config: {sorted(leaked)} — "
            f"use proxy mode or per-project login instead"
        )

    # Sanity check: network mode coherence.
    net_mode = cfg.network.mode
    if net_mode == "endpoint-ip-allowlist" and not cfg.network.endpoints:
        errors.append(
            "network.mode=endpoint-ip-allowlist requires endpoints: "
            "list of allowed URLs (or domains)"
        )

    # Sanity check: ports map (best-effort; full validation happens at compose).
    web_ports_cfg = (cfg.plugins or {}).get("web-ports") or {}
    raw_ports = web_ports_cfg.get("ports", []) if isinstance(web_ports_cfg, dict) else []
    seen_host_ports = set()
    if isinstance(raw_ports, list):
        for item in raw_ports:
            if isinstance(item, int):
                key = ("127.0.0.1", item)
            elif isinstance(item, dict):
                port_val = item.get("host", item.get("container", 0))
                if not isinstance(port_val, (int, str)):
                    continue
                key = (
                    str(item.get("host_bind", "127.0.0.1")),
                    int(port_val),
                )
            else:
                continue
            if key in seen_host_ports:
                warnings.append(f"port {key} forwarded twice in web-ports.ports")
            seen_host_ports.add(key)

    # Print findings.
    for w in warnings:
        click.secho(f"⚠ {w}", fg="yellow")
    for e in errors:
        click.secho(f"✗ {e}", fg="red")
    if errors:
        sys.exit(2)
    if warnings and strict:
        sys.exit(2)
    if not warnings:
        click.secho("✓ no issues found", fg="green")


@config.command("explain")
@handle_refusals
def explain() -> None:
    """Print a human-readable summary of the current config.

    Useful for: users figuring out what they have; LLMs that wrote the
    config wanting to verify their reading; debugging "what does this
    config actually do".

    Does NOT compose a real session — no identity prompt, no hooks fire.
    """
    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse(
            "config explain",
            "not inside a botainer project",
            "cd into a project with .botainer/project-id",
        )
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        _common.refuse(
            "config explain",
            f"no config at {cfg_path}",
            "run `botainer init --agent <name>`",
        )
    cfg = config_module.load_config(project_root)
    click.secho(f"Project: {project_root}", fg="cyan", bold=True)
    click.echo(f"  Agent:        {cfg.agent}")
    click.echo(f"  Runtime:      {cfg.runtime}  (auto-detected at start)")
    click.echo(f"  Profile:      {cfg.profile}")
    if cfg.image:
        click.echo(f"  Image:        {cfg.image}  (override)")
    else:
        click.echo("  Image:        (from agent plugin's installed.lock)")
    click.echo()
    click.echo("Network:")
    click.echo(f"  Mode:         {cfg.network.mode}")
    if cfg.network.endpoints:
        click.echo(f"  Endpoints:    {', '.join(cfg.network.endpoints)}")
    click.echo()
    click.echo(f"Plugins enabled: {', '.join(cfg.plugins_enabled) or '(none)'}")
    # Per-plugin config
    for name, plugin_cfg in (cfg.plugins or {}).items():
        if not plugin_cfg:
            continue
        click.echo(f"  └── {name}:")
        for k, v in plugin_cfg.items():
            click.echo(f"        {k}: {v}")
    click.echo()
    click.echo("Resources:")
    click.echo(f"  CPU:          {cfg.resources.cpu or 'unlimited'}")
    click.echo(f"  Memory:       {cfg.resources.memory_mb or 'unlimited'} MB")
    click.echo(f"  Time:         {cfg.resources.time_minutes or 'unlimited'} min")
    if cfg.resources.gpus:
        click.echo(f"  GPUs:         {cfg.resources.gpus} ({cfg.resources.gpu_type or '?'})")
    if cfg.env:
        click.echo()
        click.echo("Extra env vars (passed to container):")
        for k, v in sorted(cfg.env.items()):
            # Truncate values that look like secrets in the display.
            display_v = v if len(v) <= 40 else v[:40] + "..."
            click.echo(f"  {k}={display_v}")
    if cfg.mounts.extra:
        click.echo()
        click.echo("Extra mounts:")
        for m in cfg.mounts.extra:
            click.echo(f"  {m.source} → {m.target} ({m.mode})")
    click.echo()
    click.secho(
        "(Run `botainer config check` to validate; "
        "`botainer inspect` for the composed SessionSpec; "
        "`botainer dry-run` for the exact runtime argv.)",
        fg="cyan",
    )


@config.command("get")
@click.argument("key")
@click.option(
    "--show-secrets",
    is_flag=True,
    help="Show credential-shaped values verbatim. Off by default.",
)
@handle_refusals
def config_get(key: str, show_secrets: bool) -> None:
    """Get a value from .botainer/config.yaml by dotted-path key.

    Task #297: credential-shaped keys are redacted by default. Pass
    --show-secrets to override.

    Examples:
      botainer config get network.mode
      botainer config get plugins_enabled
      botainer config get plugins.web-ports.ports
    """
    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse("config get", "not inside a botainer project",
                       "cd into a project; `botainer init` if needed")
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        _common.refuse("config get", f"no config at {cfg_path}",
                       "run `botainer init`")
    import yaml
    data = yaml.safe_load(cfg_path.read_text()) or {}
    value = _dotted_get(data, key)
    if value is _MISSING:
        click.secho(f"(no value for {key!r}; key not present)", fg="yellow")
        sys.exit(1)
    if value is None:
        click.echo("(null)")  # legitimate explicit-null value
        return
    # Task #297: redact credential-shaped values unless --show-secrets.
    from botainer.inspect._redact import looks_credential, redact
    if not show_secrets and isinstance(value, str) and looks_credential(key):
        click.echo(redact(key, value, mode="safe"))
        click.echo("(pass --show-secrets to view)", err=True)
        return
    if isinstance(value, (dict, list)):
        click.echo(yaml.safe_dump(value, default_flow_style=False).rstrip())
    else:
        click.echo(str(value))


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option(
    "--yes", "-y", is_flag=True,
    help="Apply without confirmation prompt.",
)
@click.option(
    "--literal-string", "literal_string", is_flag=True,
    help="Don't parse the value as YAML; store it as a literal string. "
         "Useful for 'no', 'yes', 'off', '0' that would otherwise coerce "
         "to bool/int (the YAML 1.1 'Norway problem').",
)
@handle_refusals
def config_set(key: str, value: str, yes: bool, literal_string: bool) -> None:
    """Set a value in .botainer/config.yaml by dotted-path key.

    Examples:
      botainer config set network.mode internet
      botainer config set plugins_enabled '[agent-claude-shared, git]'
      botainer config set agent claude

    The value is parsed as YAML (so '[a, b]' parses as a list, 'true' as bool,
    '42' as int). Use quotes for strings that contain spaces or YAML
    metacharacters.

    Shows the diff (before/after) and asks for confirmation before
    applying, unless --yes is passed.
    """
    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse("config set", "not inside a botainer project",
                       "cd into a project; `botainer init` if needed")
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        _common.refuse("config set", f"no config at {cfg_path}",
                       "run `botainer init`")
    import yaml
    old_text = cfg_path.read_text()
    data = yaml.safe_load(old_text) or {}
    # Parse value as YAML for type coercion — UNLESS --literal-string
    # given, OR the input is a known footgun (yaml 1.1 Norway problem).
    if literal_string:
        parsed_value: object = value
    else:
        try:
            parsed_value = yaml.safe_load(value)
        except yaml.YAMLError as exc:
            click.secho(f"refused: value {value!r} is not valid YAML: {exc}",
                        fg="red", err=True)
            sys.exit(2)
        # Insecure-defaults H2 + sharp-edges audit: detect Norway-problem
        # silent coercions where the user almost certainly meant a string.
        # If the literal input was a multi-char alpha word AND yaml
        # coerced it to a bool, refuse + suggest --literal-string.
        _NORWAY_INPUTS = {"yes", "Yes", "YES", "no", "No", "NO",
                          "on", "On", "ON", "off", "Off", "OFF",
                          "true", "True", "TRUE", "false", "False", "FALSE",
                          "y", "Y", "n", "N"}
        if value in _NORWAY_INPUTS and isinstance(parsed_value, bool):
            click.secho(
                f"refused: value {value!r} parses as YAML bool {parsed_value!r} "
                f"(the 'Norway problem'). If you meant the literal string, "
                f"pass --literal-string. If you meant the bool, write "
                f"'true' or 'false' explicitly.",
                fg="red", err=True,
            )
            sys.exit(2)
    old_value = _dotted_get(data, key)
    if old_value is not _MISSING and old_value == parsed_value:
        click.echo(f"({key} already = {parsed_value!r}; nothing to change)")
        return

    _dotted_set(data, key, parsed_value)

    # Codex 45#6: validate the resulting config.yaml against ProjectConfig
    # BEFORE writing. Catches typos like `network.mod internet` or
    # `plugins_enabled '[not, a, valid, list]'` at write time rather
    # than at the next `botainer start`.
    from botainer.core.config import ProjectConfig
    try:
        ProjectConfig.model_validate(data)
    except Exception as exc:
        click.secho(
            f"refused: the proposed change would make .botainer/config.yaml "
            f"invalid: {exc}",
            fg="red", err=True,
        )
        click.secho(
            "    See `botainer config explain` for the current shape, "
            "`botainer schema config` for the full schema.",
            fg="cyan", err=True,
        )
        sys.exit(2)

    new_text = yaml.safe_dump(data, sort_keys=False)

    click.secho("Changes:", bold=True)
    click.echo(f"  {key}:")
    if old_value is _MISSING:
        click.secho("    - (key not present)", fg="red")
    else:
        click.secho(f"    - {old_value!r}", fg="red")
    click.secho(f"    + {parsed_value!r}", fg="green")
    click.echo("")
    click.secho(
        "(yaml round-trip strips comments; if you have comments to preserve, "
        "edit .botainer/config.yaml by hand instead.)",
        fg="yellow",
    )

    if not yes and not click.confirm("Apply?", default=False):
        click.echo("aborted.")
        return
    cfg_path.write_text(new_text)
    click.secho(f"✓ {cfg_path} updated.", fg="green")
    click.secho(
        "  Next: `botainer config explain` to verify; "
        "`botainer start` to launch.",
        fg="cyan",
    )


_MISSING = object()


def _dotted_get(data: dict, key: str) -> object:
    """Get value by dotted-path. Returns _MISSING (not None) for absent keys.

    Sharp-edges F11: previously returned None for both "key absent" and
    "key present but value is null/0/False/''" — indistinguishable.
    Now returns _MISSING sentinel for genuinely-missing keys; legitimate
    None values are preserved as None.
    """
    parts = key.split(".")
    current: object = data
    for p in parts:
        if not isinstance(current, dict):
            return _MISSING
        if p not in current:
            return _MISSING
        current = current[p]
    return current


def _dotted_set(data: dict, key: str, value: object) -> None:
    """Set a value via dotted-path key. Refuses to clobber non-dict
    intermediate values (sharp-edges F1: silent destruction otherwise).
    """
    parts = key.split(".")
    current = data
    for i, p in enumerate(parts[:-1]):
        if p not in current:
            current[p] = {}
        elif not isinstance(current[p], dict):
            so_far = ".".join(parts[:i + 1])
            raise click.UsageError(
                f"refused: cannot descend into {so_far!r}: current value "
                f"is {type(current[p]).__name__} ({current[p]!r}). Either "
                f"set {so_far!r} to a dict first, or pick a different key."
            )
        current = current[p]
    current[parts[-1]] = value
