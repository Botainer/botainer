"""`doctor` must not call an image healthy that `start` refuses.

THE DEFECT, measured before the fix: with a `.sif` replaced after botainer
recorded its sha256,

    botainer doctor           ✓ image.agent-claude.apptainer  …sif built (1 MiB)
    botainer doctor --strict  same, exit 0
    botainer hpc submit       refused: [image-invalid]

`collect_image_findings` globbed four candidate filenames, found one, and
reported its SIZE. `composition._verify_apptainer_sif_provenance` hashed the
same file and compared it to the `apptainer:sha256:` marker in installed.lock.
Two surfaces answering different questions about one file — and `doctor
--strict` is the command the HPC guide sends people to BEFORE launching jobs,
so it is the one most obliged to agree.

WHY PLAIN `doctor` STILL DOES NOT HASH. A real agent .sif is 3-5 GB. Hashing it
costs seconds locally and considerably more on a cluster parallel filesystem,
and `doctor` is run casually. So the comparison is gated on `--strict`, and
plain `doctor` says in its own output that it did NOT compare. What it must
never do is print a bare tick that READS as verified — that is the same
"presence is not effect" failure this project keeps finding, pointed at the
user instead of at a test.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from botainer.cli import doctor as doctor_mod
from botainer.core import composition
from botainer.core.refusal import Refused
from botainer.plugins import provenance as prov

_SIF = b"BUILT BY BOTAINER" * 70_000


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A state root with one installed plugin and a recorded .sif."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)

    plugin_dir = paths.root / "plugins" / "agent-claude"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "agent-claude.def").write_text("Bootstrap: docker\n")

    lock = paths.installed_lock_path
    lock.parent.mkdir(parents=True, exist_ok=True)
    prov.append_lock(lock, prov.ProvenanceEntry(
        name="agent-claude", version="1.0", source="file://bundled",
        tree_sha="sha256:a", image_digest=None,
        installed_at=prov.now_iso(), tier="first-party"))

    sif = paths.apptainer_sif_path("agent-claude")
    sif.parent.mkdir(parents=True, exist_ok=True)
    sif.write_bytes(_SIF)

    # `collect_image_findings` imports this INSIDE the function, so it must be
    # patched at the source module — patching the doctor namespace silently
    # does nothing and the real installed plugins leak into the fixture.
    from botainer.plugins import lifecycle as lifecycle_module
    monkeypatch.setattr(
        lifecycle_module, "list_installed",
        lambda: [types.SimpleNamespace(name="agent-claude",
                                       plugin_dir=plugin_dir)])
    # apptainer present, docker absent — the cluster shape.
    monkeypatch.setattr(
        doctor_mod, "shutil",
        types.SimpleNamespace(
            which=lambda n: "/usr/bin/apptainer" if "apptainer" in n else None))
    return types.SimpleNamespace(paths=paths, sif=sif)


def _apptainer_finding(verify_digest):
    found = [f for f in doctor_mod.collect_image_findings(
        verify_digest=verify_digest) if f.check.endswith(".apptainer")]
    assert len(found) == 1, found
    return found[0]


def test_strict_REFUSES_what_the_launcher_refuses(installed):
    """THE FIX, stated as the agreement that was missing.

    Both surfaces are driven against the same replaced file, so this fails if
    either one drifts — not just if doctor does.
    """
    prov.record_apptainer_sif("agent-claude", installed.sif)
    installed.sif.write_bytes(b"REPLACED OUT OF BAND" * 70_000)

    with pytest.raises(Refused):
        composition._verify_apptainer_sif_provenance(
            "agent-claude", installed.sif)

    finding = _apptainer_finding(verify_digest=True)

    assert finding.severity == "err", (
        f"the launcher REFUSES this image and doctor --strict reports "
        f"{finding.severity!r}: {finding.detail}")
    assert "does NOT match" in finding.detail, finding.detail


def test_the_error_names_BOTH_real_remedies(installed):
    """A refusal that names no command is the defect, not the user's problem.

    Rebuilding and deliberate-replacement are different situations with
    different answers, and naming only one sends half the readers in a circle.
    """
    prov.record_apptainer_sif("agent-claude", installed.sif)
    installed.sif.write_bytes(b"REPLACED" * 150_000)

    remediation = _apptainer_finding(verify_digest=True).remediation

    assert "botainer hpc build agent-claude --force" in remediation, remediation
    assert "botainer image forget agent-claude" in remediation, remediation


def test_strict_stays_OK_when_the_sif_is_intact(installed):
    """The control. Without it the check could report err unconditionally."""
    prov.record_apptainer_sif("agent-claude", installed.sif)

    finding = _apptainer_finding(verify_digest=True)

    assert finding.severity == "ok", finding.detail
    assert "matches" in finding.detail, finding.detail


def test_plain_doctor_DISCLOSES_that_it_did_not_compare(installed):
    """Cheap is fine; implying a check you skipped is not.

    Plain `doctor` deliberately does not hash a multi-GB file. It must say so,
    because a bare "✓ built" is read as "verified" — and for the replaced-image
    case that reading is wrong.
    """
    prov.record_apptainer_sif("agent-claude", installed.sif)
    installed.sif.write_bytes(b"REPLACED" * 150_000)

    finding = _apptainer_finding(verify_digest=False)

    assert finding.severity == "info", (
        f"plain doctor reported {finding.severity!r}; an image it did NOT "
        f"verify has not earned a tick, and must not fail the run either")
    assert "NOT checked here" in finding.detail, finding.detail
    assert "--strict" in finding.detail, (
        f"plain doctor does not tell the reader how to get the real answer: "
        f"{finding.detail}")


def test_an_UNRECORDED_image_says_start_will_not_verify_it(installed):
    """The launcher's documented fail-open case, surfaced instead of hidden.

    No marker means `start` proceeds without checking. That is deliberate — the
    lock is user-co-writable, so it is not a tamper-proof root of trust — but a
    plain tick would let a reader believe an unverifiable image was verified.
    """
    finding = _apptainer_finding(verify_digest=True)

    assert finding.severity == "info", (
        f"an image with no baseline reported {finding.severity!r}; `ok` would "
        f"read as verified and `warn` would be scenery on every honest install")
    assert "no recorded digest" in finding.detail, finding.detail
    assert "nothing will verify it" in finding.detail, finding.detail


def test_strict_actually_reaches_the_comparison(installed, monkeypatch):
    """Discriminates the two modes by OBSERVING the hash being computed.

    THE TZAR'S STANDING RULE, applied preemptively: a test that only asserts
    severities would pass if `verify_digest` were ignored and both modes
    hashed, or if neither did and the err came from somewhere else. Counting
    calls to `sha256_file` pins which mode does the expensive work — which is
    the whole design decision this fix rests on.
    """
    prov.record_apptainer_sif("agent-claude", installed.sif)

    calls = []
    real = prov.sha256_file
    monkeypatch.setattr(prov, "sha256_file",
                        lambda p: (calls.append(p), real(p))[1])

    _apptainer_finding(verify_digest=False)
    assert calls == [], (
        f"plain doctor hashed {len(calls)} file(s); it must not touch a "
        f"multi-GB .sif")

    _apptainer_finding(verify_digest=True)
    assert len(calls) == 1, (
        f"doctor --strict hashed {len(calls)} times, expected exactly 1 — the "
        f"comparison is not being reached, or is being done twice")


def test_the_STRICT_FLAG_IS_WIRED_to_the_check(installed):
    """Drives `botainer doctor --strict` itself, not the helper it calls.

    CAUGHT BY MUTATION BEFORE THIS FILE WAS COMMITTED, and it is the exact
    shape the loop tzar pre-declared a HALT for. Every other test here calls
    `collect_image_findings(verify_digest=...)` directly, so reverting the
    command's call site to `collect_image_findings()` — i.e. `--strict` never
    reaching the comparison, which IS the pre-fix behaviour — left all six of
    them green. They tested the helper; the defect was the wiring.

    This one asserts through the CLI, so the flag has to actually travel.
    """
    from click.testing import CliRunner

    prov.record_apptainer_sif("agent-claude", installed.sif)
    installed.sif.write_bytes(b"REPLACED OUT OF BAND" * 70_000)

    result = CliRunner().invoke(
        doctor_mod.doctor, ["--strict"], catch_exceptions=False)

    assert "does NOT match" in result.output, (
        f"`doctor --strict` did not report the mismatch the launcher refuses "
        f"on — the flag is not reaching the digest comparison.\n{result.output}")
    assert result.exit_code != 0, (
        f"`doctor --strict` exited 0 on an image `start` will REFUSE:\n"
        f"{result.output}")


def test_plain_doctor_does_not_fail_on_a_replaced_image(installed):
    """The other half of the wiring: plain mode must NOT hash, so it must not
    turn a casual `doctor` into a multi-GB read — and must still exit 0."""
    from click.testing import CliRunner

    prov.record_apptainer_sif("agent-claude", installed.sif)
    installed.sif.write_bytes(b"REPLACED OUT OF BAND" * 70_000)

    result = CliRunner().invoke(
        doctor_mod.doctor, [], catch_exceptions=False)

    assert "does NOT match" not in result.output, (
        "plain doctor performed the expensive comparison")
    assert "NOT checked here" in result.output, (
        f"plain doctor did not disclose that it skipped the check:\n"
        f"{result.output}")


def test_doctor_checks_the_file_the_LAUNCHER_WOULD_RESOLVE(installed):
    """The agreement must be about the same file, not the same variable.

    REFUTED VERSION OF THIS FILE: every other test hands `installed.sif` to
    BOTH surfaces, so reversing doctor's four-candidate list — making it
    resolve a different path than the launcher — left all eight green. The
    filename claims agreement; the assertions only checked that two functions
    given one path agree about that path.

    Pinning doctor's reported path against the launcher's own resolver is what
    makes the claim real.

    TWO CANDIDATES MUST EXIST or the test is inert. With only the canonical
    file present, reversing doctor's candidate order still finds that one file
    and the mutation passes — measured. Both naming conventions really do occur
    (the unprefixed name is the drift DN-036 tracks), so a fixture holding one
    of them is the "minimum that makes the code path run", not a real install.
    """
    prov.record_apptainer_sif("agent-claude", installed.sif)

    decoy = installed.sif.parent / "agent-claude.sif"
    decoy.write_bytes(b"THE OTHER NAMING CONVENTION" * 50_000)

    reported = _apptainer_finding(verify_digest=True).detail.split(" built")[0]
    resolved = composition._resolve_apptainer_sif_path("agent-claude")

    assert resolved is not None, "the launcher resolves no .sif for this fixture"
    assert reported == str(resolved), (
        f"doctor inspected {reported!r} but the launcher would exec "
        f"{str(resolved)!r} — doctor's verdict is about a different file")


def test_a_MALFORMED_marker_is_not_read_as_no_digest(installed):
    """Selection must not silently downgrade a broken marker to "unverifiable".

    `apptainer:sha256::/path` (empty hex) makes the launcher REFUSE. If doctor
    selected entries with `parse_apptainer_marker` instead of
    `is_apptainer_marker`, the falsy hex would read as "no marker recorded" and
    doctor would report an unverifiable image where the launcher refuses — the
    original disagreement, re-created by the fix meant to close it.
    """
    prov.record_apptainer_sif("agent-claude", installed.sif)
    lock = installed.paths.installed_lock_path
    entry = [e for e in prov.read_lock(lock) if e.name == "agent-claude"][0]
    lock.write_text(lock.read_text().replace(
        prov.parse_apptainer_marker(entry.image_digest), ""))

    with pytest.raises(Refused):
        composition._verify_apptainer_sif_provenance(
            "agent-claude", installed.sif)

    finding = _apptainer_finding(verify_digest=True)

    assert finding.severity == "err", (
        f"the launcher REFUSES a malformed marker and doctor reported "
        f"{finding.severity!r}: {finding.detail}")


def test_an_unreadable_sif_is_an_error_not_a_tick(installed):
    """A file doctor cannot read is not a file doctor can vouch for."""
    prov.record_apptainer_sif("agent-claude", installed.sif)
    installed.sif.chmod(0o000)
    try:
        finding = _apptainer_finding(verify_digest=True)
    finally:
        installed.sif.chmod(0o644)

    assert finding.severity == "err", finding.detail
    assert "unreadable" in finding.detail, finding.detail


def test_a_RENAMED_sif_whose_hash_MATCHES_is_a_fact_not_a_fault(installed):
    """THIS TEST ASSERTED THE OPPOSITE, AND THE OPPOSITE STOPPED BEING TRUE.

    It was written when `hpc-launcher`'s `_resolve_apptainer_image` took the
    recorded path ahead of any conventional filename: the launcher then went
    looking for a file that was gone while doctor hashed the renamed one and said
    "matches", i.e. doctor agreed with itself and not with the launcher. Both
    resolvers now consult the recorded path LAST, so all three launch paths run
    the file doctor hashed — and this state is the ordinary result of MOVING or
    COPYING `$MY_BOTAINER`, which is documented and supported. `cp -a` copies the
    bytes, so the file in the new root matches the record BY CONSTRUCTION.

    Calling that `err` cost a user a 10-20 minute rebuild of a multi-GiB image
    (doctor's first remedy) or `image forget`, which — measured by the refuting
    review that found this — permanently turns off the only integrity check the
    image has, for an install that was never broken.

    So: `info`, and it must still SAY the record names a path that is not there.
    Silence would be the other failure, and severity alone is not the claim.
    """
    prov.record_apptainer_sif("agent-claude", installed.sif)
    recorded_name = installed.sif.name
    renamed = installed.sif.parent / "agent-claude.sif"
    installed.sif.rename(renamed)

    finding = _apptainer_finding(verify_digest=True)

    assert finding.severity == "info", (
        f"a .sif whose hash MATCHES was reported as {finding.severity!r} — a "
        f"moved state root is not a fault: {finding.detail}")
    assert "matches what was recorded" in finding.detail, finding.detail
    assert recorded_name in finding.detail and str(renamed) in finding.detail, (
        f"the user is not told WHICH path the record names, nor which file is "
        f"actually being run: {finding.detail}")
    assert not finding.remediation, (
        f"a remedy was offered for a working install: {finding.remediation}")


def test_a_MISMATCH_still_names_the_recorded_path_when_it_differs(installed):
    """The error case keeps the information the info case gained.

    When the hash does NOT match, "the record is for <other>, and <this> was
    resolved instead" is the sentence that makes it actionable — a bare pair of
    truncated hashes tells a user nothing about which file to look at.
    """
    prov.record_apptainer_sif("agent-claude", installed.sif)
    recorded_name = installed.sif.name
    renamed = installed.sif.parent / "agent-claude.sif"
    installed.sif.rename(renamed)
    renamed.write_bytes(b"DIFFERENT-BYTES" * 1000)      # now the hash is wrong too

    finding = _apptainer_finding(verify_digest=True)

    assert finding.severity == "err", finding.detail
    assert "does NOT match" in finding.detail, finding.detail
    assert recorded_name in finding.detail and str(renamed) in finding.detail, (
        f"the mismatch does not say which file was resolved: {finding.detail}")
    assert "botainer image forget agent-claude" in finding.remediation


def test_BOTH_runtimes_are_reported_when_both_are_available(
        installed, monkeypatch):
    """A plugin buildable two ways must not get one verdict.

    The docker branch used to `continue`, so on a host with both runtimes
    doctor reported the docker image and never examined the .sif — while
    `start --runtime apptainer` would refuse that unexamined image. One
    plugin, two artefacts, two answers.
    """
    import subprocess as _sp

    prov.record_apptainer_sif("agent-claude", installed.sif)
    (installed.paths.root / "plugins" / "agent-claude" / "Dockerfile").write_text(
        "FROM scratch\n")

    monkeypatch.setattr(doctor_mod, "shutil",
                        types.SimpleNamespace(which=lambda n: "/usr/bin/" + n))
    monkeypatch.setattr(doctor_mod, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="sha256:deadbeef\n"),
        TimeoutExpired=_sp.TimeoutExpired))

    checks = {f.check for f in doctor_mod.collect_image_findings(
        verify_digest=True)}

    assert "image.agent-claude.docker" in checks, checks
    assert "image.agent-claude.apptainer" in checks, (
        f"the .sif was never examined on a host that has apptainer: {checks}")


def test_no_image_finding_PROMISES_a_launcher_will_refuse(installed):
    """doctor may report what it measured; it may not predict a refusal.

    THIS IS THE THIRD TIME. Two consecutive commits shipped a string asserting
    unconditionally what a launcher would do, and both were false for the same
    reason: a top-level `image:` in the project config is resolved AHEAD of the
    recorded digest — case 2 vs case 3 in the hpc-launcher, and an unverified
    branch entirely in `start`. That config is what the HPC guide documents.

    The loop tzar pre-declared a HALT for a third one, so the sentence now has
    exactly one owner and this test refuses any finding that promises a refusal
    without naming the condition.

    NARROWED 2026-09-12, DELIBERATELY, AND THIS IS THE ACKNOWLEDGEMENT. The
    sentence now DOES say the launchers refuse — because "who checks this digest"
    has no honest prediction-free answer, and a doctor finding that refuses to
    answer it sends the user to read the source. What the prohibition means now:

      * no UNCONDITIONAL promise. The exception must be in the sentence, which
        is what the `EXCEPT` and `top-level \\`image:\\`` assertions below hold.
      * the prediction must be PINNED by behaviour, not by prose.
        `tests/unit/test_doctor_digest_sentence_matches_the_policy.py` drives the
        resolver per resolution source, asserts which policy each selects, and
        asserts this sentence against what it just measured — so flipping the
        policy fails there until the sentence is rewritten.

    Both earlier falsehoods were unconditional and unpinned. That is the property
    that changed; the two greps I first wrote as the pin were evadable, which is
    why the pin is a test.
    """
    prov.record_apptainer_sif("agent-claude", installed.sif)
    installed.sif.write_bytes(b"REPLACED" * 150_000)

    detail = _apptainer_finding(verify_digest=True).detail

    assert "will REFUSE" not in detail, (
        f"doctor promises a refusal it cannot know will happen: {detail}")
    assert "top-level `image:`" in detail, (
        f"doctor names neither the check nor the condition that defeats it: "
        f"{detail}")
    assert "EXCEPT" in detail, (
        f"the prediction became unconditional again, which is the exact shape "
        f"that was false twice: {detail}")
    pin = (Path(__file__).parent
           / "test_doctor_digest_sentence_matches_the_policy.py")
    assert pin.exists(), (
        f"{pin.name} is gone. The prohibition in this test was narrowed to allow "
        f"a PINNED prediction; without the pin, the narrowing is unjustified and "
        f"doctor is back to promising what nothing checks.")


def test_the_who_refuses_sentence_is_on_the_finding_that_CLAIMS_a_refusal(
        installed):
    """One owner, and only where the claim belongs.

    This compared the RENDERED text of two findings, because two copies of the
    sentence is how two different falsehoods shipped. There is now one finding
    that carries it: the digest MISMATCH, which is the only one that says anything
    about who refuses. The renamed-but-matching case became `info` — no refusal is
    coming, so promising one there would be the third falsehood in this sentence's
    history.

    So the property is: exactly one call site, and the mismatch detail renders it
    in full.
    """
    import inspect as _inspect

    prov.record_apptainer_sif("agent-claude", installed.sif)
    installed.sif.write_bytes(b"REPLACED" * 150_000)
    mismatch_detail = _apptainer_finding(verify_digest=True).detail

    shared = doctor_mod._WHO_CHECKS_THE_DIGEST
    assert shared in mismatch_detail, mismatch_detail

    src = _inspect.getsource(doctor_mod._apptainer_image_finding)
    assert src.count("_WHO_CHECKS_THE_DIGEST") == 1, (
        "a second interpolation of the shared sentence appeared; either it now "
        "makes a claim on a finding that is not a refusal, or the constant is "
        "being copied again")

    prov.record_apptainer_sif("agent-claude", installed.sif)
    installed.sif.rename(installed.sif.parent / "agent-claude.sif")
    matching_detail = _apptainer_finding(verify_digest=True).detail
    assert shared not in matching_detail, (
        f"an info finding about a working install promises a refusal that is not "
        f"coming: {matching_detail}")
