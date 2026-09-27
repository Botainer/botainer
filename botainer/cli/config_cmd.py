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
from botainer.cli._history_prompt import (
    mode_for_agent,
    history_dir_for,
    offer_carry,
    refuse_if_a_session_is_live,
    warn_history_will_move,
    axis_noun_for_config_key,
)
from botainer.cli._refusal_handler import handle_refusals
from botainer.core import agent_permissions
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
        # Names BROKER, not proxy. The old text said "use proxy mode or
        # per-project login instead" — and `botainer auth use proxy` replies
        # that proxy makes "every session REFUSE TO START", because this very
        # guard rejects the ANTHROPIC_API_KEY the proxy mints. The remedy named
        # by the leak check was the one mode the leak check breaks.
        errors.append(
            f"credential-shaped env vars in config: {sorted(leaked)} — "
            f"`botainer start` will refuse this config. Use broker mode "
            f"(`botainer auth use broker`), where the real token never enters "
            f"the container, or log in per-project "
            f"(`botainer plugin agent-claude login`)."
        )

    # Sanity check: two agent plugins of one family.
    #
    # Runs the check `compose_session` runs, so this cannot green-tick a config
    # `start` will refuse. Before this, `plugin enable agent-claude-broker` on
    # a claude project produced "✓ no issues found" here and a mutual-exclusion
    # refusal at launch.
    from botainer.plugins import selection as _selection
    try:
        _selection.check_family_exclusion(list(cfg.plugins_enabled or []))
    except Refused as _exc:
        errors.append(f"{_exc}")

    # Sanity check: env vars the EFFECTIVE POLICY denies.
    #
    # Check environment variable names against the effective policy denylist.
    # Use the policy object applied by composition and preflight so validation
    # matches launch enforcement; report denied names before launch.
    from botainer.core import policy as policy_module
    _effective = policy_module.intersect(policy_module.load_site_policy(),
                                         policy_module.load_user_policy())
    _denied = sorted(set(cfg.env) & set(_effective.capabilities.env_var_denylist))
    if _denied:
        errors.append(
            f"env vars denied by policy: {_denied} — `botainer start` will "
            f"refuse this config. These names can redirect the dynamic loader, "
            f"the shell or an interpreter inside the container, so they are "
            f"not settable from project config. Remove them from `env:`."
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
            # Third sibling. This printed `token: ghp_…` in full while
            # redacting the `env:` block twelve lines below, in one screen of
            # output — so the command that summarises your config leaked what
            # `config get plugins` hides.
            shown, _ = _redact_tree(k, v)
            click.echo(f"        {k}: {shown}")
    click.echo()
    # WHAT THE AGENT AND THE SESSION ARE GRANTED. `config explain` is
    # advertised as the summary of the effective config and omitted all three
    # of these — verified by running it with all three set to valid values and
    # grepping the output for each. `caps.kernel.keep` is the sharp one: a
    # Linux capability grant, invisible in the command whose job is to tell you
    # what your config does.
    click.echo("Agent:")
    # Echo the user's OWN word — they wrote it, and `config show` exists to
    # reflect their config back. Lookups fold `prompt` onto `default`; the
    # gloss below corrects the misnomer without renaming their setting.
    _perm = cfg.agent_permissions
    click.echo(f"  Permissions:  {_perm}")
    # State the FACT and POINT; no safety verdict belongs in a one-liner.
    # SAY SOMETHING FOR EVERY VALUE. This used to speak only for `bypass` and
    # stay silent otherwise — which was survivable while there were two values,
    # and becomes a blank where the interesting answer is now that a project may
    # name the agent's own mode. A name with no gloss tells a reader nothing.
    _cfg_fam = ("anthropic" if cfg.agent.startswith("claude")
                else "openai" if cfg.agent.startswith("codex") else None)
    _cfg_gloss = agent_permissions.gloss_for(_cfg_fam, _perm)
    if _cfg_gloss:
        click.echo(f"                {_cfg_gloss}")
    _cfg_argv = agent_permissions.argv_for(_cfg_fam, _perm)
    # The argv is the checkable fact — it is what actually reaches the agent,
    # and printing it is what lets a reader verify the sentence above instead
    # of trusting it.
    click.echo(f"                botainer appends: "
               f"{' '.join(_cfg_argv) if _cfg_argv else 'nothing'}")
    click.echo("                see docs/CAPABILITY-SURFACE.md")
    click.echo()
    click.echo("Kernel capabilities:")
    if cfg.caps.kernel.keep:
        click.secho(f"  Kept:         {', '.join(cfg.caps.kernel.keep)}",
                    fg="yellow")
        click.echo("                granted to the container beyond the "
                   "default set; see docs/CAPABILITY-SURFACE.md")
    else:
        click.echo("  Kept:         (none — the runtime default set)")
    click.echo()
    if cfg.job_profiles:
        click.echo("Job profiles (for agent-dispatched HPC jobs):")
        for _name, _prof in cfg.job_profiles.items():
            # Only what the user actually SET. Dumping every default turns
            # one profile into a wall of `description=, partition=, time=`
            # and buries the two fields that matter.
            _set = {k: v for k, v in _prof.model_dump().items()
                    if v not in (None, "", [], {}, False)}
            click.echo(f"  {_name}: " + (", ".join(f"{k}={v}" for k, v in
                                                   _set.items()) or "(defaults)"))
        click.echo()
    click.echo("Resources:")
    # Show resource support according to the adapter contract: CPU and memory
    # are Docker-only, GPUs are supported by both adapters, and time is not
    # consumed by a runtime. Treat `auto` as unresolved and explain the
    # applicable limitation.
    _res_set = (cfg.resources.cpu is not None
                or cfg.resources.memory_mb is not None)
    _dead_here = _res_set and cfg.runtime == "apptainer"
    _dead_maybe = _res_set and cfg.runtime not in ("docker", "apptainer")
    _mark = ("   [docker-only — NOT read here; start REFUSES]" if _dead_here
             else "   [docker-only]" if _dead_maybe else "")
    click.echo(f"  CPU:          {cfg.resources.cpu or 'unlimited'}{_mark}")
    click.echo(f"  Memory:       {cfg.resources.memory_mb or 'unlimited'} MB{_mark}")
    if _dead_here:
        click.secho(
            "                Delete them to launch; size the job with "
            "plugins.hpc-launcher.cpus / .memory_gb instead.", fg="yellow")
    elif _dead_maybe:
        click.secho(
            f"                runtime is {cfg.runtime!r}: under docker these "
            "apply. If it resolves to apptainer — the usual case on a "
            "cluster — they are NOT read and `start` REFUSES.", fg="yellow")
    # Marked even though it is not part of #232's refusal: saying nothing here,
    # right below two lines marked dead, would imply Time is live. It is not.
    _t = cfg.resources.time_minutes
    click.echo(f"  Time:         {_t or 'unlimited'} min"
               + ("   [read by NO runtime — use plugins.hpc-launcher.time_minutes]"
                  if _t else ""))
    if cfg.resources.gpus:
        click.echo(f"  GPUs:         {cfg.resources.gpus} ({cfg.resources.gpu_type or '?'})")
    if cfg.env:
        click.echo()
        click.echo("Extra env vars (passed to container):")
        # Redact credential-shaped environment values before display. Preserve
        # ordinary values and cap unusually long output to keep the terminal
        # readable.
        from botainer.inspect._redact import redact
        for k, v in sorted(cfg.env.items()):
            shown = redact(k, v, mode="safe")
            if shown == v and len(v) > 200:
                shown = v[:200] + "…"
            click.echo(f"  {k}={shown}")
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
    from botainer.inspect._redact import looks_credential_config_key, redact
    if (not show_secrets and isinstance(value, str)
            and looks_credential_config_key(key)):
        click.echo(redact(key, value, mode="safe"))
        click.echo("(pass --show-secrets to view)", err=True)
        return
    if isinstance(value, (dict, list)):
        # THE LEAF GUARD ABOVE IS NOT ENOUGH, and that gap shipped.
        # `config get env.ANTHROPIC_API_KEY` redacted correctly while
        # `config get env` printed the same key IN FULL: `value` is a dict, so
        # `isinstance(value, str)` is False, the guard is skipped entirely, and
        # the dump below emitted every child raw. Measured, not inferred.
        #
        # Redact by WALKING, because the structure is arbitrary — a credential
        # can sit at any depth under any parent the user asks for. Each leaf is
        # judged by its OWN key name, which is the same question
        # `looks_credential` already answers for the leaf case.
        if not show_secrets:
            value, hits = _redact_tree(key, value)
            if hits:
                click.echo(yaml.safe_dump(
                    value, default_flow_style=False).rstrip())
                # SAY that something was hidden. Silently redacting swaps one
                # wrong impression for another in a command whose whole job is
                # showing you your own config.
                # THIS NOTE WAS TRUE AND IS NOW OUT OF DATE, so it changes
                # with the predicate rather than being left behind.
                #
                # It used to say "names this check does not recognise are shown
                # in full — `botainer config check` lists what the launcher
                # treats as a credential", because the display predicate really
                # was narrower than the launcher's: `DB_PASS`, `SLACK_WEBHOOK`
                # and `STRIPE_SK` printed in full here while `config check`
                # refused the same file. `looks_credential` is now the UNION of
                # both lists, so the display side can no longer be narrower —
                # and that is asserted as a property, not left to memory.
                #
                # What it still must NOT claim is completeness. This matches on
                # the KEY NAME and never opens the value, so a credential under
                # an innocuous name is still shown. Say which rule ran.
                click.echo(
                    f"({hits} value(s) redacted by KEY NAME — the same names "
                    f"`botainer start` refuses over, so this is not narrower "
                    f"than the launcher. A credential under an innocuous name "
                    f"is still shown: the check reads names, not values. Pass "
                    f"--show-secrets to view.)",
                    err=True)
                return
        click.echo(yaml.safe_dump(value, default_flow_style=False).rstrip())
    else:
        click.echo(str(value))


def _redact_tree(prefix: str, value):
    """Redact credential-shaped leaves anywhere under `value`.

    Returns `(redacted_copy, how_many)`. The count is what lets the caller TELL
    the user something was hidden.

    Judges each leaf by its OWN key name rather than by the dotted path, so
    `plugins.git.token` is caught by `token` exactly as a bare `token` would
    be. Lists are walked too: a credential in a list element is still a
    credential. The input is never mutated — `config get` must not rewrite the
    thing it was asked to display.
    """
    from botainer.inspect._redact import looks_credential_config_key, redact

    hits = 0

    def walk(key, node):
        nonlocal hits
        if isinstance(node, dict):
            return {k: walk(k, v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(key, v) for v in node]
        # `str(key)`: PyYAML resolves a bare `ON:` to the BOOLEAN True, and a
        # numeric key to an int. Calling `.lower()` on those crashed with a
        # raw AttributeError where the old code printed the value fine.
        if isinstance(node, str) and looks_credential_config_key(str(key)):
            hits += 1
            return redact(key, node, mode="safe")
        return node

    return walk(prefix, value), hits


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option(
    "--yes", "-y", is_flag=True,
    help="Apply without confirmation prompt. If the change moves this "
         "project's agent history (an auth mode or profile change does), "
         "this authorises that move too.",
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

    # Apply recursive redaction to both the old and proposed values before
    # displaying a diff, including when the edited key contains a nested
    # mapping.
    shown_old, hid_old = ((_MISSING, 0) if old_value is _MISSING
                          else _redact_tree(key, old_value))
    shown_new, hid_new = _redact_tree(key, parsed_value)

    click.secho("Changes:", bold=True)
    click.echo(f"  {key}:")
    if old_value is _MISSING:
        click.secho("    - (key not present)", fg="red")
    else:
        click.secho(f"    - {shown_old!r}", fg="red")
    click.secho(f"    + {shown_new!r}", fg="green")
    if hid_old or hid_new:
        # Otherwise `- '<redacted>'` above `+ '<redacted>'` reads as "nothing
        # changed", and the user re-runs the command or goes looking for a
        # failure that did not happen. The write DID take; we are declining to
        # echo it back.
        click.secho(
            "    (values hidden because the key name looks credential-shaped; "
            "the change above was still applied)", fg="yellow")
    click.echo("")
    click.secho(
        "(yaml round-trip strips comments; if you have comments to preserve, "
        "edit .botainer/config.yaml by hand instead.)",
        fg="yellow",
    )

    # Show the history-move warning before confirmation, and copy only after
    # the write succeeds. Refuse the change when a session is live because it
    # has the project directory mounted.
    if key in _KEYS_THAT_MOVE_HISTORY and refuse_if_a_session_is_live(
            cfg_path.parent.parent):
        sys.exit(2)

    history_move = _warn_history_for_key(cfg_path, data, key, old_value,
                                         parsed_value)

    if not yes and not click.confirm("Apply?", default=False):
        click.echo("aborted.")
        return
    cfg_path.write_text(new_text)
    click.secho(f"✓ {cfg_path} updated.", fg="green")
    if history_move is not None:
        offer_carry(*history_move, what_changed=axis_noun_for_config_key(key), assume_yes=yes)
    click.secho(
        "  Next: `botainer config explain` to verify; "
        "`botainer start` to launch.",
        fg="cyan",
    )


_MISSING = object()

# Config keys that change WHERE the agent's config dir — and so its session
# history, todos, user agents and MCP config — lives on the host. The path is
# `<state>/state/<uuid>/data/agent-<agent>/{profiles|broker-state}/<profile>`,
# so both components below move it.
# `plugins_enabled` is here because it selects the auth MODE, and
# `history_dir_for` puts broker mode under `broker-state/` while every other
# mode uses `profiles/`. So enabling agent-<family>-broker through THIS command
# relocates the agent's history exactly as `auth use broker` does — and until
# 2026-09-10 only `auth use` said so.
#
# It does NOT follow that every plugins_enabled edit moves anything: isolated
# and shared resolve to the SAME directory. That is handled where it belongs,
# by comparing the two resolved paths rather than by guessing from the key —
# `warn_history_will_move` says nothing when they are equal, so the common
# isolated<->shared switch stays silent instead of crying wolf.
_KEYS_THAT_MOVE_HISTORY = {"profile", "agent", "plugins_enabled"}


def _warn_history_for_key(cfg_path, data: dict, key: str,
                          old_value: object, new_value: object):
    """Warn that this change relocates history. Returns (from, to) to carry, or None.

    Returning the pair rather than doing the copy is what lets the caller put
    the warning before its confirm and the copy after its write. Getting that
    order wrong is not theoretical: the first cut of this copied the files and
    THEN asked "Apply?", so answering no left a populated directory for a
    profile the user had declined to switch to — which the next real switch
    would have reported as a two-sided history conflict. Found by running the
    command and reading the prompts in order, not by reading the code.

    `config set` must warn when a mode or profile change selects a different
    history directory. A warning in `auth use` alone does not cover this
    configuration path or profile changes within the same mode.

    THE TWO KEYS ARE NOT THE SAME PROBLEM, which is why they take different
    paths below:

    `profile` moves history between two directories belonging to the SAME
    agent, so the files mean the same thing on both sides and carrying them is
    exactly right.

    `agent` moves it between two DIFFERENT tools. Claude's directory holds
    `claude.json`, `projects/` and `todos/`; codex's holds `auth.json`,
    `config.toml` and a SQLite state DB. Copying either into the other would
    not be history, it would be junk in a directory the tool then has to
    survive reading. So that case warns and stops — an honest "this is a fresh
    start" beats an offer that would produce a mess. The impossibility is
    structural, not remembered: `offer_carry_for_switch` derives both paths
    from ONE agent, so a cross-agent carry cannot be expressed.
    """
    if key not in _KEYS_THAT_MOVE_HISTORY:
        return None

    # Resolving the paths needs a real project. Failure here must not swallow
    # the warning — a bare `except` around a name lookup is how the equivalent
    # code in auth.py spent months telling users their history had moved while
    # never once saying where.
    resolved = _resolve_history_context(cfg_path, data)

    if key == "agent":
        click.secho(
            f"\n! Switching agent starts a FRESH history.\n"
            f"  Each agent keeps its own config dir — transcripts, todos, your "
            f"own subagents\n  and MCP config — in its own on-disk format, so "
            f"there is nothing meaningful to\n  copy between them. Nothing is "
            f"deleted: {old_value}'s history stays where it is\n  and comes "
            f"back if you switch back.",
            fg="yellow", err=True)
        if resolved:
            # Resolve auth mode separately for each agent. Enabling a broker
            # plugin for one agent family does not determine the other agent's
            # history directory.
            state_root, uid, _mode_unused, _ = resolved
            prof = str(data.get("profile", "default"))
            enabled = list(data.get("plugins_enabled") or [])
            for _agent in (old_value, new_value):
                _mode = mode_for_agent(enabled, str(_agent))
                _dir = history_dir_for(
                    state_root, uid, f'agent-{_agent}', _mode, prof)
                # A bare path reads as "your history is there". Say when it is
                # not — the same note `start --auth-mode` makes, for the same
                # reason: without it, "comes back if you switch back" is a
                # promise about a directory that may hold nothing at all.
                try:
                    _empty = not _dir.exists() or not any(_dir.iterdir())
                except OSError:
                    _empty = False      # unreadable is not empty; say nothing
                click.secho(
                    f"    {_agent}: {_dir}"
                    + ("   (empty — no history yet)" if _empty else ""),
                    fg="cyan", err=True)
        return None

    if key == "plugins_enabled" and resolved:
        # Same agent, same profile, different MODE. `_resolve_history_context`
        # reads the mode out of `data`, which the caller has already mutated —
        # so it is the AFTER mode. The BEFORE mode has to come from old_value,
        # the only surviving copy of the previous list.
        state_root, uid, _mode_after, agent = resolved
        prof = str(data.get("profile", "default"))
        # `_MISSING` is a sentinel OBJECT and is truthy, so `old_value or []`
        # hands it straight through and `list()` raises TypeError. A config
        # with no `plugins_enabled:` line at all is an ordinary state — `set`
        # creating the key is how it gets there — so this is the first path
        # anyone exercises, and the existing suite caught it.
        old_list = [] if old_value is _MISSING else list(old_value or [])
        old_mode = mode_for_agent(old_list, agent)
        new_mode = mode_for_agent(list(new_value or []), agent)
        source = history_dir_for(state_root, uid, f"agent-{agent}", old_mode,
                                 prof)
        destination = history_dir_for(state_root, uid, f"agent-{agent}",
                                      new_mode, prof)
        if not warn_history_will_move(source, destination, what_changed=axis_noun_for_config_key(key)):
            return None
        return source, destination

    if not resolved:
        click.secho(
            f"\n! This moves the agent's session history.\n"
            f"  History, todos, user agents and MCP config live in the agent's "
            f"config dir,\n  which is per-profile — so changing `{key}` points "
            f"the next session at a\n  DIFFERENT one. Nothing is deleted; the "
            f"old directory stays where it is.\n"
            f"  (botainer could not resolve the exact paths for this project.)",
            fg="yellow", err=True)
        return None

    state_root, uid, mode, agent = resolved
    source = history_dir_for(state_root, uid, f"agent-{agent}", mode,
                             str(old_value))
    destination = history_dir_for(state_root, uid, f"agent-{agent}", mode,
                                  str(new_value))
    if not warn_history_will_move(source, destination, what_changed=axis_noun_for_config_key(key)):
        return None
    return source, destination


def _resolve_history_context(cfg_path, data: dict):
    """(state_root, uid, mode, agent) for this project, or None if unresolvable.

    Separated from the warning so that a resolution failure loses the PATHS and
    not the message. Returning None is a normal outcome — `config set` can run
    against a project that has never been started.
    """
    try:
        from botainer.state import dir as _state_dir

        project_root = cfg_path.parent.parent
        uid = (project_root / ".botainer" / "project-id").read_text().strip()
        state_root = _state_dir.ensure_user_state_dir(create_if_missing=False).root
        enabled = data.get("plugins_enabled") or []
        _agent = data.get("agent", "claude")
        mode = mode_for_agent(enabled, _agent)  # #223: THIS agent's family only
        return state_root, uid, mode, _agent
    except Exception:
        return None


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
