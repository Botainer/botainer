"""`botainer inspect --protection` — per-piece protection mode view."""

from __future__ import annotations

from botainer.core.spec import Provenance, SessionSpec


def render(spec: SessionSpec) -> str:
    lines = ["Protection view:"]
    lines.append("")
    lines.append("Mounts:")
    for b in spec.mount_plan.binds:
        # Task #117: removed the misleading "hash-based-TOFU" tamper
        # label for core binds; the launcher never computes or verifies
        # any such hash. Per CLAUDE.md directive, TOFU is per-project
        # info banner only (no security claim). 'tamper=not-checked' is
        # the accurate label.
        tamper = "not-checked"
        lines.append(
            f"  {b.target:50s} mode={b.mode.value:11s} "
            f"agent_rendering={b.agent_rendering.value:11s} tamper={tamper}"
        )
    lines.append("")
    lines.append("Capabilities (effective):")
    for c in spec.capabilities:
        if c.provenance is Provenance.PLUGIN and c.value is None:
            who = c.source_plugin or "unknown plugin"
            lines.append(f"  {c.name:30s}   declared by {who} (may contribute)")
        else:
            lines.append(
                f"  {c.name:30s} = {c.value!r:40s} provenance={c.provenance.value}")
    lines.append("")
    # Task #115 + #201 + #257: self-tests are DECLARED in manifest +
    # composition (`self_test="SELFTEST_..."`). At v0.1 the opt-in
    # `botainer selftest` command (botainer/preflight/runner.py) DOES
    # execute them in a throwaway probe container, but the default
    # `start` path does NOT run them automatically yet (v0.2 wires the
    # runner into start + aborts on failure — #115, #201). The labels
    # appear in this view for reference.
    lines.append(
        "Self-tests DECLARED (run on demand via `botainer selftest`; "
        "not auto-run on start yet — see #115, #201):"
    )
    seen: set[str] = set()
    for b in spec.mount_plan.binds:
        if b.self_test and b.self_test not in seen:
            seen.add(b.self_test)
            lines.append(f"  - {b.self_test} (declared; run `botainer selftest`)")
    return "\n".join(lines)
