"""Tests for preflight host readback against the SessionSpec."""

from __future__ import annotations

import json

from botainer.core.spec import (
    Bind,
    BindMode,
    MountPlan,
    Provenance,
    SessionSpec,
)
from botainer.preflight.host import run_host_readback


def _minimal_spec() -> SessionSpec:
    plan = MountPlan(
        binds=(
            Bind(
                source="/host/proj",
                target="/workspace",
                mode=BindMode.RW,
                provenance=Provenance.CORE,
                self_test="SELFTEST_WORKSPACE_BIND",
            ),
            Bind(
                source="/host/anchor",
                target="/workspace/.botainer",
                mode=BindMode.NULL_BIND,
                provenance=Provenance.CORE,
                self_test="SELFTEST_NULL_BIND",
            ),
            Bind(
                source="/host/aas.txt",
                target="/workspace/.botainer/AGENT_ACCESS.txt",
                mode=BindMode.RO,
                provenance=Provenance.CORE,
                self_test="SELFTEST_AGENT_ACCESS_RO",
                nested_under="/workspace/.botainer",
            ),
        )
    )
    return SessionSpec(
        session_id="abc123",
        project_uuid="11111111-1111-1111-1111-111111111111",
        project_root="/host/proj",
        state_dir="/s",
        runtime="docker",
        image="x@sha256:" + "a" * 64,
        mount_plan=plan,
    )


def test_host_readback_passes_when_runtime_matches() -> None:
    spec = _minimal_spec()
    blob = json.dumps(
        [
            {
                "Mounts": [
                    {
                        "Source": "/host/proj",
                        "Destination": "/workspace",
                        "RW": True,
                        "Type": "bind",
                    },
                    {
                        "Source": "/host/anchor",
                        "Destination": "/workspace/.botainer",
                        "RW": True,
                        "Type": "bind",
                    },
                    {
                        "Source": "/host/aas.txt",
                        "Destination": "/workspace/.botainer/AGENT_ACCESS.txt",
                        "RW": False,
                        "Type": "bind",
                    },
                ]
            }
        ]
    )
    result = run_host_readback(spec, inspect_blob=blob)
    assert result.ok
    assert "SELFTEST_WORKSPACE_BIND" in result.passed
    assert "SELFTEST_AGENT_ACCESS_RO" in result.passed


def test_host_readback_fails_when_ro_planned_but_runtime_rw() -> None:
    spec = _minimal_spec()
    blob = json.dumps(
        [
            {
                "Mounts": [
                    {
                        "Source": "/host/proj",
                        "Destination": "/workspace",
                        "RW": True,
                        "Type": "bind",
                    },
                    {
                        "Source": "/host/anchor",
                        "Destination": "/workspace/.botainer",
                        "RW": True,
                        "Type": "bind",
                    },
                    {
                        "Source": "/host/aas.txt",
                        "Destination": "/workspace/.botainer/AGENT_ACCESS.txt",
                        "RW": True,
                        "Type": "bind",
                    },  # mismatch
                ]
            }
        ]
    )
    result = run_host_readback(spec, inspect_blob=blob)
    assert not result.ok
    assert result.failed


def test_host_readback_fails_when_bind_missing() -> None:
    spec = _minimal_spec()
    blob = json.dumps(
        [
            {
                "Mounts": [
                    {
                        "Source": "/host/proj",
                        "Destination": "/workspace",
                        "RW": True,
                        "Type": "bind",
                    },
                ]
            }
        ]
    )
    result = run_host_readback(spec, inspect_blob=blob)
    assert not result.ok


def test_host_readback_with_no_source_returns_structured_failure() -> None:
    spec = _minimal_spec()
    result = run_host_readback(spec)
    assert not result.ok
    assert any("no readback source" in f[1] for f in result.failed)
