"""`botainer start` — launch the session container.

Composes config + policy + plugins → SessionSpec, validates the MountPlan,
renders runtime argv, prints capability summary (with confirmation gate on
first launch + after image change), then execs the runtime.
"""

from __future__ import annotations

import os
import re as _re
import sys
from pathlib import Path

import click

from botainer.auth_modes import (AUTH_MODES, ONE_SHOT_AXIS_HELP,
                                  choice_help)

from botainer.cli._refusal_handler import handle_refusals
from botainer.core import composition
from botainer.inspect import capability_summary

_PROFILE_RE = _re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _has_global_setup() -> bool:
    """True if `botainer setup` has been run on this host."""
    from botainer.state import dir as state_dir
    try:
        paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    except Exception:
        return False
    # The site policy is written by setup; its presence is the signal.
    return (paths.root / "policy.yaml").exists()


def _fast_fail_hint(rc: int, elapsed_s: float, runtime: str) -> str | None:
    """When a session dies almost immediately with an error, tell the user how to
    diagnose it — instead of leaving them with a raw runtime error they can't
    decode (the foreground path hands the terminal to docker, so botainer can't
    capture that error text; a pointer is the reliable channel).

    Heuristic + guardrails: a real session runs for a while, so a NON-zero exit
    within a few seconds almost always means it couldn't START (disk full, bad
    image, config) rather than the agent's work failing. Excludes the signal
    exit codes a user gets by quitting fast (130 = Ctrl-C, 143 = SIGTERM) to
    avoid a false positive. Pure so the thresholds are unit-tested."""
    if rc == 0 or rc in (130, 143) or elapsed_s >= 6.0:
        return None
    msg = (
        f"\nThe session ended after {elapsed_s:.1f}s with an error — that usually "
        "means it couldn't START (not your work failing). Run `botainer doctor` to "
        "diagnose."
    )
    if runtime == "docker":
        msg += (
            " Common cause on a laptop: Docker is out of disk (its own hidden disk, "
            "separate from your Mac's free space). Reclaim it SAFELY with `docker "
            "builder prune -af` — do NOT use `docker system prune -a`, whose `-a` "
            "deletes your built agent image. `botainer doctor` shows usage."
        )
    return msg


# NOTE: `_apply_auth_mode_override` (eager disk-mutate + atexit) was
# removed per insecure-defaults H3 + sharp-edges F3. The replacement
# is `compose_session(..., auth_mode_override=...)`: an in-memory
# parameter that swaps plugin variants WITHOUT touching the on-disk
# config. SIGKILL/OOM/exec* during start no longer leak persistent
# config edits.


def _maybe_prompt_login(project_root: Path) -> None:
    """If shared/proxy mode is active but no shared creds exist, offer login.

    Single-command onboarding: `botainer start` should walk users
    through everything missing.
    """
    from botainer.cli.auth import (
        _discover_families,
        _family_creds_filename,
        _family_to_agent_name,
        _invoke_plugin_login,
        _read_plugins_enabled,
        _resolve_active_family_mode,
    )
    from botainer.state import dir as state_dir
    families = _discover_families()
    enabled = _read_plugins_enabled(project_root)
    active = _resolve_active_family_mode(enabled, families)
    state_root = state_dir.ensure_user_state_dir(create_if_missing=False).root
    for family, mode in active.items():
        if mode not in {"shared", "proxy"}:
            continue
        shared_file = (
            state_root / "shared-auth" / f"agent-{_family_to_agent_name(family)}"
            / _family_creds_filename(family)
        )
        if shared_file.exists():
            continue
        click.secho(
            f"\nNo shared credential for {family} at {shared_file}.",
            fg="yellow",
        )
        # Sharp-edges F9: default=False so a user who hit Enter on what
        # looks like a hung process doesn't accidentally trigger an
        # OAuth browser flow.
        if click.confirm(
            f"Run `botainer auth login --shared --agent "
            f"{_family_to_agent_name(family)}` now?",
            default=False,
        ):
            shared_plugin = families[family].get("shared") or families[family].get("proxy")
            if shared_plugin:
                rc = _invoke_plugin_login(shared_plugin, shared=True)
                if rc != 0:
                    click.secho(
                        f"login failed (exit {rc}); aborting start.",
                        fg="red", err=True,
                    )
                    sys.exit(rc)
        else:
            click.secho(
                f"  Skipping. The session may fail at start time if "
                f"{family} can't authenticate.",
                fg="cyan",
            )


def _shared_auth_family(spec) -> str | None:
    """Which agent family this session uses in SHARED mode, or None.

    Returns the family ("claude", "codex"), not a bool, because the caller has
    to know WHICH credential is at stake. The old bool made every caller treat
    shared auth as one global thing, and the concurrency prompt then warned a
    codex launch about a live Claude session (reported 2026-09-04).
    """
    for p in getattr(spec, "plugins_enabled", []) or []:
        if isinstance(p, str) and p.startswith("agent-") and p.endswith("-shared"):
            return p[len("agent-"):-len("-shared")]
    return None


def _uses_shared_auth(spec) -> bool:
    """True when the COMPOSED session enabled a shared auth variant.

    Reads spec.plugins_enabled, not the policy default — the same ground truth
    the disclosure banner uses. A session can enable `agent-*-shared` under an
    isolated-DEFAULT policy and vice versa, so the policy is the wrong source.
    """
    return any(
        isinstance(p, str) and p.startswith("agent-") and p.endswith("-shared")
        for p in getattr(spec, "plugins_enabled", []) or []
    )


def shared_mode_banner() -> list[str]:
    """The shared-mode warning, as data.

    A PURE FUNCTION rather than two inline `click.secho` calls, because the
    only test of this text used to read the source of `start.callback` and
    grep it — while its own docstring claimed "Asserted on the emitted string,
    not on source text". It was not, and the docstring would have satisfied the
    assertion on its own. Returning the lines makes the real thing testable,
    which is a better answer than a cleverer grep.

    WHAT IT LEADS WITH, and why. "One login, used by every shared-mode project"
    was the old first sentence, and it sold the mode on the one thing it cannot
    do. ONE SESSION AT A TIME is the defining constraint and it appeared on NO
    forward-facing surface — not here, not `auth use`, not `init`, not a single
    doc. Only `auth doctor` said it, after the user was already broken.
    """
    return [
        "⚠ Auth mode: SHARED — ONE SESSION AT A TIME, across all "
        "shared-mode projects on this machine. Starting a second one "
        "logs the first out: refreshing mints a new token and revokes "
        "the old, so whichever refreshes first wins. For projects you "
        "run side by side, use `botainer auth use isolated` in each.",
        "  Also: one credential shared by every project means a "
        "compromised agent in ANY of them can read AND OVERWRITE it — "
        "overwriting changes which account they all run as. (Write "
        "access is required so the agent can save its refreshed "
        "token.)",
    ]


@click.command("start")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the runtime argv that would execute, without launching.",
)
@click.option(
    "--accept-identity-change",
    is_flag=True,
    help="Non-interactive accept of path-history change for the same project UUID.",
)
@click.option(
    "--fork",
    is_flag=True,
    help=(
        "This checkout is an independent COPY, not a moved one: mint a new "
        "project UUID for it and leave the original's state dir alone. The "
        "fork starts with an empty /packages and /scratch."
    ),
)
@click.option(
    "--runtime",
    type=click.Choice(["docker", "apptainer", "auto"]),
    default="auto",
    help="Force a runtime adapter. Default: auto-detect.",
)
@click.option(
    "--yes",
    "auto_yes",
    is_flag=True,
    help="Auto-accept the capability-summary confirmation prompt.",
)
@click.option(
    "--quiet",
    is_flag=True,
    help="Suppress the capability summary line on launch.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the session info as JSON instead of human-readable summary.",
)
@click.option(
    "--detach",
    "-d",
    "--background",
    is_flag=True,
    help=(
        "Run the container detached (background). Returns immediately "
        "with the container ID. Required to use `botainer nudge` against "
        "the session from another shell. Docker runtime only at v0.1.0. "
        "(`--background` is an accepted alias for `--detach`.)"
    ),
)
@click.option(
    "--agent",
    "agent_override",
    default=None,
    help=(
        "One-shot override of WHICH AGENT runs this session (e.g. `codex`), "
        "for this launch only. Swaps the enabled agent plugin via in-memory "
        "composition and does NOT modify .botainer/config.yaml — so the next "
        "launch uses whatever the config says. Keeps your current auth mode "
        "across the swap (shared stays shared). For a persistent change, edit "
        "`agent:` in .botainer/config.yaml."
    ),
)
@click.option(
    "--auth-mode",
    type=click.Choice(AUTH_MODES),
    default=None,
    help=choice_help(
        "One-shot override of auth mode. Switches the enabled auth-family "
        "plugin variant via in-memory composition. "
        + ONE_SHOT_AXIS_HELP + " Modes:"
    ),
)
@click.option(
    "--no-auto-onboard", "no_auto_onboard",
    is_flag=True,
    help=(
        "Suppress the auto-onboarding prompts (init, setup, login). If "
        "the project isn't initialized / setup not done / not logged in, "
        "refuse with the structured error instead of prompting. Useful "
        "for scripts and CI."
    ),
)
@click.option(
    "--auth-profile",
    default=None,
    help=(
        "One-shot override of auth profile — reads "
        "<creds-dir>/profiles/<name>/ instead of .../default/. "
        + ONE_SHOT_AXIS_HELP
    ),
)
@click.option(
    "--preflight",
    is_flag=True,
    help=(
        "Compose the session + verify capability-surface invariants, but DO "
        "NOT launch. Pre-merge gate for security-surface changes: prints "
        "every bind, env var and entrypoint wrap the COMPOSED session has — "
        "pre_session hooks have not run, so binds THEY add at launch are not "
        "shown (`dry-run --include-hooks` shows those). Exit 0 iff the "
        "surface matches docs/CAPABILITY-SURFACE.md expectations (no "
        "forbidden binds, no unexpected env). Needs no container runtime "
        "installed: nothing is started and the network is never touched. "
        "With no --runtime it checks BOTH the docker and the apptainer plan; "
        "a plan it could not compose here is named and exits 3 — NOT a pass — "
        "unless the project has no such plan at all (e.g. an apptainer-only "
        "plugin has no docker plan). The apptainer plan needs the .sif it "
        "names to exist. With --runtime X it checks X only."
    ),
)
@handle_refusals
def start(
    dry_run: bool,
    accept_identity_change: bool,
    fork: bool,
    runtime: str,
    auto_yes: bool,
    quiet: bool,
    as_json: bool,
    detach: bool,
    auth_mode: str | None,
    agent_override: str | None,
    auth_profile: str | None,
    no_auto_onboard: bool,
    preflight: bool,
) -> None:
    """Launch the agent session container."""
    from botainer.cli import _common

    # Sharp-edges F9: suppress all auto-onboarding when EITHER the
    # user passed --no-auto-onboard, OR --yes (--yes means "don't
    # ask me anything"), OR --quiet, OR --json, OR stdin/stdout
    # isn't a TTY.
    can_prompt = (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and not quiet
        and not as_json
        and not no_auto_onboard
        and not auto_yes
    )

    # Project-init check first.
    project_root = _common.find_project_root()
    if project_root is None:
        if can_prompt:
            cwd = Path.cwd()
            click.secho(
                f"This directory ({cwd}) is not a botainer project yet.",
                fg="yellow",
            )
            if not click.confirm(
                "Initialize it now (botainer init)?", default=False
            ):
                click.secho("aborted; run `botainer init` manually when ready.",
                            fg="cyan")
                sys.exit(0)
            from botainer.cli import init as init_cli
            init_cli.do_init(cwd, agent=None, name=None, force=False,
                            runtime=runtime if runtime != "auto" else None,
                            quiet=quiet)
            project_root = cwd
        else:
            project_root = Path.cwd()
            # compose_session refuses with config-missing below.

    # Now check global setup. Same precedence as above.
    # Independent-read F-11: also short-circuit on dry_run. A `--dry-run`
    # caller is asking "what WOULD this look like"; it should never run
    # `setup --interactive` via CliRunner as a side effect.
    if not _has_global_setup() and not as_json and not dry_run and can_prompt:
        click.secho(
            "It looks like `botainer setup` hasn't been run on this host.",
            fg="yellow",
        )
        click.secho(
            "  Setup is a one-time per-host step that installs the bundled\n"
            "  plugins and writes your user policy (~/.botainer/policy.yaml).\n"
            "  It does NOT build the agent image — run "
            "`botainer image build <agent>` for that.",
            fg="cyan",
        )
        if click.confirm("Run `botainer setup` now?", default=False):
            import subprocess
            # Readiness audit #1: run setup as a REAL subprocess so its
            # interactive prompts reach the user's TTY. CliRunner captures
            # stdin/stdout, so `setup --interactive` under it can't prompt (it
            # gets EOF) and its output is hidden — the auto-onboard silently
            # aborted. subprocess.run inherits this process's stdio → real TTY.
            rc = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "botainer.cli.main",
                 "setup", "--interactive"],
                # Suppress the child's tip footer so the parent shows exactly one
                # (Fable-5 M1: self-invocations inherit the TTY → double footer).
                env={**os.environ, "BOTAINER_NO_TIPS": "1"},
            ).returncode
            if rc != 0:
                click.secho(f"setup failed (exit {rc})", fg="red", err=True)
                sys.exit(rc)
        else:
            click.secho(
                "aborted; run `botainer setup` manually when ready.",
                fg="cyan",
            )
            sys.exit(0)

    # --auth-profile: validate against a charset (sharp-edges F5 +
    # insecure-defaults audit: prevents path-traversal via profile name).
    if auth_profile is not None:
        if not _PROFILE_RE.fullmatch(auth_profile):
            click.secho(
                f"refused: --auth-profile {auth_profile!r} must match "
                f"^[a-z][a-z0-9_-]{{0,31}}$",
                fg="red", err=True,
            )
            sys.exit(2)
        # #149, RESOLVED. This used to set os.environ, which
        # (a) PROPAGATED into every process the launcher exec's — screen
        # sessions, child shells — violating the one-shot contract, and
        # (b) did not reach the hooks anyway: `run_hook` scrubs the host
        # environment to `_HOOK_ENV_ALLOWLIST`, and BOTAINER_PROFILE is not
        # on it. Measured: with BOTAINER_PROFILE=work set exactly as this line
        # did, the hook created `profiles/default`.
        #
        # So `--auth-profile` reached NOTHING at session time, while
        # `auth login --profile` DID honour it — botainer created a directory
        # for an account and then never used it.
        #
        # Now it is a compose_session parameter, overriding cfg.profile in
        # memory, and composition puts spec.profile into the hook env. No
        # os.environ mutation, so nothing leaks into child processes.

    # --auth-mode handled via compose_session parameter (in-memory; no
    # disk mutation). Sharp-edges F3 + insecure-defaults H3: eager
    # mutation + atexit could leak persistent config edits on
    # SIGKILL/OOM/exec*. Now: composition sees the override; on-disk
    # config is unchanged regardless of how the process exits.
    if auth_mode == "proxy" and not as_json and not quiet:
        click.secho(
            "⚠ --auth-mode proxy is EXPERIMENTAL at v0.1.0: no refresh-on-401, "
            "per-project credentials only, OAuth files may not work. "
            "See `botainer auth use proxy` for the full caveat list.",
            fg="red", err=True,
        )
    # Auth-not-logged-in check: only prompts if can_prompt AND not
    # dry_run (dry-run shouldn't OAuth-browse). Per F9: --yes also
    # suppresses this prompt.
    if project_root is not None and can_prompt and not dry_run:
        _maybe_prompt_login(project_root)
    # #192: a HAND-EDIT of config.yaml moves the agent's history and, until
    # now, carried nothing — `auth use` and `config set` both offer the carry;
    # editing the file and running `start` did not, and that is the route
    # botainer's own --auth-profile help recommends for a persistent change.
    #
    # BEFORE compose, so the question is asked before anything is launched, and
    # never on dry-run/preflight: those render a plan, and neither prompting
    # nor recording "what config said at the last start" is part of rendering.
    if project_root is not None and not dry_run and not preflight:
        from botainer.cli._history_prompt import (
            offer_carry_for_declared_change,
        )
        offer_carry_for_declared_change(
            project_root, assume_yes=auto_yes, can_prompt=can_prompt)
        # A ONE-SESSION override reads a different history dir and carries
        # nothing — correctly, since a flag must not move history permanently.
        # Name both directories so an empty overridden session is not mistaken
        # for lost transcripts in the configured history directory.
        from botainer.cli._history_prompt import warn_override_moves_history
        warn_override_moves_history(
            project_root,
            auth_mode_override=auth_mode,
            auth_profile_override=auth_profile,
            agent_override=agent_override,
        )
    # ONE set of compose arguments, shared by the launch below and by
    # `--preflight`'s per-runtime composes. Not tidiness: `inspect`/`dry-run`
    # drifting from what `start` actually composes is a defect this project
    # has already had (11 of start's 13 options never reached the preview),
    # and a gate that composes with different arguments than the launch is
    # the same bug wearing the word "gate". Adding a parameter to the call
    # below now reaches the checks by construction.
    _compose_kwargs = dict(
        identity_accept=accept_identity_change,
        fork=fork,
        auth_mode_override=auth_mode,
        # The option is `default=None`, so any value here was TYPED. The old
        # `if auth_profile != "default" else None` therefore discarded exactly
        # one thing: an explicit `--auth-profile default`. That is a real
        # request whenever the project config says something else, and `inspect`
        # (inspect.py:82) honoured it — so preview and launch disagreed,
        # silently, about which credential and history directory to use. (#222)
        auth_profile_override=auth_profile,
        agent_override=agent_override,
        # #227: the masking-dir reset belongs to `start`, not to `inspect` or
        # `dry-run`, which must not mutate anything. `--preflight` shares this
        # dict and therefore also resets — true before this dict existed too
        # (it shared the one compose call), and idempotent, so a second
        # runtime's compose re-resets an already-empty anchor dir. Worth
        # knowing rather than assuming: a preflight is not a pure read.
        reset_null_anchor=True,
    )
    # --preflight: verify capability-surface invariants without launching.
    # Pre-merge gate for security-surface changes: the project's own review
    # protocol requires exit 0 before a commit touching the surface is pushed.
    #
    # BEFORE the launch compose, and that placement is the fix rather than a
    # style choice. The launch compose resolves the runtime by PATH discovery
    # and falls back to `mock`, whose image resolution takes the docker-shaped
    # branch — so on a host with no docker (every cluster login node, this dev
    # container) a project holding a built `.sif` and no docker image was
    # refused `config-missing: no recorded image` and exited 2, and the
    # apptainer plan the developer wanted checked was never composed. That is
    # the same defect as the `runtime-not-available` exit 3 below: a
    # LAUNCH-shaped precondition stopping a check that launches nothing.
    # `run_all` composes each REAL runtime's plan itself and never consults the
    # mock fallback.
    if preflight:
        from botainer.inspect import preflight as preflight_mod

        # The shared-mode banner lives below the launch compose, which a
        # preflight no longer reaches — and it was the ONLY place a preflight
        # said "this session shares one credential with your other projects"
        # (the /shared-auth bind itself is a hook contribution, so it is not in
        # the compose-time plan the gate prints). Said once, here, for the
        # first plan that composes: a disclosure the gate used to make and
        # stopped making is a regression even when nothing else changed.
        _said = {"shared": False}

        def _compose_for_preflight(rt: str):
            spec = composition.compose_session(
                project_root, runtime_choice=rt, **_compose_kwargs)
            if (not as_json and not quiet and not _said["shared"]
                    and _uses_shared_auth(spec)):
                _said["shared"] = True
                for _line in shared_mode_banner():
                    click.secho(_line, fg="yellow", err=True)
            return spec

        rc = preflight_mod.run_all(
            _compose_for_preflight,
            requested_runtime=runtime,
            # Which plan a clean verdict is OBLIGED to cover. `init --runtime
            # apptainer` writes `runtime: apptainer`, and for such a project
            # the apptainer plan is the session — a verdict that skipped it and
            # passed on docker would be a false all-clear.
            targeted_runtime=composition.targeted_runtime(project_root, runtime),
        )
        sys.exit(rc)

    spec = composition.compose_session(
        project_root, runtime_choice=runtime, **_compose_kwargs)

    # Insecure-defaults H4: refuse mock runtime for `start` (it's fine for
    # dry-run / inspect / access where no real container is launched). If
    # composition fell back to mock because no docker/apptainer was found,
    # surface that as a real refusal rather than silently proceeding.
    # `--preflight` is not in this condition because it has already returned
    # above; it used to reach here and exit 3 on exactly the hosts its own
    # help called safe, offering `dry-run` and `inspect` — which check
    # nothing — as the remedies.
    if spec.runtime == "mock" and not dry_run:
        click.secho(
            "refused: runtime-not-available: no docker or apptainer found.",
            fg="red",
            err=True,
        )
        click.secho(
            "    → Install Docker (laptop) or apptainer (HPC) and re-run.",
            fg="cyan",
            err=True,
        )
        click.secho(
            "    → To preview without launching, use `botainer dry-run` or `botainer inspect`.",
            fg="cyan",
            err=True,
        )
        sys.exit(3)  # runtime error (exit 4 is reserved for selftest posture fail)

    # Readiness audit #2: SHARED-credential disclosure derived from the ACTUAL
    # composed session (which auth-family VARIANT is enabled), NOT the policy
    # DEFAULT. The old pre-compose check read `default_auth_mode`, so a session
    # that actually enabled `agent-*-shared` under an isolated-DEFAULT policy got
    # NO warning (under-disclosure), and an isolated session under a shared
    # default got a FALSE one. spec.plugins_enabled is ground truth (mirrors the
    # capability-summary MOUNT/PROXY disclosure + the HPC submit consent).
    if not as_json and not quiet:
        _shared_variant = any(
            p.startswith("agent-") and p.endswith("-shared")
            for p in spec.plugins_enabled
        )
        if _shared_variant:
            for _line in shared_mode_banner():
                click.secho(_line, fg="yellow", err=True)

    # THE ENFORCEMENT, not another paragraph. Prose about "one session at a
    # time" is what was missing for months AND would not have helped: the
    # person who wrote the mode did not know the constraint. So ask the
    # bookkeeping instead of the user's memory — a live shared-mode session in
    # another project is a fact the launcher already records.
    _shared_family = None if (dry_run or preflight) else _shared_auth_family(spec)
    if _shared_family:
        from botainer.cli import _common as _c_shared
        _c_shared.confirm_no_other_shared_session(
            project_root, agent_family=_shared_family,
            as_json=as_json, assume_yes=auto_yes)

    if dry_run:
        from botainer.cli import dry_run as dry_run_cli  # avoid circular at import

        dry_run_cli.print_dry_run(spec)
        return

    # Task #145: hooks-then-confirm order. Was the inverse -- user saw the
    # capability summary, said YES, then plugin pre_session hooks added
    # binds/env/sidecars the user never consented to. Now: run hooks FIRST so
    # the summary reflects the actual spec the adapter sees. host_pre_launch
    # runs before pre_session because it may produce env-files that flow into
    # the spec for adapter rendering (hpc-modules uses this).
    spec = composition.run_host_pre_launch_hooks(spec)
    spec = composition.run_pre_session_hooks(spec)

    # Audit T10: re-render the agent-facing files from the POST-hook spec so the
    # agent's AGENT_ACCESS.txt / AGENT_HINTS.md reflect what it actually has --
    # the credential bind, the git overlay, and the #160 module software-root
    # binds -- not the understated compose-time view.
    composition.render_agent_files(spec)

    # Capability summary + confirmation gate (sharp-edges F6) -- now
    # accurate because spec includes all hook contributions.
    # #215: is something ELSE on this host already able to redeem this login?
    # A refresh token can be redeemed once; shared mode refreshes inside the
    # container under no lock, so a shared session running beside another
    # shared session or beside a broker will log one of them out with an
    # `invalid_grant` that names no cause. Computed here, where the state tree
    # is readable, and printed as part of the block the user is deciding on.
    #
    # DETECTION, NOT A FIX. Concurrent shared sessions still do not work; #110
    # is that decision. Saying so in the warning matters — "we warn about it
    # now" reads like a resolution and is not one.
    from botainer.core import credential_holders
    proceed = capability_summary.print_and_maybe_confirm(
        spec, quiet=quiet, as_json=as_json,
        # `start` runs at a terminal; `--yes` is a real pre-authorisation.
        pre_authorised=auto_yes, interactive=True,
        extra_warnings=credential_holders.warning_lines_for_spec(spec),
        # Not the sbatch path: this IS `start`, so broker mode is reachable
        # and the credential paragraph may say so.
        on_sbatch_path=False,
    )
    if not proceed:
        # TEAR DOWN WHAT COMPOSE STARTED. Consent is asked AFTER compose,
        # because it shows the composed capability summary — that is the point
        # of asking it there. But compose has by then run this project's
        # pre_session hooks, and a hook may have started a HOST PROCESS
        # (wolfram-sidecar Popens a helper; the broker hooks start a daemon).
        # Declining left that running: the only `run_post_session_hooks` on this
        # path was inside the launch `finally`, which a decline never reaches.
        #
        # Measured by a refuting review with a probe plugin: `n` at the prompt,
        # and the helper was still alive afterwards. This is the same rule the
        # sbatch launcher applies to its non-launching exits — anything that
        # composes and does not launch tears down — and `start` is the path
        # where the two named examples can actually run, since broker mode is
        # refused on the sbatch path and socket binds are refused cross-node.
        try:
            composition.run_post_session_hooks(spec)
        except Exception as exc:      # best-effort, exactly like compose's
            click.secho(
                f"post_session cleanup after a declined launch reported: {exc}",
                fg="yellow", err=True)
        click.secho("Aborted by user.", fg="yellow", err=True)
        sys.exit(0)

    # Set the user's terminal window title so multiple sessions are
    # visually distinct across terminals. The OSC sequence is suppressed
    # in --quiet / --json modes and when stdout isn't a TTY.
    if not quiet and not as_json and not detach:
        from botainer.inspect import terminal_title
        terminal_title.emit_title(spec)

    if detach:
        handle = composition.launch(spec, detach=True)
        # Don't run post_session hooks for detach — the container is
        # still running. Post_session fires on `botainer stop` (or
        # equivalent cleanup).
        # Audit T11: include a breadcrumb back to the session (session dir +
        # project) so a long-lived detached session can be re-found.
        _rec_path = str(Path(spec.state_dir) / "sessions" / spec.session_id)
        if as_json:
            import json as _json
            click.echo(_json.dumps({
                "session_id": spec.session_id,
                "container_id": handle.id,
                "runtime": spec.runtime,
                "nudge_supported": "nudge" in spec.plugins_enabled,
                "project_root": spec.project_root,
                "session_record": _rec_path,
            }))
        elif not quiet:
            click.secho(
                f"started detached: session={spec.session_id} container={handle.id}",
                fg="green",
            )
            click.secho(
                f"    project: {spec.project_root}\n"
                f"    record:  {_rec_path}",
                fg="cyan",
            )
            if "nudge" in spec.plugins_enabled:
                click.secho(
                    "    use `botainer nudge \"<text>\"` from another shell to inject input.",
                    fg="cyan",
                )
            else:
                click.secho(
                    "    use `botainer stop` to terminate; `botainer attach` to connect.",
                    fg="cyan",
                )
        return

    # Auto-start the HPC job dispatcher for this session so the agent's
    # `botainer-job submit` requests actually RUN (otherwise they sit `pending`
    # forever — the feature was unusable without this). Fully guarded: returns
    # None + warns on any problem; the session launches regardless.
    from botainer.hpc import autodispatch as _autodisp
    _disp_pid = _autodisp.maybe_start(spec)
    import time as _time
    _t0 = _time.monotonic()
    try:
        handle = composition.launch(spec)
        rc = composition.attach(handle)
    finally:
        # post_session hooks always fire (cleanup), even if launch failed.
        composition.run_post_session_hooks(spec)
        _autodisp.stop(_disp_pid)
    # Point the user at the fix when a session dies almost instantly (a launch
    # failure the foreground path can't surface any other way).
    _hint = _fast_fail_hint(rc, _time.monotonic() - _t0, spec.runtime)
    if _hint:
        click.secho(_hint, fg="yellow", err=True)
    sys.exit(rc)
