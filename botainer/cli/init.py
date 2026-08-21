"""`botainer init` — initialize a project (writes Main/.botainer/project-id + config.yaml)."""

from __future__ import annotations

from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals as _handle_refusals
from botainer.core import config as config_module
from botainer.core import identity


@click.command("init")
@click.option(
    "--agent",
    default="claude",
    help="The agent plugin this project will use (default: claude).",
)
@click.option(
    "--runtime",
    type=click.Choice(["auto", "docker", "apptainer", "mock"]),
    default="auto",
    show_default=True,
    help=(
        "Container runtime to record in the project config. `auto` picks "
        "docker if available, else apptainer (HPC). Use `apptainer` "
        "explicitly when you're prepping a project on an HPC login node "
        "for Slurm submission."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing Main/.botainer/ contents.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Refuse if any prompt would be required.",
)
@click.option(
    "--name",
    default=None,
    help="Friendly project name (recorded in meta.json; cosmetic only). "
         "Defaults to the project root basename.",
)
@_handle_refusals
def init(agent: str, runtime: str, force: bool, non_interactive: bool,
         name: str | None) -> None:
    """Initialize the project in cwd."""
    do_init(Path.cwd(), agent=agent, runtime=runtime, force=force,
            non_interactive=non_interactive, name=name, quiet=False)


def _mode_of_written_config(project_root) -> str:
    """The project's auth mode, derived from the config that was ACTUALLY written.

    One source of truth: the agent plugin recorded in `plugins_enabled`. Not
    policy.default_auth_mode, which is what init REQUESTED and can differ — the
    requested variant may not be installed, in which case build_default_config
    falls back to the isolated one.

    Returns "" when nothing can be determined, which suppresses the mode banner
    rather than guessing. Silence beats a confident wrong mode.
    """
    try:
        import yaml as _yaml
        cfg = _yaml.safe_load(
            (project_root / ".botainer" / "config.yaml").read_text(encoding="utf-8")
        ) or {}
    except Exception:
        return ""
    from botainer.auth_modes import AUTH_MODES
    for name in cfg.get("plugins_enabled") or []:
        if not isinstance(name, str) or not name.startswith("agent-"):
            continue
        tail = name.rsplit("-", 1)[-1]
        if tail in AUTH_MODES:
            return tail
        return "isolated"      # bare `agent-<x>` IS the isolated variant
    return ""


def do_init(
    project_root: Path,
    *,
    agent: str | None = None,
    name: str | None = None,
    force: bool = False,
    non_interactive: bool = False,
    runtime: str | None = None,
    quiet: bool = False,
) -> None:
    """Callable init entry — usable by start.py's auto-init prompt.

    Creates `.botainer/project-id` (stable UUID), `.botainer/config.yaml`
    (capabilities + plugin config), and the host-only state dir at
    `${MY_BOTAINER:-~/.botainer}/state/<uuid>/`.
    """
    # Validate --agent against known plugins so a typo fails NOW, not at
    # `botainer start` time after the user has already invested in setup.
    # (Advanced-user review CRITICAL #1.)
    agent = agent or "claude"
    _validate_agent_name(agent, force=force)

    result = identity.init_project(
        project_root,
        agent=agent,
        force=force,
        non_interactive=non_interactive,
    )
    config_module.write_initial_config(
        project_root, agent=agent, force=force,
        runtime=runtime or "auto",
    )
    plugin_name = f"agent-{agent}" if not agent.startswith("agent-") else agent
    if quiet:
        return
    click.echo(f"Initialized {project_root} (project-id: {result.project_id})")
    click.echo(f"State dir: {result.state_dir}")
    click.echo(f"Config:    {project_root / '.botainer' / 'config.yaml'}")

    # AUTH-PRODUCT-PLAN §9: when init writes shared-mode plugins (the
    # default per policy.default_auth_mode), print a prominent warning
    # so the user knows what they're getting + how to switch back.
    # READ WHAT WAS WRITTEN, NOT WHAT WAS REQUESTED.
    #
    # This used to read policy.default_auth_mode — the mode init ASKED for. On a
    # host where `botainer setup` has not run, `agent-claude-shared` is not
    # installed, so build_default_config falls back to `agent-claude` (isolated)
    # and says so on stderr. The policy still reads "shared", so this banner, the
    # comment written into the user's own config.yaml, and the `auth login
    # --shared` next-step ALL announced a shared project that had just been
    # written as isolated.
    #
    # That is exactly the road `botainer start` takes on a virgin host: its
    # auto-onboard runs init BEFORE it offers setup (start.py ~:291 vs ~:303).
    # Observed on a clean wheel install: `plugins_enabled: [agent-claude]` under
    # a bold warning headed "SHARED (host-wide credentials)". The one true line
    # — the fallback notice — was plain stderr; the false one was bold. The user
    # then runs the printed `auth login --shared`, starts, and the agent asks
    # them to log in.
    #
    # STRUCTURAL, not a filter: there is one source of truth for a project's
    # auth mode, and it is the plugin actually recorded in plugins_enabled. Same
    # move the `runtime_hint` block below already makes — it re-reads the
    # written config rather than trusting the requested value.
    default_mode = _mode_of_written_config(project_root)
    if default_mode == "shared":
        from botainer.cli import style as _style
        click.echo("")
        _style.warn("Auth mode for this project: SHARED (host-wide credentials)")
        _style.body(
            "A compromised agent in ANY project using shared mode can read\n"
            "AND OVERWRITE the login shared by all of them — overwriting it\n"
            "would change which account every shared-mode project uses.\n"
            "(Write access is required so the agent can save its refreshed\n"
            "token; that is what shared mode is.) If your data is sensitive:\n"
            "    botainer auth use isolated\n"
            "To make isolation the default for new projects host-wide:\n"
            "    botainer policy set default_auth_mode isolated"
        )
    elif default_mode == "proxy":
        from botainer.cli import style as _style
        click.echo("")
        _style.alert("Auth mode for this project: PROXY (RETIRED — non-functional)")
        _style.body(
            "Proxy mode does not work at v0.1.0 and is retired. Switch now:\n"
            "  • isolated/shared (default) — creds mounted into the container:\n"
            "        botainer auth use isolated\n"
            "  • broker — real key stays host-side, container sees a sentinel:\n"
            "        enable the `agent-claude-broker` plugin"
        )
    # Codex 45 Rule 5: explicit next commands. Make them policy-aware
    # (suggest the right flag based on auth_mode) AND runtime-aware
    # (mention image build for docker / apptainer build for HPC).
    agent_short = (
        agent[len("agent-"):]
        if agent.startswith("agent-")
        else agent
    )
    # Detect runtime from the project config (default: auto).
    runtime_hint = "docker"
    try:
        import yaml as _yaml
        cfg = _yaml.safe_load(
            (project_root / ".botainer" / "config.yaml").read_text(encoding="utf-8")
        ) or {}
        runtime_hint = str(cfg.get("runtime", "auto"))
    except (FileNotFoundError, OSError, _yaml.YAMLError):
        pass

    # Compose the login-flag suggestion based on policy.
    if default_mode == "shared":
        login_cmd = (
            f"botainer auth login --shared --agent {agent_short}   "
            f"# OAuth, host-wide (matches this project's shared mode)"
        )
    elif default_mode == "isolated":
        login_cmd = (
            f"botainer auth login --isolated --agent {agent_short}   "
            f"# OAuth, per-project (matches this project's isolated mode)"
        )
    else:
        login_cmd = (
            f"botainer auth login --agent {agent_short}   "
            f"# OAuth (honors policy.default_auth_mode)"
        )

    # Image build step + variant based on runtime.
    if runtime_hint == "apptainer":
        build_cmd = (
            f"botainer image build agent-{agent_short} --runtime apptainer  "
            f"# build .sif (~10-20 min first time)"
        )
        start_cmd = (
            "botainer hpc submit                         "
            "# submit to Slurm (sbatch with the agent)"
        )
    else:
        build_cmd = (
            f"botainer image build agent-{agent_short}   "
            f"# build docker image (~8-12 min first time)"
        )
        start_cmd = (
            "botainer start                              "
            "# launch the session"
        )

    click.echo(
        f"\nNext:\n"
        f"  {build_cmd}\n"
        f"  {login_cmd}\n"
        f"  botainer inspect                            "
        f"# review what would be launched\n"
        f"  {start_cmd}\n"
        f"\n"
        f"  (Explicit plugin form: "
        f"`botainer plugin {plugin_name} login`)"
    )


def _agent_plugin_families() -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Split the agent plugins into BASE agents and auth-mode VARIANTS.

    `agent:` in config names an agent — `claude`, `codex`. The auth mode is a
    separate axis, carried by `plugins_enabled` and switched with
    `botainer auth use`. But `agent-claude-shared` is also a plugin whose name
    starts with `agent-`, so a check that only asks "is this an agent plugin?"
    accepts `--agent claude-shared` and writes a project whose `agent:` is an
    auth-mode variant. That project then asks for `agent-claude-shared-shared`
    — a name that has never existed — and falls back silently (#128).

    Grouping is by the manifest's `auth_family`, and the base agent of a family
    is its shortest plugin name. Nothing here pattern-matches on `-shared` /
    `-broker` suffixes: a new mode added tomorrow is classified correctly
    without touching this function.

    Returns (base_short_name -> plugin_name,
             variant_short_name -> (base_short_name, auth_mode)).
    """
    from botainer.plugins.builtin import BUILTIN_PLUGIN_NAMES
    from botainer.plugins.lifecycle import list_installed
    from botainer.plugins.manifest import load_manifest

    by_family: dict[str, list[tuple[str, str]]] = {}   # family -> [(name, mode)]
    unfamilied: set[str] = set()
    seen: set[str] = set()
    for inst in list_installed():
        if not inst.name.startswith("agent-"):
            continue
        seen.add(inst.name)
        try:
            man = load_manifest(inst.plugin_dir)
        except Exception:
            unfamilied.add(inst.name)
            continue
        fam = getattr(man, "auth_family", "") or ""
        mode = getattr(man, "auth_mode", "") or ""
        if fam:
            by_family.setdefault(fam, []).append((inst.name, mode))
        else:
            unfamilied.add(inst.name)
    # Bundled-but-not-installed agents still count as spellable.
    for n in BUILTIN_PLUGIN_NAMES:
        if n.startswith("agent-") and n not in seen:
            unfamilied.add(n)

    base: dict[str, str] = {}
    variants: dict[str, tuple[str, str]] = {}
    for _fam, members in by_family.items():
        members.sort(key=lambda t: (len(t[0]), t[0]))
        base_name = members[0][0]
        base_short = base_name[len("agent-"):]
        base[base_short] = base_name
        for name, mode in members[1:]:
            variants[name[len("agent-"):]] = (base_short, mode or "?")
    for n in unfamilied:
        short = n[len("agent-"):]
        # An agent plugin with no declared family can't be a mode variant of
        # anything, so it is its own base.
        base.setdefault(short, n)
    return base, variants


def _validate_agent_name(agent: str, *, force: bool) -> None:
    """Check `--agent` names a BASE agent, not an auth-mode variant of one.

    Accepts the short form ("claude") or the full plugin name ("agent-claude").
    `--force` bypasses, for developing a new agent plugin.
    """
    import sys

    short = agent[len("agent-"):] if agent.startswith("agent-") else agent
    base, variants = _agent_plugin_families()

    if short in base:
        return

    if short in variants:
        base_short, mode = variants[short]
        # The specific, actionable message. This is the case that used to
        # succeed and produce a project nobody could explain.
        click.secho(
            f"refused: --agent {agent!r} is an auth MODE of "
            f"{base_short!r}, not an agent.",
            fg="red", err=True,
        )
        click.secho(
            f"  The agent and the auth mode are separate settings. Use:\n"
            f"      botainer init --agent {base_short}\n"
            f"      botainer auth use {mode}",
            fg="cyan", err=True,
        )
        if not force:
            sys.exit(2)
        click.secho(
            f"  proceeding anyway because --force; `agent: {short}` will be "
            f"written to config.yaml and is unlikely to work.",
            fg="yellow", err=True,
        )
        return

    if force:
        click.secho(
            f"warning: --agent {agent!r} doesn't match any known agent "
            f"({sorted(base)}); proceeding because --force.",
            fg="yellow",
            err=True,
        )
        return
    click.secho(
        f"refused: --agent {agent!r} doesn't match any installed or bundled "
        f"agent plugin",
        fg="red",
        err=True,
    )
    click.secho(
        f"  available agents: {sorted(base)}",
        fg="cyan",
        err=True,
    )
    click.secho(
        "  hint: pass --force to write the config anyway (e.g. you're "
        "developing a new agent plugin)",
        fg="cyan",
        err=True,
    )
    sys.exit(2)
