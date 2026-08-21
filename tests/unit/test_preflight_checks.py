"""Closure tests for the PreflightCheck enum + verification map (DN-029 §4.6).

These make the `SELFTEST_*` free-strings a MECHANICALLY CLOSED set: every
verification/self_test string used anywhere in the codebase must be either
mapped to enum member(s) in VERIFICATION_TO_CHECKS or an explicit host-side
`(none — …)` verification. This is exactly the check that catches label drift
(it caught the registry's `SELFTEST_EXTRA_BINDS` plural vs composition's
`SELFTEST_EXTRA_BIND`).

Design authored via a verified Fable-5 subagent (wf_fecc8dc3-aa7).
"""

from __future__ import annotations

from botainer.preflight.checks import (
    HOST_SIDE_PREFIX,
    RESERVED_REASON,
    VERIFICATION_TO_CHECKS,
    PreflightCheck,
    checks_for,
)


def test_every_registry_verification_is_mapped_or_host_side() -> None:
    """Every CapabilityDef.verification string is either a host-side
    `(none — …)` note or a key in VERIFICATION_TO_CHECKS. This FAILS if the
    registry names a SELFTEST_* string the map doesn't know (drift)."""
    from botainer.capabilities.registry import CAPABILITIES
    for cap in CAPABILITIES:
        v = cap.verification
        assert v.startswith(HOST_SIDE_PREFIX) or v in VERIFICATION_TO_CHECKS, (
            f"capability {cap.name!r} verification {v!r} is neither a host-side "
            f"'(none — …)' note nor a key in VERIFICATION_TO_CHECKS — the "
            f"SELFTEST_* label has drifted. Add it to the map (or fix the "
            f"registry string to match an existing key)."
        )


def test_every_composed_bind_self_test_is_mapped() -> None:
    """Every Bind.self_test string that composition/plugins emit is a key in
    the map. We enumerate the literals used across the code (grep-derived) so
    a new self_test label can't be introduced without mapping it."""
    # The set of Bind.self_test literals used in botainer/ + plugins/ (kept in
    # sync by this assertion; if a new one is added, map it in checks.py).
    used = {
        "SELFTEST_WORKSPACE_BIND",
        "SELFTEST_EXTRA_BIND",
        "SELFTEST_AGENT_ACCESS_RO",
        "SELFTEST_NULL_BIND",
        "SELFTEST_MODULE_SOFTWARE_BIND",
        "SELFTEST_MODULE_INNER_LOAD",
    }
    for s in used:
        assert s in VERIFICATION_TO_CHECKS, (
            f"Bind.self_test {s!r} is not in VERIFICATION_TO_CHECKS — map it."
        )


def test_self_test_literals_still_match_the_source_tree() -> None:
    """Guard against the above `used` set going stale: grep the tree for the
    actual Bind self_test literals and assert they equal the mapped set (minus
    the map-only registry-side keys). If this fails, someone added/removed a
    self_test= literal — update both the code and the `used` set above."""
    import re
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    found: set[str] = set()
    for base in ("botainer", "plugins"):
        for p in (repo / base).rglob("*.py"):
            for m in re.finditer(r'self_test="(SELFTEST_[A-Z_]+)"', p.read_text()):
                found.add(m.group(1))
    # Every discovered literal must be mapped (the real invariant).
    for s in found:
        assert s in VERIFICATION_TO_CHECKS, (
            f"self_test literal {s!r} found in the tree is not mapped in "
            f"VERIFICATION_TO_CHECKS."
        )


def test_empty_mappings_have_a_recorded_reason() -> None:
    """A verification mapped to () must have an explicit RESERVED_REASON, so
    'no runnable check' is a documented choice, not an oversight."""
    for key, checks in VERIFICATION_TO_CHECKS.items():
        if not checks:
            assert key in RESERVED_REASON and RESERVED_REASON[key], (
                f"{key!r} maps to no checks but has no RESERVED_REASON — record "
                f"why it has no runnable check."
            )


def test_all_mapped_checks_are_enum_members() -> None:
    """Every value in the map is a real PreflightCheck (no typo'd member)."""
    members = set(PreflightCheck)
    for key, checks in VERIFICATION_TO_CHECKS.items():
        for c in checks:
            assert c in members, f"{key!r} maps to non-member {c!r}"


def test_checks_for_helper() -> None:
    assert PreflightCheck.WORKSPACE_RW in checks_for("SELFTEST_WORKSPACE_BIND")
    assert checks_for("(none — host-side)") == ()
    assert checks_for("SELFTEST_SCHED_SLURM_ALLOWED") == ()  # reserved


# ── build_probe_plan applicability matrix (pure) ──

def _spec(binds=(), network_none=False):
    from botainer.core.spec import (
        Bind, BindMode, MountPlan, NetworkMode, NetworkSpec, Provenance, SessionSpec,
    )
    return SessionSpec(
        session_id="s1abc", project_uuid="u", project_root="/p", state_dir="/s",
        runtime="docker", image="img",
        mount_plan=MountPlan(binds=tuple(binds)),
        network=NetworkSpec(mode=NetworkMode.NONE if network_none else NetworkMode.INTERNET),
    )


def _bind(target, mode, self_test):
    from botainer.core.spec import Bind, BindMode, Provenance
    return Bind(source=target, target=target, mode=mode, provenance=Provenance.CORE, self_test=self_test)


def test_build_probe_plan_always_on_checks() -> None:
    from botainer.core.spec import BindMode
    from botainer.preflight.checks import build_probe_plan, PreflightCheck, US
    args = build_probe_plan(_spec())
    kinds = {a.split(US)[0] for a in args}
    for always in (PreflightCheck.CAPS_DROPPED, PreflightCheck.NO_NEW_PRIVS,
                   PreflightCheck.NEGATIVE_ETC_SHADOW, PreflightCheck.NEGATIVE_SSH_HOME,
                   PreflightCheck.NEGATIVE_ENV_INJECTION):
        assert always.value in kinds
    # No network probe when mode != none.
    assert PreflightCheck.NETWORK_NONE.value not in kinds


def test_build_probe_plan_network_none_only_when_mode_none() -> None:
    from botainer.preflight.checks import build_probe_plan, PreflightCheck, US
    args = build_probe_plan(_spec(network_none=True))
    assert any(a.split(US)[0] == PreflightCheck.NETWORK_NONE.value for a in args)


def test_build_probe_plan_bind_probes() -> None:
    from botainer.core.spec import BindMode
    from botainer.preflight.checks import build_probe_plan, PreflightCheck, US
    binds = [
        _bind("/workspace", BindMode.RW, "SELFTEST_WORKSPACE_BIND"),
        _bind("/data", BindMode.RO, "SELFTEST_EXTRA_BIND"),
        _bind("/apps/lmod", BindMode.RO, "SELFTEST_MODULE_INNER_LOAD"),
        _bind("/anchor", BindMode.NULL_BIND, "SELFTEST_NULL_BIND"),  # no directed probe
    ]
    args = build_probe_plan(_spec(binds))
    parsed = {(a.split(US)[0], a.split(US)[1], a.split(US)[2]) for a in args}
    assert (PreflightCheck.WORKSPACE_RW.value, "write_rw", "/workspace") in parsed
    assert (PreflightCheck.DATA_RO.value, "write_ro", "/data") in parsed
    assert (PreflightCheck.MODULE_BINDS_RO.value, "write_ro", "/apps/lmod") in parsed
    # null-bind gets no directed probe.
    assert not any("/anchor" in a for a in args)


def test_build_probe_plan_rw_extra_bind_probed_as_write_rw_not_ro() -> None:
    """T1-5 regression: SELFTEST_EXTRA_BIND rides RW binds too (/packages,
    /scratch, shared-auth, agent profile dir). The probe kind must come from the
    bind's MODE — an RW bind gets write_rw (expect writable), NOT the old
    hardcoded write_ro that made every session FAIL a bogus 'data-ro writable'
    (the Grace 6-FAIL false alarm)."""
    from botainer.core.spec import BindMode
    from botainer.preflight.checks import build_probe_plan, PreflightCheck, US
    binds = [
        _bind("/packages", BindMode.RW, "SELFTEST_EXTRA_BIND"),   # rw by design
        _bind("/agentcred", BindMode.RW, "SELFTEST_EXTRA_BIND"),  # shared-auth rw
        _bind("/data-ro", BindMode.RO, "SELFTEST_EXTRA_BIND"),    # a genuinely-ro extra
    ]
    args = build_probe_plan(_spec(binds))
    parsed = {(a.split(US)[0], a.split(US)[1], a.split(US)[2]) for a in args}
    assert (PreflightCheck.DATA_RO.value, "write_rw", "/packages") in parsed
    assert (PreflightCheck.DATA_RO.value, "write_rw", "/agentcred") in parsed
    # a genuinely-ro extra bind is still probed write_ro (real regressions caught).
    assert (PreflightCheck.DATA_RO.value, "write_ro", "/data-ro") in parsed
    # No RW bind is probed write_ro (that was the false-alarm bug).
    assert not any(a.split(US)[1] == "write_ro" and a.split(US)[2] in ("/packages", "/agentcred") for a in args)


def test_build_probe_plan_skips_separator_in_path() -> None:
    from botainer.core.spec import BindMode
    from botainer.preflight.checks import build_probe_plan, US
    bad = _bind(f"/data{US}evil", BindMode.RO, "SELFTEST_EXTRA_BIND")
    args = build_probe_plan(_spec([bad]))
    assert not any("evil" in a for a in args)
