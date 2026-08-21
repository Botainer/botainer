"""Self-test runner — launch the in-container probe, verify the posture.

`run_selftest` composes a probe spec (the real session spec + the PROBE_SH
entrypoint), launches it via the runtime adapter, captures stdout, parses the
sentinel-framed results, and folds in the host-side mount-plan readback
(`preflight/host.py`). It returns a SelftestReport whose exit_code follows
DN-028 §9:

  0 = every PLANNED check ran and passed (skips allowed)
  3 = runtime error: the probe could not be trusted to have run every check —
      launch failed / timed out / probe frame missing / result set incomplete
      / no /proc/self/mounts. NEVER a substitute for a security-check failure.
  4 = at least one check FAILED.

Exit-code precedence (Fable-5 review MEDIUM-2): a CONFIRMED failure wins over a
co-occurring runtime error. If the probe timed out but we salvaged a real FAIL
from partial output, that is exit 4 (loud "cage is broken"), with the runtime
condition recorded in the report for humans — a confirmed insecurity must never
be downgraded to "infra hiccup, retry". A runtime error with NO observed fail
is exit 3.

Core invariant (Fable-5 review HIGH-1): "exit 0 ⇒ every planned check ran and
passed", not "≥1 check ran". The runner derives the planned check set
(`planned_probe_ids`, keyed on (check,target)) and treats any planned probe with no result as an
exit-3 incompleteness — a probe that silently ran only a subset can't yield a
false all-pass.

The probe run is UNVALIDATED on a given real runtime until the manual
per-platform checklist passes, so the CLI wraps results in an EXPERIMENTAL
banner. run_selftest never touches the launch path and adds no bind/env — the
probe travels as argv (entrypoint_wraps).

Design authored via a verified Fable-5 subagent (wf_fecc8dc3-aa7); hardened
against that agent's own adversarial re-review.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from botainer.preflight.checks import CheckResult, PreflightCheck, planned_probe_ids
from botainer.preflight.parse import (
    ProbeFrameError,
    parse_selftest_output,
    salvage_results,
)
from botainer.preflight.probe_script import render_probe_args

EXIT_OK = 0
EXIT_RUNTIME = 3
EXIT_CHECK_FAILED = 4


@dataclass(frozen=True)
class SelftestReport:
    results: list[CheckResult] = field(default_factory=list)
    runtime_error: str | None = None  # set → probe couldn't be trusted to run

    @property
    def exit_code(self) -> int:
        # A confirmed FAIL dominates a co-occurring runtime error (see module
        # docstring): never downgrade "cage is broken" to "infra hiccup".
        if any(r.result == "fail" for r in self.results):
            return EXIT_CHECK_FAILED
        if self.runtime_error is not None:
            return EXIT_RUNTIME
        return EXIT_OK

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


def _probe_spec(spec):
    """Spec identical to `spec` but with the probe as the entrypoint. No bind
    or capability change — the probe rides argv via entrypoint_wraps, and this
    REPLACES any agent/command wrap so the real agent never execs (verified:
    both adapters compose the exec line solely from entrypoint_wraps)."""
    return spec.model_copy(update={"entrypoint_wraps": (tuple(render_probe_args(spec)),)})


def _stderr_tail(proc, n: int = 400) -> str:
    err = (getattr(proc, "stderr", "") or "")
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    err = err.strip()
    if not err:
        return ""
    return " runtime-stderr-tail: " + err[-n:].replace("\n", " / ")


def _reap_docker(spec, run) -> None:
    """Best-effort force-remove a leaked probe container after a timeout.

    docker only kills its client on TimeoutExpired; the container (holding the
    full session cage) keeps running until the probe exits. `--rm` fires only
    on exit, so a hung probe leaks the container. Uses the same injected `run`
    so unit tests stay hermetic; every failure is swallowed."""
    if getattr(spec, "runtime", None) != "docker":
        return
    name = f"botainer-{spec.session_id[:12]}"
    try:
        run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=15)
    except Exception:
        pass


def _fails_only(checks) -> list[CheckResult]:
    """Keep only confirmed FAILs — passes from an incomplete/aborted run are
    not trustworthy (a check we never reached is not a pass)."""
    return [c for c in checks if c.result == "fail"]


def run_selftest(spec, *, timeout: int = 120, run=subprocess.run) -> SelftestReport:
    """Launch the probe for `spec` and return a SelftestReport.

    `run` is injectable (defaults to subprocess.run) so tests can feed canned
    probe stdout without a real container. Compose-level refusals are raised by
    the CLI's compose step (exit 2 via @handle_refusals) before this is called;
    an adapter-level refusal here maps to a runtime error (exit 3)."""
    from botainer.core.composition import _adapter_for
    from botainer.core.refusal import Refused

    probe = _probe_spec(spec)
    adapter = _adapter_for(spec.runtime)
    try:
        argv = adapter.render_argv(probe, interactive=False)
    except Refused as exc:
        return SelftestReport(runtime_error=f"adapter refused to render probe argv: {exc}")

    try:
        proc = run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _reap_docker(spec, run)
        # Salvage confirmed FAILs from partial stdout (Fable-5 MEDIUM-2): a
        # cage failure that also hangs must not vanish with the timeout. Use the
        # tolerant salvage (partial output has BEGIN but no END sentinel).
        partial = getattr(exc, "stdout", None) or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        salvaged = _fails_only(salvage_results(partial))
        extra = f"; {len(salvaged)} FAIL(s) observed before timeout" if salvaged else ""
        return SelftestReport(
            results=salvaged,
            runtime_error=f"probe timed out after {timeout}s (container reaped){extra}",
        )
    except OSError as exc:
        return SelftestReport(runtime_error=f"probe launch failed: {exc}")

    stdout = getattr(proc, "stdout", "") or ""
    try:
        checks, mounts = parse_selftest_output(stdout)
    except ProbeFrameError as exc:
        rc = getattr(proc, "returncode", None)
        return SelftestReport(
            runtime_error=(
                f"probe produced no valid result frame (exit={rc}): {exc}."
                f"{_stderr_tail(proc)} Common causes: the image has no /bin/sh, "
                f"or the container failed to start."
            )
        )

    # Completeness (HIGH-1 + Codex MEDIUM): every planned PROBE must have
    # produced a result, keyed on (check, target) IDENTITY — not the enum alone.
    # Two targets can share a check enum (e.g. two RO binds → data-ro), so an
    # enum-only set would pass if only one reported. Drop results for probes that
    # were NOT planned (a probe can't pad the pass set with unplanned ids).
    planned = planned_probe_ids(spec)
    present = {(c.check, c.target) for c in checks}
    result_checks = [c for c in checks if (c.check, c.target) in planned]
    missing = planned - present
    if missing:
        return SelftestReport(
            results=_fails_only(result_checks),  # surface any confirmed FAIL
            runtime_error=(
                "probe result set incomplete — planned probe(s) produced no "
                "result (the probe silently did not run them): "
                + ", ".join(sorted(f"{c.value}@{t or '-'}" for c, t in missing))
            ),
        )

    results = list(result_checks)

    # Mount readback (MEDIUM-1): the mount-plan check is load-bearing, so a
    # missing OR empty /proc/self/mounts frame is a runtime error (exit 3) —
    # never a silent skip-to-exit-0, and never mislabeled as a posture FAIL.
    if mounts is None or not mounts.strip():
        return SelftestReport(
            results=_fails_only(results),
            runtime_error="probe emitted no /proc/self/mounts (cannot verify the mount plan)",
        )

    # NOTE: fed the probe's OWN /proc/self/mounts, this is a CONSISTENCY check
    # on the agent's reported view, NOT the host-authoritative readback (which
    # sources docker inspect / host /proc/<pid>/mounts — awaits start.py
    # wiring, Task #115). See botainer/preflight/host.py.
    from botainer.preflight.host import run_host_readback
    hr = run_host_readback(spec, proc_mounts_text=mounts)
    if hr.ok:
        results.append(CheckResult(
            PreflightCheck.MOUNT_PLAN_READBACK, "pass",
            "reported mount table consistent with the composed plan",
        ))
    else:
        for name, reason in hr.failed:
            results.append(CheckResult(
                PreflightCheck.MOUNT_PLAN_READBACK, "fail", f"{name}: {reason}"[:200],
            ))
    return SelftestReport(results=results)
