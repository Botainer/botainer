"""`botainer policy` — show effective site policy."""

from __future__ import annotations

import click

from botainer.cli._refusal_handler import handle_refusals

from botainer.core import policy as policy_module


@click.group("policy")
def policy() -> None:
    """Read/write the per-user policy (~/.botainer/policy.yaml).

    Scope: per-USER. This command edits YOUR user policy — a per-host file you
    own. For most fields it acts as a ceiling you can TIGHTEN over the defaults
    (a user can narrow, never widen) and a default for new projects.

    NOT the root-owned SITE policy. On a multi-tenant cluster the authoritative
    ceiling is /etc/botainer/policy.yaml, owned by root — only an ADMIN can edit
    it. Some fields are read from the SITE policy ONLY and ignore your user
    policy entirely — notably `mounts.cluster_software_roots` (the hpc-modules
    software-root bind ceiling). Setting those here has NO effect; ask your
    cluster admin. For per-project settings, use `botainer config`.

    Subcommands:
      get   Read a value by dotted-path key.
      set   Write a value (shows diff + asks).
      show  Print the effective policy (site ∩ user).
      path  Print the user policy file path.

    Examples:
      botainer policy show
      botainer policy set default_auth_mode shared
      botainer policy get plugins.allowed_tiers
    """


@handle_refusals
@policy.command("show")
@click.option(
    "--user-only", is_flag=True,
    help="Show only the user policy from policy.yaml (default: effective = site ∩ user).",
)
def show(user_only: bool) -> None:
    """Print the effective policy (site ∩ user).

    Task #209: this previously showed user_policy ALONE, omitting any
    tightening from the site policy. Users could see (and assume they
    had) capabilities that the site admin had locked down. Default is
    now the EFFECTIVE policy (site ∩ user); pass --user-only to see
    the user file in isolation.
    """
    user = policy_module.load_user_policy()
    if user_only:
        click.echo(policy_module.render_human(user))
        return
    site = policy_module.load_site_policy()
    eff = policy_module.intersect(site, user)
    click.echo(policy_module.render_human(eff))


@handle_refusals
@policy.command("path")
def path() -> None:
    """Print the path to the policy.yaml file."""
    from botainer.state import dir as state_dir

    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    click.echo(str(paths.root / "policy.yaml"))


@handle_refusals
@policy.command("get")
@click.argument("key")
def policy_get(key: str) -> None:
    """Get a value from policy.yaml by dotted-path key.

    Examples:
      botainer policy get default_auth_mode
      botainer policy get plugins.allowed_tiers
    """
    import sys

    import yaml

    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    policy_path = paths.root / "policy.yaml"
    if not policy_path.exists():
        click.secho(f"(no policy.yaml at {policy_path}; run `botainer setup`)",
                    fg="yellow", err=True)
        sys.exit(1)
    data = yaml.safe_load(policy_path.read_text()) or {}
    # Re-audit (T11 follow-up): SITE-ONLY fields are read from the root-owned
    # /etc/botainer/policy.yaml only; a value in THIS user policy is inert.
    if any(key == f or key.startswith(f + ".")
           for f in ("mounts.cluster_software_roots",
                     "mounts.cluster_lmod_root",
                     "mounts.cluster_modulepath_roots")):
        click.secho(
            f"NOTE: {key!r} is a SITE-ONLY field — the effective value comes from "
            f"the root-owned /etc/botainer/policy.yaml, NOT this user policy. "
            f"Any value shown below is inert. Use `botainer policy show` for the "
            f"effective value.",
            fg="yellow", err=True,
        )
    # Sharp-edges F11: distinguish missing key from explicit-null value.
    _MISSING = object()
    parts = key.split(".")
    current: object = data
    for p in parts:
        if not isinstance(current, dict):
            current = _MISSING
            break
        if p not in current:
            current = _MISSING
            break
        current = current[p]
    if current is _MISSING:
        click.secho(f"(no value for {key!r}; key not present)",
                    fg="yellow", err=True)
        sys.exit(1)
    if current is None:
        click.echo("(null)")
        return
    if isinstance(current, (dict, list)):
        click.echo(yaml.safe_dump(current, default_flow_style=False).rstrip())
    else:
        click.echo(str(current))


@handle_refusals
@policy.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--yes", "-y", is_flag=True,
              help="Apply without confirmation prompt.")
@click.option("--literal-string", "literal_string", is_flag=True,
              help="Don't parse value as YAML; store literal string. "
                   "(Avoids 'no'→False yaml-coercion footgun.)")
def policy_set(key: str, value: str, yes: bool, literal_string: bool) -> None:
    """Set a value in policy.yaml by dotted-path key.

    Examples:
      botainer policy set default_auth_mode shared
      botainer policy set plugins.allowed_tiers '[first-party]'

    The value is parsed as YAML (so '[a, b]' parses as a list).
    Shows the diff (before/after) and asks for confirmation, unless
    --yes is passed.
    """
    import sys

    import yaml

    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    policy_path = paths.root / "policy.yaml"
    if not policy_path.exists():
        click.secho(f"refused: no policy.yaml at {policy_path}",
                    fg="red", err=True)
        click.secho("    Run `botainer setup` to create one.",
                    fg="cyan", err=True)
        sys.exit(2)
    data = yaml.safe_load(policy_path.read_text()) or {}
    if literal_string:
        parsed_value: object = value
    else:
        try:
            parsed_value = yaml.safe_load(value)
        except yaml.YAMLError as exc:
            click.secho(f"refused: value {value!r} is not valid YAML: {exc}",
                        fg="red", err=True)
            sys.exit(2)
        # Norway-problem detection (same as config set).
        _NORWAY = {"yes", "Yes", "YES", "no", "No", "NO",
                   "on", "On", "ON", "off", "Off", "OFF",
                   "true", "True", "TRUE", "false", "False", "FALSE",
                   "y", "Y", "n", "N"}
        if value in _NORWAY and isinstance(parsed_value, bool):
            click.secho(
                f"refused: value {value!r} parses as YAML bool {parsed_value!r}. "
                f"Pass --literal-string for the string, or 'true'/'false' for "
                f"the bool.",
                fg="red", err=True,
            )
            sys.exit(2)
    # Sharp-edges F1: refuse to clobber non-dict intermediate values.
    parts = key.split(".")
    current = data
    for i, p in enumerate(parts[:-1]):
        if p not in current:
            current[p] = {}
        elif not isinstance(current[p], dict):
            so_far = ".".join(parts[:i + 1])
            click.secho(
                f"refused: cannot descend into {so_far!r}: current value "
                f"is {type(current[p]).__name__} ({current[p]!r}). Either "
                f"set {so_far!r} to a dict first, or pick a different key.",
                fg="red", err=True,
            )
            sys.exit(2)
        current = current[p]
    old_value = current.get(parts[-1])
    if old_value == parsed_value:
        click.echo(f"({key} already = {parsed_value!r}; nothing to change)")
        return
    current[parts[-1]] = parsed_value

    # Codex 45#6: validate the resulting policy.yaml against SitePolicy
    # BEFORE writing. Otherwise `policy set bogus.key value` (or a typo
    # like `default_auth_mode shred`) silently writes a broken policy
    # that fails to load on next start.
    from botainer.core import policy as policy_module
    try:
        policy_module.SitePolicy.model_validate(data)
    except Exception as exc:
        click.secho(
            f"refused: the proposed change would make policy.yaml invalid: "
            f"{exc}",
            fg="red", err=True,
        )
        click.secho(
            "    Allowed top-level keys: plugins, mounts, network, "
            "capabilities, naming, default_auth_mode, version.",
            fg="cyan", err=True,
        )
        sys.exit(2)

    # Adversarial-review T3: some fields are read from the ROOT-OWNED site
    # policy ONLY (intersect() takes them verbatim from /etc/botainer/policy.yaml,
    # ignoring the user policy) — setting them here has NO effect. Warn loudly
    # rather than let the user believe they turned a feature on.
    _SITE_ONLY_FIELDS = (
        "mounts.cluster_software_roots",
        "mounts.cluster_lmod_root",
        "mounts.cluster_modulepath_roots",
    )
    if any(key == f or key.startswith(f + ".") for f in _SITE_ONLY_FIELDS):
        click.secho(
            f"WARNING: {key!r} is read from the ROOT-OWNED site policy "
            f"(/etc/botainer/policy.yaml) ONLY; a value in YOUR user policy is "
            f"IGNORED for it. Setting it here will NOT take effect — ask your "
            f"cluster admin to set it in /etc/botainer/policy.yaml. Proceeding "
            f"anyway (it will be stored but inert).",
            fg="yellow", err=True,
        )

    click.secho("Changes to policy.yaml:", bold=True)
    click.echo(f"  {key}:")
    click.secho(f"    - {old_value!r}", fg="red")
    click.secho(f"    + {parsed_value!r}", fg="green")
    # H4/L10: warn about comment-stripping symmetry with config set.
    click.secho(
        "(yaml round-trip strips comments; if your policy.yaml has "
        "comments to preserve, edit by hand instead.)",
        fg="yellow",
    )
    if not yes and not click.confirm("Apply?", default=False):
        click.echo("aborted.")
        return
    policy_path.write_text(yaml.safe_dump(data, sort_keys=False))
    click.secho(f"✓ {policy_path} updated.", fg="green")
    click.secho(
        "  Next: `botainer policy show` to verify; new projects will "
        "pick this up on `botainer init`.",
        fg="cyan",
    )
