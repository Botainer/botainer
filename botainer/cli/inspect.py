"""`botainer inspect` — show the SessionSpec the launcher would build."""

from __future__ import annotations

import json
from pathlib import Path

import click

from botainer.auth_modes import AUTH_MODES, ONE_SHOT_AXIS_HELP

from botainer.cli._refusal_handler import handle_refusals
from botainer.core import composition
from botainer.inspect import json_out, protection, tree


@click.command("inspect")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of tree view.")
@click.option(
    "--protection",
    "show_protection",
    is_flag=True,
    help="Show per-piece protection mode/visibility/protection.",
)
@click.option(
    "--agent",
    "agent_override",
    default=None,
    help=(
        "Preview the plan for a DIFFERENT agent (e.g. `codex`) without "
        "editing .botainer/config.yaml. Same one-shot, in-memory swap "
        "`botainer start --agent` performs, and it keeps your current auth "
        "mode across the swap."
    ),
)
@click.option(
    "--runtime",
    type=click.Choice(["docker", "apptainer", "auto"]),
    default="auto",
    help=(
        "Inspect the plan for a specific runtime. docker and apptainer render "
        "COMPLETELY different binds and argv, so an inspection that cannot be "
        "told which one you mean is an inspection of something else."
    ),
)
@click.option(
    "--auth-mode",
    type=click.Choice(AUTH_MODES),
    default=None,
    help=(
        "Inspect a different auth mode. This decides WHICH CREDENTIAL is "
        "bound, so it is the option whose absence mattered most here."
    ),
)
@click.option(
    "--auth-profile",
    default=None,
    help="Inspect a different auth profile. " + ONE_SHOT_AXIS_HELP,
)
@handle_refusals
def inspect(as_json: bool, show_protection: bool,
            agent_override: str | None, runtime: str,
            auth_mode: str | None, auth_profile: str | None) -> None:
    """Render the SessionSpec that `botainer start` would build."""
    from botainer.cli import _common
    project_root = _common.find_project_root() or Path.cwd()
    # `start` has had --agent since #112; inspect and dry-run did not, so the
    # two commands whose whole job is "show me what will happen" could not show
    # what `start --agent codex` would do. Reported as codex being "messed up":
    # a project inited for claude has no way to PREVIEW the other agent, and
    # the only alternative is to launch it and find out. The swap is in-memory
    # in composition, so nothing here writes to disk — same contract as start.
    # #204: see the note in dry_run.py — `start` forwards six things to the
    # composer and this forwarded one, hardcoding the runtime and the auth
    # mode that decide the argv and the credential bind respectively.
    spec = composition.compose_session(
        project_root,
        runtime_choice=runtime,
        identity_accept=False,
        agent_override=agent_override,
        auth_mode_override=auth_mode,
        auth_profile_override=auth_profile,
    )
    # AUDIT (MEDIUM): inspect calls compose_session ONLY — it does
    # NOT run pre_session hooks, so the rendered mount plan / env OMITS their
    # contributions (the agent credential bind, the proxy socket, the git
    # guarded-mode overlay, …). DN-028 §3 calls inspect "run before any
    # session to see exactly what will happen", so the omission must be flagged
    # (dry-run already warns; inspect was silent). Emit to STDERR so --json
    # stdout stays clean/parseable.
    # The human (tree/protection) views get the caveat; --json is the machine
    # surface (a tool wanting the full plan runs `dry-run --include-hooks`), and
    # a note there would muddy combined-stream capture.
    # #160 (adversarial-review S2): host_pre_launch contributors (hpc-modules)
    # also add binds/env at start time that the compose-time view omits — name
    # them too, not just pre_session, so the "see exactly what will happen"
    # surface doesn't hide the module software-root binds.
    _hook_plugins = sorted({
        h.plugin for h in spec.hooks
        if h.when in ("pre_session", "host_pre_launch")
    })
    if _hook_plugins and not as_json:
        click.secho(
            "# NOTE: this is the compose-time spec; pre_session / host_pre_launch "
            "hooks have NOT run, so binds/env they contribute at start time are "
            f"NOT shown here (plugins with such hooks: {', '.join(_hook_plugins)}). "
            "Run `botainer dry-run --include-hooks` (or `botainer start`) to see "
            "the complete plan including credential mounts, overlays, and "
            "hpc-modules software-root binds.",
            fg="yellow", err=True,
        )
    # Task #150: was `if as_json: ... elif show_protection: ...` — passing
    # both flags silently dropped --protection because as_json branch won.
    # Now: combine modes orthogonally — --protection swaps the data source;
    # --json swaps the renderer.
    # Audit T11: the human views get the compose-time caveat above, but --json
    # (the machine surface) had no signal that the plan is incomplete. Add a
    # machine-readable flag so tooling doesn't treat a pre-hook plan as final.
    def _with_incompleteness(d: dict) -> dict:
        d = dict(d)
        d["hooks_not_run"] = bool(_hook_plugins)
        d["incomplete"] = bool(_hook_plugins)
        if _hook_plugins:
            d["hook_contributors_not_shown"] = list(_hook_plugins)
        return d

    if show_protection:
        if as_json:
            click.echo(json.dumps(_with_incompleteness(json_out.render(spec, with_protection=True)), indent=2, sort_keys=True))
        else:
            click.echo(protection.render(spec))
    else:
        if as_json:
            click.echo(json.dumps(_with_incompleteness(json_out.render(spec)), indent=2, sort_keys=True))
        else:
            click.echo(tree.render(spec))
