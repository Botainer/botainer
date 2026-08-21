"""Tests for MountPlan composition (core base, plugin contributions, user extras)."""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core.config import MountExtra, MountsConfig, ProjectConfig
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import Bind, BindMode, Provenance
from botainer.mount_plan.composition import (
    add_extras,
    add_plugin_contribution,
    core_base,
)


def test_core_base_contains_workspace_null_bind_and_agent_access(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    aas = tmp_path / "scratch" / "AGENT_ACCESS.txt"
    aas.parent.mkdir()
    aas.write_text("hi")
    data = tmp_path / "data"
    data.mkdir()
    plan = core_base(proj, agent_access_source=aas, state_data_root=data)
    targets = {b.target for b in plan.binds}
    assert targets == {
        "/workspace",
        "/workspace/.botainer",
        "/workspace/.botainer/AGENT_ACCESS.txt",
    }
    # /workspace is rw; null-bind is null-bind; AGENT_ACCESS is ro
    modes = {b.target: b.mode for b in plan.binds}
    assert modes["/workspace"] == BindMode.RW
    assert modes["/workspace/.botainer"] == BindMode.NULL_BIND
    assert modes["/workspace/.botainer/AGENT_ACCESS.txt"] == BindMode.RO


def test_add_extras_propagates_provenance(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    aas = tmp_path / "aas.txt"
    aas.write_text("hi")
    data = tmp_path / "data"
    data.mkdir()
    plan = core_base(proj, agent_access_source=aas, state_data_root=data)
    cfg = ProjectConfig(
        mounts=MountsConfig(
            extra=[MountExtra(source="/host/data", target="/data", mode="ro", reason="dataset")],
        )
    )
    plan2 = add_extras(plan, cfg)
    extras = [b for b in plan2.binds if b.target == "/data"]
    assert len(extras) == 1
    assert extras[0].mode == BindMode.RO
    assert extras[0].provenance == Provenance.USER
    assert "dataset" in extras[0].provenance_detail


def test_nested_bind_placeholders_created_for_virtiofs(tmp_path: Path) -> None:
    """Mac/virtiofs needs host-side mountpoints to exist BEFORE docker run.
    For any bind whose target is nested under a null-bind, mirror the
    file/dir at the null-bind's source. See _prepare_nested_bind_placeholders.
    """
    from botainer.core.composition import _prepare_nested_bind_placeholders
    from botainer.core.spec import MountPlan

    anchor = tmp_path / "anchor"
    anchor.mkdir()
    hints_src = tmp_path / "hints.md"
    hints_src.write_text("hi")
    state_src = tmp_path / "state"
    state_src.mkdir()

    plan = MountPlan(
        binds=(
            Bind(
                source=str(anchor),
                target="/workspace/.botainer",
                mode=BindMode.NULL_BIND,
                provenance=Provenance.CORE,
            ),
            Bind(
                source=str(hints_src),
                target="/workspace/.botainer/AGENT_HINTS.md",
                mode=BindMode.RO,
                provenance=Provenance.CORE,
                nested_under="/workspace/.botainer",
            ),
            Bind(
                source=str(state_src),
                target="/workspace/.botainer/agent-claude",
                mode=BindMode.RO,
                provenance=Provenance.PLUGIN,
                nested_under="/workspace/.botainer",
            ),
        )
    )
    _prepare_nested_bind_placeholders(plan)

    # File source → file placeholder
    file_ph = anchor / "AGENT_HINTS.md"
    assert file_ph.is_file(), "placeholder file must exist for file-source bind"
    # Dir source → dir placeholder
    dir_ph = anchor / "agent-claude"
    assert dir_ph.is_dir(), "placeholder dir must exist for dir-source bind"


def test_nested_bind_placeholders_skip_when_no_null_bind(tmp_path: Path) -> None:
    """If no null-bind exists, the function is a no-op (doesn't crash)."""
    from botainer.core.composition import _prepare_nested_bind_placeholders
    from botainer.core.spec import MountPlan

    plan = MountPlan(
        binds=(
            Bind(
                source=str(tmp_path / "x"),
                target="/some/where",
                mode=BindMode.RO,
                provenance=Provenance.USER,
            ),
        )
    )
    # Must not raise.
    _prepare_nested_bind_placeholders(plan)


def test_plugin_contribution_outside_envelope_refused(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    aas = tmp_path / "aas.txt"
    aas.write_text("hi")
    data = tmp_path / "data"
    data.mkdir()
    plan = core_base(proj, agent_access_source=aas, state_data_root=data)
    bad_bind = Bind(
        source="/host/x",
        target="/secret-target",
        mode=BindMode.RO,
        provenance=Provenance.PLUGIN,
    )
    with pytest.raises(Refused) as exc:
        add_plugin_contribution(
            plan,
            "evilplug",
            [bad_bind],
            declared_target_prefixes=["/workspace/.botainer/evilplug/"],
        )
    assert exc.value.category == RefusalCategory.PLUGIN_CONTRIBUTION_OUT_OF_ENVELOPE
