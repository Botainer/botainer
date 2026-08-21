"""`botainer selftest` — launch a throwaway container and verify the cage.

EXPERIMENTAL (v0.1). This composes the *real* session spec for the current
project, swaps the agent entrypoint for a read-only POSIX-sh probe, launches
it, and checks the security posture the launcher claims to enforce:

  - declared RW binds are writable, RO binds refuse writes
  - capabilities dropped (CapEff == 0), no_new_privs set
  - /etc/shadow not readable, no ~/.ssh key material leaked in
  - no env-injection vars (BASH_ENV / LD_PRELOAD) present
  - network is actually NONE when the policy says so
  - the runtime mount table matches the composed mount plan (host readback)

It changes nothing about the launch path — the probe rides argv, adds no bind
or capability — so it is safe to run anywhere the real session would run. It
is EXPERIMENTAL because the probe run is unvalidated on a given runtime until
that platform's manual checklist in the project's internal test plan passes.

Exit codes (DN-028 §9): 0 = every planned check passed; 3 = self-test
runtime error (launch failed / timed out / probe frame missing / result set
incomplete — the probe couldn't be trusted to have run every check); 4 = at
least one posture check FAILED. A compose/policy refusal happens BEFORE the
probe runs and exits 2 (via @handle_refusals), not 3.
"""

from __future__ import annotations

from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals

_RESULT_STYLE = {
    "pass": ("  ✓ ", "green"),
    "fail": ("  ✗ ", "red"),
    "skip": ("  – ", "yellow"),
}


@click.command("selftest")
@click.option(
    "--runtime",
    type=click.Choice(["auto", "docker", "apptainer"]),
    default="auto",
    show_default=True,
    help="Runtime to compose for (auto = detect, same as `start`).",
)
@click.option(
    "--timeout", type=int, default=120, show_default=True,
    help="Seconds to wait for the probe container before declaring a runtime error.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON instead of the human report.")
@handle_refusals
def selftest(runtime: str, timeout: int, as_json: bool) -> None:
    """EXPERIMENTAL: launch a throwaway probe container and verify the cage.

    Composes the real session for the current project, runs a read-only
    in-container probe, and reports whether the enforced posture matches
    what the launcher promises. Adds nothing to the launch path.
    """
    import json as _json
    import sys

    from botainer.cli import _common
    from botainer.core import composition
    from botainer.preflight.runner import EXIT_RUNTIME, run_selftest

    project_root = _common.find_project_root() or Path.cwd()
    spec = composition.compose_session(
        project_root, runtime_choice=runtime, identity_accept=False
    )
    # Run the real start-time hooks so the probed spec carries the binds/env a
    # real session would have (hpc-modules software-root binds, module env, the
    # credential-proxy socket bind). These have side effects (file writes,
    # sidecars) — acceptable for an explicit opt-in diagnostic — but pre_session
    # can spawn DETACHED, credential-bearing host proxies. We MUST tear them
    # down: a leaked token-bearing daemon for a session that never launches is
    # more than a stray file (Fable-5 review MEDIUM-3). Hence the try/finally.
    spec = composition.run_host_pre_launch_hooks(spec)
    spec = composition.run_pre_session_hooks(spec)

    try:
        report = run_selftest(spec, timeout=timeout)
    finally:
        # Stop any sidecars/proxies the pre_session hooks spawned. Errors are
        # logged inside run_post_session_hooks, not raised.
        composition.run_post_session_hooks(spec)

    if as_json:
        click.echo(_json.dumps({
            "runtime": spec.runtime,
            "image": spec.image,
            "exit_code": report.exit_code,
            "runtime_error": report.runtime_error,
            "results": [
                {"check": r.check.value, "result": r.result, "detail": r.detail}
                for r in report.results
            ],
        }, indent=2))
        sys.exit(report.exit_code)

    click.secho(
        "botainer selftest — EXPERIMENTAL (v0.1): probes the live cage; "
        "unvalidated on a runtime until its per-platform checklist passes.",
        fg="yellow", err=True,
    )
    click.secho(f"─── self-test ({spec.runtime}: {spec.image}) "
                f"─────", fg="cyan")

    if report.runtime_error is not None:
        click.secho(f"  ✗ runtime error: {report.runtime_error}", fg="red")
        # A runtime error can co-occur with a CONFIRMED failure salvaged from
        # partial output (e.g. timeout after a FAIL). Only claim "not a
        # security failure" when nothing failed; otherwise fall through so the
        # confirmed FAIL(s) are printed and the exit code reflects them (4).
        if report.exit_code == EXIT_RUNTIME:
            click.secho(
                "selftest could not run every check (this is NOT a security-"
                "check failure; exit 3 — treat as an infra/setup problem, not "
                "a broken cage).", fg="yellow", err=True,
            )
            sys.exit(report.exit_code)
        click.secho(
            "selftest also OBSERVED a real failure before the runtime error "
            "below — treating as a posture FAILURE (exit 4), not a retryable "
            "infra error.", fg="red", err=True,
        )

    for r in report.results:
        prefix, color = _RESULT_STYLE.get(r.result, ("  ? ", "white"))
        line = f"{prefix}{r.check.value}"
        if r.detail:
            line += f": {r.detail}"
        click.secho(line, fg=color)

    n_fail = sum(1 for r in report.results if r.result == "fail")
    n_pass = sum(1 for r in report.results if r.result == "pass")
    n_skip = sum(1 for r in report.results if r.result == "skip")
    click.echo()
    if report.ok:
        click.secho(f"PASS — {n_pass} checks passed, {n_skip} skipped.", fg="green")
    else:
        click.secho(
            f"FAIL — {n_fail} check(s) FAILED ({n_pass} passed, {n_skip} skipped). "
            f"The enforced cage does not match the composed spec; do NOT trust "
            f"this session's isolation until resolved.",
            fg="red",
        )
    sys.exit(report.exit_code)
