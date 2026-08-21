"""Tier A — entrypoint_wrap composition + manifest schema + adapter rendering.

Covers:
- EntrypointWrapDecl validation (empty cmd refused; NUL/newline refused)
- ContributesDecl accepts entrypoint_wrap
- SessionSpec.entrypoint_wraps is a tuple of tuples (immutable)
- Adapter (docker, apptainer) prepends wraps onto the entrypoint
- Layer ordering puts outer wraps before inner wraps (§A19 + MEDIUM 13)
"""

from __future__ import annotations

import pytest

from botainer.core.spec import (
    AgentRendering,
    Bind,
    BindMode,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    Provenance,
    SessionSpec,
)
from botainer.plugins.manifest import (
    ContributesDecl,
    EntrypointWrapDecl,
    PluginManifest,
)

# ────────── EntrypointWrapDecl ──────────


def test_entrypoint_wrap_decl_accepts_valid_command() -> None:
    decl = EntrypointWrapDecl(command=["/usr/bin/tmux", "-S", "/control/tmux.sock"])
    assert decl.command == ["/usr/bin/tmux", "-S", "/control/tmux.sock"]


def test_entrypoint_wrap_decl_refuses_empty_command() -> None:
    with pytest.raises(Exception, match="must not be empty"):
        EntrypointWrapDecl(command=[])


def test_entrypoint_wrap_decl_refuses_nul() -> None:
    with pytest.raises(Exception, match="NUL"):
        EntrypointWrapDecl(command=["/usr/bin/tmux", "ar\x00g"])


def test_entrypoint_wrap_decl_refuses_newline() -> None:
    with pytest.raises(Exception, match="newline"):
        EntrypointWrapDecl(command=["/usr/bin/tmux", "ar\ng"])


def test_contributes_decl_accepts_entrypoint_wrap() -> None:
    c = ContributesDecl(
        entrypoint_wrap=EntrypointWrapDecl(command=["/usr/bin/tmux", "--"]),
    )
    assert c.entrypoint_wrap is not None
    assert c.entrypoint_wrap.command == ["/usr/bin/tmux", "--"]


def test_contributes_decl_default_no_entrypoint_wrap() -> None:
    c = ContributesDecl()
    assert c.entrypoint_wrap is None


def test_entrypoint_wrap_decl_layer_defaults_to_default() -> None:
    """Impl review MEDIUM 13: layer is optional in the manifest and defaults
    to 'default' so existing plugins keep working."""
    d = EntrypointWrapDecl(command=["/bin/x"])
    assert d.layer == "default"


def test_entrypoint_wrap_decl_layer_accepts_valid_values() -> None:
    for layer in ("outer", "default", "inner"):
        d = EntrypointWrapDecl(command=["/bin/x"], layer=layer)
        assert d.layer == layer


def test_entrypoint_wrap_decl_layer_refuses_invalid() -> None:
    with pytest.raises(Exception, match="layer must be one of"):
        EntrypointWrapDecl(command=["/bin/x"], layer="middle")


def test_manifest_with_entrypoint_wrap_round_trip() -> None:
    """Pydantic round-trips entrypoint_wrap.command verbatim; the value
    is stored as-is (no template substitution at v0.1 — see #293).
    Test data mirrors the agent-claude wrap shape, the only style
    of wrap a bundled plugin actually ships."""
    m = PluginManifest(
        name="agent-claude",
        version="0.1.0",
        contributes=ContributesDecl(
            entrypoint_wrap=EntrypointWrapDecl(
                command=["/usr/local/bin/agent-claude-entrypoint"]
            )
        ),
    )
    assert m.contributes.entrypoint_wrap is not None
    assert m.contributes.entrypoint_wrap.command[0] == "/usr/local/bin/agent-claude-entrypoint"


# ────────── SessionSpec.entrypoint_wraps ──────────


def test_session_spec_default_no_wraps() -> None:
    spec = SessionSpec(
        session_id="s1",
        project_uuid="u",
        project_root="/p",
        state_dir="/s",
        runtime="docker",
        image="ubuntu:24.04",
    )
    assert spec.entrypoint_wraps == ()


def test_session_spec_accepts_wraps() -> None:
    wraps = (("/usr/bin/tmux", "-S", "/control/tmux.sock", "--"),)
    spec = SessionSpec(
        session_id="s1",
        project_uuid="u",
        project_root="/p",
        state_dir="/s",
        runtime="docker",
        image="ubuntu:24.04",
        entrypoint_wraps=wraps,
    )
    assert spec.entrypoint_wraps == wraps
    assert isinstance(spec.entrypoint_wraps, tuple)


@pytest.mark.parametrize("wraps", [
    (),                                                       # no wraps (guard skips)
    (("/usr/local/bin/agent-claude-entrypoint",),),          # single-element (every real agent)
    (("/usr/bin/tmux", "-S", "/c.sock", "--"),),             # one multi-element wrap
    (("/usr/local/bin/agent-claude-entrypoint",), ("a", "b")),  # multiple wraps
])
def test_preflight_run_handles_entrypoint_wraps(wraps) -> None:
    """AUDIT (H5): `start --preflight` unpacked each wrap as
    (layer, wrap_argv), but entrypoint_wraps is tuple[tuple[str,...],...]
    (sort_entrypoint_wraps drops the layer). Every real session has an agent
    plugin contributing a single-element wrap, so the unpack raised
    ValueError and the CLAUDE.md-mandated preflight gate crashed before it
    could verify anything. Regression: preflight.run must complete (rc 0)
    on a spec carrying wraps."""
    from botainer.inspect import preflight
    spec = SessionSpec(
        session_id="s1",
        project_uuid="11111111-1111-1111-1111-111111111111",
        project_root="/p",
        state_dir="/s",
        runtime="docker",
        image="ubuntu:24.04",
        entrypoint_wraps=wraps,
    )
    assert preflight.run(spec) == 0


def test_session_spec_wraps_immutable() -> None:
    """SessionSpec is frozen; wraps cannot be mutated post-init."""
    spec = SessionSpec(
        session_id="s1",
        project_uuid="u",
        project_root="/p",
        state_dir="/s",
        runtime="docker",
        image="ubuntu:24.04",
        entrypoint_wraps=(("/usr/bin/tmux",),),
    )
    with pytest.raises(Exception):
        spec.entrypoint_wraps = (("/other",),)  # type: ignore[misc]


# ────────── Adapter rendering: docker ──────────


def _minimal_docker_spec(**kw) -> SessionSpec:
    workspace_bind = Bind(
        source="/host/workspace",
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="workspace",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_WORKSPACE_RW",
    )
    return SessionSpec(
        session_id="docker-session-1",
        project_uuid="u",
        project_root="/proj",
        state_dir="/state",
        runtime="docker",
        image="ubuntu:24.04@sha256:0000000000000000000000000000000000000000000000000000000000000000",
        mount_plan=MountPlan(binds=(workspace_bind,)),
        network=NetworkSpec(mode=NetworkMode.INTERNET),
        **kw,
    )


def test_docker_render_no_wrap_no_entrypoint() -> None:
    from botainer.adapters.docker import DockerAdapter

    spec = _minimal_docker_spec()
    argv = DockerAdapter().render_argv(spec)
    assert "--entrypoint" not in argv
    # ends at workdir + image
    assert argv[-1] == spec.image


def test_docker_render_with_wrap_only() -> None:
    """A wrap on its own (no entrypoint) becomes the docker --entrypoint."""
    from botainer.adapters.docker import DockerAdapter

    wrap = ("/usr/bin/tmux", "-S", "/control/tmux.sock", "new-session", "-A", "-s", "botainer", "--")
    spec = _minimal_docker_spec(entrypoint_wraps=(wrap,))
    argv = DockerAdapter().render_argv(spec)
    assert "--entrypoint" in argv
    ep_idx = argv.index("--entrypoint")
    assert argv[ep_idx + 1] == "/usr/bin/tmux"
    # Subsequent wrap args appear after the image.
    image_idx = argv.index(spec.image)
    rest = argv[image_idx + 1:]
    assert rest == [
        "-S", "/control/tmux.sock", "new-session", "-A", "-s", "botainer", "--",
    ]


def test_docker_render_with_wrap() -> None:
    """The outermost wrap is docker's --entrypoint; the rest of its
    argv follows the image. #293: dead `spec.entrypoint` field is gone;
    the agent's exec line is fully owned by entrypoint_wraps. To put
    the agent binary at the end, include it in the innermost wrap."""
    from botainer.adapters.docker import DockerAdapter

    wrap = (
        "/usr/local/bin/agent-claude-entrypoint", "--", "/usr/bin/claude",
    )
    spec = _minimal_docker_spec(entrypoint_wraps=(wrap,))
    argv = DockerAdapter().render_argv(spec)
    image_idx = argv.index(spec.image)
    rest = argv[image_idx + 1:]
    # Final exec line: <wrap[1:]> after image; wrap[0] is --entrypoint.
    assert rest == ["--", "/usr/bin/claude"]
    ep_idx = argv.index("--entrypoint")
    assert argv[ep_idx + 1] == "/usr/local/bin/agent-claude-entrypoint"


def test_docker_render_multiple_wraps_layered() -> None:
    """Multiple wraps layer in order (outer wrap first). #293: agent
    binary lives in the innermost wrap's argv (no separate entrypoint
    field)."""
    from botainer.adapters.docker import DockerAdapter

    outer = ("/bin/wrap-outer", "--flag")
    inner = ("/bin/wrap-inner", "/bin/agent")
    spec = _minimal_docker_spec(entrypoint_wraps=(outer, inner))
    argv = DockerAdapter().render_argv(spec)
    image_idx = argv.index(spec.image)
    rest = argv[image_idx + 1:]
    # Final exec line: outer-args ... inner argv (incl. agent).
    assert rest == ["--flag", "/bin/wrap-inner", "/bin/agent"]
    ep_idx = argv.index("--entrypoint")
    assert argv[ep_idx + 1] == "/bin/wrap-outer"


# ────────── Adapter rendering: apptainer ──────────


def _minimal_apptainer_spec(**kw) -> SessionSpec:
    workspace_bind = Bind(
        source="/host/workspace",
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="workspace",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_WORKSPACE_RW",
    )
    return SessionSpec(
        session_id="apptainer-session-1",
        project_uuid="u",
        project_root="/proj",
        state_dir="/state",
        runtime="apptainer",
        image="/path/to/image.sif",
        mount_plan=MountPlan(binds=(workspace_bind,)),
        network=NetworkSpec(mode=NetworkMode.INTERNET),
        **kw,
    )


def test_apptainer_render_no_wrap() -> None:
    from botainer.adapters.apptainer import ApptainerAdapter

    spec = _minimal_apptainer_spec()
    argv = ApptainerAdapter().render_argv(spec)
    assert spec.image in argv
    # No entrypoint, no command — just stops after image.
    image_idx = argv.index(spec.image)
    assert argv[image_idx + 1:] == []


def test_apptainer_render_with_wrap() -> None:
    """#293: agent binary lives in the wrap argv; no separate
    entrypoint field. Apptainer has no `--entrypoint` flag — the
    full wrap argv follows the image directly."""
    from botainer.adapters.apptainer import ApptainerAdapter

    wrap = (
        "/usr/local/bin/agent-claude-entrypoint", "--", "/usr/bin/claude",
    )
    spec = _minimal_apptainer_spec(entrypoint_wraps=(wrap,))
    argv = ApptainerAdapter().render_argv(spec)
    image_idx = argv.index(spec.image)
    rest = argv[image_idx + 1:]
    assert rest == ["/usr/local/bin/agent-claude-entrypoint", "--", "/usr/bin/claude"]


# ────────── Layer-based ordering (impl review MEDIUM 13) ──────────


def test_layer_ordering_outer_before_inner() -> None:
    """outer-layer wrap sorts before inner-layer wrap regardless of plugin name.

    Bug fixed: alphabetical-only sort put e.g. agent-claude (inner) before
    a later-named outer wrap, so the inner-layer script would have ended
    up as the docker entrypoint and the outer wrap buried as an argument —
    the outer wrap would not have been the actual session leader.

    Now: layer ordering (outer=0 < default=1 < inner=2) puts outer first
    regardless of name. §A19: no shipped plugin currently uses
    layer=outer (the former user, `nudge`, was migrated to a host-side
    screen wrap), so this test exercises the sort with synthetic
    in-memory plugins to keep the ordering invariant covered.
    """
    from botainer.core.composition import sort_entrypoint_wraps

    # Names chosen so alphabetical sort alone would put the inner-layer
    # wrap first; the layer sort must override.
    wraps_by_plugin = {
        "aname": ("inner", ("/bin/ainner",)),
        "zname": ("outer", ("/bin/zouter",)),
    }
    sorted_wraps = sort_entrypoint_wraps(wraps_by_plugin)
    assert sorted_wraps[0] == ("/bin/zouter",)
    assert sorted_wraps[1] == ("/bin/ainner",)
