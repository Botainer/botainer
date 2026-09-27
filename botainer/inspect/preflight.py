"""Preflight check — compose a session and verify capability-surface
invariants WITHOUT launching anything.

WHAT IT READS, stated plainly because the docstring and the `--preflight`
help text both used to claim it "renders the runtime argv" and it does not:
this module reads the COMPOSED SPEC — `spec.mount_plan.binds`, `spec.env`,
`spec.entrypoint_wraps` — and prints the runtime's NAME. No adapter is
called, so nothing here checks how a bind becomes `--bind` or `-v`, nor
`--cleanenv` / `--drop-caps`; `tests/unit/test_adapter_argv.py` owns that.
A reader who believed the old claim would think flag rendering was covered.

Designed to be:
- Hermetic: no network, no container start, no real credentials touched.
- A pre-merge gate: `botainer start --preflight` from any project tells
  you whether the composed session would expose anything that the
  capability-surface inventory forbids.
- Cheap to run on every commit (no docker / no apptainer needed).

This module exists to catch, at commit time, the failure it was written
after: a bind whose SOURCE is a PARENT of a protected path — `/` above
`/etc`, `$HOME` above `~/.ssh` — smuggles the protected child into the
container while the literal source string never matches the denylist.
A denylist that only compares equality or descendants does not see it.
That is why the forbidden-path checks below are split by MATCH SEMANTICS
rather than kept as one list: merging them back makes this gate pass
while missing docker.sock, ~/.ssh, ~/.aws and ~/.kube.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import click

from botainer.core.spec import BindMode, SessionSpec

# Bind sources that must NEVER appear in the runtime argv. Mirrors the
# "Things NOT in this inventory" section of docs/CAPABILITY-SURFACE.md.
#
# AUDIT (C1): these used to be ONE list matched with exact-or-under
# against the whole source path, but the list mixed two different kinds of
# entry — absolute paths AND bare names. Bare names can never match an absolute
# source, so 5 of the 7 entries were DEAD:
#
#   /var/run/docker.sock  -> NOT CAUGHT   (".../docker" is not a path prefix of
#                                          ".../docker.sock" — no '/' boundary)
#   /home/user/.ssh       -> NOT CAUGHT   (".ssh" != "/home/user/.ssh")
#   /home/user/.aws       -> NOT CAUGHT
#   /home/user/.kube      -> NOT CAUGHT
#   /etc/passwd           -> caught
#
# `--preflight` is the command CLAUDE.md calls "the gate" to run before pushing
# any security-surface change, so a silently non-matching entry is exactly the
# theatre this project keeps finding. Split by MATCH SEMANTICS so every entry is
# functional and its intent is unambiguous.

# Matched exact-or-under: the forbidden path itself, or anything beneath it.
_FORBIDDEN_BIND_PATHS: tuple[str, ...] = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/var/run/docker",
    "/etc/passwd",
    "/etc/shadow",
)

# Matched against any PATH COMPONENT, because these are credential directories
# that live under an arbitrary home (/home/u/.ssh, /Users/u/.ssh, /root/.ssh …)
# and can never be written as one absolute prefix.
_FORBIDDEN_BIND_COMPONENTS: tuple[str, ...] = (
    "docker.sock",
    ".ssh",
    ".aws",
    ".kube",
    ".gnupg",
    ".docker",
)

# Bind target prefixes that must NEVER appear (e.g. a plugin trying
# to bind /etc/ or /var/ into the container).
_FORBIDDEN_BIND_TARGETS: tuple[str, ...] = (
    "/etc/",
    "/var/",
    "/proc/sys/",
    "/sys/",
)


# The runtimes whose plan this gate can verify. `mock` is deliberately NOT
# here: it is a THIRD plan, neither docker's nor apptainer's. Image
# resolution takes the docker-shaped branch under mock, every
# apptainer-only bind is absent, and so is the docker-only viewer
# port-forward — so verifying a mock plan would report a clean surface for
# a session nobody runs. MEASURED 2026-09-13: for a minimal claude project
# mock, docker and apptainer compose the same 8 binds and 2 env vars, but
# that equality is a property of THAT project, not of the runtimes — an
# `hpc-launcher` project cannot compose under docker at all
# (`unsupported-runtime-feature`), and composition branches on the runtime
# at six sites.
CHECKABLE_RUNTIMES: tuple[str, ...] = ("docker", "apptainer")


def run_all(
    compose_for,
    *,
    requested_runtime: str,
    targeted_runtime: str | None = None,
) -> int:
    """Verify the invariants for every runtime whose plan composes HERE.

    CLAUDE.md calls `botainer start --preflight` "the gate" to clear before
    pushing a security-surface change, and its own help called it safe to run
    anywhere. Two things broke that, and both were the same mistake — treating
    a check that launches nothing as if it were a launch:

    * `start` refused the mock fallback BEFORE this module was reached, so on a
      host with no docker and no apptainer the gate exited 3 — on exactly the
      hosts it called safe — offering `dry-run` and `inspect`, neither of which
      checks anything, as the remedies. PRECISELY: that hit projects whose
      config says `runtime: auto`, which is what plain `botainer init` writes.
      A project that says `runtime: apptainer` (what `init --runtime apptainer`
      writes) was unaffected, because `_resolve_runtime` consults PATH only for
      `auto` — the refuting review corrected me here, after I had written the
      broader claim into this docstring and a test.
    * one runtime per invocation meant the config, or failing that whatever
      runtime the host happened to have, decided which plan got checked — and
      the verdict read as "the surface". An `auto` project on a laptop had its
      docker plan checked and its apptainer plan not; the two are not
      substitutes, since an `hpc-launcher` project has no docker plan at all.

    So an explicit `--runtime X` checks X and only X (unchanged — a user who
    named a runtime does not silently get a second compose), and with no
    `--runtime` every runtime in `CHECKABLE_RUNTIMES` is composed and checked.

    A runtime that CANNOT be composed here is named in the verdict together
    with its refusal, never dropped. And naming it is not enough, because
    "exit 0 is the gate": a SKIP IS A PASS ONLY WHEN THE PLAN DOES NOT EXIST.
    The refuting review of the first version of this function caught exactly
    that — five invocations went from exit 2 at the old single-runtime gate to
    exit 0 and the green word "clean", including a `.sif` whose sha256 no
    longer matched `installed.lock`, i.e. botainer's own tamper refusal,
    reported as though the host were merely short of a binary. So:

    * `unsupported-runtime-feature` — a plugin declares it does not support
      that runtime, so THERE IS NO SUCH PLAN to check, on any host. Benign;
      stated, and does not hold the verdict back.
    * anything else (a missing image, an invalid one, a provenance mismatch) —
      a plan that this project could otherwise run went UNVERIFIED. Exit 3.
    * and whatever `targeted_runtime` says this project would actually run
      must be among the CHECKED, whatever the skip reason. A verdict that
      covers only the plan this machine cannot run is a false all-clear.

    `targeted_runtime` is `composition.targeted_runtime(...)`; `None` or
    `"mock"` means this host targets nothing, and then the rules above decide.

    `compose_for(runtime)` composes the session for one runtime and may raise
    `Refused`. The caller must NOT hand in a pre-composed spec: `start`
    resolves an absent runtime to `mock`, whose image resolution takes the
    docker-shaped branch, so a cluster project holding a built `.sif` and no
    docker image was refused `config-missing` before the apptainer plan was
    ever composed. This gate composes what it checks, per runtime, itself.

    Exit codes:
      0 — every plan this project could run here was composed and is clean
      1 — at least one forbidden bind / env / target, in any runtime's plan
      3 — a plan went UNVERIFIED (including: none could be composed at all)
    """
    from botainer.core.refusal import Refused, RefusalCategory

    targets = (
        (requested_runtime,)
        if requested_runtime in CHECKABLE_RUNTIMES
        else CHECKABLE_RUNTIMES
    )

    checked: list[str] = []
    skipped: list[tuple[str, Refused]] = []
    worst = 0
    for rt in targets:
        try:
            spec = compose_for(rt)
        except Refused as exc:
            if len(targets) == 1:
                # The user named this runtime, so there is nothing to
                # aggregate and nothing to be partial about: a refusal IS the
                # answer to "check THIS plan". Let it reach @handle_refusals
                # and keep the ordinary refusal contract (exit 2) rather than
                # re-reporting it as a preflight verdict.
                raise
            skipped.append((rt, exc))
            continue
        click.secho(f"\n══ runtime: {rt} ══", bold=True)
        worst = max(worst, run(spec))
        checked.append(rt)

    # A skip is benign ONLY when the plan does not exist to be checked.
    no_such_plan = [
        (rt, exc) for rt, exc in skipped
        if exc.category is RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE
        and rt != targeted_runtime
    ]
    unverified = [p for p in skipped if p not in no_such_plan]

    for rt, exc in no_such_plan:
        click.secho(
            f"\nnot applicable: this project has no {rt} plan — {exc}",
            fg="cyan", err=True,
        )
    for rt, exc in unverified:
        click.secho(
            f"\n⚠ NOT VERIFIED: the {rt} plan could not be composed here, so "
            f"this verdict says NOTHING about it"
            + (" — and it is the plan this project targets"
               if rt == targeted_runtime else "")
            + f" — {exc}",
            fg="yellow", err=True,
        )

    if not checked:
        click.secho(
            "\n✗ PREFLIGHT DID NOT RUN — no runtime's plan could be composed "
            "here, so NOTHING was checked. This is not a pass.",
            fg="red", bold=True, err=True,
        )
        return 3

    tail = "".join([
        f"; not applicable: {', '.join(rt for rt, _ in no_such_plan)}"
        if no_such_plan else "",
        f"; NOT VERIFIED {', '.join(rt for rt, _ in unverified)}"
        if unverified else "",
    ])
    if worst:
        verdict, colour = "FAILED", "red"
    elif unverified:
        verdict, colour = "INCOMPLETE (exit 3, NOT a pass)", "red"
    else:
        verdict, colour = "clean", "green"
    click.secho(
        f"\npreflight {verdict}: checked {', '.join(checked)}{tail}",
        fg=colour, bold=True,
    )
    return worst or (3 if unverified else 0)


def run(spec: SessionSpec) -> int:
    """Print the surface + verify invariants. Returns exit code.

    Exit codes:
      0 — surface is clean
      1 — found at least one forbidden bind / env / target
      2 — internal error during checks (file:line in stderr)
    """
    findings: list[str] = []

    # ── Bind surface ───────────────────────────────────────────────
    click.echo("Binds:")
    for b in spec.mount_plan.binds:
        marker = {
            BindMode.RO: "ro",
            BindMode.RW: "rw",
            BindMode.UNIX_SOCKET: "sock",
            BindMode.FIFO: "fifo",
            BindMode.NULL_BIND: "null",
        }[b.mode]
        click.echo(f"  {b.source:60s} → {b.target:40s} {marker:4s}  {b.provenance.value}")

        # Task #186: use exact-match + proper prefix-with-boundary instead
        # of substring/startswith. 'in b.source' caught any path with the
        # substring anywhere (false +); startswith without trailing '/'
        # caught '/etc-something' as if it were '/etc' (also false +).
        # New: exact equal OR path-prefix-with-/.
        def _path_matches(candidate: str, forbidden: str) -> bool:
            fclean = forbidden.rstrip("/")
            return candidate == fclean or candidate.startswith(fclean + "/")

        def _component_matches(candidate: str, name: str) -> bool:
            """True if `name` is one of the path's components.

            Credential dirs live under an arbitrary home, so they cannot be
            expressed as a single absolute prefix. Component-wise (not
            substring) so `.kube-backup` does NOT match `.kube`.
            """
            return name in PurePosixPath(candidate).parts

        # Forbidden sources (path-aware match)
        for forbidden in (*_FORBIDDEN_BIND_PATHS, *_FORBIDDEN_BIND_COMPONENTS):
            if (_path_matches(b.source, forbidden)
                    or (forbidden in _FORBIDDEN_BIND_COMPONENTS
                        and _component_matches(b.source, forbidden))):
                findings.append(
                    f"FORBIDDEN bind source: {b.source!r} is at or under "
                    f"{forbidden!r}. Per CAPABILITY-SURFACE.md §5 "
                    f"this is never exposed to the agent."
                )
        # Forbidden targets (path-aware match)
        for forbidden in _FORBIDDEN_BIND_TARGETS:
            if _path_matches(b.target, forbidden):
                findings.append(
                    f"FORBIDDEN bind target: {b.target!r} is at or under "
                    f"{forbidden!r}. Per CAPABILITY-SURFACE.md §5 "
                    f"the agent never sees these host paths."
                )

    # Umbrella-bind guard: any bind whose source AND target are the
    # entire state root (no subpath) is the disaster shape.
    # Detect by looking for binds where source == target AND source
    # contains "/.botainer" or matches a likely MY_BOTAINER pattern.
    # Heuristic; the exact pattern is "<root>:<root>:rw with root being
    # MY_BOTAINER". We can't know MY_BOTAINER here, but we CAN detect
    # the shape: source == target, mode rw, source has no project-uuid
    # segment, and source ends with `.botainer`, `.botainer-v0_1`, or
    # the older `.botainer-v1` form some early installs used.
    for b in spec.mount_plan.binds:
        if (
            b.source == b.target
            and b.mode == BindMode.RW
            and Path(b.source).name in (".botainer", ".botainer-v0_1", ".botainer-v1")
        ):
            findings.append(
                f"UMBRELLA bind detected: {b.source!r} → {b.target!r} (rw). "
                f"This is the 2026-05-18 CRITICAL bug shape: an over-broad "
                f"bind that smuggles protected paths in without ever matching "
                f"the denylist literally."
            )

    # ── Env surface ────────────────────────────────────────────────
    click.echo("\nEnv vars:")
    for k, v in sorted(spec.env.values.items()):
        # Truncate values that look like credentials so preflight
        # output is safe to paste into reviews.
        display = _redact_if_credential(k, v)
        click.echo(f"  {k}={display}")
        # Refuse known-bad env vars in the spec (these should have been
        # caught earlier by the env denylist; preflight is belt-and-
        # suspenders).
        if k in ("LD_PRELOAD", "PYTHONPATH", "NODE_OPTIONS"):
            findings.append(
                f"FORBIDDEN env var in spec: {k}={v!r}. The denylist "
                f"should have rejected this; preflight caught a leak."
            )

    # ── Entrypoint wraps ───────────────────────────────────────────
    # AUDIT (H5): entrypoint_wraps is tuple[tuple[str,...],...] —
    # sort_entrypoint_wraps drops the layer label (composition.py). The old
    # `for layer, wrap_argv in ...` unpacked each single-element wrap command
    # as (layer, wrap_argv) and raised ValueError on every real session (all
    # bundled agent plugins contribute a one-element wrap), making the
    # CLAUDE.md-mandated `start --preflight` gate inoperable. Iterate the wrap
    # commands directly; index is the outermost-first position label.
    if spec.entrypoint_wraps:
        click.echo("\nEntrypoint wraps (outermost first):")
        for i, wrap_argv in enumerate(spec.entrypoint_wraps):
            click.echo(f"  [{i}] {' '.join(wrap_argv)}")

    # ── Runtime argv (rendered, NOT executed) ──────────────────────
    click.echo(f"\nRuntime: {spec.runtime}")
    click.echo(f"Image:   {spec.image}")

    # Audit T11: preflight runs on the COMPOSE-time spec (before hooks), so the
    # binds/env above OMIT what host_pre_launch/pre_session hooks add at start
    # (credential bind, git overlay, #160 module software-root binds). Flag it
    # so a clean preflight isn't read as "this is the complete plan".
    _hook_plugins = sorted({
        h.plugin for h in spec.hooks
        if h.when in ("pre_session", "host_pre_launch")
    })
    if _hook_plugins:
        click.secho(
            "\nNOTE: compose-time plan — pre_session / host_pre_launch hooks "
            "have NOT run, so binds/env they add at start time are not shown "
            f"(plugins with such hooks: {', '.join(_hook_plugins)}). "
            "Run `botainer dry-run --include-hooks` for the complete plan.",
            fg="yellow", err=True,
        )

    # ── Verdict ────────────────────────────────────────────────────
    if findings:
        click.echo("")
        click.secho(
            f"✗ PREFLIGHT FAILED — {len(findings)} finding(s):",
            fg="red", bold=True, err=True,
        )
        for f in findings:
            click.secho(f"  • {f}", fg="red", err=True)
        return 1

    click.echo("")
    # "this plan", not "preflight": with several runtimes checked in one
    # invocation, a per-plan success line that says "preflight clean" reads as
    # the verdict — and the verdict may be INCOMPLETE because ANOTHER plan
    # could not be composed. `run_all` prints the verdict.
    click.secho(f"✓ this {spec.runtime} plan is clean", fg="green")
    return 0


def _redact_if_credential(key: str, value: str) -> str:
    """Preflight-mode redact: shows AAAA…ZZZZ + length for terminal viewing.

    Tasks #229 + #298: factored to botainer/inspect/_redact.py so json_out
    + config get + other renderers share the source of truth. Preflight
    keeps 'preview' mode because the user has already consented to
    seeing the full env (they typed `botainer start --preflight`).
    """
    from botainer.inspect._redact import redact as _shared_redact
    return _shared_redact(key, value, mode="preview")
