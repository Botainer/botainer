"""Attribution must travel with the code it attributes.

botainer's own source is Apache-2.0, but every distribution carries the vendored noVNC
client under MPL-2.0 (`plugins/browser/gateway_web/novnc/`). There are THREE
paths that ship it — the sdist, the wheel (via `force-include` of `plugins/`),
and `tools/pkg/build-distrib.sh` — and none of them had a top-level attribution
file, while `pyproject.toml` declared the distribution "MIT" (the licence became
Apache-2.0 on; the MPL half is unchanged and is what this pins).

The invariant asserted here is conditional, not a checklist: *if* MPL-2.0 code
is bundled, *then* each release path must carry the notice and the license text.
Drop the noVNC tree and these tests go quiet on their own.

Note what is NOT claimed: this cannot verify that the attribution is
*complete* — a newly vendored library nobody mentioned would pass. It covers
the one bundled dependency that exists, and the wiring that makes attribution
reach a recipient.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NOVNC = REPO / "plugins" / "browser" / "gateway_web" / "novnc"
NOTICE = REPO / "THIRD-PARTY-LICENSES.md"
MPL_TEXT = REPO / "licenses" / "MPL-2.0.txt"

mpl_code_is_bundled = pytest.mark.skipif(
    not NOVNC.is_dir(),
    reason="no vendored noVNC tree — nothing to attribute",
)


@mpl_code_is_bundled
def test_bundled_mpl_code_has_a_notice_and_the_license_text() -> None:
    assert NOTICE.is_file(), (
        f"{NOVNC.relative_to(REPO)} ships MPL-2.0 code; THIRD-PARTY-LICENSES.md "
        f"is the notice that has to go with it")
    body = NOTICE.read_text(encoding="utf-8")
    assert "noVNC" in body and "MPL-2.0" in body, (
        "THIRD-PARTY-LICENSES.md must name noVNC and its license")
    assert "pako" in body, (
        "vendor/pako ships under its own MIT license, not noVNC's MPL — say so")

    assert MPL_TEXT.is_file(), "licenses/MPL-2.0.txt (the full license text) is missing"
    head = MPL_TEXT.read_text(encoding="utf-8")[:200]
    assert "Mozilla Public License Version 2.0" in head, (
        f"{MPL_TEXT} does not look like the MPL-2.0 text")


@mpl_code_is_bundled
def test_project_license_does_not_claim_the_bundle_is_plain_mit() -> None:
    body = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"^license = (.+)$", body, re.M)
    assert m, "no license field in pyproject.toml"
    declared = m.group(1)
    assert "MPL-2.0" in declared, (
        f"pyproject declares license={declared.strip()} but the sdist and the "
        f"wheel both carry MPL-2.0 code. PEP 639 spells this `Apache-2.0 AND MPL-2.0`.")


@mpl_code_is_bundled
def test_every_release_path_that_ships_plugins_also_ships_the_attribution() -> None:
    """The three release paths are configured independently — check each."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    build_sh = (REPO / "tools" / "pkg" / "build-distrib.sh").read_text(encoding="utf-8")

    sdist = re.search(r"\[tool\.hatch\.build\.targets\.sdist\].*?^include = \[(.*?)^\]",
                      pyproject, re.S | re.M)
    assert sdist, "sdist include list not found"
    assert "THIRD-PARTY-LICENSES.md" in sdist.group(1), "sdist omits the notice"
    assert "licenses/" in sdist.group(1), "sdist omits licenses/ (the MPL text)"

    wheel = re.search(r"\[tool\.hatch\.build\.targets\.wheel\.force-include\](.*?)(?=^\[)",
                      pyproject, re.S | re.M)
    assert wheel, "wheel force-include block not found"
    assert '"plugins"' in wheel.group(1), (
        "guard assumption: this test exists because the wheel force-includes plugins/")
    assert "THIRD-PARTY-LICENSES.md" in wheel.group(1), (
        "the wheel force-includes plugins/ — and so ships MPL-2.0 code — but not "
        "the notice; a `pip install botainer` would get the code without it")
    assert '"licenses"' in wheel.group(1), "wheel omits licenses/ (the MPL text)"

    top = re.search(r"TOP_FILES=\((.*?)\)", build_sh, re.S)
    assert top and "THIRD-PARTY-LICENSES.md" in top.group(1), (
        "build-distrib.sh's tarball omits the notice")
    dirs = re.search(r"DIST_DIRS=\((.*?)\)", build_sh, re.S)
    assert dirs and '"licenses"' in dirs.group(1), (
        "build-distrib.sh's tarball omits licenses/ (the MPL text)")


@mpl_code_is_bundled
def test_notice_does_not_claim_botainer_modified_the_vendored_tree() -> None:
    """MPL-2.0 §3 obligations differ for modified files.

    PROVENANCE.md's whole procedure is "re-vendor, never edit", and the notice
    says the tree is unmodified. If that ever stops being true, the notice is
    wrong in a way that matters legally — so tie the claim to the file that
    states the procedure.
    """
    prov = (NOVNC.parent / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "Do not edit files under" in prov, (
        "PROVENANCE.md no longer states the do-not-edit rule that "
        "THIRD-PARTY-LICENSES.md's 'Modified by botainer? No' relies on")
