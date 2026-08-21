"""A non-Slurm site must be REFUSED clearly, not fail with command-not-found.

Found while transcribing ~40 real clusters from public docs. Roughly
a quarter of them do not run Slurm:

    ALCF Polaris, NCI Gadi     PBS Pro
    OLCF Summit                LSF
    UCL Myriad                 Grid Engine
    Indiana Jetstream2         no batch scheduler at all (cloud VMs)

Every `botainer hpc` command emits `sbatch` and `#SBATCH` directives and parses
`squeue`. On those sites the submission failed with a bare command-not-found,
which reads as a broken botainer install rather than an unsupported platform —
so we would have shipped profiles that look supported and cannot work.

Declaring the limitation is deliberately separate from removing it. Real
PBS/LSF support needs its own security review: the job dispatcher's directional
mailbox was reasoned about in terms of what `slurmstepd` does with `--output`
as the uncaged user, and that analysis does not carry over to `qsub`'s output
staging. A half-ported adapter would repeat the yale-grace error with a much
larger blast radius.
"""
from __future__ import annotations

import pytest

from botainer.cli.hpc import _refuse_unsupported_scheduler
from botainer.state.cluster_profile import ClusterProfile


def _profile(scheduler: str) -> ClusterProfile:
    return ClusterProfile(name="testsite", scheduler=scheduler)


@pytest.mark.parametrize("sched", ["pbs", "PBS", "lsf", "sge", "pbspro"])
def test_non_slurm_schedulers_are_refused(monkeypatch, capsys, sched) -> None:
    monkeypatch.setattr(
        "botainer.state.cluster_profile.active_profile", lambda: _profile(sched))
    with pytest.raises(SystemExit) as exc:
        _refuse_unsupported_scheduler("hpc submit")
    assert exc.value.code == 2
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "refused" in combined.lower()
    # names the actual scheduler, so the user knows WHICH platform is the issue
    assert sched.upper() in combined
    # and offers the thing that still works
    assert "interactive allocation" in combined


def test_no_scheduler_at_all_gets_its_own_message(monkeypatch, capsys) -> None:
    """Jetstream2 is a cloud VM resource. Telling someone "this site runs NONE"
    would be nonsense; the useful advice is to run botainer on the instance."""
    monkeypatch.setattr(
        "botainer.state.cluster_profile.active_profile", lambda: _profile("none"))
    with pytest.raises(SystemExit):
        _refuse_unsupported_scheduler("hpc submit")
    combined = "".join(capsys.readouterr())
    assert "has none" in combined
    assert "directly" in combined
    assert "NONE" not in combined, "leaked the sentinel value into the message"


def test_slurm_is_silent(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "botainer.state.cluster_profile.active_profile", lambda: _profile("slurm"))
    _refuse_unsupported_scheduler("hpc submit")
    assert capsys.readouterr().out == ""


def test_no_profile_is_silent(monkeypatch, capsys) -> None:
    """Absence of a profile must not become an accusation. Most users on an
    unrecognised cluster have no profile at all, and they are not doing
    anything wrong."""
    monkeypatch.setattr(
        "botainer.state.cluster_profile.active_profile", lambda: None)
    _refuse_unsupported_scheduler("hpc submit")
    assert capsys.readouterr().out == ""


def test_a_broken_profile_lookup_does_not_block_submission(monkeypatch) -> None:
    """Fail OPEN here, deliberately.

    This check is an ergonomic guard, not a security boundary — the worst case
    if it wrongly stays quiet is the confusing command-not-found we had before.
    Blocking a legitimate Slurm submission because a profile file is unreadable
    would be a strictly worse failure than the one being prevented.
    """
    def _boom():
        raise RuntimeError("profile unreadable")
    monkeypatch.setattr(
        "botainer.state.cluster_profile.active_profile", _boom)
    _refuse_unsupported_scheduler("hpc submit")  # must not raise


def test_default_scheduler_is_slurm() -> None:
    """A profile that says nothing is Slurm — that is what every existing
    bundled profile means, and silence must not start refusing them."""
    assert ClusterProfile(name="x").scheduler == "slurm"


def test_scheduler_parses_from_yaml() -> None:
    prof = ClusterProfile.from_dict({
        "version": "cluster-profile-v1",
        "cluster": {"name": "polaris"},
        "slurm": {"scheduler": "PBS  ", "partitions": {}},
    })
    assert prof.scheduler == "pbs", "not normalised to lowercase/stripped"
