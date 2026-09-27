"""AC4 (DN-023 §6): inspection covers 100% of session-affecting state.

The survey (wf_808a72cc-a6b) flagged AC4 as PARTIAL: coverage was shown
by snapshot tests but no test mechanically enumerated SessionSpec fields
and asserted each is surfaced by `botainer inspect`. This is that
enumeration test — it makes "100% coverage stays 100%" a red-test
invariant rather than a hope.

Three guards:
  1. The JSON inspect surface (model_dump) carries every spec field.
  2. The spec field set is PINNED, so adding a field is a conscious act
     that forces a decision about its rendering (the drift catcher).
  3. The human-readable surfaces (tree/access/capability_summary) cover
     every SESSION-AFFECTING field — the capability/mount/runtime state a
     user must see before launch. Pure-bookkeeping fields are listed
     explicitly as intentionally not-in-the-human-tree.
"""

from __future__ import annotations

from botainer.core.spec import (
    Bind,
    BindMode,
    CapabilityGrant,
    EnvSpec,
    HookSpec,
    KernelCapsSpec,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    PortForward,
    Provenance,
    ResourceSpec,
    SessionSpec,
    SidecarSpec,
)
from botainer.inspect import access, capability_summary, json_out, tree

# Pinned as of. Adding a SessionSpec field MUST update this set
# AND decide whether the field needs human-readable rendering (guard 3) —
# that's the whole point: no field silently escapes the inspect surface.
_EXPECTED_SPEC_FIELDS = {
    "session_id",
    "project_uuid",
    "project_root",
    "state_dir",
    "runtime",
    "image",
    "entrypoint_wraps",
    "port_forwards",
    "env_files",
    "module_env_path_prepends",
    "mount_plan",
    "network",
    "resources",
    "kernel_caps",
    "sidecars",
    "hooks",
    "env",
    "capabilities",
    "plugins_enabled",
    "plugin_trust_warnings",
    "profile",
    "agent_permissions",
    "composed_at",
}

# Session-AFFECTING fields: the capability/mount/runtime state a user must
# be able to see before a container runs. Each must appear in at least one
# human-readable surface (tree, access, or capability_summary).
_SESSION_AFFECTING = {
    "runtime",
    "image",
    "network",
    "mount_plan",
    "resources",
    "capabilities",
    "plugins_enabled",
    "sidecars",
    "hooks",
    "env",
    "entrypoint_wraps",
    "port_forwards",
    # #53 / T0-2: the in-cage permission posture is security-relevant state the
    # user MUST see before launch (bypass = no per-action prompts). Disclosed by
    # capability_summary.
    "agent_permissions",
}

# Intentionally NOT required in the human tree (bookkeeping / identity /
# secondary), with the reason each is acceptable to omit:
#   session_id, project_uuid, project_root, state_dir, profile, composed_at
#     — identity/bookkeeping, shown in headers/JSON, not capability state
#   env_files — host-side env-file *paths* (hpc-modules); the resulting env
#     is what matters and `env` covers the user-visible surface
#   module_env_path_prepends — Step B helper data (the PATH-clobber
#   acknowledged risk; internal design note DN-005);
#     the resulting in-container PATH/LD_LIBRARY_PATH/… is what the user
#     ultimately sees, surfaced via env when the agent runs. Bookkeeping.
#   plugin_trust_warnings — surfaced as a warning banner, not tree state
_INTENTIONALLY_NOT_IN_HUMAN_TREE = (
    _EXPECTED_SPEC_FIELDS - _SESSION_AFFECTING
)


def _representative_spec() -> SessionSpec:
    """A spec with every session-affecting field populated, so the human
    renderers actually have something to emit for each."""
    plan = MountPlan(
        binds=(
            Bind(
                source="/host/proj",
                target="/workspace",
                mode=BindMode.RW,
                provenance=Provenance.CORE,
                provenance_detail="workspace",
            ),
        )
    )
    return SessionSpec(
        session_id="abcdef0123456789",
        project_uuid="11111111-1111-4111-8111-111111111111",
        project_root="/host/proj",
        state_dir="/state/dir",
        runtime="docker",
        image="botainer/agent-claude:0.1@sha256:" + "a" * 64,
        entrypoint_wraps=(("/usr/local/bin/agent-claude-entrypoint",),),
        port_forwards=(PortForward(container_port=8888, host_port=8888, label="jupyter"),),
        env_files=("/state/dir/sessions/x/module-env.env",),
        mount_plan=plan,
        network=NetworkSpec(mode=NetworkMode.INTERNET, endpoints=()),
        resources=ResourceSpec(cpu=4, memory_mb=8192),
        kernel_caps=KernelCapsSpec(keep=()),
        # Populated so the (v0.1-refused-at-compose, but renderable)
        # sidecar surface is actually exercised; a directly-constructed
        # spec can carry sidecars even though composition refuses them.
        sidecars=(SidecarSpec(name="wolfram", runtime="host_helper"),),
        hooks=(HookSpec(plugin="agent-claude", when="pre_session", script_path="/x/h.py"),),
        env=EnvSpec(values={"CLAUDE_CONFIG_DIR": "/home/agent/.claude"}),
        capabilities=(CapabilityGrant(name="mounts.workspace"),),
        plugins_enabled=("agent-claude", "git", "web-ports"),
        profile="default",
    )


def test_spec_field_set_is_pinned() -> None:
    """Drift catcher: if SessionSpec gains/loses a field, this fails and
    forces the author to update _EXPECTED_SPEC_FIELDS *and* decide whether
    the new field is session-affecting (guard 3) — i.e. whether `inspect`
    must render it. No field silently escapes the inspect contract."""
    actual = set(SessionSpec.model_fields.keys())
    assert actual == _EXPECTED_SPEC_FIELDS, (
        f"SessionSpec fields changed: added={actual - _EXPECTED_SPEC_FIELDS}, "
        f"removed={_EXPECTED_SPEC_FIELDS - actual}. Update _EXPECTED_SPEC_FIELDS "
        f"and, if the field is session-affecting, add it to _SESSION_AFFECTING "
        f"and ensure a human renderer surfaces it (AC4)."
    )


def test_json_inspect_covers_every_spec_field() -> None:
    """The machine-readable surface must carry every field (it's
    model_dump today; this guards against a future `exclude=` regression
    that would silently drop a field from `inspect --json`)."""
    out = json_out.render(_representative_spec())
    missing = _EXPECTED_SPEC_FIELDS - set(out.keys())
    assert not missing, f"inspect --json omits SessionSpec fields: {sorted(missing)}"


def test_human_surfaces_cover_session_affecting_fields() -> None:
    """Every session-affecting field must be visible in at least one
    human-readable surface (tree, access, or capability_summary) — the
    user has to see capability/mount/runtime state before launch."""
    spec = _representative_spec()
    blob = "\n".join(
        (
            tree.render(spec),
            access.render(spec),
            capability_summary.render_multiline(spec),
        )
    )
    # Field -> a token that proves its data is rendered somewhere.
    proof = {
        "runtime": "docker",
        "image": spec.image.split("@", 1)[0],  # name part
        "network": "internet",
        "mount_plan": "/workspace",
        "resources": "4",  # cpu
        "capabilities": "mounts.workspace",
        "plugins_enabled": "agent-claude",
        "sidecars": "wolfram",  # the sidecar name renders in tree + access
        "hooks": "pre_session",
        "env": "CLAUDE_CONFIG_DIR",
        "entrypoint_wraps": "agent-claude-entrypoint",
        "port_forwards": "8888",
        # representative spec leaves agent_permissions at its default "bypass".
        #
        # The probe was "BYPASS" until 2026-09-21, because the banner
        # upper-cased the posture. It no longer does: the banner echoes the
        # value the USER WROTE, verbatim, so someone who set `prompt` sees
        # `prompt` and someone who set `acceptEdits` sees `acceptEdits` —
        # upper-casing an arbitrary agent mode name would print a value that
        # does not exist. The loudness that ALL-CAPS carried is now carried by
        # the ⚠ marker and the "runs UNATTENDED — nothing will ask" line, which
        # `test_agent_permissions.py` pins separately.
        "agent_permissions": "Permissions: bypass",
    }
    assert set(proof) == _SESSION_AFFECTING, (
        "proof map drifted from _SESSION_AFFECTING; keep them in lockstep"
    )
    missing = [
        field for field, token in proof.items() if token not in blob
    ]
    assert not missing, (
        f"session-affecting fields NOT surfaced in any human inspect view: "
        f"{missing}.\n--- combined human output ---\n{blob}"
    )


def test_plugin_declared_capability_names_who_declared_it() -> None:
    """A manifest `capabilities:` entry is a DECLARATION, not a value.

    Dist audit, seen in a real `botainer inspect` run from an
    installed wheel: every plugin-declared capability rendered as `= None`,
    and two plugins declaring the SAME name (`mounts.extra`, contributed by
    both agent-claude-shared and git) produced two identical rows with no way
    to tell them apart. On the surface the README sells as "the user sees
    everything before anything runs", `None` reads as "unset" and the
    duplicate reads as a bug.
    """
    from botainer.inspect import protection

    spec = _representative_spec().model_copy(update={"capabilities": (
        CapabilityGrant(name="mounts.extra", value=None,
                        provenance=Provenance.PLUGIN, source_plugin="git"),
        CapabilityGrant(name="mounts.extra", value=None,
                        provenance=Provenance.PLUGIN,
                        source_plugin="agent-claude-shared"),
        CapabilityGrant(name="network", value="internet",
                        provenance=Provenance.PROJECT),
    )})
    for rendered in (tree.render(spec), protection.render(spec)):
        assert "declared by git (may contribute)" in rendered
        assert "declared by agent-claude-shared (may contribute)" in rendered
        # a real VALUE still renders as a value, with its provenance
        assert "'internet'" in rendered
        # ...and the misleading form is gone for the declaration rows
        assert "mounts.extra                   = None" not in rendered
