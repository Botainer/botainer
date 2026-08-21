"""Render a MountPlan into runtime-specific argv fragments.

Argv-only. Never shell-string concatenation. Each adapter calls its own
render function on the MountPlan, never on raw user input.
"""

from __future__ import annotations

from botainer.core.spec import BindMode, MountPlan


def render_docker_argv(plan: MountPlan) -> list[str]:
    """Render binds as `docker --mount type=bind,source=,target=,readonly=`."""
    argv: list[str] = []
    for b in plan.binds:
        if b.mode == BindMode.NULL_BIND:
            # Bind from the anchor dir (empty) — the agent sees only what's
            # explicitly nested on top.
            argv.extend(
                [
                    "--mount",
                    f"type=bind,source={b.source},target={b.target}",
                ]
            )
            continue
        if b.mode in (BindMode.UNIX_SOCKET, BindMode.FIFO):
            # Bind-mount the socket/fifo file directly.
            argv.extend(
                [
                    "--mount",
                    f"type=bind,source={b.source},target={b.target}",
                ]
            )
            continue
        ro = ",readonly" if b.mode == BindMode.RO else ""
        argv.extend(
            [
                "--mount",
                f"type=bind,source={b.source},target={b.target}{ro}",
            ]
        )
    return argv


def render_apptainer_argv(plan: MountPlan) -> list[str]:
    """Render binds as `apptainer exec --bind <src>:<dest>[:ro]` fragments."""
    argv: list[str] = []
    for b in plan.binds:
        if b.mode == BindMode.NULL_BIND:
            argv.extend(["--bind", f"{b.source}:{b.target}"])
            continue
        if b.mode in (BindMode.UNIX_SOCKET, BindMode.FIFO):
            argv.extend(["--bind", f"{b.source}:{b.target}"])
            continue
        suffix = ":ro" if b.mode == BindMode.RO else ""
        argv.extend(["--bind", f"{b.source}:{b.target}{suffix}"])
    return argv


def render_summary(plan: MountPlan) -> str:
    """Plain-text rendering for inspection."""
    lines = []
    for b in plan.binds:
        marker = {
            BindMode.RO: "ro",
            BindMode.RW: "rw",
            BindMode.UNIX_SOCKET: "sock",
            BindMode.FIFO: "fifo",
            BindMode.NULL_BIND: "null",
        }[b.mode]
        lines.append(f"  {b.source:60s} → {b.target:40s} {marker:4s}  {b.provenance.value}")
    return "\n".join(lines)
