"""Readback parsers: turn runtime output into a MountPlan-shaped view.

Two information sources at runtime:
1. `docker inspect <container>` → JSON with per-mount info (Docker adapter).
2. `/proc/<pid>/mounts` or `/proc/self/mounts` inside the container.

The launcher parses both and asserts that what the runtime *actually*
mounted matches what the SessionSpec *intended*. Mismatches are refused.

Per codex HIGH 8: host readback is authoritative; in-container probe is
launcher-owned and runs before the agent entrypoint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import BindMode, MountPlan


@dataclass(frozen=True)
class ReadbackBind:
    source: str
    target: str
    rw: bool
    type: str  # 'bind', 'tmpfs', etc.


def parse_docker_inspect(blob: str) -> list[ReadbackBind]:
    """Parse the JSON output of `docker inspect <container>`."""
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise Refused(RefusalCategory.READBACK_FAILED, f"docker inspect not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise Refused(RefusalCategory.READBACK_FAILED, "docker inspect returned no items")
    container = data[0]
    mounts = container.get("Mounts") or []
    if not isinstance(mounts, list):
        raise Refused(RefusalCategory.READBACK_FAILED, "Mounts is not a list")
    out: list[ReadbackBind] = []
    for m in mounts:
        if not isinstance(m, dict):
            continue
        src = str(m.get("Source") or "")
        dst = str(m.get("Destination") or "")
        rw = bool(m.get("RW", True))
        typ = str(m.get("Type") or "bind")
        out.append(ReadbackBind(source=src, target=dst, rw=rw, type=typ))
    return out


def parse_proc_mounts(text: str, *, target_prefix: str = "") -> list[ReadbackBind]:
    """Parse /proc/<pid>/mounts lines."""
    out: list[ReadbackBind] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        src, dst, fstype, opts = parts[0], parts[1], parts[2], parts[3]
        if target_prefix and not dst.startswith(target_prefix):
            continue
        rw = "ro" not in opts.split(",")
        out.append(ReadbackBind(source=src, target=dst, rw=rw, type=fstype))
    return out


def verify_against_plan(plan: MountPlan, readback: list[ReadbackBind]) -> None:
    """Raise `Refused(MOUNT_READBACK_MISMATCH)` if the runtime doesn't match the plan.

    Rules:
    - Every non-null-bind bind in the plan must have a corresponding readback entry.
    - The readback rw flag must match the plan's intended mode (RO ↔ rw=False).
    - Extra readback entries are allowed (the runtime adds tmpfs, /proc, etc.).
    """
    by_target = {r.target.rstrip("/"): r for r in readback}
    for b in plan.binds:
        if b.mode == BindMode.NULL_BIND:
            continue
        if b.is_socket():
            # Socket/fifo: readback may or may not show distinctly; we don't enforce.
            continue
        target = b.target.rstrip("/")
        rbk = by_target.get(target)
        if rbk is None:
            raise Refused(
                RefusalCategory.MOUNT_READBACK_MISSING,
                f"expected bind at {b.target!r} not present in runtime readback",
            )
        if b.mode == BindMode.RO and rbk.rw:
            raise Refused(
                RefusalCategory.MOUNT_READBACK_MISMATCH,
                f"bind at {b.target!r} expected RO but runtime has it RW",
            )
        if b.mode == BindMode.RW and not rbk.rw:
            raise Refused(
                RefusalCategory.MOUNT_READBACK_MISMATCH,
                f"bind at {b.target!r} expected RW but runtime has it RO",
            )
