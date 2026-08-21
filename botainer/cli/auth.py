"""`botainer auth` — auth-mode visibility, login, and mode switching.

Per AUTH-PRODUCT-PLAN.md and CLI-COMMAND-AUDIT.md:

  botainer auth login [--agent X] [--shared]   — obtain credentials
                                                 (walks through agents
                                                 if no --agent given)
  botainer auth use <mode>                     — switch this project to
                                                 isolated | shared | proxy
                                                 (shows config diff + asks)
  botainer auth status                         — show per-family state
  botainer auth check                          — verify proxy audit chain

The launcher subcommand is GENERIC: it discovers auth-family plugins
by introspecting installed manifests. Adding a new family (e.g.,
google) requires no launcher changes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import click

from botainer.auth_modes import AUTH_MODES

from botainer.cli import _common
from botainer.cli._refusal_handler import handle_refusals
from botainer.state import dir as state_dir


@click.group("auth")
def auth() -> None:
    """Manage credentials and per-project auth mode."""


# ─────────────────────────── helpers ───────────────────────────


def _discover_families() -> dict[str, dict[str, str]]:
    """Walk installed plugins; return {family: {mode: plugin_name}}.

    Empty for plugins without auth_family declared.
    """
    from botainer.plugins import lifecycle as lifecycle_module
    from botainer.plugins.manifest import load_manifest
    families: dict[str, dict[str, str]] = defaultdict(dict)
    for inst in lifecycle_module.list_installed():
        try:
            man = load_manifest(inst.plugin_dir)
        except Exception:
            continue
        if not man.auth_family or not man.auth_mode:
            continue
        families[man.auth_family][man.auth_mode] = inst.name
    return dict(families)


def _read_plugins_enabled(project_root: Path) -> set[str]:
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        return set()
    try:
        import yaml
        data = yaml.safe_load(cfg_path.read_text()) or {}
        return set(data.get("plugins_enabled") or [])
    except Exception:
        return set()


def _resolve_active_family_mode(
    enabled: set[str], families: dict[str, dict[str, str]]
) -> dict[str, str]:
    """For each family, which mode is currently enabled (or '' if none).

    Returns {family: mode}. Mode is '' if no variant of that family is enabled.
    """
    out: dict[str, str] = {}
    for family, modes in families.items():
        active = ""
        for mode, plugin_name in modes.items():
            if plugin_name in enabled:
                active = mode
                break
        out[family] = active
    return out


# ─────────────────────────── login ─────────────────────────────


@auth.command("login")
@click.option(
    "--agent",
    default=None,
    help="Log in for one specific agent family (claude / codex / ...). "
         "Without this flag, walks through each installed family.",
)
@click.option(
    "--mode",
    "mode_flag",
    type=click.Choice(AUTH_MODES),
    default=None,
    help=(
        "Force a specific login mode. If absent, the project's "
        "default_auth_mode policy decides (default: shared). NOTE: broker mode "
        "has no login of its own — it reads the SHARED login credential, so "
        "`--mode broker` performs a shared login."
    ),
)
@click.option(
    "--shared/--isolated",
    "shared_short_flag",
    default=None,
    help=(
        "Shorthand: --shared = --mode=shared, --isolated = --mode=isolated. "
        "(proxy mode is NOT FUNCTIONAL at v0.1.0 — a proxy session refuses "
        "to start.)"
    ),
)
@click.option(
    "--profile",
    "auth_profile",
    default="default",
    help="Profile name (allows multiple credentials per agent: personal "
         "vs work etc.). Default: 'default'. Stored at "
         "<creds-dir>/profiles/<profile>/.",
)
@handle_refusals
def auth_login(
    agent: str | None,
    mode_flag: str | None,
    shared_short_flag: bool | None,
    auth_profile: str,
) -> None:
    """Run agent login flow(s). Per AUTH-PRODUCT-PLAN.md §11.

    The mode is chosen by, in order:
      1. Explicit --mode=<shared|isolated|proxy> if given.
      2. Otherwise, the --shared/--isolated shorthand if given.
      3. Otherwise the project's `default_auth_mode` policy (default
         'shared' since 181384b).

    Previous behavior collapsed the three-value mode to a boolean and
    silently re-routed `default_auth_mode=proxy` to isolated login.
    Codex review flagged this; fixed by dispatching on the mode string.
    """
    # AUDIT (C1 sibling): --profile becomes the path component
    # profiles/<profile> used as an apptainer --bind source + on-host mkdir
    # (see _run_login). Validate the charset here (parity with `start
    # --auth-profile`), so this CLI source can't introduce a traversal that
    # the type-level SessionSpec/ProjectConfig validators would otherwise be
    # the only guard against.
    from botainer.core.spec import validate_profile_name as _validate_profile_name
    try:
        _validate_profile_name(auth_profile)
    except ValueError as exc:
        click.secho(f"refused: {exc}", fg="red", err=True)
        sys.exit(2)

    # Resolve the effective mode as a 3-state string.
    explicit_mode: str | None = None
    if mode_flag is not None:
        explicit_mode = mode_flag
    elif shared_short_flag is not None:
        explicit_mode = "shared" if shared_short_flag else "isolated"

    # THE PROJECT YOU ARE STANDING IN WINS (user report).
    #
    # This used to fall straight through to the POLICY default, never once
    # consulting the project's own configured mode. Standing in a project
    # configured `isolated` and running `botainer auth login --agent claude`
    # logged you into the SHARED store and left the project still not logged
    # in — no error, no mention, and `auth status` then showed the project
    # unauthenticated right after a successful login.
    #
    # Order is now: explicit flag > THIS PROJECT's active mode > policy default.
    # Rationale: `auth login` is otherwise a cwd-sensitive command that ignores
    # the cwd, which is the worst of both worlds.
    _project_root = None
    _project_modes: dict[str, str] = {}
    try:
        _project_root = _common.find_project_root()
    except Exception:
        _project_root = None              # identity refusal / not a project
    if _project_root is not None:
        try:
            _enabled = _read_plugins_enabled(_project_root)
            _project_modes = _resolve_active_family_mode(
                _enabled, _discover_families())
        except Exception:
            _project_modes = {}

    if explicit_mode is None:
        from botainer.core import policy as _policy
        site = _policy.load_site_policy()
        user = _policy.load_user_policy()
        effective = _policy.intersect(site, user)
        policy_mode = effective.default_auth_mode or "shared"
        resolved_mode = policy_mode      # per-family override applied below
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            click.secho(
                f"(default_auth_mode={policy_mode!r})", fg="cyan", err=True,
            )
    else:
        policy_mode = ""
        resolved_mode = explicit_mode
        click.secho(
            f"  mode={resolved_mode} (from the command line)", fg="cyan")

    families = _discover_families()
    if not families:
        click.secho(
            "No auth-family plugins installed. Run `botainer setup` first.",
            fg="red", err=True,
        )
        sys.exit(2)

    # Filter to one family if --agent given. Accept either family name
    # ("claude" → "anthropic"?) — keep it simple: accept the plugin's
    # auth_family value (anthropic / openai / ...) OR a common alias.
    aliases = {"claude": "anthropic", "anthropic": "anthropic",
               "codex": "openai", "openai": "openai"}
    target_families: list[str]
    if agent is None:
        target_families = sorted(families.keys())
    else:
        family = aliases.get(agent, agent)
        if family not in families:
            click.secho(
                f"refused: no installed plugin with auth_family={family!r}. "
                f"Available: {sorted(families.keys())}",
                fg="red", err=True,
            )
            sys.exit(2)
        target_families = [family]

    # Non-zero exits from the per-family login hooks, reported at the end so a
    # multi-family run still attempts every family before failing.
    failures: list[tuple[str, int]] = []

    for family in target_families:
        modes = families[family]
        # MISMATCH WARNING: an explicit --shared/--isolated that disagrees with
        # the mode THIS project actually uses means the login lands in a store
        # the project will never read. It "succeeds", and the project is still
        # unauthenticated. Say so at the point of the mistake.
        if explicit_mode is not None and _project_root is not None:
            _proj_mode = _project_modes.get(family) or ""
            if _proj_mode and _proj_mode != explicit_mode:
                click.secho(
                    f"  ⚠ this project uses {_proj_mode} mode for {family}, "
                    f"but you asked for a {explicit_mode} login.",
                    fg="yellow", bold=True,
                )
                click.secho(
                    f"    This writes the {explicit_mode} credential store; "
                    f"THIS project reads the {_proj_mode} one, so it will "
                    f"still be logged out afterwards.",
                    fg="yellow",
                )
                if _proj_mode == "isolated":
                    click.secho(
                        "    For this project, either log in from inside the "
                        "session with Claude Code's own `/login`, or run "
                        "`botainer auth login --isolated` here.",
                        fg="cyan",
                    )
                else:
                    click.secho(
                        f"    For this project: botainer auth login "
                        f"--{_proj_mode} --agent "
                        f"{_family_to_agent_name(family)}",
                        fg="cyan",
                    )
        if explicit_mode is None:
            _proj_mode = _project_modes.get(family) or ""
            # ALWAYS say which mode was chosen and WHERE it came from. The
            # report was caused end-to-end by silence: a login that
            # quietly picked a different store than the project uses looks
            # identical to a login that worked.
            if _proj_mode and _proj_mode in modes:
                resolved_mode = _proj_mode
                click.secho(
                    f"  {family}: mode={resolved_mode} "
                    f"(from THIS PROJECT's config"
                    + (f"; policy default is {policy_mode}"
                       if policy_mode and _proj_mode != policy_mode else "")
                    + ")",
                    fg="cyan",
                )
            else:
                resolved_mode = policy_mode
                _why = ("no botainer project here"
                        if _project_root is None
                        else f"this project has no active {family} mode")
                click.secho(
                    f"  {family}: mode={resolved_mode} "
                    f"(from the policy default — {_why})",
                    fg="cyan",
                )
        # Isolated writes PER-PROJECT credentials into a project's state dir.
        # Outside a project there is nowhere legitimate to put them, so refuse
        # rather than inventing state in whatever directory you happened to be
        # standing in (user requirement: "if I go to some random
        # folder and run auth, I expect it to go into shared auth, not create
        # something in that specific folder").
        #
        # Checked on the RESOLVED mode, however it was reached. The first cut of
        # this guard sat inside the no-flag branch only, so an EXPLICIT
        # `--isolated` in a random folder sailed past it — which is the likelier
        # mistake, not the safer one. A behavioural test caught that.
        if resolved_mode == "isolated" and _project_root is None:
            click.secho(
                "refused: isolated login writes PER-PROJECT credentials, and "
                "this directory is not a botainer project — there is nowhere "
                "to put them.\n"
                "  Either cd into a project and re-run, or log in host-wide:\n"
                f"      botainer auth login --shared --agent "
                f"{_family_to_agent_name(family)}",
                fg="red", err=True,
            )
            continue

        # Dispatch on resolved_mode (3-state, not boolean).
        if resolved_mode == "broker":
            # Broker mode has no login of its own: it reads the SHARED login
            # credential host-side (the same .credentials.json shared mode
            # binds). So a broker login IS a shared login. Fall back to
            # isolated if the family ships no shared variant.
            login_plugin = modes.get("shared") or modes.get("isolated")
            if not login_plugin:
                click.secho(
                    f"refused: no shared/isolated variant installed for "
                    f"family {family!r}; broker mode reads that login credential.",
                    fg="yellow", err=True,
                )
                continue
            click.secho(
                "ℹ broker mode reads the SHARED login credential; performing a "
                "shared login now. After it completes, switch the project with "
                "`botainer auth use broker`.",
                fg="cyan", err=True,
            )
        elif resolved_mode == "shared":
            login_plugin = modes.get("shared") or modes.get("proxy")
            if not login_plugin:
                click.secho(
                    f"refused: no shared/proxy variant installed for "
                    f"family {family!r}. Try --isolated (per-project).",
                    fg="yellow", err=True,
                )
                continue
        elif resolved_mode == "proxy":
            login_plugin = modes.get("proxy")
            if not login_plugin:
                click.secho(
                    f"refused: no proxy variant installed for family "
                    f"{family!r}. (proxy mode is NOT FUNCTIONAL at v0.1.0 "
                    f"regardless — a proxy session refuses to start; use "
                    f"--shared or --isolated.)",
                    fg="yellow", err=True,
                )
                continue
        else:  # isolated
            login_plugin = modes.get("isolated")
            if not login_plugin:
                click.secho(
                    f"refused: no isolated variant installed for family "
                    f"{family!r}. Try --shared.",
                    fg="yellow", err=True,
                )
                continue

        # If the login is destined for the proxy variant, say plainly that
        # proxy-mode SESSIONS don't start at v0.1.0 (T0-3 / #59). This login
        # still writes a usable credential file (shared/isolated read it too),
        # but it will NOT enable a working proxy session.
        if "proxy" in login_plugin:
            click.secho(
                "⚠ PROXY mode is NOT FUNCTIONAL at v0.1.0 — a proxy session"
                "\n  refuses to start (the agent's ANTHROPIC_API_KEY is blocked"
                "\n  by botainer's credential-leak guard). This login writes a"
                "\n  credential file, but you must run the session in --shared or"
                "\n  --isolated mode. (Tracked in the project's internal design notes.)",
                fg="red", err=True,
            )

        click.secho(
            f"\n— {family} (via {login_plugin}, "
            f"mode={resolved_mode}, "
            f"profile={auth_profile}) —",
            fg="cyan", bold=True,
        )
        if not click.confirm(f"Run {login_plugin} login now?", default=True):
            click.echo("  skipped.")
            continue

        # _invoke_plugin_login's `shared` parameter controls whether the
        # subprocess runs with project-cwd context (isolated) or not
        # (shared/proxy both write to a host-wide path, not per-project).
        shared_subprocess = resolved_mode in ("shared", "proxy")
        # KEEP THE RETURN CODE. This discarded it, so `botainer auth login`
        # exited 0 after a login that plainly failed — measured 3/3 with no
        # container runtime present, and again with a hook returning 2 ("no
        # credential file appeared"). Any script or wrapper checking $? saw
        # success, and so did a user who trusted the shell.
        #
        # cli/start.py already calls this SAME helper, captures rc, and aborts on
        # non-zero. Two callers of one function, one honouring its contract and
        # one ignoring it — the defect class this project keeps finding.
        rc = _invoke_plugin_login(
            login_plugin, shared=shared_subprocess, profile=auth_profile,
        )
        if rc != 0:
            failures.append((login_plugin, rc))

    if failures:
        click.echo("")
        for plugin_name, rc in failures:
            click.secho(
                f"login FAILED for {plugin_name} (exit {rc}).", fg="red", err=True)
        click.secho(
            "No credential was written. Re-run after fixing the error above; "
            "`botainer auth status` shows the current state.",
            fg="cyan", err=True)
        # Exit non-zero so a script — or a user reading $? — is not told this
        # succeeded. Use the first hook's code so the cause survives.
        sys.exit(failures[0][1])


# Env scrub allowlist (sharp-edges F10): plugin login subprocess
# should NOT inherit credential-shaped env vars or LD_*/PYTHON*
# overrides from the parent shell. We allow the standard
# session/locale vars + botainer-specific vars + the agent's
# own credential namespace (CLAUDE_*/ANTHROPIC_*/OPENAI_* are
# RE-INJECTED by the plugin login hook itself with the right values).
_PLUGIN_ENV_ALLOWLIST_PREFIXES = (
    "LANG", "LC_", "LANGUAGE",
    "TERM", "TERMINFO", "DISPLAY",
    "HOME", "USER", "LOGNAME", "SHELL",
    "PATH", "PWD",
    "XDG_",
    "MY_BOTAINER",
    "BOTAINER_",
    "TMPDIR", "TMP", "TEMP",
    "SSH_AUTH_SOCK",   # in case user has ssh_forward
    "SLURM_",          # HPC contexts
    # Anthropic/OpenAI auth-related vars passed through deliberately:
    # the plugin's login hook may need ANTHROPIC_BASE_URL etc. set.
    "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
)
# Drop these even if they match an allowlist prefix; they're known
# to interfere with subprocess behavior in ways we don't want.
_PLUGIN_ENV_BLOCKLIST_EXACT = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
    "PYTHONDONTWRITEBYTECODE",
    "NODE_OPTIONS", "NODE_PATH",
    "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
    # Credential env vars: don't inherit; the login hook writes the
    # real credential to disk and the agent reads it from there.
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "GH_TOKEN", "GITHUB_TOKEN",
})


def _scrubbed_env_for_plugin(extras: dict[str, str]) -> dict[str, str]:
    """Build a clean subprocess env. F10."""
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in _PLUGIN_ENV_BLOCKLIST_EXACT:
            continue
        if any(k == p or k.startswith(p) for p in _PLUGIN_ENV_ALLOWLIST_PREFIXES):
            out[k] = v
    out.update(extras)
    return out


def _invoke_plugin_login(
    plugin_name: str, *, shared: bool, profile: str = "default"
) -> int:
    """Run a plugin's `login` command via the plugin subprocess.

    Sharp-edges F10 hardening:
    - Validate script exists + is executable (pre-flight).
    - Scrub env to a minimal allowlist (drops LD_*, PYTHONPATH,
      credential vars).
    - Pass cwd=project_root for isolated-mode invocations.
    - Apply a generous 600s timeout (OAuth flows can take a while if
      user gets distracted; but unbounded == bad UX).
    """
    from botainer.plugins import lifecycle as lifecycle_module
    from botainer.plugins.manifest import load_manifest
    for inst in lifecycle_module.list_installed():
        if inst.name != plugin_name:
            continue
        man = load_manifest(inst.plugin_dir)
        login_cmd = next((c for c in man.commands if c.name == "login"), None)
        if not login_cmd:
            click.secho(
                f"refused: plugin {plugin_name!r} has no `login` command.",
                fg="red", err=True,
            )
            return 1
        script_path = inst.plugin_dir / login_cmd.script
        if not script_path.exists():
            click.secho(
                f"refused: plugin {plugin_name!r} login script not found: "
                f"{script_path}. The plugin install may be corrupt; "
                f"refresh the bundled plugins with `botainer setup`.",
                fg="red", err=True,
            )
            return 1
        # For .py scripts: always invoke via the same Python interpreter
        # as botainer itself (sys.executable). Reason: the script's
        # shebang `/usr/bin/env python3` resolves at exec time to whatever
        # Python is first on PATH — typically the system Python on HPC
        # clusters, which doesn't have botainer's deps (pyyaml, click,
        # pydantic). Real-host failure on Grace:
        # ModuleNotFoundError: No module named 'yaml'. Also tightens
        # PATH-injection resistance: an attacker manipulating PATH no
        # longer affects which interpreter runs the script.
        # For non-.py scripts (bash, etc.): require the executable bit
        # as before — there's no equivalent "use our interpreter" trick.
        is_py = script_path.suffix == ".py"
        if not is_py and not os.access(script_path, os.X_OK):
            click.secho(
                f"refused: plugin {plugin_name!r} login script not "
                f"executable: {script_path}. Fix: `chmod +x` it.",
                fg="red", err=True,
            )
            return 1
        # Inject canonical state env (BOTAINER_STATE_ROOT etc.) so the
        # login subprocess reads the resolved root the launcher computed,
        # not whatever MY_BOTAINER it might inherit independently. This
        # is the same helper used by manifest commands, pre_session
        # hooks, and post_session hooks — drift between dispatchers has
        # caused multiple v0.1.0 bugs.
        from botainer.state.dir import subprocess_state_env
        extras = {
            "BOTAINER_PLUGIN": plugin_name,
            "BOTAINER_PROFILE": profile,
            **subprocess_state_env(),
        }
        cwd_for_sub: Path | None = None
        # Task #155: BOTAINER_PROJECT_UUID was set ONLY in isolated mode.
        # Shared/proxy login hooks that read per-project state (the
        # agent-claude-proxy login flow does — it writes audit records
        # under data/<plugin>/audit.jsonl per project) couldn't find
        # the project. Set the UUID for ALL modes when we can resolve
        # one; only fall back to omitting it when we're in a context
        # without a project at all.
        project_root = _common.find_project_root()
        if project_root is not None:
            from botainer.core import identity
            try:
                uid, _ = identity.resolve_identity(project_root, identity_accept=False)
                extras["BOTAINER_PROJECT_ROOT"] = str(project_root)
                extras["BOTAINER_PROJECT_UUID"] = uid
                cwd_for_sub = project_root
            except Exception as exc:
                if not shared:
                    click.secho(
                        f"refused: identity check failed: {exc}",
                        fg="red", err=True,
                    )
                    click.secho(
                        "    hint: if you moved the project, accept once via "
                        "`botainer start --accept-identity-change`.",
                        fg="cyan", err=True,
                    )
                    return 1
                # shared mode: identity check failure is non-fatal —
                # login can proceed without the project context.
        elif not shared:
            click.secho(
                "refused: isolated-mode login requires being in a "
                "botainer project (cd to a project dir first).",
                fg="red", err=True,
            )
            return 1
        env = _scrubbed_env_for_plugin(extras)
        # Build argv: prepend sys.executable for .py scripts (see comment
        # above about why we don't use the shebang).
        argv: list[str] = (
            [sys.executable, str(script_path)] if is_py else [str(script_path)]
        )
        try:
            rc = subprocess.run(
                argv,
                env=env,
                cwd=str(cwd_for_sub) if cwd_for_sub else None,
                check=False,
                timeout=600,  # 10 min: OAuth can be slow if user is distracted
            ).returncode
        except subprocess.TimeoutExpired:
            click.secho(
                f"refused: plugin {plugin_name!r} login timed out after 600s",
                fg="red", err=True,
            )
            return 2
        return rc
    click.secho(f"refused: plugin {plugin_name!r} not installed.",
                fg="red", err=True)
    return 1


# ─────────────────────────── use (mode switch) ──────────────────


def _history_dir_for(state_root, uid: str, family_plugin: str, mode: str,
                     profile: str = "default"):
    """Where THIS mode keeps the agent's config dir — and so its session history.

    Every mode binds the same container path (/home/agent/.claude, .codex) from a
    DIFFERENT host directory. Broker deliberately uses `broker-state/` because
    that dir must hold NO credential — that separation is the whole point of
    broker mode and must not be "tidied" away. The side effect is that switching
    to or from broker moves the agent's conversation history with it.
    """
    agent = family_plugin.replace("-shared", "").replace("-broker", "") \
                         .replace("-proxy", "")
    sub = "broker-state" if mode == "broker" else "profiles"
    return state_root / "state" / uid / "data" / agent / sub / profile


def _warn_if_history_moves(project_root, disabled: list[str],
                           enabled: list[str], mode: str) -> None:
    """Say so when a mode switch relocates the agent's session history.

    User,: "I think there's something funny about session not being
    saved after I exit… maybe when I make a config change?" — then nearly
    withdrew it as "maybe I'm wrong". They were right, and the reason it felt
    like a maybe is that NOTHING SAID ANYTHING. History silently moved and the
    next session looked amnesiac.

    The proper fix is to stop partitioning history by auth mode at all
    (task #122) — history is not credential material and has no business
    following a security setting. That needs credential DELIVERY to change,
    which is the most fragile machinery here (the symlink exists because
    rename() destroys it; post_session reconciles refreshed tokens through it),
    so it is a designed change, not a late-night one.

    Until then, at least make it LOUD and say where the old history went. A
    silent surprise is the part that wasted the user's time; the relocation
    itself is merely annoying once you know.
    """
    crossed = any(("broker" in p) for p in (disabled + enabled))
    if not crossed:
        return                     # shared <-> isolated share a dir: no move
    try:
        from botainer.state import dir as _state_dir
        state_root = _state_dir.state_root()
        uid = (project_root / ".botainer" / "project-id").read_text().strip()
    except Exception:
        state_root = uid = None

    click.secho(
        "\n! Your agent's session history lives in its config dir, and each "
        "auth mode\n  uses a different one — so this switch MOVES it. Past "
        "conversations will\n  not appear in the new mode. Nothing is deleted.",
        fg="yellow", err=True)
    if state_root and uid:
        for plugin in sorted(set(disabled + enabled)):
            if not plugin.startswith("agent-"):
                continue
            was = "broker" if "broker" in plugin and plugin in disabled else (
                "shared/isolated" if plugin in disabled else mode)
            d = _history_dir_for(state_root, uid,
                                 plugin, "broker" if "broker" in plugin else "shared")
            if d.exists():
                click.secho(f"    {was:16s} history: {d}", fg="cyan", err=True)
    click.secho(
        "  Tracked as task #122 — history should not follow a security "
        "setting.", fg="cyan", err=True)


@auth.command("use")
@click.argument("mode", type=click.Choice(AUTH_MODES))
@click.option(
    "--family",
    default=None,
    help="Switch one family only (anthropic / openai / ...). "
         "Default: switch ALL installed families.",
)
@click.option(
    "--global",
    "is_global",
    is_flag=True,
    help="Also set this mode as the host-wide default for NEW projects "
         "(edits ~/.botainer/policy.yaml's default_auth_mode). Without "
         "this flag, only the current project changes.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Apply without confirmation (suppresses the diff prompt).",
)
@handle_refusals
def auth_use(mode: str, family: str | None, is_global: bool, yes: bool) -> None:
    """Switch THIS PROJECT's auth mode (edits .botainer/config.yaml).

    Pass --global to ALSO set host-wide default for new projects.

    Scope clarification:
      botainer auth use shared              ← edits .botainer/config.yaml
                                              (THIS project only)
      botainer auth use shared --global     ← also edits ~/.botainer/policy.yaml
                                              (default for NEW projects)

    Examples:
      botainer auth use shared              # all families to shared, this project
      botainer auth use isolated            # back to per-project, this project
      botainer auth use proxy --family anthropic   # just anthropic family
      botainer auth use shared --global     # also make it the new-project default
    """
    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse(
            "auth use",
            "not inside a botainer project",
            "cd into a project (run `botainer init` if needed)",
        )

    families = _discover_families()
    enabled = _read_plugins_enabled(project_root)

    # #128: an unknown --family used to `continue` past the loop with a yellow
    # "skipping", fall through to the no-op branch, print "already in the
    # requested mode; nothing to change." and exit 0. The second sentence
    # contradicted the first and was false — nothing had been checked. The
    # common way to hit it is `--family claude`: claude is an AGENT, and the
    # family it belongs to is `anthropic`. Refuse, and say which is which.
    if family and family not in families:
        agent_hint = ""
        for fam, modes in sorted(families.items()):
            if any(family in p for p in modes.values()):
                agent_hint = (
                    f"\n    {family!r} is an agent; its auth family is {fam!r}. "
                    f"Did you mean `--family {fam}`?"
                )
                break
        _common.refuse(
            "auth use",
            f"unknown auth family {family!r}",
            f"installed families: {sorted(families)}.{agent_hint}",
        )

    target_families = [family] if family else sorted(families.keys())

    diff_disable: list[str] = []
    diff_enable: list[str] = []
    skipped: list[str] = []
    for fam in target_families:
        modes = families[fam]
        target_plugin = modes.get(mode)
        if not target_plugin:
            click.secho(
                f"family {fam!r}: no plugin installed for mode {mode!r}. "
                f"Available modes: {sorted(modes.keys())}.",
                fg="yellow", err=True,
            )
            skipped.append(fam)
            continue
        # Disable any currently-enabled siblings in this family.
        for _sibling_mode, sibling_plugin in modes.items():
            if sibling_plugin in enabled and sibling_plugin != target_plugin:
                diff_disable.append(sibling_plugin)
        if target_plugin not in enabled:
            diff_enable.append(target_plugin)

    if not diff_disable and not diff_enable:
        # #128: this branch used to say "already in the requested mode" even
        # when every family had just been SKIPPED for want of a plugin — a
        # false success, exit 0, from a command that had changed nothing and
        # could not. Distinguish the two.
        if skipped and len(skipped) == len(target_families):
            _common.refuse(
                "auth use",
                f"no family could be switched to {mode!r}",
                f"not installed for: {', '.join(skipped)}. Install the missing "
                f"variant with `botainer setup`, or pick a mode that is "
                f"installed.",
            )
        if skipped:
            click.secho(
                f"note: {', '.join(skipped)} left unchanged (no {mode!r} "
                f"plugin installed).", fg="yellow", err=True,
            )
        click.echo("already in the requested mode; nothing to change.")
        return

    click.secho("\nChanges to .botainer/config.yaml:", bold=True)
    click.secho("  plugins_enabled:", fg="cyan")
    for p in sorted(diff_disable):
        click.secho(f"  -   - {p}", fg="red")
    for p in sorted(diff_enable):
        click.secho(f"  +   - {p}", fg="green")
    click.echo("")

    if mode == "shared":
        click.secho(
            "⚠ SHARED mode: a compromised agent in any project can read AND "
            "OVERWRITE the login all your shared-mode projects use — "
            "overwriting it would change which account they run as. Write "
            "access is required so the agent can save its refreshed token. "
            "Switch back with `botainer auth use isolated` if you're working "
            "with untrusted data.",
            fg="yellow",
        )
    elif mode == "proxy":
        click.secho(
            "⚠ PROXY mode is NOT FUNCTIONAL at v0.1.0 — setting it will make\n"
            "  every session REFUSE TO START.\n"
            "    - The proxy hands the agent an ephemeral ANTHROPIC_API_KEY,\n"
            "      but botainer's credential-leak guard refuses any env var\n"
            "      named ANTHROPIC_API_KEY on principle. The two contradict,\n"
            "      so a proxy session cannot start on any runtime today.\n"
            "    - Making it work needs a scoped leak-check exemption for the\n"
            "      proxy-minted token + OAuth refresh-on-401. Plan:\n"
            "      (tracked in the project's internal design notes)\n"
            "    Use isolated or shared. Only set proxy if you are actively\n"
            "    developing the proxy itself (BOTAINER_PROXY_EXPERIMENTAL=1 +\n"
            "    a patched leak check).",
            fg="red",
        )

    if not yes and not click.confirm("Apply these changes?", default=False):
        click.echo("aborted.")
        return

    from botainer.plugins import lifecycle as lifecycle_module
    for p in diff_disable:
        lifecycle_module.disable(project_root, p)
    for p in diff_enable:
        lifecycle_module.enable(project_root, p)
    click.secho(
        f"✓ applied to {project_root / '.botainer' / 'config.yaml'} "
        "(this project)",
        fg="green",
    )
    click.echo(f"  Next `botainer start` will use {mode} mode.")
    if mode == "shared":
        # The constraint that DEFINES the mode, at the moment of choosing it.
        # It appeared on no forward-facing surface  — not here,
        # not the start banner, not `init`, not a doc — so users adopted shared
        # precisely because it sounded like the multi-project option, and found
        # out otherwise as "expired" tokens hours later.
        click.secho(
            "\n  NOTE: shared mode runs ONE SESSION AT A TIME across all\n"
            "  shared-mode projects. Starting a second logs the first out —\n"
            "  refreshing revokes the previous token. For projects you run\n"
            "  side by side, use `botainer auth use isolated` in each.",
            fg="yellow",
        )
    _warn_if_history_moves(project_root, diff_disable, diff_enable, mode)

    if is_global:
        # Also set the host-wide default for new projects.
        try:
            paths = state_dir.ensure_user_state_dir(create_if_missing=False)
            policy_path = paths.root / "policy.yaml"
            if not policy_path.exists():
                click.secho(
                    f"  warning: --global skipped (no {policy_path}; "
                    "run `botainer setup` first).",
                    fg="yellow",
                )
            else:
                import yaml
                data = yaml.safe_load(policy_path.read_text()) or {}
                old_global = data.get("default_auth_mode")
                if old_global == mode:
                    click.echo(f"  (--global: already set to {mode!r})")
                else:
                    data["default_auth_mode"] = mode
                    policy_path.write_text(yaml.safe_dump(data, sort_keys=False))
                    click.secho(
                        f"✓ also set ~/.botainer/policy.yaml default_auth_mode "
                        f"= {mode!r} (host-wide; new projects)",
                        fg="green",
                    )
        except Exception as exc:
            click.secho(f"  warning: --global failed: {exc}", fg="yellow")


# ─────────────────────────── status ─────────────────────────────


def _credential_expiry(path: "Path | str") -> tuple[str, str]:
    """(state, detail) for an OAuth credential file — presence is not health.

    Added after a real Grace session: two projects, both in shared
    mode, one working and one reporting "login expired". `auth status` said
    both were fine, because it only tested that the FILE EXISTS. A dead token
    and a live token look identical to a stat() call, so the command could not
    distinguish "you are logged in" from "your token expired hours ago" —
    exactly the question the user had.

    Only the broker path parsed `expiresAt` (broker/credential_source.py). In
    shared/isolated MOUNT modes nothing on the host ever reads it: the file is
    bound rw into the container and Claude Code refreshes it in place, so if no
    session runs, nothing refreshes and nothing notices.

    Returns ("valid"|"expired"|"unknown"|"absent", human detail). Best-effort
    and non-fatal: a shape we don't recognise reports "unknown", never a crash
    and never a false "valid".
    """
    import json as _json
    import time as _time

    p = Path(path)
    if not p.exists():
        return "absent", "no credential file"
    try:
        doc = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return "unknown", "unreadable or not JSON"
    # Same shape the broker consumes: {"claudeAiOauth": {"expiresAt": <ms>}}
    oauth = None
    if isinstance(doc, dict):
        for key in ("claudeAiOauth", "oauth"):
            if isinstance(doc.get(key), dict):
                oauth = doc[key]
                break
    if oauth is None:
        # API-key / non-OAuth shapes carry no expiry — say so rather than
        # implying a check happened.
        return "unknown", "no OAuth block (API-key store?); no expiry to check"
    raw = oauth.get("expiresAt")
    if not isinstance(raw, (int, float)) or raw <= 0:
        return "unknown", "no usable expiresAt"
    remaining = (float(raw) / 1000.0) - _time.time()
    if remaining <= 0:
        return "expired", f"expired {int(-remaining // 3600)}h ago"
    return "valid", f"valid for {int(remaining // 3600)}h {int((remaining % 3600) // 60)}m"


def collect_auth_rows(project_root) -> list[dict[str, object]]:
    """Per-family auth state: which mode is active, and is a credential THERE.

    Extracted so `botainer doctor` reports the same thing
    `botainer auth status` does. Before this, doctor's auth section checked
    shell env vars only — it exited 0 and said nothing while the user sat in
    the commonest first-run failure (setup+init done, not logged in), and its
    own docstring claimed it "reports whether the .credentials.json file
    exists per project state dir found". `auth status`, in the same directory,
    got it right.

    Two readers of one fact with nothing comparing them is this project's
    most-repeated defect shape, so the fix is one function rather than a
    second implementation in doctor.

    `project_root` may be None (not inside a project); shared-host info is
    still returned.
    """
    families = _discover_families()
    enabled = _read_plugins_enabled(project_root) if project_root else set()
    active_modes = _resolve_active_family_mode(enabled, families)

    state_root = state_dir.ensure_user_state_dir(create_if_missing=False).root
    rows: list[dict[str, object]] = []
    for fam, modes_for_fam in sorted(families.items()):
        active = active_modes.get(fam, "")
        shared_creds = state_root / "shared-auth" / f"agent-{_family_to_agent_name(fam)}"
        per_project_creds_present = False
        per_project_creds_path = ""
        if project_root:
            # F12: read-only operation. identity_accept=False so a
            # casual `auth status` doesn't silently bind a moved
            # project's path to its existing credential pool. On
            # identity-change refusal, just skip per-project info.
            from botainer.core import identity
            try:
                uid, _ = identity.resolve_identity(project_root, identity_accept=False)
                proj_dir = state_root / "state" / uid
                profile_dir = (
                    proj_dir / "data" / f"agent-{_family_to_agent_name(fam)}"
                    / "profiles" / "default"
                )
                # Task #159: check ALL candidate filenames the isolated
                # login may have written, not just the shared-mode one.
                for _cand in _family_isolated_creds_filenames(fam):
                    cand_path = profile_dir / _cand
                    if cand_path.exists():
                        per_project_creds_path = str(cand_path)
                        per_project_creds_present = True
                        break
                if not per_project_creds_present:
                    # Show the first candidate path even when absent
                    # so the user knows where it WOULD be.
                    per_project_creds_path = str(
                        profile_dir / _family_isolated_creds_filenames(fam)[0]
                    )
            except Exception:
                # Skip per-project info (likely identity-change refusal
                # OR project not initialized). Shared-host info still shown.
                pass
        shared_creds_file = shared_creds / _family_creds_filename(fam)
        rows.append({
            "family": fam,
            "active_mode": active,
            "active_plugin": modes_for_fam.get(active, "") if active else "",
            "available_modes": sorted(modes_for_fam.keys()),
            "shared_creds_path": str(shared_creds_file),
            "shared_creds_present": shared_creds_file.exists(),
            "shared_creds_state": _credential_expiry(shared_creds_file)[0],
            "shared_creds_detail": _credential_expiry(shared_creds_file)[1],
            "per_project_creds_path": per_project_creds_path,
            "per_project_creds_present": per_project_creds_present,
            "per_project_creds_state": (
                _credential_expiry(per_project_creds_path)[0]
                if per_project_creds_path else "absent"),
            "per_project_creds_detail": (
                _credential_expiry(per_project_creds_path)[1]
                if per_project_creds_path else "no credential file"),
        })
    return rows


@auth.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@handle_refusals
def auth_status(as_json: bool) -> None:
    """Show auth state for this project + host."""
    project_root = _common.find_project_root()
    rows = collect_auth_rows(project_root)
    if as_json:
        click.echo(json.dumps({
            "project_root": str(project_root) if project_root else None,
            "families": rows,
        }, indent=2))
        return

    if project_root:
        click.secho(f"Project:    {project_root}", bold=True)
    else:
        click.secho("(not inside a botainer project)", fg="yellow")
    if sys.platform == "darwin":
        click.secho(
            "  Note: credentials are stored as files, NOT in macOS Keychain. "
            "Ensure FileVault is on for at-rest protection.",
            fg="yellow",
        )

    for row in rows:
        click.echo("")
        click.secho(f"family: {row['family']}", fg="cyan", bold=True)
        active = row["active_mode"]
        if active:
            mode_str = f"  active mode:  {active} (plugin: {row['active_plugin']})"
            if active == "proxy":
                click.secho(mode_str, fg="red")
                click.secho(
                    "    ⚠ proxy mode is NOT FUNCTIONAL at v0.1.0 — a proxy"
                    " session refuses to start (ANTHROPIC_API_KEY is blocked by"
                    " the credential-leak guard). Switch to shared or isolated.",
                    fg="red",
                )
            else:
                click.echo(mode_str)
        else:
            click.secho("  active mode:  (none enabled)", fg="yellow")
        click.echo(f"  available:    {', '.join(row['available_modes'])}")
        # SHARED-MASTER-STALE: shared mode keeps a per-project working COPY and
        # back-fills refreshes up to the master. If a back-fill is refused
        # (anti-poisoning checks, #158) the master silently stops being
        # refreshed — the project that did the refresh keeps working off its own
        # copy while every NEW project inherits the stale master and reports
        # "login expired". Reported from a real Grace session, where
        # the user had to deduce this from two `auth status` outputs side by
        # side. The command can see both files; it should say it.
        if (row["active_mode"] == "shared"
                and row["shared_creds_state"] == "expired"
                and row["per_project_creds_state"] == "valid"):
            click.secho(
                "    ⚠ THIS PROJECT STILL WORKS, BUT NEW PROJECTS WILL NOT.",
                fg="yellow", bold=True)
            click.secho(
                "      The host-wide (shared) token is expired while THIS "
                "project's working copy is still valid — so refreshes stopped "
                "flowing back to the shared store. Any project that has not "
                "built up its own copy will read the expired one and report "
                "'login expired'.", fg="yellow")
            click.secho(
                f"      Fix: botainer auth login --shared --agent "
                f"{_family_to_agent_name(row['family'])}", fg="cyan")
        click.echo("  credentials:")
        if row["shared_creds_present"]:
            _st, _detail = row["shared_creds_state"], row["shared_creds_detail"]
            if _st == "expired":
                # Presence is not health. A file exists AND the token is dead is
                # the exact state that reads as "login expired" inside the
                # container while this command used to print a green tick.
                click.secho(f"    ✗ shared: {row['shared_creds_path']}", fg="red")
                click.secho(f"        TOKEN EXPIRED ({_detail}).", fg="red")
                click.secho(
                    "        Nothing refreshes a mount-mode token from the "
                    "host — the refresh happens INSIDE a running session, so "
                    "an idle store just goes stale.",
                    fg="red",
                )
                click.secho(
                    f"        Fix: botainer auth login --shared --agent "
                    f"{_family_to_agent_name(row['family'])}",
                    fg="cyan",
                )
            elif _st == "valid":
                click.secho(
                    f"    ✓ shared: {row['shared_creds_path']}  ({_detail})",
                    fg="green")
            else:
                click.secho(f"    ✓ shared: {row['shared_creds_path']}", fg="green")
                click.secho(f"        (expiry not checked: {_detail})", fg="yellow")
        else:
            click.secho(f"    ✗ shared: {row['shared_creds_path']} (not present)",
                        fg="yellow")
            click.secho(
                f"      Run: botainer auth login --shared --agent "
                f"{_family_to_agent_name(row['family'])}",
                fg="cyan",
            )
        if row["per_project_creds_path"]:
            if row["per_project_creds_present"]:
                click.secho(f"    ✓ per-project: {row['per_project_creds_path']}",
                            fg="green")
            else:
                click.secho(
                    "    ✗ per-project: not present",
                    fg="yellow",
                )

        # Cross-mode hint per user direction:
        if active == "isolated" and row["shared_creds_present"]:
            click.secho(
                f"  Hint: shared credentials exist for {row['family']}. "
                f"Switch this project to shared mode with "
                f"`botainer auth use shared --family {row['family']}`. "
                f"(Note: shared mode lets any project on this host see this token.)",
                fg="cyan",
            )


def _family_to_agent_name(family: str) -> str:
    return {"anthropic": "claude", "openai": "codex"}.get(family, family)


def _family_creds_filename(family: str) -> str:
    # Anthropic claude CLI uses .credentials.json (OAuth).
    # OpenAI shared mode uses auth.json (OAuth via `codex login`).
    # OpenAI ISOLATED mode (agent-codex/hooks/login.py) writes a bare
    # 'api_key' file (key-paste). Task #159: auth status was hardcoded
    # to look up 'auth.json' even in isolated mode → always reported
    # MISSING for codex-isolated even after the user pasted their key.
    return {"anthropic": ".credentials.json", "openai": "auth.json"}.get(
        family, ".credentials.json"
    )


def _family_isolated_creds_filenames(family: str) -> tuple[str, ...]:
    """Filenames the per-project (isolated) login may write.

    Distinct from shared-mode filename because isolated mode uses
    different credential shapes: codex isolated stores a bare 'api_key'
    (paste flow) while shared-mode stores OAuth 'auth.json'. Auth
    status checks ALL candidate names so 'present' isn't a lie.
    """
    return {
        "anthropic": (".credentials.json",),
        "openai": ("auth.json", "api_key"),
    }.get(family, (".credentials.json",))


# ─────────────────────────── check ──────────────────────────────


@auth.command("check")
@handle_refusals
def auth_check() -> None:
    """Verify proxy audit log integrity (hash chain)."""
    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse(
            "auth check",
            "not inside a botainer project",
            "cd into a project; run `botainer init` if needed",
        )
    from botainer.core import identity
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    # F12: identity_accept=False — don't silently rebind a moved project.
    try:
        uid, _ = identity.resolve_identity(project_root, identity_accept=False)
    except Exception as exc:
        click.secho(f"refused: identity check failed: {exc}",
                    fg="red", err=True)
        click.secho(
            "    hint: if you moved the project, accept once via "
            "`botainer start --accept-identity-change`.",
            fg="cyan", err=True,
        )
        sys.exit(2)
    proj_paths = paths.for_project(uid)
    audit_log = proj_paths.base / "data" / "agent-claude-proxy" / "audit.jsonl"

    if not audit_log.exists():
        # Tasks #99 + #104: was exit 0 ("nothing to verify"). That meant
        # `rm audit.log && botainer auth check` reported success — the
        # prior "tamper detection" claim was a lie because tampering =
        # delete the log. Now exit 5 ("audit log absent; cannot attest").
        # If the proxy
        # genuinely isn't configured for this project, that's a different
        # condition than 'chain verified' and the exit code reflects it.
        click.secho(
            f"✗ audit log absent at {audit_log}; cannot attest chain.",
            fg="yellow", err=True,
        )
        click.echo(
            "  (If agent-claude-proxy isn't configured for this project,",
            err=True,
        )
        click.echo(
            "   this is expected; nothing to verify. Otherwise: tampering.)",
            err=True,
        )
        sys.exit(5)

    ok, detail = _verify_audit_chain(audit_log)
    if ok:
        click.secho(f"✓ {detail}", fg="green")
        sys.exit(0)
    click.secho(f"✗ audit chain failed: {detail}", fg="red", err=True)
    sys.exit(2)


def _verify_audit_chain(audit_log: Path) -> tuple[bool, str]:
    import hashlib
    if not audit_log.exists():
        # Task #99: caller has already exited 5 for this case; if we ever
        # reach here, we treat it as a verifier failure rather than
        # success. Defense-in-depth.
        return False, f"audit log absent at {audit_log}"
    expected_prev = "0" * 64
    with open(audit_log, "rb") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.rstrip(b"\n")
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                return False, f"line {lineno}: JSON decode: {exc}"
            actual_prev = entry.get("prev_hash")
            if actual_prev != expected_prev:
                return False, (
                    f"line {lineno}: prev_hash mismatch "
                    f"(expected {expected_prev[:16]}..., got "
                    f"{str(actual_prev)[:16]}...)"
                )
            expected_prev = hashlib.sha256(line).hexdigest()
    return True, f"chain verified ({audit_log})"


# `doctor` lives in its own module: it is a pure read-only diagnostic with no
# overlap with the login/use/status flows, and keeping it separate means the
# credential-mutating code in this file stays easy to audit as one unit.
from botainer.cli.auth_doctor import auth_doctor as _auth_doctor  # noqa: E402

auth.add_command(_auth_doctor)

from botainer.cli.auth_doctor import rotation_test as _rotation_test  # noqa: E402

auth.add_command(_rotation_test)

from botainer.cli.auth_probe import rotation_probe as _rotation_probe  # noqa: E402

auth.add_command(_rotation_probe)
