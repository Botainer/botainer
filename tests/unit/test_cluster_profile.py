"""Cluster-profile loading — fail-closed on a malformed cluster.yaml.

AUDIT (MEDIUM): load_user_profile previously returned None (silent
degrade to autodetect) on a PRESENT-but-malformed ~/.botainer/cluster.yaml,
the opposite fail-mode from SitePolicy (which raises). cluster.yaml is operator
HPC config; a typo silently picking wrong partitions/account/Lmod yields
confusing downstream sbatch failures. Now: absent → None (autodetect); present
but unparseable/invalid → Refused (surface the operator error immediately).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import dataclasses

import yaml

from botainer.core.refusal import Refused, RefusalCategory
from botainer.state import cluster_profile as cp


def test_absent_cluster_yaml_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    assert cp.load_user_profile() is None


def test_valid_cluster_yaml_loads(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    (tmp_path / "cluster.yaml").write_text(
        "version: cluster-profile-v1\ncluster:\n  name: testcluster\n"
    )
    prof = cp.load_user_profile()
    assert prof is not None and prof.name == "testcluster"


@pytest.mark.parametrize("body", [
    "a: : :\n  bad indent",          # YAML parse error
    "just a bare string",            # valid YAML, not a mapping
    "- list\n- not a mapping",       # valid YAML list, not a mapping
    "version: wrong-version\n",      # mapping but invalid (from_dict rejects)
])
def test_malformed_present_cluster_yaml_fails_closed(
    monkeypatch, tmp_path: Path, body: str
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    (tmp_path / "cluster.yaml").write_text(body)
    with pytest.raises(Refused) as exc:
        cp.load_user_profile()
    assert exc.value.category == RefusalCategory.CONFIG_INVALID


# ── round trip: `hpc setup` must not silently erase what a profile declared ──

def test_write_user_profile_preserves_the_scheduler_declaration(tmp_path, monkeypatch):
    """A non-Slurm site must survive `hpc setup`.

    THE BUG THIS ENCODES. `write_user_profile` hand-builds a dict of the fields
    it thinks matter. `scheduler` was not among them, so writing a profile and
    reading it back turned a declared `pbs`/`lsf`/`sge` site into the "slurm"
    default — silently, with no error and no diff a user would notice.

    Seven bundled profiles state in their own headers, in capitals, that their
    site is NOT Slurm (Polaris, Summit, Gadi, ABCI, Aspire2A, CX3, Myriad).
    Every one of them has hostname_patterns that autodetect() matches. So a
    Polaris user ran `hpc setup`, the declaration was erased on the way to
    cluster.yaml, and botainer generated Slurm output for a PBS Pro machine
    with the "this site is not Slurm" warning suppressed — because the warning
    is conditional on the field that had just been dropped.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    for sched in ("pbs", "lsf", "sge", "slurm"):
        prof = cp.ClusterProfile(name=f"x-{sched}", scheduler=sched)
        # force=: each iteration deliberately REPLACES the previous file, which
        # `write_user_profile` now refuses by default (#227 — `hpc setup` used
        # to silently destroy a hand-edited cluster.yaml). The subject here is
        # the serialisation round trip, not the clobber guard, which has its own
        # tests in test_hpc_setup_does_not_eat_your_cluster_yaml.py.
        path = cp.write_user_profile(prof, force=True)
        back = yaml.safe_load(path.read_text())
        assert back["slurm"]["scheduler"] == sched, (
            f"write_user_profile dropped scheduler={sched!r} on the round trip; "
            f"a non-Slurm site silently becomes Slurm"
        )


def test_write_user_profile_drops_no_declared_field(tmp_path, monkeypatch):
    """The general form, so the next field added to the model cannot be lost.

    The specific test above only covers `scheduler`. This one fails for ANY
    model field the writer forgets, which is the actual defect class: a model
    that grows and a hand-written serialiser that does not. `aliases`,
    `verification.*` and the per-partition `preemptible`/`exclusive`/
    `charge_factor` were all being dropped alongside `scheduler`.

    Deliberately compares SEMANTICS, not YAML shape — the writer is free to
    nest things differently, it just may not lose them.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    prof = cp.ClusterProfile(
        name="round-trip", aliases=("rt", "roundtrip"),
        hostname_patterns=("rt-login-*",), description="d",
        scheduler="pbs", lmod_bootstrap="/x/lmod",
        slurm_default_partition="p", slurm_default_account="a",
        slurm_default_time_minutes=99,
        partitions=(cp.PartitionSpec(
            name="p", max_time_minutes=10, max_cpus=2, max_memory_gb=3,
            gpu_types=("a100",), preemptible=True, exclusive=True,
            charge_factor=2.5),),
        scratch_template="/s/{user}", scratch_cleanup_days=7,
        apptainer_cachedir="/c", apptainer_prebuilt_url="https://x",
        modules_denylist=("bad",), modules_always_load=("good",),
        policy_refuse_network=True, agent_hints_preamble="hi",
        verification_status="from-public-docs",
        verification_checked="2026-08-01", verification_source="https://src",
    )
    back = cp.ClusterProfile.from_dict(
        yaml.safe_load(cp.write_user_profile(prof).read_text()))

    lost = []
    for f in dataclasses.fields(cp.ClusterProfile):
        before, after = getattr(prof, f.name), getattr(back, f.name)
        if before != after:
            lost.append(f"  {f.name}: wrote {before!r} -> read back {after!r}")
    assert not lost, (
        "write_user_profile lost fields on the round trip. `hpc setup` rewrites "
        "cluster.yaml through this function, so anything listed here is erased "
        "from a user's profile without warning:\n" + "\n".join(lost)
    )
