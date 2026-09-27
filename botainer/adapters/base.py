"""Adapter protocol — the contract every runtime adapter satisfies.

Adapters are stateless. They translate a SessionSpec into runtime argv,
exec the runtime, and return a typed handle for attach/stop. They never
read user config or env directly.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Protocol

from botainer.core.spec import SessionSpec


@dataclass(frozen=True)
class RuntimeHandle:
    """Opaque handle returned by an adapter's launch().

    `id` is runtime-specific (container ID for Docker, job ID for Apptainer
    under Slurm). `pid` is the host process the user can wait on; may be None
    for daemon-launched containers.

    `extras` carries runtime-specific extras the adapter knows about
    (Slurm jobid, step_id, compute node hostname). Composition writes
    these into the session record without needing to know which runtime
    contributed them. Architecture review #10: keeps Slurm-env-reading
    out of composition (was a layering leak before).
    """

    runtime: str
    id: str
    pid: int | None = None
    extras: dict[str, str] = field(default_factory=dict)
    # In-memory ownership of the original runtime client. Never serialized into
    # session records: a PID alone cannot recover an already-reaped exit result.
    process: subprocess.Popen | None = field(default=None, repr=False, compare=False)


class Adapter(Protocol):
    name: str

    def validate(self, spec: SessionSpec) -> None:
        """Refuse spec features this runtime cannot enforce."""

    def render_argv(
        self, spec: SessionSpec, *, detach: bool = False, interactive: bool = True
    ) -> list[str]:
        """Return the exact argv that would launch this spec.

        `detach=True` requests a backgrounded launch (Docker -d). Adapters
        that don't support detach should raise Refused. `interactive=False`
        requests a non-interactive, output-capturable launch (used by
        `botainer selftest`); adapters whose exec is already non-interactive
        (apptainer, mock) accept and ignore it.
        """

    def launch(self, spec: SessionSpec, *, detach: bool = False) -> RuntimeHandle:
        """Translate spec to runtime argv; exec; return a handle."""

    def attach(self, handle: RuntimeHandle) -> int:
        """Connect stdio for interactive use. Returns the exit code."""

    def stop(self, handle: RuntimeHandle) -> None:
        """Stop the container cleanly."""

    def inspect(self, handle: RuntimeHandle) -> str:
        """Return JSON readback (or empty-string if not available)."""
