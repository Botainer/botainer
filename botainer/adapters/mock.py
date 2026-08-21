"""Mock adapter for testing.

Records what would have been done; produces deterministic readback for
verification tests. Never invokes a real runtime.

validate() enforces the universal invariants every real adapter does so
that tests using MockAdapter don't pass vacuously (task #199).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from botainer.adapters.base import RuntimeHandle
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import NetworkMode, SessionSpec


@dataclass
class MockAdapter:
    name: str = "mock"
    launched: list[SessionSpec] = field(default_factory=list)
    fake_inspect_output: str = ""

    def validate(self, spec: SessionSpec) -> None:
        """Mirror universal invariants every real adapter enforces.

        Task #199: was `return None` unconditionally → tests using
        MockAdapter passed vacuously, never observing refusal. Root
        cause of Pattern J at scale.
        """
        if spec.runtime != "mock":
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                f"MockAdapter received spec for runtime={spec.runtime!r}",
            )
        if spec.kernel_caps.keep:
            raise Refused(
                RefusalCategory.KERNEL_CAP_KEEP_NOT_ALLOWED,
                f"kernel cap keep-list non-empty at v0.1.0: {spec.kernel_caps.keep}",
            )
        if (
            spec.network.mode == NetworkMode.ENDPOINT_IP_ALLOWLIST
            and not spec.network.endpoints
        ):
            raise Refused(
                RefusalCategory.API_ONLY_REQUIRES_ENDPOINTS,
                "network.mode=endpoint-ip-allowlist requires at least one endpoint",
            )

    def render_argv(
        self, spec: SessionSpec, *, detach: bool = False, interactive: bool = True
    ) -> list[str]:
        argv = ["mock-runtime", "--session", spec.session_id, spec.image]
        if detach:
            argv.append("--detach")
        if not interactive:
            argv.append("--no-interactive")
        return argv

    def launch(self, spec: SessionSpec, *, detach: bool = False) -> RuntimeHandle:
        self.launched.append(spec)
        return RuntimeHandle(runtime="mock", id=f"mock-{spec.session_id[:12]}", pid=None)

    def attach(self, handle: RuntimeHandle) -> int:
        return 0

    def stop(self, handle: RuntimeHandle) -> None:
        return None

    def inspect(self, handle: RuntimeHandle) -> str:
        return self.fake_inspect_output
