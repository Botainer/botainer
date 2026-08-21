"""Render the SessionSpec as a human-readable tree.

Goal: a user reading `botainer inspect` should see *everything* that will hit
the runtime — every bind, every flag, every endpoint, every plugin contribution.
"""

from __future__ import annotations

from botainer.core.spec import Provenance, SessionSpec


def render(spec: SessionSpec) -> str:
    lines: list[str] = []
    lines.append(f"Session:    {spec.session_id}")
    lines.append(f"Project:    {spec.project_root}  ({spec.project_uuid})")
    # "mock" is what `_resolve_runtime` falls back to when neither docker nor
    # apptainer is on PATH, so `inspect` and `dry-run` still work. Printed bare
    # it reads like a test artefact — say what it means instead.
    lines.append(
        "Runtime:    mock (no docker or apptainer on PATH — this plan is "
        "composed but cannot be launched)"
        if spec.runtime == "mock"
        else f"Runtime:    {spec.runtime}"
    )
    lines.append(f"Image:      {spec.image}")
    lines.append(f"Profile:    {spec.profile}")
    lines.append(f"Plugins:    {', '.join(spec.plugins_enabled) or '(none)'}")
    lines.append("")
    lines.append("Network:")
    lines.append(f"  mode:     {spec.network.mode.value}")
    if spec.network.endpoints:
        lines.append(f"  endpoints: {', '.join(spec.network.endpoints)}")
    lines.append("")
    lines.append("Resources:")
    # Both lines say "(default)" when unset. `cpu` used to interpolate the raw
    # value and printed a bare `None` at the user — four lines above a sibling
    # that already handled it. Unset is the common case: `botainer init` writes
    # no resource limits, so this is what a first `inspect` shows.
    lines.append(
        f"  cpu:      {spec.resources.cpu}"
        if spec.resources.cpu
        else "  cpu:      (default — no limit set)"
    )
    lines.append(
        f"  memory:   {spec.resources.memory_mb} MB"
        if spec.resources.memory_mb
        else "  memory:   (default — no limit set)"
    )
    # #175: HPC scheduling fields (gpus, partition, account, time,
    # gpu_type) live on cfg.resources and are surfaced by
    # `botainer config explain` / hpc-launcher's submit-script preview.
    # They're not on SessionSpec because no adapter consumes them
    # post-compose.
    lines.append("")
    lines.append("Mounts:")
    for b in spec.mount_plan.binds:
        marker = {
            "ro": "ro  ",
            "rw": "rw  ",
            "unix-socket": "sock",
            "fifo": "fifo",
            "null-bind": "null",
        }.get(b.mode.value, "????")
        lines.append(
            f"  {b.source:60s} → {b.target:40s} {marker}  "
            f"[{b.provenance.value}] {b.provenance_detail}"
        )
    if spec.sidecars:
        # Tasks #95/#291: composition refuses contributes.sidecars at
        # compose time. This branch is dead under normal flow; if a
        # SessionSpec was constructed directly with sidecars (test/fuzz),
        # label them explicitly so the operator can't miss the gap.
        lines.append("")
        lines.append("Sidecars (DECLARED but NOT launched in v0.1; see #95):")
        for s in spec.sidecars:
            lines.append(f"  {s.name} ({s.runtime}) image={s.image} lifetime={s.lifetime}  [NOT RUNNING]")
    if spec.hooks:
        lines.append("")
        lines.append("Plugin hooks (run on host as you; not sandboxed):")
        for h in spec.hooks:
            lines.append(f"  {h.plugin} {h.when}: {h.script_path}")
    if spec.env.values:
        # AC4: env vars are session-affecting state the user must be able
        # to see — they're injected into the agent's environment. Show
        # NAMES only; values can be credential-shaped (the JSON surface
        # redacts them, and inspect must not be the leak the proxy/scrub
        # paths prevent). Sorted for determinism.
        lines.append("")
        lines.append("Environment variables (names only; values hidden):")
        for k in sorted(spec.env.values):
            lines.append(f"  {k}")
    if spec.env_files:
        lines.append("")
        lines.append("Env-files sourced into the container (host paths):")
        for ef in spec.env_files:
            lines.append(f"  {ef}")
    lines.append("")
    lines.append("Capabilities (effective):")
    for c in spec.capabilities:
        if c.provenance is Provenance.PLUGIN and c.value is None:
            # A manifest `capabilities:` entry is a DECLARATION that the plugin
            # may contribute to this capability — there is no value yet. Saying
            # "= None" read as "unset", and identical declarations from two
            # plugins produced two indistinguishable rows.
            who = c.source_plugin or "unknown plugin"
            lines.append(f"  {c.name:30s}   declared by {who} (may contribute)")
        else:
            lines.append(f"  {c.name:30s} = {c.value!r:40s} [{c.provenance.value}]")
    lines.append("")
    lines.append("Reminder: botainer protects your reach *outside* the project.")
    lines.append("It does not protect this project's checkout from the agent — the agent")
    lines.append("can rewrite files under /workspace. Review .git/, tests, and any new")
    lines.append("scripts before running them on the host after a session.")
    return "\n".join(lines)
