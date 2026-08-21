"""Task #150: `botainer inspect --json --protection` must honor both flags.

Old: if/elif precedence — --json silently dropped --protection.
New: with_protection=True flag on json_out.render combines both views.
"""

from __future__ import annotations

from unittest.mock import patch

from botainer.inspect import json_out


def test_render_with_protection_true_adds_protection_key() -> None:
    # Build a minimal mock spec via patched render
    with patch("botainer.inspect.protection.render", return_value="line-A\nline-B"):
        from botainer.core.spec import (
            EnvSpec,
            KernelCapsSpec,
            NetworkMode,
            NetworkSpec,
            ResourceSpec,
            SessionSpec,
        )
        from botainer.mount_plan.plan import MountPlan

        spec = SessionSpec(
            session_id="x",
            project_uuid="x",
            project_root="/x",
            state_dir="/x",
            runtime="mock",
            image="i:t",
            entrypoint_wraps=(),
            env=EnvSpec(values={}),
            env_files=(),
            mount_plan=MountPlan(binds=()),
            network=NetworkSpec(mode=NetworkMode.INTERNET, endpoints=()),
            kernel_caps=KernelCapsSpec(keep=()),
            resources=ResourceSpec(),
            port_forwards=(),
            sidecars=(),
        )
        out = json_out.render(spec, with_protection=True)
        assert "protection" in out
        assert out["protection"]["lines"] == ["line-A", "line-B"]


def test_render_default_no_protection_key() -> None:
    from botainer.core.spec import (
        EnvSpec,
        KernelCapsSpec,
        NetworkMode,
        NetworkSpec,
        ResourceSpec,
        SessionSpec,
    )
    from botainer.mount_plan.plan import MountPlan

    spec = SessionSpec(
        session_id="x",
        project_uuid="x",
        project_root="/x",
        state_dir="/x",
        runtime="mock",
        image="i:t",
        entrypoint_wraps=(),
        env=EnvSpec(values={}),
        env_files=(),
        mount_plan=MountPlan(binds=()),
        network=NetworkSpec(mode=NetworkMode.INTERNET, endpoints=()),
        kernel_caps=KernelCapsSpec(keep=()),
        resources=ResourceSpec(),
        port_forwards=(),
        sidecars=(),
    )
    out = json_out.render(spec)
    assert "protection" not in out
