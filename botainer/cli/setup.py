"""`botainer setup` — one-time per-user setup.

Writes `~/.botainer/policy.yaml` (or `${MY_BOTAINER}/policy.yaml`) with safe
defaults, creates the state-dir tree, installs bundled first-party plugins,
and prints first-run guidance.

Per Phase 0 of v0.1.0 plan: bundled plugins are auto-discovered and installed
without third-party gates. Image build for dockerfile-source plugins happens
in Phase 1+ (currently the bundled agent-claude image is pre-built on host).
"""

from __future__ import annotations

import sys

import click

from botainer.cli import doctor as doctor_mod
from botainer.plugins import builtin
from botainer.state import dir as state_dir


# --allow-tier IS HIDDEN, DELIBERATELY (audit sweep C).
#
# It was a documented click.Choice of three values, TWO of which destroyed a
# fresh install:
#
#     $ botainer setup --allow-tier third-party
#     ... raw Python traceback ...
#     RuntimeError: refused: site policy plugins.allowed_tiers=[] excludes
#                   first-party; bundled plugin 'agent-claude' not installed.
#
# setup WROTE the value into policy.yaml first, then policy.intersect() met the
# site default ["first-party"] and produced the empty set — so nothing installed
# AND every later plain `botainer setup` crashed identically. Escape was
# `--force`, which nothing mentioned. The message blamed "site policy" while the
# offending value sat in the USER policy the command had just written, sending
# people to their cluster admin for a self-inflicted wound.
#
# `community-verified` was worse than broken: plugins/install.py only ever
# computes first-party or third-party, so no plugin can ever HAVE that tier. A
# choice with no reachable meaning.
#
# THE FIX IS SUPPRESSION, NOT REPAIR. There are no third-party plugins. Making
# the flag work would be building a control for something that does not exist,
# and shipping a control with no meaning is how a user finds a way to break an
# install that offers them nothing in return. The tier machinery stays — it is
# load-bearing for trust checks — but the knob is hidden until a non-first-party
# plugin is actually a thing. Un-hide it then, and fix the intersect-before-write
# ordering at the same time.
#
# Still reachable for tests and for anyone who reads the source; `--help` no
# longer offers it to someone who would only be hurt by it.
@click.command("setup")
@click.option(
    "--allow-tier",
    "allow_tiers",
    multiple=True,
    type=click.Choice(["first-party", "community-verified", "third-party"]),
    default=("first-party",),
    hidden=True,
    help="INTERNAL / NOT SUPPORTED. Which plugin trust tiers are allowed on "
         "this host. Repeatable. Hidden because no non-first-party plugin "
         "exists yet — see the note above the command.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing policy.yaml.",
)
@click.option(
    "--skip-plugins",
    is_flag=True,
    help="Skip auto-install of bundled first-party plugins.",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    help=(
        "Walk through each bundled plugin and ask whether to install it. "
        "Default: install all. Always installs `agent-claude` (the required "
        "agent) regardless of prompt."
    ),
)
def setup(
    allow_tiers: tuple[str, ...], force: bool, skip_plugins: bool,
    interactive: bool,
) -> None:
    """Create state dir, write default USER policy, install bundled plugins.

    Writes the per-user policy (`~/.botainer/policy.yaml`), NOT the root-owned
    site policy (`/etc/botainer/policy.yaml`) — that one is admin-only (see
    docs/SITE-ADMIN.md).

    Runs `doctor` preflight first to catch missing prerequisites (a container
    runtime, disk space, registry reach, host agent CLI). Errors abort;
    warnings allow setup to continue.
    """
    # ── YOUR ARGUMENTS ARE CHECKED BEFORE YOUR MACHINE IS ─────────────────
    #
    # REFUSE BEFORE WRITING, not after. The bricking bug (audit sweep C) was an
    # ORDERING bug as much as a value bug: setup wrote allowed_tiers into
    # policy.yaml and only then discovered that intersecting with the site
    # default produced the empty set. The bad value was already persisted, so
    # every later plain `botainer setup` crashed the same way and only --force
    # escaped. Validate first and nothing is written, so nothing is stuck.
    #
    # AND BEFORE THE PREFLIGHT, which is the same ordering mistake one level
    # out. This check used to sit AFTER `doctor` ran, so on any host where
    # doctor has something to say — no container runtime, no disk, no registry
    # reach; i.e. every machine that has not been set up yet, which is the only
    # kind of machine that runs `setup` — an invalid flag was reported as
    #     Setup aborted: one or more preflight checks need your attention.
    # sending the user to fix their Docker install for a typo in their own
    # command line. Exactly the "blame the wrong thing" shape the message below
    # was written to avoid, reintroduced by the order the two blocks run in.
    #
    # An argument is well-formed or not regardless of what is installed, so it
    # is checkable first, so it is checked first. (It also made
    # test_the_refusal_explains_the_consequence_and_the_way_out fail on any dev
    # box without a Docker daemon — a HARD-gated test, so this had been
    # blocking every commit on the branch.)
    #
    # --allow-tier is hidden (see the note above the command), but hidden is not
    # removed: anyone reading the source, or an old script, can still pass it.
    # A hidden footgun that still fires is not suppressed.
    if "first-party" not in allow_tiers:
        click.secho(
            f"refused: --allow-tier {', '.join(allow_tiers)} would leave no "
            f"installable plugins.\n"
            f"  Every bundled plugin is first-party, so excluding that tier "
            f"means nothing can be installed\n"
            f"  and botainer cannot start a session. Nothing has been written; "
            f"your setup is unchanged.\n"
            f"  Re-run `botainer setup` with no --allow-tier, or include "
            f"first-party in the list.",
            fg="red", err=True,
        )
        sys.exit(2)

    # Phase 1 preflight: run doctor in setup mode and abort on errors.
    click.secho("Running preflight checks...", fg="cyan")
    findings = doctor_mod.collect_findings(for_setup=True)
    doctor_mod.render_findings(findings)
    if any(f.is_actionable() for f in findings):
        click.secho(
            "\nSetup aborted: one or more preflight checks need your attention.",
            fg="red",
            err=True,
        )
        sys.exit(2)
    click.echo("")

    paths = state_dir.ensure_user_state_dir()
    click.echo(f"State dir: {paths.root}")
    written = state_dir.write_default_policy(
        paths.root, allow_tiers=list(allow_tiers), force=force
    )
    if written:
        click.echo(f"Default policy written: {paths.root / 'policy.yaml'}")
        click.echo(f"  allowed plugin tiers: {', '.join(allow_tiers)}")
    else:
        click.echo("Policy.yaml exists; left untouched (use --force to overwrite).")

    # Upgrade footgun: setup's merge only ADDS missing top-level fields — it
    # never updates a value inside an existing block. So an old policy.yaml whose
    # network ceiling predates the `internet` default is carried forward, and the
    # user's next `botainer start` refuses. Warn loudly HERE (the upgrade moment),
    # since it's exactly when the stale file is preserved. Non-destructive — we
    # don't auto-raise a security ceiling.
    try:
        from botainer.core import policy as _policy_mod
        _stale = _policy_mod.stale_restrictive_user_ceiling()
    except Exception:
        _stale = None
    if _stale:
        click.secho(f"⚠ {_stale}", fg="yellow")

    # Classification of bundled plugins for the setup summary output:
    # default-enabled means a fresh `botainer init` includes them; opt-in
    # means they're available but commented out.
    #
    # Note: `agent-claude-shared` / `agent-codex-shared` are NOT in
    # DEFAULT_ENABLED — `botainer init` picks between -shared and
    # -isolated based on `policy.default_auth_mode`, whose default is now
    # `isolated` (#210, 2026-09-02) — one credential per project. User can
    # `botainer policy set default_auth_mode shared` to restore the old
    # host-wide behaviour. The shared variants ARE marked [opt-in] so the
    # setup output doesn't show `[?]`. Same story for the wolfram and
    # other host_helper plugins.
    DEFAULT_ENABLED = {"agent-claude", "git"}
    OPT_IN = {
        "nudge",
        "web-ports",
        "agent-claude-proxy",
        "agent-claude-shared",
        "agent-codex",
        "agent-codex-shared",
        "hpc-launcher",
        "hpc-modules",
        "wolfram-sidecar",
        "browser",
    }

    # Short user-facing descriptions for the --interactive prompt.
    PLUGIN_DESCRIPTIONS = {
        "agent-claude":         "Anthropic Claude Code (per-project credentials; default)",
        "agent-claude-shared":  "Anthropic Claude Code (host-wide shared credentials; one login covers every project)",
        "agent-claude-proxy":   "Anthropic Claude Code (host-side credential proxy; ⚠ EXPERIMENTAL)",
        "agent-codex":          "OpenAI Codex CLI (per-project credentials)",
        "agent-codex-shared":   "OpenAI Codex CLI (host-wide shared credentials)",
        "git":                  "Protected git mode + filtered git config (disposable .git/config copy)",
        "nudge":                "Inject text into running agent via host-side screen (opt-in)",
        "web-ports":            "Forward Jupyter/Streamlit/etc. to host (opt-in)",
        "hpc-launcher":         "Apptainer + Slurm submission (HPC only)",
        "hpc-modules":          "Bind login-node Lmod modules into container (HPC only)",
        "wolfram-sidecar":      "Host-side wolframscript proxy via unix socket (Mac; Linux opt-in)",
        "agent-claude-broker":  "Anthropic Claude Code (host-side broker; the real token never enters the container)",
        "agent-codex-broker":   "OpenAI Codex CLI (host-side broker; ⚠ implemented but never successfully run)",
        "browser":              "Headless browser for the agent, plus an optional watchable viewer (needs an image build)",
    }
    # tests/unit/test_setup_classifies_every_bundled_plugin.py fails if a
    # bundled plugin is missing from either the OPT_IN/DEFAULT_ENABLED sets or
    # this table. Both are hand-maintained lists, and `browser` shipped absent
    # from both — so `botainer setup`, the FIRST command a new user runs,
    # printed `browser  [?]` next to thirteen classified plugins. The list
    # stays a list; the gap cannot reach a user.

    if not skip_plugins:
        builtin_sources = builtin.discover_builtin_plugins()
        if not builtin_sources:
            click.echo("")
            click.echo("(no bundled plugins discovered — running from a non-standard install)")
        else:
            # Decide which subset to install.
            install_set: set[str] = set()
            if not interactive:
                install_set = {src.name for src in builtin_sources}
            else:
                click.echo("")
                click.secho("Interactive plugin selection", fg="cyan", bold=True)
                click.echo(
                    "Each plugin is installed host-wide but only enabled in "
                    "projects that opt in via .botainer/config.yaml. Skipped "
                    "plugins can be installed later with `botainer plugin "
                    "add <path>`."
                )
                click.echo("")
                for src in builtin_sources:
                    if src.name == "agent-claude":
                        # Required; do not prompt.
                        install_set.add(src.name)
                        click.echo(
                            f"  • {src.name:24s} {PLUGIN_DESCRIPTIONS.get(src.name, '')}  "
                            f"{click.style('[required]', fg='green')}"
                        )
                        continue
                    desc = PLUGIN_DESCRIPTIONS.get(src.name, "")
                    # Task #171: default NO for agent-claude-proxy (known-DOA
                    # at v0.1; #84) and for web-ports + hpc-* (env-specific).
                    # Defaulting YES on the proxy installed it for every
                    # new user and surfaced as immediate session-failure.
                    _DEFAULT_NO = {
                        "web-ports", "hpc-launcher", "hpc-modules",
                        "agent-claude-proxy",
                    }
                    default_yes = src.name not in _DEFAULT_NO
                    answer = click.confirm(
                        f"  Install {src.name}? ({desc})",
                        default=default_yes,
                    )
                    if answer:
                        install_set.add(src.name)
                click.echo("")

            # Resolve the effective auth-family default so labels
            # reflect what `botainer init` would ACTUALLY enable.
            # Without this, agent-claude shows [default] even when
            # policy.default_auth_mode=shared (i.e., -shared would
            # actually be enabled). Misleading.
            # Tasks #69 / #167 / #281, updated by #210: read the SitePolicy
            # CLASS DEFAULT rather than naming a mode here. Two copies of the
            # answer is how the 2026-09-01 directive came to be reported as
            # delivered while the template still said otherwise. Use the
            # SitePolicy class default as the
            # single source of truth.
            try:
                from botainer.core import policy as _policy_module
                _sitepolicy_default = _policy_module.SitePolicy().default_auth_mode
                _eff_auth_mode = (
                    _policy_module.load_user_policy().default_auth_mode
                    or _sitepolicy_default
                )
            except Exception:
                from botainer.core import policy as _policy_module
                _eff_auth_mode = _policy_module.SitePolicy().default_auth_mode

            def _effective_role(name: str) -> str:
                """Return 'default' or 'opt-in' or '?' for the setup label,
                consulting policy.default_auth_mode for agent variants."""
                # Non-agent plugins use the static sets.
                if not name.startswith("agent-"):
                    if name in DEFAULT_ENABLED:
                        return "default"
                    if name in OPT_IN:
                        return "opt-in"
                    return "?"
                # Agent plugins: pick the variant matching effective mode.
                # Names look like: agent-claude, agent-claude-shared,
                # agent-claude-proxy, agent-codex, agent-codex-shared.
                family_isolated = name in {"agent-claude", "agent-codex"}
                family_shared = name.endswith("-shared")
                family_proxy = name.endswith("-proxy")
                if family_isolated and _eff_auth_mode == "isolated":
                    return "default"
                if family_shared and _eff_auth_mode == "shared":
                    return "default"
                if family_proxy and _eff_auth_mode == "proxy":
                    return "default"
                return "opt-in"

            click.echo("Installing bundled plugins:")
            for src in builtin_sources:
                if src.name not in install_set:
                    click.echo(
                        f"  - {src.name:24s} (skipped per --interactive)"
                    )
                    continue
                entry = builtin.install_bundled(src)
                role = _effective_role(entry.name)
                role_color = (
                    "green" if role == "default"
                    else "cyan" if role == "opt-in"
                    else "yellow"
                )
                click.echo(
                    f"  ✓ {entry.name:24s} v{entry.version:8s} "
                    f"[{click.style(role, fg=role_color)}]  "
                    f"tree-sha {entry.tree_sha[:19]}..."
                )
            click.echo("")
            click.echo(
                "  default = enabled out-of-the-box after `botainer init`.\n"
                "  opt-in  = available; uncomment in .botainer/config.yaml\n"
                "            to enable per project."
            )
            click.echo("")
            click.echo(
                "  Run `botainer plugin info <name>` for details about any "
                "plugin."
            )

    click.echo("")
    # Show the effective settings so the user knows what they got
    # without having to `botainer policy show` separately.
    try:
        from botainer import auth_modes as _auth_modes
        from botainer.core import policy as _policy_module
        eff = _policy_module.load_user_policy()
        click.secho("Current settings (in policy.yaml):", bold=True)
        # Name the CLASS DEFAULT, never a literal. This line said
        # "(empty → shared)" and was still saying it after the default became
        # isolated (#210) — a display string is as capable of being a stale
        # second copy as a code path is.
        _fallback = _policy_module.SitePolicy().default_auth_mode
        # The hint lists the modes that can start, so a user whose CURRENT
        # value is an experimental one would see their own setting missing
        # from the line that claims to say how to set it, with nothing saying
        # why. Say why.
        _shown = eff.default_auth_mode or f"(empty → {_fallback})"
        if eff.default_auth_mode in _auth_modes.EXPERIMENTAL_AUTH_MODES:
            _shown += " [experimental — not in the list below; sessions refuse]"
        click.echo(
            f"  default_auth_mode:  {_shown}"
            f"     # set via `botainer policy set default_auth_mode "
            f"<{_auth_modes.mode_list_hint()}>`"
        )
        click.echo(
            f"  network.default:    {eff.network.default_mode}"
            f"           # set via `botainer policy set network.default_mode <none|internet|...>`"
        )
        click.echo(
            f"  plugins.allowed:    {eff.plugins.allowed_tiers}"
        )
        click.echo(
            "  Project defaults written by `botainer init`:"
        )
        click.echo(
            "    network.mode = internet   (frictionless; pip/npm/git clone work)"
        )
        # Task #72: ssh-forward plugin was never implemented in v0.1.0;
        # this line lied to users. Removed.
    except Exception as exc:
        click.secho(f"(could not read policy.yaml: {exc})", fg="yellow")
    click.echo("")
    # Codex 45 Rule 5: "what changed / next command" — be explicit
    # about the per-host bootstrap order.
    # Task #283: detect which runtime is actually available and tailor
    # the next-steps. Previously hardcoded `botainer image build` which
    # is docker-only; HPC users were left with `docker: command not found`.
    import shutil as _shutil
    has_docker = _shutil.which("docker") is not None
    has_apptainer = _shutil.which("apptainer") is not None
    # F3: an HPC LOGIN node has sbatch but NOT apptainer (apptainer runs on
    # compute nodes). Detect it so we don't tell the user to "install apptainer"
    # (impossible on a login node) — guide them to a compute node instead.
    has_sbatch = _shutil.which("sbatch") is not None

    click.secho("Next:", bold=True)
    if has_docker:
        click.echo("  1. botainer image build agent-claude   # build the agent image (~8-12 min first time)")
    elif has_apptainer:
        click.echo("  1. botainer image build agent-claude --runtime apptainer   # build the .sif on this node")
    elif has_sbatch:
        click.echo("  1. botainer hpc setup                   # configure your cluster, then build the .sif")
        click.echo("     on a COMPUTE node (apptainer isn't on login nodes):")
        click.echo("       salloc -t 30 -c 4 ; botainer image build agent-claude --runtime apptainer")
    else:
        click.echo("  1. install docker (laptop); on HPC, get to a node with apptainer (a compute node)")
    click.echo("  2. cd <project> && botainer init        # per-project setup (do this before auth)")
    click.echo("  3. botainer auth login --agent claude   # OAuth (after init, so isolated mode works)")
    if has_docker and not has_apptainer:
        click.echo("  4. botainer start                       # launch (docker)")
    elif has_apptainer or has_sbatch:
        click.echo("  4. botainer hpc submit                  # launch via sbatch")
    else:
        click.echo("  4. botainer start                       # launch (docker)")
        click.echo("     or: botainer hpc submit              # launch via sbatch (HPC)")
    # First-run install guide: shown unless suppressed by a marker file.
    # Don't pester the user on subsequent `setup --force` runs.
    install_guide_marker = paths.root / ".install-guide-shown"
    if not install_guide_marker.exists():
        click.echo("")
        click.secho(
            "─── First-run tip ───────────────────────────────────────────",
            fg="cyan",
        )
        click.echo(
            "For package-install guidance (pip vs uv vs conda, when to use\n"
            "which, what to put in `.botainer/config.yaml`), run:"
        )
        click.echo("")
        click.secho("    botainer help install", fg="green")
        click.echo("")
        click.echo(
            "For the nudge feature (inject text into a running agent's\n"
            "prompt from another shell):"
        )
        click.echo("")
        click.secho("    botainer help nudge", fg="green")
        click.secho(
            "─────────────────────────────────────────────────────────────",
            fg="cyan",
        )
        try:
            install_guide_marker.touch()
        except OSError:
            pass
