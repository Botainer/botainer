"""Preflight check — compose a session, render the runtime argv,
and verify capability-surface invariants WITHOUT launching anything.

Designed to be:
- Hermetic: no network, no container start, no real credentials touched.
- A pre-merge gate: `botainer start --preflight` from any project tells
  you whether the runtime argv would expose anything that the
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
    click.secho("✓ preflight clean", fg="green")
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
