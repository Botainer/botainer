"""Snapshot-style tests for MountPlan → runtime argv rendering."""

from __future__ import annotations

from botainer.core.spec import Bind, BindMode, MountPlan, Provenance
from botainer.mount_plan.render import render_apptainer_argv, render_docker_argv


def _sample_plan() -> MountPlan:
    binds = [
        Bind(
            source="/host/proj", target="/workspace", mode=BindMode.RW, provenance=Provenance.CORE
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
        Bind(
            source="/var/run/wolfram.sock",
            target="/run/wolfram/main.sock",
            mode=BindMode.UNIX_SOCKET,
            provenance=Provenance.PLUGIN,
        ),
    ]
    return MountPlan(binds=tuple(binds))


def test_docker_argv_snapshot() -> None:
    argv = render_docker_argv(_sample_plan())
    assert argv == [
        "--mount",
        "type=bind,source=/host/proj,target=/workspace",
        "--mount",
        "type=bind,source=/host/anchor,target=/workspace/.botainer",
        "--mount",
        "type=bind,source=/host/aas.txt,target=/workspace/.botainer/AGENT_ACCESS.txt,readonly",
        "--mount",
        "type=bind,source=/var/run/wolfram.sock,target=/run/wolfram/main.sock",
    ]


def test_apptainer_argv_snapshot() -> None:
    argv = render_apptainer_argv(_sample_plan())
    assert argv == [
        "--bind",
        "/host/proj:/workspace",
        "--bind",
        "/host/anchor:/workspace/.botainer",
        "--bind",
        "/host/aas.txt:/workspace/.botainer/AGENT_ACCESS.txt:ro",
        "--bind",
        "/var/run/wolfram.sock:/run/wolfram/main.sock",
    ]
