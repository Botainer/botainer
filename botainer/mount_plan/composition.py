"""Compose a MountPlan from core defaults + plugin contributions + user config.

The MountPlan is built in this order (each step adds binds, never modifies
earlier ones):

1. Core base: /workspace, null-bind on /workspace/.botainer, AGENT_ACCESS.txt
2. Plugin contributions: validated against plugin's declared envelope
3. User config (mounts.extra): each must be in policy allowlist

Final plan is `validate()`d before becoming part of the SessionSpec.
"""

from __future__ import annotations

from pathlib import Path

from botainer.core.config import ProjectConfig
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import AgentRendering, Bind, BindMode, MountPlan, Provenance


def core_base(project_root: Path, *, agent_access_source: Path, state_data_root: Path) -> MountPlan:
    """Build the always-present binds the core contributes."""
    project_root = project_root.resolve()
    state_data_root = state_data_root.resolve()
    binds: list[Bind] = [
        Bind(
            source=str(project_root),
            target="/workspace",
            mode=BindMode.RW,
            provenance=Provenance.CORE,
            provenance_detail="project root → /workspace (always rw at v0.1.0)",
            agent_rendering=AgentRendering.SHOWN,
            self_test="SELFTEST_WORKSPACE_BIND",
        ),
        # Null-bind on /workspace/.botainer — hides anything user dropped in
        # Main/.botainer/ from the agent unless explicitly mounted.
        Bind(
            source=str(state_data_root / "null-bind-anchor"),
            target="/workspace/.botainer",
            mode=BindMode.NULL_BIND,
            provenance=Provenance.CORE,
            provenance_detail="null-bind defense; hides arbitrary .botainer/ contents",
            agent_rendering=AgentRendering.SUMMARIZED,
            self_test="SELFTEST_NULL_BIND",
        ),
        # AGENT_ACCESS.txt — rendered for the agent to read on session start.
        Bind(
            source=str(agent_access_source.resolve()),
            target="/workspace/.botainer/AGENT_ACCESS.txt",
            mode=BindMode.RO,
            provenance=Provenance.CORE,
            provenance_detail="agent-facing summary; ro",
            agent_rendering=AgentRendering.SHOWN,
            self_test="SELFTEST_AGENT_ACCESS_RO",
            nested_under="/workspace/.botainer",
        ),
    ]
    return MountPlan(binds=tuple(binds))


def add_extras(plan: MountPlan, config: ProjectConfig) -> MountPlan:
    """Add user-declared extra mounts from `config.yaml`."""
    new: list[Bind] = []
    for extra in config.mounts.extra:
        mode = BindMode(extra.mode) if extra.mode in {m.value for m in BindMode} else BindMode.RO
        new.append(
            Bind(
                source=extra.source,
                target=extra.target,
                mode=mode,
                provenance=Provenance.USER,
                provenance_detail=extra.reason[:120] or "user-declared in config.yaml",
                agent_rendering=AgentRendering.SHOWN,
                self_test="SELFTEST_EXTRA_BIND",
            )
        )
    return plan.with_many(new)


def add_plugin_contribution(
    plan: MountPlan,
    plugin_name: str,
    binds: list[Bind],
    declared_target_prefixes: list[str],
) -> MountPlan:
    """Merge plugin-contributed binds, enforcing the declared target envelope.

    A bind target matches a declared prefix when:
    - target == prefix, with trailing-slash treated equivalently
    - target.startswith(prefix-with-trailing-slash)
    """
    for b in binds:
        t = b.target.rstrip("/")
        matched = False
        for p in declared_target_prefixes:
            p_stripped = p.rstrip("/")
            if t == p_stripped or t.startswith(p_stripped + "/"):
                matched = True
                break
        if not matched:
            raise Refused(
                RefusalCategory.PLUGIN_CONTRIBUTION_OUT_OF_ENVELOPE,
                f"plugin {plugin_name!r} contributed bind target {b.target!r} "
                f"outside its declared prefixes {declared_target_prefixes}",
            )
    return plan.with_many(binds)
