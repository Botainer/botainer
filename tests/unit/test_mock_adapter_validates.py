"""Task #199: MockAdapter.validate must enforce universal invariants.

Was `def validate(self, spec): return None` → tests using MockAdapter
passed vacuously. Now mirrors the runtime-matches, kernel-caps-empty,
endpoint-needs-endpoints invariants real adapters enforce.
"""

from __future__ import annotations

import pytest

from botainer.adapters.mock import MockAdapter
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import NetworkMode


def _make_mock_spec(**overrides):
    """Build a minimal SessionSpec with runtime='mock'."""
    from botainer.core.spec import (
        EnvSpec,
        KernelCapsSpec,
        NetworkSpec,
        ResourceSpec,
        SessionSpec,
    )
    from botainer.mount_plan.plan import MountPlan

    defaults = dict(
        session_id="test-session-id",
        project_uuid="test-project-uuid",
        project_root="/test/project",
        state_dir="/test/state",
        runtime="mock",
        image="mock:test",
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
    defaults.update(overrides)
    return SessionSpec(**defaults)


def test_mock_adapter_validates_runtime_match() -> None:
    adapter = MockAdapter()
    spec = _make_mock_spec(runtime="docker")
    with pytest.raises(Refused) as exc:
        adapter.validate(spec)
    assert exc.value.category == RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE


def test_mock_adapter_validates_kernel_caps_empty() -> None:
    from botainer.core.spec import KernelCapsSpec

    adapter = MockAdapter()
    spec = _make_mock_spec(kernel_caps=KernelCapsSpec(keep=("CAP_SYS_ADMIN",)))
    with pytest.raises(Refused) as exc:
        adapter.validate(spec)
    assert exc.value.category == RefusalCategory.KERNEL_CAP_KEEP_NOT_ALLOWED


def test_mock_adapter_validates_endpoint_allowlist_requires_endpoints() -> None:
    from botainer.core.spec import NetworkSpec

    adapter = MockAdapter()
    spec = _make_mock_spec(
        network=NetworkSpec(mode=NetworkMode.ENDPOINT_IP_ALLOWLIST, endpoints=())
    )
    with pytest.raises(Refused) as exc:
        adapter.validate(spec)
    assert exc.value.category == RefusalCategory.API_ONLY_REQUIRES_ENDPOINTS


def test_mock_adapter_validates_clean_spec_passes() -> None:
    adapter = MockAdapter()
    spec = _make_mock_spec()
    adapter.validate(spec)  # no raise
