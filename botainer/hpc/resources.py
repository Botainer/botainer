"""Resolve a job's EFFECTIVE resources: profile defaults + optional per-request
overrides, bounded by the profile's opt-in max and (elsewhere) the site policy.

"Fixed by default, opt-in max" (jobs v2): a profile's scalar fields (cpus,
memory, gpus, nodes, time) are the DEFAULT the agent gets. If the profile also
sets a `max_*`, the agent MAY request up to it via `botainer-job submit
--cpus/--mem/--gpus/--nodes/--time`. If a resource has NO max_*, it is FIXED — an
override for it is refused. Every value is validated here (a plain, testable
function) BEFORE it becomes an sbatch directive; the profile is UNTRUSTED
(git-shareable) but the max is bounded by the root JobPolicy at the sink.
"""
from __future__ import annotations

import re

from botainer.core.refusal import RefusalCategory, Refused

_MEM_RE = re.compile(r"^(\d+)([KMGT])?$", re.IGNORECASE)
_MEM_MULT_MB = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}


def _blank_override(v) -> bool:
    """True for None or a blank/whitespace-only string — i.e. "no override".
    A non-string non-None value (int/list/dict) is NOT blank: it flows to the
    field validator, which refuses it (fail-closed), rather than being silently
    dropped."""
    return v is None or (isinstance(v, str) and not v.strip())


def parse_mem_mb(v: str) -> int:
    """SLURM ``--mem`` string → integer MEGABYTES. Bare number = MB (SLURM's
    default unit); suffix K/M/G/T. Raises ValueError on anything else.

    The type-guard is load-bearing: an override arriving as raw JSON (a
    caged agent writing straight into ``in/`` bypasses the ``botainer-job``
    argparse) can be an int/float/list/dict, and ``(v or "").strip()`` would
    then raise ``AttributeError``/``TypeError`` — NOT the ``ValueError`` callers
    catch — crashing the dispatcher (audit HIGH). Normalize to
    ValueError so the fail-closed path refuses it."""
    if not isinstance(v, str):
        raise ValueError(f"memory must be a string, got {type(v).__name__}")
    m = _MEM_RE.match(v.strip())
    if not m:
        raise ValueError(f"bad memory value {v!r} (want e.g. 512, 4096M, 32G)")
    n, suf = int(m.group(1)), (m.group(2) or "M").upper()
    return max(1, int(n * _MEM_MULT_MB[suf]))


def parse_time_seconds(v: str) -> int:
    """SLURM ``--time`` → seconds. Accepts D-HH:MM:SS, HH:MM:SS, MM:SS, MM,
    D-HH:MM, D-HH. Raises ValueError on garbage (incl. a non-string override —
    see parse_mem_mb's note; a typed-JSON override must not crash the daemon)."""
    if not isinstance(v, str):
        raise ValueError(f"time must be a string, got {type(v).__name__}")
    s = v.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = s.split(":") if s else ["0"]
    if not all(p.isdigit() for p in parts) or len(parts) > 3:
        raise ValueError(f"bad time value {v!r}")
    parts = [int(p) for p in parts]
    if len(parts) == 1:      # minutes
        h, m, sec = 0, parts[0], 0
    elif len(parts) == 2:    # HH:MM if days present, else MM:SS
        (h, m, sec) = (parts[0], parts[1], 0) if days else (0, parts[0], parts[1])
    else:                    # HH:MM:SS
        h, m, sec = parts
    return ((days * 24 + h) * 60 + m) * 60 + sec


def _resolve_int(name: str, default: int, maximum: int | None,
                 override, *, floor: int) -> int:
    if override is None:
        return int(default)
    try:
        want = int(override)
    except (TypeError, ValueError):
        raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                      f"{name} override {override!r} is not an integer")
    if maximum is None:
        raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                      f"{name} is fixed at {default} for this profile "
                      f"(no max_{name}); cannot override it")
    if want < floor or want > int(maximum):
        raise Refused(RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
                      f"{name} override {want} outside [{floor}, {maximum}] "
                      f"(profile max_{name}={maximum})")
    return want


def resolve_resources(profile, overrides: dict | None) -> dict:
    """Return the effective {cpus, memory, gpus, nodes, time, ntasks,
    ntasks_per_node} the job gets. Refuses an override that exceeds the profile
    max or targets a resource with no max_* (fixed). MPI shape (ntasks/…) is
    scaled with nodes but not independently overridable here (kept from profile).
    """
    # Normalize: a blank/whitespace-only override is "no override" (use the
    # profile default), not a value to validate. Without this, `memory: ""` is
    # Refused and `time: ""` parses to 0s and silently drops the --time
    # directive. Non-blank values (incl. non-string junk) pass through to the
    # per-field validators below, which fail CLOSED.
    o = {k: v for k, v in (overrides or {}).items() if not _blank_override(v)}
    g = lambda k, d=None: getattr(profile, k, d)  # noqa: E731

    cpus = _resolve_int("cpus", g("cpus", 1) or 1, g("max_cpus"),
                        o.get("cpus"), floor=1)
    gpus = _resolve_int("gpus", g("gpus", 0) or 0, g("max_gpus"),
                        o.get("gpus"), floor=0)
    nodes = _resolve_int("nodes", g("nodes", 1) or 1, g("max_nodes"),
                         o.get("nodes"), floor=1)

    # memory: compare parsed MB.
    default_mem = g("memory", "") or ""
    max_mem = g("max_memory")
    if o.get("memory") is not None:
        if not max_mem:
            raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                          "memory is fixed for this profile (no max_memory); "
                          "cannot override it")
        try:
            want_mb, max_mb = parse_mem_mb(o["memory"]), parse_mem_mb(max_mem)
        except (ValueError, TypeError) as exc:
            raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID, str(exc))
        if want_mb > max_mb:
            raise Refused(RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
                          f"memory override {o['memory']} > profile "
                          f"max_memory {max_mem}")
        # Return the VALIDATED/stripped form (parse_mem_mb already proved it
        # matches ^\d+[KMGT]?$), never the raw override — no stray whitespace /
        # trailing newline reaches the #SBATCH directive line.
        memory = str(o["memory"]).strip()
    else:
        memory = default_mem

    # time: compare parsed seconds.
    default_time = g("time", "") or ""
    max_time = g("max_time")
    if o.get("time") is not None:
        if not max_time:
            raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID,
                          "time is fixed for this profile (no max_time); "
                          "cannot override it")
        try:
            if parse_time_seconds(o["time"]) > parse_time_seconds(max_time):
                raise Refused(RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
                              f"time override {o['time']} > profile "
                              f"max_time {max_time}")
        except (ValueError, TypeError) as exc:
            raise Refused(RefusalCategory.CAPABILITY_VALUE_INVALID, str(exc))
        time = str(o["time"]).strip()  # validated form only (see memory note)
    else:
        time = default_time

    return {
        "cpus": cpus, "memory": memory, "gpus": gpus, "nodes": nodes,
        "time": time,
        "ntasks": g("ntasks"), "ntasks_per_node": g("ntasks_per_node"),
        "gpu_type": g("gpu_type"), "partition": g("partition", ""),
        "account": g("account"),
    }
