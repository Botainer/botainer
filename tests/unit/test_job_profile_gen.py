"""Generated job profiles must be VALID for the cluster they claim to target.

A starter config is only worth generating if it works on first submission. If
it names a partition the site does not have, or a walltime the scheduler
rejects, it is worse than the hand-editing it replaces — the user now has to
debug someone else's guess instead of writing their own.

So every test here is about the generated content being *true of the profile*,
not about it being well-formed.
"""
from __future__ import annotations

import pytest
import yaml

from botainer.hpc.job_profile_gen import (
    pick_cpu_partition,
    pick_debug_partition,
    pick_gpu_partition,
    render_job_profiles,
)
from botainer.state.cluster_profile import ClusterProfile, PartitionSpec


def _profile(*parts: PartitionSpec, **kw) -> ClusterProfile:
    return ClusterProfile(
        name=kw.pop("name", "testsite"),
        description=kw.pop("description", "Test Site"),
        partitions=parts,
        verification_status=kw.pop("verification_status", "from-public-docs"),
        **kw,
    )


_DAY = PartitionSpec("day", max_time_minutes=1440, max_cpus=1500)
_WEEK = PartitionSpec("week", max_time_minutes=10080, max_cpus=108)
_DEVEL = PartitionSpec("devel", max_time_minutes=120, max_cpus=4)
_GPU = PartitionSpec("gpu", max_time_minutes=2880, max_cpus=24,
                     gpu_types=("a100", "v100"))
_SCAVENGE = PartitionSpec("scavenge", max_time_minutes=1440, max_cpus=10000,
                          preemptible=True)
_EXCLUSIVE = PartitionSpec("compute", max_time_minutes=2880, max_cpus=128,
                           exclusive=True)


# --------------------------------------------------------------------------
# Rule 1: only partitions the profile actually lists.
# --------------------------------------------------------------------------

def test_only_emits_partitions_the_profile_declares() -> None:
    prof = _profile(_DAY, _GPU, _DEVEL)
    out = render_job_profiles(prof)
    parsed = yaml.safe_load(out)
    declared = {p.name for p in prof.partitions}
    for entry in parsed["job_profiles"].values():
        assert entry["partition"] in declared, entry


def test_nothing_is_invented_when_every_partition_is_a_trap() -> None:
    """A profile whose ONLY partitions are preemptible or exclusive must
    generate nothing — not quietly fall back to one of them.

    This is the case where the "be helpful" instinct does the most damage: the
    generator has candidates, they are simply all unsafe as defaults, and
    emitting one because it is the only option available is exactly the silent
    harm rule 4 exists to prevent. Emitting a plausible-looking
    `partition: compute` would be a fabrication with a bill attached.
    """
    prof = _profile(_SCAVENGE, _EXCLUSIVE)
    out = render_job_profiles(prof)
    assert yaml.safe_load(out) is None, "emitted config despite only trap partitions"
    assert "No usable partition" in out
    # Was `assert "hpc discover" in out` — but that command was never built
    # (task #98) and the generated text named it anyway, writing a nonexistent
    # command into the user's own config.yaml. The obligation is to offer a way
    # forward, not to name a specific command; assert the obligation.
    assert "sinfo" in out, "no path forward offered"


def test_a_short_queue_is_still_usable_as_a_quick_profile() -> None:
    """The converse: a site whose only queue is short is not unusable — a
    5-minute test job is a legitimate thing to generate, clamped to fit."""
    prof = _profile(PartitionSpec("tiny", max_time_minutes=5))
    parsed = yaml.safe_load(render_job_profiles(prof))
    assert parsed, "a short queue should still yield a quick profile"
    for entry in parsed["job_profiles"].values():
        h, m, _ = entry["time"].split(":")
        assert int(h) * 60 + int(m) <= 5


def test_profile_text_containing_newlines_cannot_escape_into_yaml() -> None:
    """A real profile (ALCF Polaris) has a multi-line `verification.source`,
    which broke generation outright. It is also an injection vector: a
    community-contributed description could otherwise write live config.
    """
    hostile = _profile(
        _DAY,
        description="Innocent\njob_profiles:\n  evil:\n    partition: \"root\"",
    )
    out = render_job_profiles(hostile)

    # Assert on the TEXT, not on the parse. Checking `parsed["job_profiles"]`
    # is not enough: an injected block and the legitimate one are duplicate
    # keys, and PyYAML silently keeps the last — so the injection succeeds
    # while the test reports success. (Caught by mutation,.)
    top_level = [ln for ln in out.splitlines() if ln and not ln[0].isspace()
                 and not ln.lstrip().startswith("#")]
    assert top_level == ["job_profiles:"], (
        f"non-comment top-level lines escaped from profile text: {top_level}")
    parsed = yaml.safe_load(out)
    assert "evil" not in parsed["job_profiles"]
    assert parsed["job_profiles"]["cpu-small"]["partition"] == "day"


def test_carriage_returns_cannot_smuggle_a_line_break() -> None:
    hostile = _profile(_DAY, description="A\r\njob_profiles:\r\n  evil: {}")
    out = render_job_profiles(hostile)
    top_level = [ln for ln in out.splitlines() if ln and not ln[0].isspace()
                 and not ln.lstrip().startswith("#")]
    assert top_level == ["job_profiles:"], top_level


# --------------------------------------------------------------------------
# Rule 2: never emit a time the partition would reject.
# --------------------------------------------------------------------------

def test_time_never_exceeds_the_partition_ceiling() -> None:
    """THE test. A generated config the scheduler refuses on first use is
    worse than no generation at all."""
    short = PartitionSpec("brief", max_time_minutes=30)
    prof = _profile(short)
    parsed = yaml.safe_load(render_job_profiles(prof))
    for entry in (parsed or {}).get("job_profiles", {}).values():
        h, m, _ = entry["time"].split(":")
        assert int(h) * 60 + int(m) <= 30, entry
        if "max_time" in entry:
            h, m, _ = entry["max_time"].split(":")
            assert int(h) * 60 + int(m) <= 30, entry


def test_max_time_is_clamped_too() -> None:
    prof = _profile(PartitionSpec("cpu", max_time_minutes=90))
    parsed = yaml.safe_load(render_job_profiles(prof))
    for entry in parsed["job_profiles"].values():
        if "max_time" in entry:
            h, m, _ = entry["max_time"].split(":")
            assert int(h) * 60 + int(m) <= 90


# --------------------------------------------------------------------------
# Rule 3: chosen by role, not by name.
# --------------------------------------------------------------------------

def test_cpu_choice_prefers_the_shortest_adequate_queue() -> None:
    """A 7-day queue and a 24-hour queue serve a 1-hour job equally well, and
    the shorter one almost always starts sooner. "Biggest limits" is the
    tempting wrong answer."""
    assert pick_cpu_partition(_profile(_WEEK, _DAY)).name == "day"


def test_gpu_partition_is_found_by_having_gpus_not_by_being_named_gpu() -> None:
    odd = PartitionSpec("accel-v100", max_time_minutes=1440, gpu_types=("v100",))
    assert pick_gpu_partition(_profile(_DAY, odd)).name == "accel-v100"


def test_a_cpu_partition_is_never_chosen_for_the_gpu_profile() -> None:
    assert pick_gpu_partition(_profile(_DAY, _WEEK)) is None


def test_debug_partition_is_recognised_by_its_short_ceiling() -> None:
    assert pick_debug_partition(_profile(_DAY, _DEVEL)).name == "devel"


# --------------------------------------------------------------------------
# Rule 4: the traps. This is the load-bearing safety property.
# --------------------------------------------------------------------------

def test_preemptible_partitions_are_never_a_generated_default() -> None:
    """`scavenge` has the highest core limit and often the shortest queue wait,
    so any "pick the best partition" heuristic lands on it — and the job can be
    killed at any moment. Several bundled profiles set it as their cluster
    default, which is exactly how it would leak in."""
    prof = _profile(_SCAVENGE, _DAY)
    parsed = yaml.safe_load(render_job_profiles(prof))
    for entry in parsed["job_profiles"].values():
        assert entry["partition"] != "scavenge"
    assert pick_cpu_partition(prof).name == "day"


def test_exclusive_partitions_are_never_a_generated_default() -> None:
    """SDSC Expanse `compute` bills all 128 cores for a one-core job."""
    prof = _profile(_EXCLUSIVE, _DAY)
    parsed = yaml.safe_load(render_job_profiles(prof))
    for entry in parsed["job_profiles"].values():
        assert entry["partition"] != "compute"


@pytest.mark.parametrize("part,word", [
    (_SCAVENGE, "PREEMPTIBLE"),
    (_EXCLUSIVE, "EXCLUSIVE"),
])
def test_excluded_partitions_are_explained_not_hidden(part, word) -> None:
    """Silently omitting them would be its own defect: they are legitimate
    choices, often the cheapest, and a user who never learns they exist cannot
    make that trade deliberately."""
    out = render_job_profiles(_profile(part, _DAY))
    assert part.name in out
    assert word in out
    assert "checkpoint" in out or "one core" in out


# --------------------------------------------------------------------------
# Provenance and accuracy of the generated file.
# --------------------------------------------------------------------------

def test_generated_file_states_where_its_numbers_came_from() -> None:
    """A user hitting a wrong limit must be able to tell instantly that these
    values were transcribed from docs, not probed from the live scheduler."""
    out = render_job_profiles(_profile(_DAY))
    assert "NOT run on the cluster" in out
    # Same correction: `botainer hpc check-profile` does not exist (task #99).
    # What the header must do is tell the reader how to verify the numbers.
    assert "sinfo" in out and "verify" in out.lower()


def test_non_slurm_site_is_warned_about_in_the_generated_file() -> None:
    prof = _profile(_DAY, scheduler="pbs")
    out = render_job_profiles(prof)
    assert "PBS" in out and "NOT Slurm" in out


def test_account_is_a_discoverable_placeholder_not_a_bare_one() -> None:
    """We cannot know the account; leaving `my_allocation` taught the user
    nothing. The discovery command is the actionable version."""
    out = render_job_profiles(_profile(_DAY))
    assert "sacctmgr" in out


def test_output_is_parseable_yaml_for_every_bundled_profile() -> None:
    """The generator runs against real, messy profiles — including ones with
    zero partitions, non-Slurm schedulers and missing limits."""
    from botainer.state import cluster_profile as cp
    profiles = cp.list_bundled()
    assert profiles, "no bundled profiles found"
    for prof in profiles:
        out = render_job_profiles(prof)
        yaml.safe_load(out)  # must not raise


def test_charge_multiplier_is_surfaced_not_silently_carried() -> None:
    """A field with no consumer is decoration. `charge_factor` records that
    NERSC `premium` costs 2-4x — a fact whose entire value is being seen
    BEFORE the allocation is spent, so it has to reach the generated file."""
    pricey = PartitionSpec("premium", max_time_minutes=2880, charge_factor=4.0)
    out = render_job_profiles(_profile(pricey))
    assert "4.0x" in out and "COSTS" in out, out


def test_charge_multiplier_appears_in_the_skip_reason_too() -> None:
    both = PartitionSpec("prem-excl", max_time_minutes=2880,
                         exclusive=True, charge_factor=2.0)
    out = render_job_profiles(_profile(both, _DAY))
    assert "2.0x" in out
