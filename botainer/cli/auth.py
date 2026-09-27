"""`botainer auth` — auth-mode visibility, login, and mode switching.

Per internal design note DN-040 and internal design note DN-043:

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
from botainer.cli._history_prompt import (
    history_dir_for,
    offer_carry,
    refuse_if_a_session_is_live,
    warn_history_will_move,
)
from botainer.cli._refusal_handler import handle_refusals
from botainer.core import exec_bit
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


def _read_project_profile(project_root: Path) -> str:
    """The profile THIS project uses. Defaults to 'default' only when unset.

    `auth status` used to build its per-project path from the literal string
    "default" and never load the config at all, so a project on `profile: work`
    was told about a slot it does not use — reporting "not present" while it was
    signed in, and offering a `--shared` login inside an isolated project.
    """
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        return "default"
    try:
        import yaml
        data = yaml.safe_load(cfg_path.read_text()) or {}
        value = data.get("profile")
        return str(value) if value else "default"
    except Exception:
        return "default"


def _read_profile_notes(project_root: Path) -> dict[str, str]:
    """The project's optional one-line notes about what each profile is FOR.

    Read from project config, NOT from the profile directory: that directory is
    bound into the container and the agent writes there, so a note kept in it
    would be a label the agent could forge — and this label exists to inform an
    account decision.
    """
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(cfg_path.read_text()) or {}
        notes = data.get("profile_notes") or {}
        return {str(k): str(v) for k, v in notes.items()} if isinstance(
            notes, dict) else {}
    except Exception:
        return {}


def _credential_kind(filename: str) -> str:
    """What KIND of credential a file of this name holds, in the user's terms.

    The mode name does not answer this. codex's `isolated` accepts EITHER a
    pasted API key OR an OAuth login, so "isolated" tells you where the
    credential lives and nothing about what it is — and swapping between the two
    is a re-login inside one mode, not a mode switch. Printing only the path
    leaves the answer encoded in a filename the user has no reason to know.
    """
    return {
        ".credentials.json": "account login (OAuth)",
        "auth.json": "account login (OAuth)",
        "api_key": "API key (pasted)",
    }.get(filename, "credential")


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


def _broker_credential_scope(project_root: Path | None) -> str:
    """Which store this project's BROKER reads: 'shared' (default) or 'isolated'.

    Broker has no login of its own — it reads the store `credential_scope`
    names. This is the SAME read `core/credential_holders.py` performs for the
    concurrent-session warning; if the two disagreed, they would be telling the
    user different things about one file.
    """
    if project_root is None:
        return "shared"
    try:
        from botainer.core.config import load_config
        cfg = load_config(project_root)
        for name in (cfg.plugins or {}):
            if not name.endswith("-broker"):
                continue
            scope = str((cfg.plugins.get(name) or {}).get(
                "credential_scope", "shared"))
            if scope in {"shared", "isolated"}:
                return scope
    except Exception:
        pass
    return "shared"


def _login_store_for_mode(mode: str, project_root: Path | None) -> str:
    """The STORE a mode reads/writes: 'shared', 'isolated', or '' for unknown.

    THE WARNING BELOW IS ABOUT STORES, AND MODE NAMES ARE NOT STORES. Comparing
    the names told a BROKER project that ran `botainer auth login --shared` —
    the CORRECT command, because broker reads the shared store — that it "will
    still be logged out afterwards". Three things were wrong at once: the
    premise (there is no separate broker store), the consequence (that login
    works), and the remedy, which emitted `botainer auth login --broker` and
    produced `Error: No such option '--broker'`.

    Returning a STORE also makes the remedy structurally safe. `--shared` and
    `--isolated` are real flags, so a suggestion naming one cannot fail to
    parse; `--broker` and `--proxy` are not flags at all and never were.

    '' means DO NOT COMPARE. `proxy` is the only such case today: it reads the
    per-project store, but a proxy session refuses to start at v0.1.0, so a
    store comparison would answer a question the user cannot act on. It gets
    its own message instead.
    """
    if mode == "broker":
        return _broker_credential_scope(project_root)
    if mode in {"shared", "isolated"}:
        return mode
    return ""


# ─────────────────────────── login ─────────────────────────────


# Mode resolution order + the four modes: internal design note DN-040 §11.
# NOT in the docstring: click prints that verbatim as `--help`, and an id a
# reader cannot resolve is noise in the one place they came for instructions.
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
        "default_auth_mode policy decides (default: isolated — one credential "
        "per project). NOTE: broker mode "
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
    # #174: `--auth-profile` FIRST, so it is the name `--help` shows and the
    # name that ends up in someone's script. `start`, `inspect` and `dry-run`
    # already spell it this way — they had to, because `hpc setup --profile`
    # had taken the bare name for a completely different axis (a machine).
    # This is the one command where the bare `--profile` still meant the auth
    # axis, which is exactly what made the word ambiguous. Both work; only one
    # says which of the three axes it selects. See docs/PROFILES.md.
    "--auth-profile",
    "--profile",
    "auth_profile",
    default="default",
    help="Which AUTH profile. Default: 'default'. It always selects this "
         "agent's history, notes and settings. Whether it also selects a "
         "separate CREDENTIAL depends on the auth mode: in ISOLATED it does, "
         "at <creds-dir>/profiles/<name>/, so you can keep personal and work "
         "logins side by side. In SHARED and BROKER it does NOT — both read "
         "one host-wide login every project links to, so a non-default "
         "profile is refused at login rather than silently overwriting it. "
         "`--profile` means the same thing here; on `hpc setup` it means a "
         "cluster instead.",
)
@handle_refusals
def auth_login(
    agent: str | None,
    mode_flag: str | None,
    shared_short_flag: bool | None,
    auth_profile: str,
) -> None:
    """Run agent login flow(s).

    The mode is chosen by, in order:
      1. Explicit --mode=<mode> if given — the `--mode` entry below lists
         the modes.
      2. Otherwise, the --shared/--isolated shorthand if given.
      3. Otherwise the project's `default_auth_mode` policy (default
         'isolated' since 2026-09-02; was 'shared').

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

    # Resolve login mode in this order: explicit flag, active mode configured
    # for the current project, then policy default. This prevents login from
    # writing a credential to a store that the active project does not read.
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
        # Fall back to the CLASS default rather than a literal — a second
        # hardcoded copy is exactly how #210 happened.
        policy_mode = (effective.default_auth_mode
                       or _policy.SitePolicy().default_auth_mode)
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
        # Per-iteration, not per-call: a stale True from a previous
        # family would print a broker note for a family that has none.
        broker_note = False
        # MISMATCH WARNING: an explicit --shared/--isolated that disagrees with
        # the mode THIS project actually uses means the login lands in a store
        # the project will never read. It "succeeds", and the project is still
        # unauthenticated. Say so at the point of the mistake.
        if explicit_mode is not None and _project_root is not None:
            _proj_mode = _project_modes.get(family) or ""
            _proj_store = _login_store_for_mode(_proj_mode, _project_root)
            _login_store = _login_store_for_mode(explicit_mode, _project_root)
            if _proj_mode == "proxy":
                # NOT a store comparison. proxy is NOT FUNCTIONAL at v0.1.0 —
                # a proxy session refuses to start — so telling someone which
                # store it reads answers a question they cannot act on.
                click.secho(
                    f"  ⚠ this project is set to proxy mode for {family}, "
                    f"which is NOT FUNCTIONAL at v0.1.0: a proxy session "
                    f"refuses to start.", fg="yellow", bold=True)
                click.secho(
                    f"    Your {explicit_mode} login will be written, but the "
                    f"project cannot launch until its mode changes:\n"
                    f"      botainer auth use {explicit_mode}", fg="cyan")
            elif _proj_store and _login_store and _proj_store != _login_store:
                # COMPARE STORES, NOT MODE NAMES. A broker project asked for a
                # shared login is doing the right thing — broker READS the
                # shared store — and used to be warned it would fail.
                _how = (f"{_proj_mode} mode, which reads the {_proj_store} "
                        f"store") if _proj_mode != _proj_store else \
                       f"{_proj_mode} mode"
                click.secho(
                    f"  ⚠ this project uses {_how} for {family}, but you "
                    f"asked for a {explicit_mode} login.",
                    fg="yellow", bold=True,
                )
                click.secho(
                    f"    That writes the {_login_store} credential store; "
                    f"THIS project reads the {_proj_store} one, so it will "
                    f"still be logged out afterwards.",
                    fg="yellow",
                )
                if _proj_store == "isolated":
                    click.secho(
                        "    For this project, either log in from inside the "
                        "session with Claude Code's own `/login`, or run "
                        "`botainer auth login --isolated` here.",
                        fg="cyan",
                    )
                else:
                    # The STORE is always a real flag. `--{_proj_mode}` was not:
                    # for a broker project it emitted `--broker`, which exits
                    # with `Error: No such option '--broker'`.
                    click.secho(
                        f"    For this project: botainer auth login "
                        f"--{_proj_store} --agent "
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
        # Isolated credentials are project-scoped, so there is no destination
        # when no project is active. Apply this refusal regardless of whether
        # isolated mode came from policy or an explicit flag.
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
            # Deferred past the profile check below: announcing "performing a
            # shared login now" and then refusing it reads as a contradiction.
            broker_note = True
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
                "\n  credential file, but the session must run in another mode:"
                "\n      botainer auth use shared      # or: isolated"
                "\n  (`--shared` / `--isolated` are flags of THIS command, not of"
                "\n  `botainer start`, which takes `--auth-mode` for one session.)"
                "\n  (Tracked in the project's internal design notes.)",
                fg="red", err=True,
            )

        # A SHARED CREDENTIAL HAS NO PROFILES OF ITSELF, SO REFUSE THE LOGIN.
        #
        # `--auth-profile` used to promise, in its own help, that you could
        # "keep personal and work logins side by side" at
        # `<creds-dir>/profiles/<name>/`. For a shared login that is false: the
        # hook writes `shared-auth/agent-<agent>/` unconditionally — the string
        # "profile" does not appear in it at all — so `--auth-profile work`
        # followed by `--auth-profile personal` OVERWRITES the first account's
        # refresh token. Silently, in the mode the HPC guide teaches, with no
        # way to get the first login back.
        #
        # KEYED ON THE RESOLVED PLUGIN, NOT ON THE REQUESTED MODE, and that is
        # the whole point: `--broker` resolves to the SHARED login plugin just
        # above (broker has no login of its own), so a mode-name list would
        # have missed it and let broker overwrite the same file. Reading the
        # variable the dispatch just set cannot drift from the dispatch.
        # Proxy is deliberately NOT included: its `login` command runs
        # `start_proxy.py`, so what it writes is not established here, and
        # proxy sessions already refuse to start for other reasons.
        #
        # Refusing rather than implementing per-profile shared credentials,
        # deliberately: the per-project credential is a SYMLINK to that one
        # fixed path, so making login profile-aware without making the bind
        # profile-aware would produce a link pointing at a file nobody writes.
        # That is a bind-path change, which is the security surface.
        #
        # THE PROFILE AXIS STILL MEANS SOMETHING HERE — it selects the agent's
        # history and settings directory. It is only the CREDENTIAL that is
        # host-wide. So this refuses the LOGIN, not the profile, and says which.
        if login_plugin == modes.get("shared") and auth_profile != "default":
            click.secho(
                f"refused: `--auth-profile {auth_profile}` cannot select a "
                f"credential for this login.",
                fg="red", err=True)
            click.secho(
                f"    It would write the host-wide SHARED credential at "
                f"shared-auth/agent-{_family_to_agent_name(family)}/ — ONE "
                f"login per agent, which every project links to. Logging in "
                f"under a second profile name would overwrite the first "
                f"account's token, not sit beside it.",
                fg="yellow", err=True)
            click.secho(
                "    The profile still selects this agent's HISTORY and "
                "settings; it is only the credential that is host-wide.",
                fg="yellow", err=True)
            click.secho(
                "    For side-by-side logins use isolated mode, which does "
                "keep one credential per profile:\n"
                "        botainer auth use isolated",
                fg="cyan", err=True)
            if resolved_mode == "broker":
                click.secho(
                    "    (broker is not an alternative here: broker mode "
                    "reads this same shared credential.)",
                    fg="cyan", err=True)
            failures.append((login_plugin, 2))
            continue

        if broker_note:
            click.secho(
                "\u2139 broker mode reads the SHARED login credential; performing "
                "a shared login now. After it completes, switch the project "
                "with `botainer auth use broker`.",
                fg="cyan", err=True,
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
        # Preserve the plugin exit code. A failed login hook must be reported
        # as failure so callers and shell scripts do not treat the login as
        # successful.
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
        # Invoke Python login hooks with the launcher interpreter so required
        # dependencies are available on clusters and PATH changes cannot
        # select a different interpreter.
        # For non-.py scripts (bash, etc.): require the executable bit
        # as before — there's no equivalent "use our interpreter" trick.
        is_py = script_path.suffix == ".py"
        # is_executable, NOT os.access — see core/exec_bit.py.
        if not is_py and not exec_bit.is_executable(script_path):
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
        # Shared/proxy login hooks that read per-project state couldn't find
        # the project. Set the UUID for ALL modes when we can resolve
        # one; only fall back to omitting it when we're in a context
        # without a project at all.
        #
        # THE PARENTHETICAL THAT USED TO BE HERE WAS FALSE, and it is worth
        # saying so rather than just deleting it. It claimed the
        # agent-claude-proxy login flow "writes audit records under
        # data/<plugin>/audit.jsonl per project". There is no login hook —
        # the plugin's `login` command dispatches to `hooks/start_proxy.py` —
        # and nothing under that plugin writes a per-project `data/.../
        # audit.jsonl`; the audit log is per SESSION, at
        # `sessions/<session_id>/proxy-audit.jsonl`. `auth check` read the
        # same imagined path, so it could never attest a real chain. One wrong
        # belief, written down twice, in the same file.
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


def _history_moves_for(project_root, disabled: list[str], enabled: list[str],
                       mode: str) -> list[tuple[str, Path, Path]]:
    """The (agent, from, to) history directories this mode switch relocates.

    Empty when nothing moves — shared <-> isolated share a directory, so only a
    switch with broker on one side changes where history lives.

    Resolution can legitimately fail (a project that has never been started has
    no project-id), and returning [] for that is correct. What must NOT happen
    is a bare `except` swallowing a NAME error: `_state_dir.state_root()` does
    not exist and never has, the AttributeError was caught here, and so this
    warning spent months telling users their history had MOVED while never once
    saying where — the exact half that was the point. Hence the narrow catch.
    """
    _profile = _read_project_profile(project_root)   # #225: never assume "default"
    if not any("broker" in p for p in (disabled + enabled)):
        return []
    try:
        from botainer.state import dir as _state_dir
        state_root = _state_dir.ensure_user_state_dir(create_if_missing=False).root
        uid = (project_root / ".botainer" / "project-id").read_text().strip()
    except (OSError, AttributeError, KeyError, ValueError):
        return []

    moves: list[tuple[str, Path, Path]] = []
    for plugin in sorted(p for p in set(disabled + enabled)
                         if p.startswith("agent-")):
        from_mode = "broker" if plugin in disabled and "broker" in plugin else (
            "shared" if plugin in disabled else "broker" if "broker" in plugin
            else "shared")
        to_mode = "broker" if mode == "broker" else "shared"
        if from_mode == to_mode:
            continue
        agent = (plugin.replace("-shared", "").replace("-broker", "")
                       .replace("-proxy", ""))
        # THE PROFILE IS A PATH COMPONENT and history_dir_for takes it as a
        # DEFAULTED 5th argument, so omitting it silently meant "default". On a
        # project with `profile: work` this named, measured and MOVED
        # profiles/default -> broker-state/default, renamed the real
        # profiles/default aside as .superseded-<ts>, and left profiles/work —
        # the history the session actually reads — untouched and unreachable,
        # while reporting "Moved N files". #225.
        #
        # auth.py already had _read_project_profile, and ITS docstring records
        # this same bug being fixed once for `auth status`. config_cmd passes
        # the profile; only this caller did not. Sibling drift.
        moves.append((
            agent,
            history_dir_for(state_root, uid, plugin, from_mode, _profile),
            history_dir_for(state_root, uid, plugin, to_mode, _profile),
        ))
    # One (from, to) pair per agent — the loop above can see both `agent-claude`
    # and `agent-claude-broker` in one diff and derive the same pair twice.
    seen: set[tuple[Path, Path]] = set()
    unique = []
    for agent, src, dst in moves:
        if (src, dst) in seen:
            continue
        seen.add((src, dst))
        unique.append((agent, src, dst))
    return unique


#: A SHARED-mode pre_session hook replaces this project's credential with a
#: SYMLINK to an in-container path under this prefix, so it dangles when read
#: from the host. Correct while the project IS in shared mode; stale the moment
#: it is not. `auth_doctor` carries the same constant for the reading side.
_SHARED_LINK_PREFIX = "/shared-auth/"


def _stale_shared_mode_artefacts(profile_dir: Path) -> list[Path]:
    """What in a project's credential dir only makes sense in SHARED mode.

    WHY THIS HAS TO BE CLEARED RATHER THAN TOLERATED. The leftover link is not
    inert. The isolated pre_session hook tests `creds_file.exists()`, which
    FOLLOWS the symlink, reads False, and refuses with "no credentials; run
    login" — and the login it names cannot write through the dangling link
    either (the container has no `/shared-auth` bound, so the write is ENOENT).
    Measured: `exists()` False, `is_symlink()` True, `lexists()` True, `stat()`
    raises. So the user is told to run a command that cannot succeed, by a
    check that cannot see what is wrong, while the shipped README beside it says
    these dangling links are "by design. Don't 'fix' them."

    Returns ONLY what is safe to delete: the symlink itself (never the shared
    credential it names — unlinking a symlink does not touch its target), and
    the README this project wrote. A REGULAR credential file is never returned:
    that is a real isolated-mode login, and removing it would be data loss.
    """
    out: list[Path] = []
    try:
        entries = sorted(profile_dir.iterdir())
    except OSError:
        return out
    for entry in entries:
        if entry.is_symlink():
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if target.startswith(_SHARED_LINK_PREFIX):
                out.append(entry)
        elif entry.name == "README.shared-mode.txt" and entry.is_file():
            out.append(entry)
    return out


def _clear_stale_shared_mode_artefacts(project_root: Path, fams: list[str],
                                       new_mode: str) -> None:
    """Called after a switch AWAY from shared, so the dead end never forms."""
    if new_mode == "shared":
        return
    try:
        from botainer.core import identity
        state_root = state_dir.ensure_user_state_dir(create_if_missing=False).root
        uid, _ = identity.resolve_identity(project_root, identity_accept=False)
    except Exception:
        # Cannot locate the project's own state. Say nothing rather than
        # guess at a path and delete something under it.
        return
    profile = _read_project_profile(project_root)
    for fam in fams:
        profile_dir = (state_root / "state" / uid / "data"
                       / f"agent-{_family_to_agent_name(fam)}"
                       / "profiles" / profile)
        stale = _stale_shared_mode_artefacts(profile_dir)
        if not stale:
            continue
        removed: list[str] = []
        for entry in stale:
            try:
                entry.unlink()
                removed.append(entry.name)
            except OSError as exc:
                click.secho(
                    f"  could not remove the stale shared-mode {entry.name}: "
                    f"{exc}", fg="yellow", err=True)
        if removed:
            click.secho(
                f"  cleared {fam}'s leftover shared-mode files "
                f"({', '.join(sorted(removed))}). They pointed into the "
                f"container and would have made this project look logged in "
                f"while refusing to start. Your shared login itself is "
                f"untouched.", fg="cyan")


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
    help="Apply without confirmation (suppresses the diff prompt). A mode "
         "change relocates this project's agent history; --yes authorises "
         "that as well, with no second question.",
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
      botainer auth use broker --family anthropic  # just one family
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

    # WHICH FAMILIES DOES A BARE `auth use <mode>` TOUCH?
    #
    # It used to be every family INSTALLED ON THE HOST, which is how switching a
    # claude-only project to broker also enabled agent-codex-broker in it. The
    # user asked to change a mode and got a family they had never mentioned —
    # and found out later, at `start`, from a notice that only makes sense to
    # someone who already knew (#185).
    #
    # "Switch" presupposes something to switch. A family with no plugin enabled
    # in this project has no mode to change, so a bare invocation now touches
    # only the families this PROJECT actually uses. Adding one is what
    # `--family` and `botainer plugin enable` are for, and `--family` still
    # works on a family the project lacks — because naming it is asking for it.
    if family:
        target_families = [family]
    else:
        target_families = sorted(
            fam for fam, modes in families.items()
            if any(plugin in enabled for plugin in modes.values())
        )
        if not target_families:
            _common.refuse(
                "auth use",
                "no agent family is enabled in this project, so there is no "
                "auth mode to switch",
                "enable one first — `botainer init` writes a default set, or "
                f"`botainer plugin enable agent-<name>`. Installed families: "
                f"{sorted(families)}.",
            )

    # Naming a family the project does not have ADDS it. That is a legitimate
    # thing to ask for and a surprising thing to have happen quietly, so say it.
    for fam in target_families:
        if not any(plugin in enabled for plugin in families[fam].values()):
            click.secho(
                f"note: {fam!r} is not enabled in this project — this will ADD "
                f"it, not switch it.", fg="yellow", err=True)

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

    # BEFORE the confirm, so "no" is still a real answer. The copy itself
    # happens after the change lands (below), where it can go into the
    # directory the next session will actually read.
    _history_moves = _history_moves_for(project_root, diff_disable,
                                        diff_enable, mode)
    # Same refusal as `config set`: a running session has the directory this
    # would relocate bound and open.
    if _history_moves and refuse_if_a_session_is_live(project_root):
        sys.exit(2)
    for _agent, _src, _dst in _history_moves:
        warn_history_will_move(_src, _dst, what_changed="auth mode")

    if not yes and not click.confirm("Apply these changes?", default=False):
        click.echo("aborted.")
        return

    from botainer.plugins import lifecycle as lifecycle_module
    # ONE write, not disable-then-enable. Two writes have a state between them
    # where this project has no agent plugin — which is a mode the user did not
    # choose, reached silently if anything stops the second write.
    lifecycle_module.swap(project_root, remove=diff_disable, add=diff_enable)
    # AFTER the swap, so this runs only on a switch that actually happened.
    _clear_stale_shared_mode_artefacts(project_root, target_families, mode)
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
    # The mode is now applied, so the destination is the directory the next
    # session will read — which is where the history should land.
    for _agent, _src, _dst in _history_moves:
        offer_carry(_src, _dst, what_changed="auth mode", assume_yes=yes)

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


def _credential_expiry(path: Path | str) -> tuple[str, str]:
    """(state, detail) for an OAuth credential file — presence is not health.

    A credential file can exist while its token is expired. This helper
    distinguishes known expiry from unknown token shapes; mount-mode refresh
    occurs inside a running session.

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
        per_project_kind = ""
        per_project_skipped = ""
        stray_broker_creds: list[str] = []
        project_profile = (
            _read_project_profile(project_root) if project_root else "default")
        if project_root:
            # F12: read-only operation. identity_accept=False so a
            # casual `auth status` doesn't silently bind a moved
            # project's path to its existing credential pool. On
            # identity-change refusal, just skip per-project info.
            from botainer.core import identity
            try:
                uid, _ = identity.resolve_identity(project_root, identity_accept=False)
                proj_dir = state_root / "state" / uid
                # The project's OWN profile, not the literal "default".
                # Broker mode keeps its state under `broker-state/` rather than
                # `profiles/`, so `profiles/` is the right place to look for a
                # per-project LOGIN.
                #
                # THIS COMMENT USED TO END "and a broker project holds no
                # per-project credential by design", and that justification was
                # circular: it assumed the property the check should be
                # establishing. Measured on a real project with a real-shaped
                # `.credentials.json` planted in `broker-state/default`, this
                # command printed "per-project (default): not present" and
                # `auth doctor` printed "No agent-claude credentials found
                # anywhere under this root" — a universal denial, of a file that
                # was there. The directory is bound rw into the container, so
                # "by design" describes an intention, not an invariant.
                #
                # A credential in `broker-state/` is not a login, it is
                # POLLUTION, so it gets its own field rather than being folded
                # into `per_project_*` — those mean "this project's login", and
                # blurring the two would trade a silent miss for a confusing
                # answer.
                profile_dir = (
                    proj_dir / "data" / f"agent-{_family_to_agent_name(fam)}"
                    / "profiles" / project_profile
                )
                # Task #159: check ALL candidate filenames the isolated
                # login may have written, not just the shared-mode one.
                for _cand in _family_isolated_creds_filenames(fam):
                    cand_path = profile_dir / _cand
                    if cand_path.exists():
                        per_project_creds_path = str(cand_path)
                        per_project_creds_present = True
                        per_project_kind = _credential_kind(_cand)
                        break
                if not per_project_creds_present:
                    # Show the first candidate path even when absent
                    # so the user knows where it WOULD be.
                    per_project_creds_path = str(
                        profile_dir / _family_isolated_creds_filenames(fam)[0]
                    )
                # Look where the contract says nothing should be. Every profile
                # directory, not just this project's — a stray credential under
                # a profile the project no longer uses is bound just the same if
                # the project switches back to it.
                from botainer.core.history_carry import credential_files_under
                broker_root = (
                    proj_dir / "data" / f"agent-{_family_to_agent_name(fam)}"
                    / "broker-state")
                if broker_root.is_dir():
                    stray_broker_creds = [
                        str(f) for f in credential_files_under(broker_root)]
            except Exception as exc:
                # Skipping is fine; skipping SILENTLY is not. This used to be a
                # bare `except: pass`, so an identity refusal and a genuine bug
                # produced the same output — no per-project line at all, with
                # nothing saying a lookup had been abandoned. Observed: a
                # logged-in project reported as not logged in, and the reason
                # was invisible.
                per_project_skipped = f"{type(exc).__name__}: {exc}"
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
            "profile": project_profile,
            "per_project_creds_kind": per_project_kind,
            "per_project_skipped": per_project_skipped,
            "stray_broker_creds": stray_broker_creds,
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
        # "login expired". Report the difference directly because both
        # credential files are available to this command.
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
                click.secho(
                    f"    ✓ per-project ({row['profile']}): "
                    f"{row['per_project_creds_kind']}",
                    fg="green")
                click.secho(f"        {row['per_project_creds_path']}", fg="cyan")
            else:
                click.secho(
                    f"    ✗ per-project ({row['profile']}): not present",
                    fg="yellow",
                )
                # WHY "not present" IS NOT THE WHOLE ANSWER. A leftover
                # shared-mode symlink also reads as absent — `exists()` follows
                # the link, and its target is a CONTAINER path. So a project
                # carrying stale debris looked identical to one that had never
                # logged in, while `auth doctor` described the link in detail.
                #
                # ONLY A LEFTOVER IF THE PROJECT HAS LEFT SHARED MODE. In shared
                # mode this symlink is the CORRECT state, and it reads as "not
                # present" on the host regardless because the target is
                # container-absolute. The finder is mode-blind by design (it was
                # written for `auth use`, which calls it precisely WHEN leaving
                # shared mode), so the caller supplies the mode. Without this
                # gate every healthy shared-mode project was told its working
                # link reached nothing — caught by running the healthy fixture.
                _pp = Path(row["per_project_creds_path"])
                _stale_iter = ([] if row["active_mode"] == "shared"
                               else _stale_shared_mode_artefacts(_pp.parent))
                for _stale in _stale_iter:
                    click.secho(
                        f"        leftover shared-mode link, reaching nothing: "
                        f"{_stale}", fg="yellow")
                    click.secho(
                        "        It points into a container path that only "
                        "exists inside a running\n"
                        "        shared-mode session. A login cannot write "
                        "through it. `botainer auth\n"
                        "        doctor` shows the target; removing the link "
                        "lets a login land here.",
                        fg="cyan")
        if row.get("stray_broker_creds"):
            # SAY ONLY WHAT WAS CHECKED. A reviewer found two overclaims in the
            # first version of this text.
            #
            # 1. It said "it is bound into the container on the next launch".
            #    False unless this project is in BROKER mode — measured: switch
            #    to isolated and the stray stays put while the launch binds
            #    `profiles/` instead. So the consequence is stated
            #    conditionally, against the mode actually in effect.
            # 2. It called the file a credential. This check matches on the
            #    FILENAME and never opens it, so an empty logged-out stub reads
            #    identically to a live token. Claiming more than was measured is
            #    the habit this whole queue exists to correct.
            click.secho(
                f"    ! {len(row['stray_broker_creds'])} file(s) with "
                f"credential NAMES are in this project's `broker-state/` "
                f"directory, which is not supposed to hold any:",
                fg="red", bold=True)
            for _stray in row["stray_broker_creds"]:
                click.secho(f"        {_stray}", fg="red")
            if row["active_mode"] == "broker":
                click.secho(
                    "      This project IS in broker mode, so that directory "
                    "is bound rw into the container: while a session runs, the "
                    "agent can read what is in it. Broker mode's premise is "
                    "that the container gets a sentinel and never the real "
                    "token.", fg="yellow")
            else:
                click.secho(
                    f"      This project is in {row['active_mode'] or 'no'} "
                    f"mode, so that directory is NOT bound right now. It would "
                    f"be if you switch to broker mode.", fg="yellow")
            click.secho(
                "      botainer has not opened these files — this check reads "
                "names only, so an empty logged-out stub looks the same as a "
                "live token. `botainer auth doctor` reads them and says which. "
                "There is no command to clear them; look at each one before "
                "deleting it, because it may be the only copy of a login.",
                fg="yellow")

        if row["per_project_skipped"]:
            # Say that a lookup was abandoned rather than rendering a blank as
            # if it were an answer.
            click.secho(
                f"    ? per-project ({row['profile']}): could not be checked "
                f"— {row['per_project_skipped']}",
                fg="yellow")

        # Cross-mode hint per user direction:
        if active == "isolated" and row["shared_creds_present"]:
            click.secho(
                f"  Hint: shared credentials exist for {row['family']}. "
                f"Switch this project to shared mode with "
                f"`botainer auth use shared --family {row['family']}`. "
                f"(Note: shared mode lets any project on this host see this token.)",
                fg="cyan",
            )

    _print_live_holders()


def _print_live_holders() -> None:
    """Host-wide, read-only view of sessions that may redeem the same rotating
    refresh token. List collisions and unresolved records; warn that
    concurrent redemption can invalidate a session.
    """
    from botainer.core import credential_holders as _ch
    try:
        paths = state_dir.ensure_user_state_dir(create_if_missing=False)
        holders = _ch.live_holders(paths)
    except Exception as exc:
        click.echo("")
        click.secho(f"live credential holders: could NOT be checked "
                    f"({type(exc).__name__}: {exc})", fg="yellow")
        click.secho("  That is not an all-clear — the check failed.", fg="yellow")
        return
    click.echo("")
    if not holders:
        click.secho("live credential holders: none "
                    "(no running session holds a login on this host)",
                    fg="green")
        return
    click.secho("live credential holders on this host:", bold=True)
    for h in holders:
        if h.is_unknown:
            click.secho(f"  ?  {h.project_label}  session {h.session_id[:12]}  "
                        f"— {h.why_unknown}", fg="yellow")
        else:
            click.echo(f"     {h.project_label}  session {h.session_id[:12]}  "
                       f"{h.family}/{h.mode}")
    # Do the pairing HERE rather than leaving the reader to do it. A list of
    # sessions is evidence; whether any two of them will log each other out is
    # the question they came with.
    clashing = set()
    for i, a in enumerate(holders):
        hits, _unknown = _ch.collisions(a, holders[i + 1:])
        for b in hits:
            clashing.add((a.session_id, b.session_id))
    if clashing:
        click.secho(
            "  ⚠ two of these can redeem the SAME refresh token, and a token "
            "can be redeemed once.", fg="red", bold=True)
        for a_id, b_id in sorted(clashing):
            click.secho(f"      {a_id[:12]}  ↔  {b_id[:12]}", fg="red")
        click.secho(
            "    Whichever refreshes second gets `invalid_grant`. Stop one, or "
            "put both on broker\n    mode — brokers refresh host-side under one "
            "lock and are safe together.", fg="red")
        click.secho(
            "    Detected, not fixed: concurrent shared sessions still do not "
            "work.", fg="red")
    elif not any(h.is_unknown for h in holders):
        click.secho("  No two of these can invalidate each other.", fg="green")


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


# ─────────────────────────── profiles ───────────────────────────


def collect_profiles(project_root: Path) -> list[dict[str, object]]:
    """Every auth profile that EXISTS for this project, per agent.

    Nothing listed profiles before this. They are created by typing a name —
    `auth login --profile X`, or `config set profile X` — and a directory
    appears. So a user discovered a profile only by remembering they had typed
    it, while `docs/CAPABILITY-SURFACE.md` §4cm treats each one as an ACCOUNT
    boundary. "Which account is this session about to spend?" had no command.

    Enumerates the directories rather than any registry, because the
    directories ARE the registry — there is nowhere else a profile is recorded.
    Both layouts are walked: `profiles/` (isolated and shared) and
    `broker-state/` (broker), which is where the same profile name lives when
    the mode is broker.
    """
    from botainer.core.history_carry import CREDENTIAL_FILENAMES

    state_root = state_dir.ensure_user_state_dir(create_if_missing=False).root
    active = _read_project_profile(project_root)
    notes = _read_profile_notes(project_root)

    # LET AN IDENTITY FAILURE SURFACE. This used to `return []`, and the caller
    # renders an empty list as "No profiles yet — one appears the first time you
    # log in." So a project whose identity could not be resolved — it moved, and
    # `identity_accept=False` correctly refuses to rebind it silently — was told
    # it HAS no profiles. That is an authoritative wrong answer to the account
    # question: the profiles are still there, and "log in to make one" is advice
    # that would create a second account slot rather than find the first.
    #
    # "I could not answer" and "the answer is none" are different, and only the
    # caller can say which without inventing a sentinel value. The narrow catch
    # below is kept for the genuinely-absent state root, where an empty list IS
    # the right answer.
    from botainer.core import identity
    uid, _ = identity.resolve_identity(project_root, identity_accept=False)

    out: list[dict[str, object]] = []
    data_dir = state_root / "state" / uid / "data"
    if not data_dir.is_dir():
        return out
    for agent_dir in sorted(data_dir.glob("agent-*")):
        for kind in ("profiles", "broker-state"):
            parent = agent_dir / kind
            if not parent.is_dir():
                continue
            for child in sorted(parent.iterdir()):
                if not child.is_dir() or child.is_symlink():
                    continue
                if ".superseded-" in child.name or ".archived-" in child.name:
                    continue          # set aside by a switch; not a profile
                cred = ""
                for cand in sorted(CREDENTIAL_FILENAMES):
                    if (child / cand).exists():
                        cred = _credential_kind(cand)
                        break
                out.append({
                    "agent": agent_dir.name,
                    "where": kind,
                    "name": child.name,
                    "active": child.name == active and kind == "profiles",
                    "credential": cred,
                    "note": notes.get(child.name, ""),
                    "path": str(child),
                })
    return out


@auth.command("profiles")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@handle_refusals
def auth_profiles(as_json: bool) -> None:
    """List the auth profiles that exist for this project.

    A profile separates ACCOUNTS, not just credential files. Add a one-line
    `profile_notes:` entry in .botainer/config.yaml to record what each is for.
    """
    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse(
            "auth profiles",
            "not inside a botainer project",
            "cd into a project (run `botainer init` if needed)",
        )
    try:
        rows = collect_profiles(project_root)
    except Exception as exc:
        # SAY WHICH QUESTION FAILED. Rendering this as "No profiles yet" told
        # the user a fact about their ACCOUNTS that was not established.
        click.secho(f"refused: cannot list profiles: {exc}", fg="red", err=True)
        click.echo(
            "  This is NOT 'you have no profiles' — the profiles could not be\n"
            "  LOOKED UP, because this project's identity did not resolve. If\n"
            "  you moved the project, accept the change once:\n"
            "      botainer start --accept-identity-change",
            err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("No profiles yet — one appears the first time you log in.")
        click.secho("  botainer auth login --profile <name>", fg="cyan")
        return

    click.secho(f"Profiles for {project_root}", bold=True)
    click.secho(
        "  A profile separates ACCOUNTS. Two profiles can be two different "
        "logins.", fg="cyan")
    current_agent = ""
    for row in rows:
        if row["agent"] != current_agent:
            current_agent = str(row["agent"])
            click.echo("")
            click.echo(current_agent)
        marker = "*" if row["active"] else " "
        where = "" if row["where"] == "profiles" else "  (broker state)"
        cred = row["credential"] or "not signed in"
        click.echo(f"  {marker} {row['name']!s:<16} {cred}{where}")
        if row["note"]:
            click.secho(f"      {row['note']}", fg="cyan")
    click.echo("")
    # "the profile NAME", precisely. The marker is per-name, not per-agent, so
    # an agent the project is not currently using still shows its slot marked —
    # correct, because that IS the profile it would use, and misleading if the
    # legend claims more than that.
    click.echo("  * = the profile NAME this project uses, whichever agent you "
               "start")
    click.echo("      (`botainer config set profile <name>` to change)")
    if not any(r["note"] for r in rows):
        click.secho(
            "  Tip: say what each is for with a `profile_notes:` block in "
            ".botainer/config.yaml", fg="cyan")


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

    # LOOK WHERE THE PROXY ACTUALLY WRITES. This used to read
    # `data/agent-claude-proxy/audit.jsonl` — wrong directory AND wrong
    # filename. The proxy writes `sessions/<session_id>/proxy-audit.jsonl`
    # (its pre_session hook derives `session_scratch/proxy-audit.jsonl`), which
    # is also what the capability summary renders for the user.
    #
    # MEASURED: with a genuinely valid three-entry chain sitting in the real
    # location, this command exited 5 with "audit log absent … Otherwise:
    # tampering." An integrity command that cannot see an intact chain, and
    # whose only other suggestion is tampering, is worse than no command — it
    # manufactures an incident out of a healthy install. Nothing has ever
    # written the old path, so the check could not pass for anybody, ever.
    #
    # ONE LOG PER SESSION, so this attests over a SET and says how big it is.
    # A single-file check could not have expressed "four sessions verified"
    # or "three verified, one failed", and collapsing them would hide which.
    sessions_dir = proj_paths.base / "sessions"
    audit_logs = sorted(sessions_dir.glob("*/proxy-audit.jsonl"))

    if not audit_logs:
        # Tasks #99 + #104: was exit 0 ("nothing to verify"). That meant
        # `rm audit.log && botainer auth check` reported success — the prior
        # "tamper detection" claim was a lie because tampering = delete the
        # log. Still exit 5: absence is not attestation.
        #
        # But SAY WHAT WOULD DISTINGUISH the two cases instead of offering
        # "tampering" as the alternative to "not configured". The session
        # records are the evidence: sessions that ran with the proxy enabled
        # should each have left a log beside them.
        try:
            session_count = sum(1 for p in sessions_dir.iterdir() if p.is_dir())
        except OSError:
            session_count = 0
        click.secho(
            "✗ no proxy audit log for this project; cannot attest a chain.",
            fg="yellow", err=True,
        )
        click.echo(f"  Looked for: {sessions_dir}/<session>/proxy-audit.jsonl",
                   err=True)
        if session_count == 0:
            click.echo(
                "  This project has no session directories at all, so the "
                "proxy has\n  never run here and there is nothing to verify.",
                err=True)
        else:
            click.echo(
                f"  This project HAS {session_count} session director"
                f"{'y' if session_count == 1 else 'ies'} but none carries a "
                f"log.\n  That is expected if agent-claude-proxy was not "
                f"enabled for them; if it\n  WAS, the logs are missing and "
                f"that is what tampering looks like.",
                err=True)
        sys.exit(5)

    failures: list[str] = []
    for log in audit_logs:
        ok, detail = _verify_audit_chain(log)
        if ok:
            click.secho(f"✓ {detail}", fg="green")
        else:
            failures.append(f"{log}: {detail}")
            click.secho(f"✗ audit chain failed: {log}: {detail}",
                        fg="red", err=True)
    if failures:
        # NAME THE DENOMINATOR. "1 of 4 failed" is actionable; a bare failure
        # line leaves the reader unsure whether the rest were even looked at.
        click.secho(
            f"\n{len(failures)} of {len(audit_logs)} audit chain(s) FAILED.",
            fg="red", bold=True, err=True)
        sys.exit(2)
    click.secho(f"\nAll {len(audit_logs)} audit chain(s) verified.", fg="green")
    sys.exit(0)


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
