"""Host-side preflight readback.

These checks run on the host *after* the container has been launched but
*before* the agent entrypoint starts. The launcher reads the runtime state
(docker inspect, /proc/<pid>/mounts) and compares against the SessionSpec.

Mismatch → kill the container, refuse the session.

Per codex HIGH 8: these checks are authoritative because they execute outside
any plugin-provided image and rely only on the runtime's own metadata.

Task #115 status note (v0.1): run_host_readback is called by the
integration test suite (tests/integration/test_preflight_host.py)
and by the opt-in self-test runner (botainer/preflight/runner.py,
`botainer selftest`), which feeds it the /proc/self/mounts block the
in-container probe emits. It is NOT yet wired into the production
`botainer start` launch path — the intended call site there is
post-launch + pre-agent-exec, which requires post-launch container
state inspection that v0.1 does eagerly via the in-container preflight
(botainer/inspect/preflight.py). The host-side readback is the
stronger check (sources from runtime state, not from the agent's
view of /proc/self/mounts) and should land in v0.2 wired to start.py.
Until then: usable, tested, exercised by `botainer selftest`, just not
on the start.py critical path.

NOTE: when run_host_readback is fed the probe's own /proc/self/mounts
(as the selftest runner does), the readback source is the *agent's*
view, so it is NOT the stronger host-authoritative check in that mode —
it is a consistency check on the probe's reported mounts. The
authoritative host-sourced mode (docker inspect / host /proc/<pid>/
mounts) still awaits the start.py wiring.
"""

from __future__ import annotations

from dataclasses import dataclass

from botainer.core.refusal import Refused
from botainer.core.spec import SessionSpec
from botainer.mount_plan.readback import (
    ReadbackBind,
    parse_docker_inspect,
    parse_proc_mounts,
    verify_against_plan,
)


@dataclass(frozen=True)
class PreflightResult:
    passed: list[str]
    failed: list[tuple[str, str]]  # (selftest name, reason)

    @property
    def ok(self) -> bool:
        return not self.failed


def run_host_readback(
    spec: SessionSpec,
    *,
    inspect_blob: str | None = None,
    proc_mounts_text: str | None = None,
) -> PreflightResult:
    """Run host-side readback checks against the given inspect/mounts text.

    At least one of `inspect_blob` or `proc_mounts_text` must be non-None;
    callers (live launcher / tests) provide whichever they have.
    """
    if inspect_blob is None and proc_mounts_text is None:
        return PreflightResult(passed=[], failed=[("preflight-source", "no readback source given")])

    readback: list[ReadbackBind] = []
    if inspect_blob:
        try:
            readback.extend(parse_docker_inspect(inspect_blob))
        except Refused as exc:
            return PreflightResult(passed=[], failed=[("docker-inspect", str(exc))])
    if proc_mounts_text:
        readback.extend(parse_proc_mounts(proc_mounts_text))

    try:
        verify_against_plan(spec.mount_plan, readback)
    except Refused as exc:
        return PreflightResult(passed=[], failed=[("mount-readback", str(exc))])

    passed = [b.self_test for b in spec.mount_plan.binds if b.self_test]
    return PreflightResult(passed=passed, failed=[])
