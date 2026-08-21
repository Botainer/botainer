"""Tests for the capability registry."""

from __future__ import annotations

import pytest

from botainer.capabilities.registry import (
    CAPABILITIES,
    all_capability_names,
    get_capability,
)

EXPECTED_CAPABILITIES = {
    # Capabilities the closed namespace currently exposes. Each is
    # declared by at least one bundled plugin AND consumed somewhere
    # in the launcher (validator, adapter, or policy gate).
    "mounts.workspace",
    "mounts.extra",
    "network",
    "env.values",
    "sched.slurm",
    "caps.modules_env_override",
    "caps.modules_software_roots",
    "caps.modules_inner_load",
    "host.credential_access",
    "host.subprocess",
    "net.outbound_proxy",
    "net.port_forward",
}


def test_registry_has_known_capabilities() -> None:
    names = set(all_capability_names())
    assert names == EXPECTED_CAPABILITIES, f"diff: {names ^ EXPECTED_CAPABILITIES}"


def test_each_capability_has_required_fields() -> None:
    for c in CAPABILITIES:
        assert c.name, c
        assert c.description, c.name
        assert isinstance(c.value_schema, dict)
        assert c.enforcement
        assert c.failure_mode
        # `verification` may be empty for capabilities with no in-container self-test
        # (e.g., `lifecycle.hooks`).


def test_sched_slurm_requires_host_helper() -> None:
    sl = get_capability("sched.slurm")
    assert sl is not None
    assert sl.requires_host_helper


def test_get_capability_returns_none_for_unknown() -> None:
    assert get_capability("totally.bogus") is None


def test_validate_capabilities_refuses_unknown_name() -> None:
    """AC7 validator-parity audit: _validate_capabilities is the
    closed-namespace gate (#68/#258). A plugin-contributed grant for an
    unknown / typo'd capability name (e.g. 'mounts.extras') must be refused
    with CAPABILITY_UNKNOWN — pin it so a future refactor can't reopen the
    pre-#258 hole where any typo'd cap name went unnoticed."""
    from botainer.core.composition import _validate_capabilities
    from botainer.core.refusal import RefusalCategory, Refused
    from botainer.core.spec import CapabilityGrant, Provenance

    # A known cap passes through unchanged.
    known = [CapabilityGrant(name="mounts.workspace", provenance=Provenance.PLUGIN)]
    assert _validate_capabilities(known) == known

    # An unknown/typo'd cap is refused.
    with pytest.raises(Refused) as exc:
        _validate_capabilities(
            [CapabilityGrant(name="mounts.extras", provenance=Provenance.PLUGIN)]
        )
    assert exc.value.category == RefusalCategory.CAPABILITY_UNKNOWN
