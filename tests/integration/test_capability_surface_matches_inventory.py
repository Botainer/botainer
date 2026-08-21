"""The actual capability surface must match what's claimed in
docs/CAPABILITY-SURFACE.md.

This is the enforcement layer for the inventory. When the runtime
exposes a binding/env-var/socket that isn't in the inventory — OR
the inventory lists something the runtime no longer exposes — this
test fails. The umbrella-bind disaster (DN-003)
is what this test class is designed to prevent.

Scope today: apptainer surface (where the disaster happened) and the
specific assertions in the inventory's "Things NOT in this inventory"
section. The docker surface coverage can grow with the same shape.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HPC_HOST_HELPER = REPO / "plugins" / "hpc-launcher" / "host_helper"
INVENTORY = REPO / "docs" / "CAPABILITY-SURFACE.md"


def _load_common():
    spec = importlib.util.spec_from_file_location(
        "hpc_launcher_common_capsurface", HPC_HOST_HELPER / "_common.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hpc_launcher_common_capsurface"] = mod
    spec.loader.exec_module(mod)
    return mod


def _build_plan_with_agent(common, *, agent_name: str, state_root: Path):
    """Build a fully-populated SubmissionPlan so to_apptainer_argv exercises
    all the conditional bind branches."""
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "shared-auth" / f"agent-{agent_name}").mkdir(parents=True)
    return common.SubmissionPlan(
        project_root=state_root.parent / "proj",
        project_uuid="c" * 32,
        state_root=state_root,
        profile="default",
        agent_name=agent_name,
        partition="day",
        account="prj1",
        time_minutes=60,
        cpus=1,
        memory_gb=4,
        gpus=0,
        gpu_type=None,
        apptainer_image="botainer-test.sif",
        submission_mode="submit",
        existing_jobid=None,
    )


# ── REGRESSION GUARDS: things the inventory explicitly says NOT to expose ──


def test_apptainer_does_not_bind_whole_state_root(tmp_path: Path) -> None:
    """CAPABILITY-SURFACE.md §2 explicitly says the umbrella bind is
    forbidden. This test makes that contractually enforced."""
    common = _load_common()
    state = tmp_path / "state"
    plan = _build_plan_with_agent(common, agent_name="claude", state_root=state)
    argv = plan.to_apptainer_argv()
    umbrella = f"--bind={state}:{state}:rw"
    assert umbrella not in argv, (
        f"FORBIDDEN: `{umbrella}` exposes every project's credentials. "
        f"The umbrella bind was the 2026-05-18 CRITICAL bug. See "
        f"DN-003."
    )


def test_apptainer_does_not_bind_docker_sock_or_ssh(tmp_path: Path) -> None:
    """Per CAPABILITY-SURFACE.md §5: docker.sock and ~/.ssh are never
    exposed."""
    common = _load_common()
    plan = _build_plan_with_agent(
        common, agent_name="claude", state_root=tmp_path / "state",
    )
    argv = plan.to_apptainer_argv()
    forbidden_substrings = (
        "docker.sock",
        ".ssh",
        ".aws",
        ".kube",
        "/etc/passwd",
        "/etc/shadow",
    )
    for forbidden in forbidden_substrings:
        for a in argv:
            assert forbidden not in a, (
                f"FORBIDDEN substring {forbidden!r} appeared in apptainer "
                f"argv: {a!r}. Inventory §5 says this is never exposed."
            )


def test_apptainer_does_not_bind_other_projects_shared_auth(tmp_path: Path) -> None:
    """Per CAPABILITY-SURFACE.md §2: only the ACTIVE agent's shared-auth
    is bound. If the project's agent is claude, agent-codex's
    shared-auth must not be bound."""
    common = _load_common()
    state = tmp_path / "state"
    # Pre-create BOTH shared-auth dirs so neither is excluded by
    # accident-of-missingness. (_build_plan_with_agent creates the
    # active agent's dir; we add codex's separately.)
    plan = _build_plan_with_agent(common, agent_name="claude", state_root=state)
    (state / "shared-auth" / "agent-codex").mkdir(parents=True, exist_ok=True)
    argv = plan.to_apptainer_argv()
    for a in argv:
        assert "/shared-auth/agent-codex" not in a, (
            f"agent-codex shared-auth must NOT be bound when agent_name "
            f"is claude. Got: {a!r}"
        )


# ── INVENTORY EXISTS + IS NOT STALE ──


def test_capability_surface_inventory_exists() -> None:
    """The inventory must exist. If you removed it without replacing it
    with another contract, restore it."""
    assert INVENTORY.exists(), (
        f"{INVENTORY} is missing. See "
        f"DN-003 for why this file is "
        f"load-bearing."
    )


def test_capability_surface_lists_required_sections() -> None:
    """The inventory must cover every dimension of the surface. If
    you added a new dimension (e.g., shared memory, FIFOs), the
    inventory needs a section for it."""
    text = INVENTORY.read_text(encoding="utf-8")
    required = (
        "## 1. Docker runtime",
        "## 2. Apptainer runtime",
        "## 3. Sockets / pipes / IPC",
        "## 4. Network",
        "## 5. Things NOT in this inventory",
    )
    for section in required:
        assert section in text, (
            f"CAPABILITY-SURFACE.md is missing section {section!r}. "
            f"Don't delete required sections; add new ones at the end "
            f"if you grow the surface."
        )


def test_capability_surface_mentions_apptainer_state_root_constraint() -> None:
    """The inventory MUST contain the explicit narrowness assertion
    about the apptainer state-root binds. If someone edits this out,
    fail loudly — that text exists specifically to discourage future
    re-introduction of the umbrella."""
    text = INVENTORY.read_text(encoding="utf-8")
    assert (
        "Only this project's subtree" in text
        or "not the whole `<state_root>/state/`" in text
    ), (
        "CAPABILITY-SURFACE.md §2 must explicitly forbid the umbrella "
        "bind. Don't remove that text without also reading "
        "DN-003."
    )
