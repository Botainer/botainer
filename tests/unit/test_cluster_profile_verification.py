"""A cluster profile must say how much it has been verified — or say it hasn't.

Profile provenance must distinguish hardware verification from documentation. A
profile transcribed from a vendor's docs and one actually run on the hardware
look identical in a list, but the difference decides whether a failed submit
means "my config is wrong" or "this profile was always a guess".

Before this, three of four bundled yale-*.yaml carried the provenance as a
free-text COMMENT in three different phrasings, and yale-grace.yaml — the one
actually run on hardware — had none at all. A comment cannot be surfaced.

The load-bearing property is the DEFAULT: a profile that records nothing must
report UNVERIFIED, never pass silently. Absence of a claim must not read as a
claim.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from botainer.state.cluster_profile import ClusterProfile

PROFILE_DIR = Path(__file__).resolve().parents[2] / "cluster_profiles"


def _load(name: str) -> ClusterProfile:
    return ClusterProfile.from_dict(
        yaml.safe_load((PROFILE_DIR / name).read_text(encoding="utf-8")))


def test_unstamped_profile_reports_unverified_not_silence() -> None:
    """The fail-safe direction: no claim ⇒ loudly unverified."""
    p = ClusterProfile(name="nameless")
    assert p.verification_status == ""
    assert not p.is_verified_on_hardware()
    assert "UNVERIFIED" in p.verification_label()


def test_unrecognised_status_is_not_trusted() -> None:
    """A typo'd or invented status must degrade to unverified, not sneak past
    as a level we never defined."""
    p = ClusterProfile(name="x", verification_status="totally-legit")
    assert not p.is_verified_on_hardware()
    assert "UNVERIFIED" in p.verification_label()


@pytest.mark.parametrize("name", [p.name for p in PROFILE_DIR.glob("*.yaml")])
def test_every_bundled_profile_declares_its_provenance(name: str) -> None:
    p = _load(name)
    assert p.verification_status in ClusterProfile.VERIFICATION_LEVELS, (
        f"{name} does not declare `verification.status`; it would ship telling "
        f"the user nothing about whether it has ever been run")


def test_public_docs_profiles_do_not_claim_hardware_testing() -> None:
    """Transcribed-from-docs profiles must not imply anyone ran them.

    Launching a session exercises the adapter, not the accuracy of the
    partition table. Hardware-verification labels must describe the values
    actually checked, including time limits, memory and storage paths.
    """
    for name in ("us-yale-bouchet.yaml", "us-yale-mccleary.yaml",
                 "us-yale-milgram.yaml", "us-yale-grace.yaml"):
        p = _load(name)
        assert p.verification_status == "from-public-docs", (name, p.verification_status)
        assert not p.is_verified_on_hardware()
        assert "NOT run on the cluster" in p.verification_label()


def test_no_profile_claims_hardware_testing_without_a_probe_command() -> None:
    """`tested-on-hardware` is not self-certifiable by editing a YAML file.

    Until `botainer hpc check-profile` (#99) exists to compare a profile against
    live `scontrol`/`sinfo` output, NOTHING we ship may carry the strongest
    stamp — because there is no mechanism that could have earned it. This test
    is designed to FAIL when #99 lands and a profile is legitimately re-stamped;
    that failure is the reminder to record how the stamp was obtained.
    """
    for path in sorted(PROFILE_DIR.glob("*.yaml")):
        p = _load(path.name)
        assert not p.is_verified_on_hardware(), (
            f"{path.name} claims tested-on-hardware, but no probe command "
            f"exists yet to verify a profile against a live scheduler. If #99 "
            f"has landed, update this test and record the probe output in "
            f"`verification.source`.")
