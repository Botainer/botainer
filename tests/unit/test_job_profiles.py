"""#54: job_profiles config (the capped resource shape) + JobPolicy ceiling."""
from __future__ import annotations

import pytest

from botainer.core.config import JobProfile, ProjectConfig
from botainer.core.policy import (
    JobPolicy,
    SitePolicy,
    check_profile_against_ceiling,
    intersect,
)
from botainer.core.refusal import Refused


# ─────────── config: the profile is untrusted → parse-time defense ───────────


def test_profile_parses_and_caps_resources() -> None:
    c = ProjectConfig(job_profiles={
        "gpu": {"partition": "gpu", "time": "04:00:00", "cpus": 8,
                "memory": "32G", "gpus": 1, "gpu_type": "a100", "max_concurrent": 1},
    })
    p = c.job_profiles["gpu"]
    assert isinstance(p, JobProfile)
    assert p.partition == "gpu" and p.gpus == 1 and p.max_concurrent == 1


@pytest.mark.parametrize("name", ["bad name", "../x", "x/y", "a!b", ""])
def test_profile_name_must_be_a_flat_token(name) -> None:
    with pytest.raises(Exception):
        ProjectConfig(job_profiles={name: {}})


@pytest.mark.parametrize("field,val", [
    ("partition", "day; rm -rf /"), ("account", "a\nb"),
    ("gpu_type", "a100 && evil"), ("time", "04:00:00\n#SBATCH --x"),
])
def test_profile_sbatch_fields_reject_injection(field, val) -> None:
    with pytest.raises(Exception):
        ProjectConfig(job_profiles={"p": {field: val}})


def test_profile_negative_resource_refused() -> None:
    with pytest.raises(Exception):
        ProjectConfig(job_profiles={"p": {"cpus": -1}})


# ─────────── policy: the ceiling is the REAL trust boundary ───────────


def test_default_ceiling_is_permissive() -> None:
    jp = SitePolicy().jobs
    # No site policy → no botainer cap (Slurm is the backstop).
    check_profile_against_ceiling("x", "anything", "acct", 64, 99, jp)


def test_ceiling_refuses_partition_account_gpus_concurrency() -> None:
    jp = JobPolicy(
        allowed_partitions=["day", "gpu"], allowed_accounts=["pi_ok"],
        max_gpus_per_job=2, max_concurrent_cap=4,
    )
    check_profile_against_ceiling("p", "gpu", "pi_ok", 2, 4, jp)  # exactly at cap: ok
    for args in [
        ("p", "bigmem", "pi_ok", 1, 1),   # partition off allowlist
        ("p", "gpu", "pi_evil", 1, 1),    # account off allowlist
        ("p", "gpu", "pi_ok", 3, 1),      # gpus > cap
        ("p", "gpu", "pi_ok", 1, 5),      # concurrency > cap
    ]:
        with pytest.raises(Refused):
            check_profile_against_ceiling(*args, jp)


def test_intersect_empty_means_all_most_restrictive() -> None:
    # site restricts, user empty (=all) → keep site's restriction.
    site = SitePolicy(jobs=JobPolicy(allowed_partitions=["day"], max_gpus_per_job=1))
    user = SitePolicy()
    eff = intersect(site, user).jobs
    assert eff.allowed_partitions == ["day"]
    assert eff.max_gpus_per_job == 1
    # both restrict → set-intersection; min cap.
    a = SitePolicy(jobs=JobPolicy(allowed_partitions=["day", "gpu"], max_gpus_per_job=4))
    b = SitePolicy(jobs=JobPolicy(allowed_partitions=["gpu", "scavenge"], max_gpus_per_job=2))
    eff2 = intersect(a, b).jobs
    assert eff2.allowed_partitions == ["gpu"]
    assert eff2.max_gpus_per_job == 2
