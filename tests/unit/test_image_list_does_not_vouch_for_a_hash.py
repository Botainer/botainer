"""`image list` printed a recorded sha256 beside a ✓, having compared nothing.

THE DEFECT, as observed:

    ✓ …/botainer-agent-claude.sif (1 MB)  (sha256=10901c54ec1e9952…)

The ✓ is `sif_path.exists()`. The digest is read out of `installed.lock`. The
two were never compared, so the ONE surface that shows a hash was the one place
a mismatch could not be seen — a green tick and a hash, side by side, saying
nothing about each other while implying everything.

This is the same defect `botainer doctor` had, fixed the same way and with the
same reasoning: hashing is not free (a real agent .sif is 3-5 GB and
`image list` is run casually), so the check is opt-in — and the surface that
SKIPS it must say so rather than printing a number that looks like a verdict.
`--verify` calls `doctor`'s `_apptainer_image_finding`, the single owner of the
comparison, rather than carrying a second implementation that would answer
differently the first time either moved.
"""
from __future__ import annotations

import hashlib

import pytest
from click.testing import CliRunner

from botainer.cli import image as image_mod


def _run(args):
    return CliRunner().invoke(image_mod.image, args, catch_exceptions=False)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A plugin with a .def, a built .sif, and a recorded digest.

    Shaped like a real install: the plugin dir carries the `.def` that makes
    `image list` consider apptainer at all, and the lock is written through the
    real recorder so the marker format cannot drift from what the product
    writes.
    """
    root = tmp_path / "st"
    (root / "plugins" / "agent-claude").mkdir(parents=True)
    (root / "plugins" / "agent-claude" / "agent-claude.def").write_text("Bootstrap: docker\n")
    (root / "plugins" / "agent-claude" / "botainer-plugin.yaml").write_text(
        "name: agent-claude\nversion: 0.1.0\ntier: first-party\n")
    images = root / "images"
    images.mkdir(parents=True)
    # `botainer-<plugin>.sif`, via the SAME convention the product uses. My
    # first fixture wrote `agent-claude.sif` and the whole --verify branch
    # silently never ran, because `sif_path.exists()` was False — the test
    # would have "passed" the default-line case while proving nothing about
    # verification. The naming is `state/dir.apptainer_sif_path`'s, and DN-036
    # exists because two builders once disagreed about it.
    sif = images / "botainer-agent-claude.sif"
    sif.write_bytes(b"SIF-CONTENT-v1" * 1000)
    monkeypatch.setenv("MY_BOTAINER", str(root))
    return root, sif


def _record(root, sif, *, digest_hex):
    """Record `digest_hex` for `sif` THROUGH THE REAL RECORDER.

    `record_image_digest` is what `hpc build` and `image build` call, so the
    marker format here cannot drift from what the product actually writes —
    which is the whole reason the verify side has one shape to parse.
    """
    from botainer.plugins import provenance as prov
    from botainer.state import dir as sd
    sd.ensure_user_state_dir(create_if_missing=True)
    prov.record_image_digest(
        "agent-claude", prov.apptainer_marker(sif, digest_hex))


def _actual(sif):
    return hashlib.sha256(sif.read_bytes()).hexdigest()


def test_the_default_line_does_not_imply_the_hash_was_checked(installed):
    """THE DEFECT. A bare `sha256=…` beside a ✓ reads as a verification."""
    root, sif = installed
    _record(root, sif, digest_hex=_actual(sif))

    result = _run(["list"])

    assert "NOT compared" in result.output, (
        f"the line shows a digest without saying the file was not hashed:\n"
        f"{result.output}")


def test_a_MISMATCHED_digest_is_reported_under_verify(installed):
    """The whole point. A replaced .sif must not read as healthy."""
    root, sif = installed
    _record(root, sif, digest_hex="0" * 64)

    result = _run(["list", "--verify"])

    assert "does NOT match" in result.output, (
        f"`--verify` did not report a digest mismatch:\n{result.output}")

    # THE LINE FOR THIS PLUGIN, not "the last apptainer line". My first version
    # used `.split("[apptainer]")[-1]`, which picked agent-codex — a plugin with
    # no .sif and therefore no tick — so the assertion passed no matter what
    # agent-claude's line said. Found by mutation: leaving the ✓ on a mismatched
    # image kept all five tests green.
    line = next(ln for ln in result.output.splitlines()
                if "[apptainer]" in ln and "agent-claude" in ln)
    assert "✓" not in line, (
        f"the mismatched image still carries a tick:\n{line}")


def test_a_MATCHING_digest_is_confirmed_under_verify(installed):
    """THE CONTROL, and it is mutation-proven.

    Without it, `--verify` could report a mismatch unconditionally and the
    test above would still pass — an alarm that always fires is the scenery
    this project has a rule about, and it would make the real one unreadable.
    """
    root, sif = installed
    _record(root, sif, digest_hex=_actual(sif))

    result = _run(["list", "--verify"])

    assert "matches what was recorded" in result.output, result.output
    assert "does NOT match" not in result.output, (
        f"claimed a mismatch for a file whose digest is correct:\n"
        f"{result.output}")


def test_an_image_that_EXISTS_with_no_digest_says_nothing_can_verify_it(installed):
    """The documented cluster route, and the state my first fix got wrong.

    Build the `.sif` on a workstation, copy it to a login node that cannot run
    `apptainer build` (GETTING_STARTED-HPC says to do exactly this) and botainer
    never records a marker. My first version printed `✓` here — so the one
    verification affordance in the product reassured the user about an image
    nothing has ever checked, which is the defect this file is named after,
    one state over. `doctor` renders the same state as `·`.
    """
    root, sif = installed

    result = _run(["list", "--verify"])
    line = next(ln for ln in result.output.splitlines()
                if "[apptainer]" in ln and "agent-claude" in ln)

    assert "✓" not in line, (
        f"a tick for an image with NO recorded digest — nothing can verify "
        f"it:\n{line}")
    assert "NOTHING can verify this file" in line, line


def test_the_swap_WARNING_is_not_printed_about_an_image_that_is_absent(installed):
    """A present-tense hazard about a file that is not there is scenery.

    My first version printed "nothing would detect a swap" for every plugin
    with a `.def` and no `.sif` — 2 of 4 lines on a fresh `setup`, and every
    line on a docker-only host. Found by a reviewer, in the line I had just
    written to stop a different false impression.
    """
    result = _run(["list"])
    codex = next(ln for ln in result.output.splitlines()
                 if "[apptainer]" in ln and "agent-codex" in ln)

    assert "would detect a swap" not in codex, (
        f"warned about swapping an image that does not exist:\n{codex}")
    assert "not built" in codex, codex


def test_verify_SAYS_SO_when_there_is_nothing_to_hash(installed):
    """`--verify` must never be silently inert.

    With a digest recorded but the `.sif` absent, the line was byte-identical
    with and without the flag: the user asked for a comparison, got none, and
    had no way to tell. Measured by a reviewer with `diff`.
    """
    root, sif = installed
    _record(root, sif, digest_hex=_actual(sif))
    sif.unlink()

    plain = _run(["list"]).output
    verified = _run(["list", "--verify"]).output

    assert plain != verified, (
        "`--verify` produced byte-identical output — the flag was inert and "
        "said nothing about being inert")
    assert "no botainer-agent-claude.sif here to hash" in verified, verified


def test_the_DOCKER_line_says_its_digest_is_never_compared(installed):
    """THE SECOND HASH-BESIDE-A-TICK SURFACE, which I claimed did not exist.

    The row this fix came from said "the one surface that shows a hash". There
    are two, on adjacent lines of one command. The docker line prints
    `✓ id=sha256:…  (recorded=sha256:…)` — two hashes, no comparison, ever:
    `docs/CAPABILITY-SURFACE.md` §4bh already states that docker has no
    image-identity check at all. Once the apptainer line below began saying
    "NOT compared", a reader could only infer that this one WAS.

    `--verify` cannot help here; there is nothing to call. So the line says it.
    """
    root, sif = installed
    from botainer.plugins import provenance as prov
    from botainer.state import dir as sd
    sd.ensure_user_state_dir(create_if_missing=True)
    prov.record_image_digest("agent-claude", "sha256:" + "a" * 64)

    result = _run(["list"])
    line = next(ln for ln in result.output.splitlines()
                if "[docker]" in ln and "agent-claude" in ln)

    assert "NEVER compared" in line, (
        f"the docker line shows a recorded digest and implies, by contrast "
        f"with the apptainer line, that it was checked:\n{line}")


def test_an_UNPARSEABLE_marker_does_not_crash_the_listing(installed):
    """A corrupted lock must produce a sentence, not a traceback.

    `apptainer:sha256` with no digest and no path raised IndexError and put a
    Python traceback at the user with exit 1. This is a listing; an
    unparseable record is a fact to report.
    """
    root, sif = installed
    from botainer.plugins import provenance as prov
    from botainer.state import dir as sd
    sd.ensure_user_state_dir(create_if_missing=True)
    prov.record_image_digest("agent-claude", "apptainer:sha256")

    result = _run(["list"])

    assert result.exit_code == 0, result.output
    assert "UNPARSEABLE" in result.output, result.output
    assert "Traceback" not in result.output, result.output


def test_verify_reuses_doctors_comparison_rather_than_copying_it(installed):
    """STRUCTURAL, and the reason is three copies of a scan earlier tonight.

    If `image list` grows its own hash-and-compare, it and `doctor --strict`
    will answer differently the first time either is touched — which is exactly
    how `auth status` and `auth profiles` came to disagree about whether a
    credential existed. Pinned by AST so a reformat cannot fool it, and so the
    failure names the drift rather than some downstream symptom.
    """
    import ast
    from pathlib import Path

    src = Path(image_mod.__file__).read_text()
    tree = ast.parse(src)
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute)
        else getattr(node.func, "id", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "_apptainer_image_finding" in called, (
        "image list --verify no longer calls doctor's comparison. If that is "
        "deliberate, the two surfaces now need their own agreement test — see "
        "tests/unit/test_broker_state_scans_agree.py for the shape.")
    assert "sha256_file" not in called, (
        "image.py hashes the .sif itself. That is the second implementation "
        "this test exists to prevent.")


def test_image_list_and_doctor_AGREE_about_whether_a_sif_exists(installed):
    """They shared the digest COMPARISON and not the RESOLVER, so they disagreed
    about the prior question — whether there is a file at all.

    Measured by a reviewer on one state root at one moment, with the legacy
    `<plugin>.sif` name present and the canonical one absent:

        image list --verify   ✗ no botainer-agent-claude.sif …          exit 0
        doctor --strict       ✗ the image recorded at build time was
                                …/botainer-agent-claude.sif, which no longer
                                exists (…/agent-claude.sif is there instead)
                                                                        exit 1

    "not built" was false about a file the user could `ls`. `image list` looked
    only at the canonical path; `doctor` walked four candidates, accepting the
    DN-036 naming drift. Both now call `Paths.find_apptainer_sif`.

    This is the fourth instance of share-the-check-but-not-the-lookup in this
    subsystem, which is why the resolver moved to one owner rather than being
    copied a second time.
    """
    root, sif = installed
    legacy = sif.parent / "agent-claude.sif"       # the pre-DN-036 spelling
    sif.rename(legacy)
    assert not sif.exists(), "precondition: the canonical name is gone"

    from botainer.state import dir as sd
    paths = sd.ensure_user_state_dir(create_if_missing=True)
    resolved = paths.find_apptainer_sif("agent-claude")
    assert resolved == legacy, (
        f"the shared resolver did not find the legacy name: {resolved}")

    result = _run(["list"])
    line = next(ln for ln in result.output.splitlines()
                if "[apptainer]" in ln and "agent-claude" in ln)

    assert "agent-claude.sif" in line and "no " not in line.split("(")[0], (
        f"`image list` still reports an image it can see as absent:\n{line}")
    assert str(legacy) in line, (
        f"it does not name the file it actually found, so a user cannot tell "
        f"which spelling is on disk:\n{line}")


def test_when_BOTH_spellings_exist_the_canonical_one_wins(installed):
    """ORDER IS PART OF THE CONTRACT, and a mutation showed it was unprotected.

    Swapping the resolver's candidate order so the legacy `<plugin>.sif` is
    tried first passed every other test here. It must not: a correct
    `botainer image build` / `hpc build` writes the PREFIXED name, and that is
    the path the launcher's provenance marker records. Preferring the legacy
    file would make `--verify` hash one file while the launcher runs another —
    reintroducing the mismatch this whole area is about, one level down.
    """
    root, sif = installed
    legacy = sif.parent / "agent-claude.sif"
    legacy.write_bytes(b"LEGACY-DIFFERENT-CONTENT" * 100)
    assert sif.exists() and legacy.exists(), "precondition: both are present"

    from botainer.state import dir as sd
    paths = sd.ensure_user_state_dir(create_if_missing=True)

    assert paths.find_apptainer_sif("agent-claude") == sif, (
        f"the legacy spelling won over the canonical one. A correct build "
        f"writes {sif.name}, and that is the path the provenance marker "
        f"records — resolving to {legacy.name} would hash a file the launcher "
        f"does not run.")
