"""Resource resolution: profile defaults + optional maxes + agent overrides."""
from __future__ import annotations

import pytest

from botainer.core.config import JobProfile
from botainer.core.refusal import Refused
from botainer.hpc import resources as R


def test_parse_mem_mb() -> None:
    assert R.parse_mem_mb("512") == 512      # bare = MB
    assert R.parse_mem_mb("4096M") == 4096
    assert R.parse_mem_mb("32G") == 32 * 1024
    assert R.parse_mem_mb("1T") == 1024 * 1024
    with pytest.raises(ValueError):
        R.parse_mem_mb("lots")


def test_parse_time_seconds() -> None:
    assert R.parse_time_seconds("01:00:00") == 3600
    assert R.parse_time_seconds("30") == 1800          # minutes
    assert R.parse_time_seconds("10:30") == 630        # MM:SS
    assert R.parse_time_seconds("1-00:00:00") == 86400 # D-HH:MM:SS
    with pytest.raises(ValueError):
        R.parse_time_seconds("noon")


def test_no_override_uses_defaults() -> None:
    p = JobProfile(partition="day", cpus=4, memory="16G", gpus=1, nodes=1)
    r = R.resolve_resources(p, None)
    assert r["cpus"] == 4 and r["memory"] == "16G" and r["gpus"] == 1


def test_override_within_max_is_used() -> None:
    p = JobProfile(partition="day", cpus=4, max_cpus=32, memory="16G",
                   max_memory="128G", gpus=1, max_gpus=4, nodes=1, max_nodes=8)
    r = R.resolve_resources(p, {"cpus": 16, "memory": "64G", "gpus": 2, "nodes": 4})
    assert r["cpus"] == 16 and r["memory"] == "64G" and r["gpus"] == 2 and r["nodes"] == 4


def test_override_beyond_max_refused() -> None:
    p = JobProfile(partition="day", cpus=4, max_cpus=8)
    with pytest.raises(Refused):
        R.resolve_resources(p, {"cpus": 16})
    p2 = JobProfile(partition="day", memory="16G", max_memory="32G")
    with pytest.raises(Refused):
        R.resolve_resources(p2, {"memory": "64G"})


def test_override_on_fixed_resource_refused() -> None:
    # cpus has no max_cpus → fixed → cannot override
    p = JobProfile(partition="day", cpus=4)
    with pytest.raises(Refused):
        R.resolve_resources(p, {"cpus": 8})


def test_override_can_go_below_default() -> None:
    p = JobProfile(partition="day", cpus=8, max_cpus=16)
    assert R.resolve_resources(p, {"cpus": 2})["cpus"] == 2   # conservative is ok


def test_time_override_bounded() -> None:
    p = JobProfile(partition="day", time="01:00:00", max_time="04:00:00")
    assert R.resolve_resources(p, {"time": "02:00:00"})["time"] == "02:00:00"
    with pytest.raises(Refused):
        R.resolve_resources(p, {"time": "08:00:00"})


def test_sbatch_injection_in_overrides_refused() -> None:
    """The agent's --mem/--time overrides bypass the pydantic field validators
    (they come from the request), so resolve_resources MUST reject anything that
    isn't a clean SLURM value — an injection attempt into the #SBATCH line."""
    pm = JobProfile(partition="day", memory="16G", max_memory="128G")
    for bad in ("16G\n#SBATCH --uid=0", "16G ; rm -rf ~", "16G\n\nx",
                "$(whoami)", "16G`id`", "16 G"):
        with pytest.raises(Refused):
            R.resolve_resources(pm, {"memory": bad})
    pt = JobProfile(partition="day", time="01:00:00", max_time="04:00:00")
    for bad in ("1:00:00 ; rm", "01:00:00\n#SBATCH", "$(x)", "1:00:00 && y"):
        with pytest.raises(Refused):
            R.resolve_resources(pt, {"time": bad})


def test_nonstring_override_refused_not_crashed() -> None:
    """A caged agent writing raw JSON into in/ (bypassing the botainer-job CLI)
    can give memory/time as a NON-string (int/float/list/dict/bool). Those must
    raise Refused, NOT AttributeError/TypeError — the latter escaped every
    catcher and crash-looped the dispatcher permanently (audit HIGH).
    """
    pm = JobProfile(partition="day", memory="16G", max_memory="128G")
    for bad in (1, 1.5, [1], {"a": 1}, True):
        with pytest.raises(Refused):
            R.resolve_resources(pm, {"memory": bad})
    pt = JobProfile(partition="day", time="01:00:00", max_time="04:00:00")
    for bad in (1, 1.5, [1], {"a": 1}, True):
        with pytest.raises(Refused):
            R.resolve_resources(pt, {"time": bad})


def test_parse_helpers_reject_nonstring() -> None:
    """The parse sinks themselves normalize a non-string to ValueError (so the
    Refused conversion above holds even if a new caller forgets to guard)."""
    for bad in (1, 1.5, None, [1], {"a": 1}):
        with pytest.raises(ValueError):
            R.parse_mem_mb(bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            R.parse_time_seconds(bad)  # type: ignore[arg-type]


def test_blank_override_is_no_override() -> None:
    """A blank/whitespace-only override string means 'use the profile default',
    not 'validate an empty value' — `memory:""` must NOT be Refused and
    `time:""` must NOT silently drop the directive (it keeps the default)."""
    pm = JobProfile(partition="day", memory="16G", max_memory="128G",
                    time="01:00:00", max_time="04:00:00")
    r = R.resolve_resources(pm, {"memory": "", "time": "   "})
    assert r["memory"] == "16G"      # default kept, not refused
    assert r["time"] == "01:00:00"   # default kept, not zeroed/dropped
