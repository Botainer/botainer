"""Tests for readback parsing (docker inspect + /proc/mounts) and verification."""

from __future__ import annotations

import json

import pytest

from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import (
    Bind,
    BindMode,
    MountPlan,
    Provenance,
)
from botainer.mount_plan.readback import (
    parse_docker_inspect,
    parse_proc_mounts,
    verify_against_plan,
)

_INSPECT_FIXTURE = json.dumps(
    [
        {
            "Mounts": [
                {"Source": "/host/proj", "Destination": "/workspace", "RW": True, "Type": "bind"},
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
            ],
            "State": {"Running": True},
        }
    ]
)


_PROC_MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec 0 0
/host/proj /workspace ext4 rw,relatime 0 0
/host/anchor /workspace/.botainer ext4 rw,relatime 0 0
/host/aas.txt /workspace/.botainer/AGENT_ACCESS.txt ext4 ro,relatime 0 0
"""


def _plan() -> MountPlan:
    return MountPlan(
        binds=(
            Bind(
                source="/host/proj",
                target="/workspace",
                mode=BindMode.RW,
                provenance=Provenance.CORE,
            ),
            Bind(
                source="/host/anchor",
                target="/workspace/.botainer",
                mode=BindMode.NULL_BIND,
                provenance=Provenance.CORE,
            ),
            Bind(
                source="/host/aas.txt",
                target="/workspace/.botainer/AGENT_ACCESS.txt",
                mode=BindMode.RO,
                provenance=Provenance.CORE,
                nested_under="/workspace/.botainer",
            ),
        )
    )


def test_parse_docker_inspect_extracts_mounts() -> None:
    rb = parse_docker_inspect(_INSPECT_FIXTURE)
    assert len(rb) == 3
    assert rb[2].rw is False


def test_parse_proc_mounts_filters_target_prefix() -> None:
    rb = parse_proc_mounts(_PROC_MOUNTS, target_prefix="/workspace")
    targets = {r.target for r in rb}
    assert "/workspace" in targets
    assert "/workspace/.botainer/AGENT_ACCESS.txt" in targets
    assert "/" not in targets


def test_verify_against_plan_passes_on_match() -> None:
    rb = parse_docker_inspect(_INSPECT_FIXTURE)
    verify_against_plan(_plan(), rb)  # should not raise


def test_verify_detects_missing_bind() -> None:
    blob = json.dumps(
        [
            {
                "Mounts": [
                    {"Source": "/x", "Destination": "/workspace", "RW": True, "Type": "bind"},
                ]
            }
        ]
    )
    rb = parse_docker_inspect(blob)
    with pytest.raises(Refused) as exc:
        verify_against_plan(_plan(), rb)
    assert exc.value.category == RefusalCategory.MOUNT_READBACK_MISSING


def test_verify_detects_rw_ro_mismatch() -> None:
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
                    },  # planned RO, runtime RW
                ]
            }
        ]
    )
    rb = parse_docker_inspect(blob)
    with pytest.raises(Refused) as exc:
        verify_against_plan(_plan(), rb)
    assert exc.value.category == RefusalCategory.MOUNT_READBACK_MISMATCH


def test_parse_docker_inspect_not_json() -> None:
    with pytest.raises(Refused) as exc:
        parse_docker_inspect("not json")
    assert exc.value.category == RefusalCategory.READBACK_FAILED


def test_parse_docker_inspect_empty_list() -> None:
    with pytest.raises(Refused) as exc:
        parse_docker_inspect("[]")
    assert exc.value.category == RefusalCategory.READBACK_FAILED
