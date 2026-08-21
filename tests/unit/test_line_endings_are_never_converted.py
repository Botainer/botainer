"""Published sha256 digests must survive a checkout on any platform.

Several facts this project publishes are digests of files in this tree:

    CLA.md                     quoted in CONTRIBUTING.md as the thing a
                               contributor signs — the only link between an
                               acceptance and specific text
    LICENSE                    pyproject.toml says the Apache-2.0 text is
                               verbatim "so anyone can confirm it was not
                               tampered with"
    plugins/<name>/            hashed per plugin in trusted_plugins.lock and
                               verified at load time

A digest is a claim about BYTES. Git's `core.autocrlf` rewrites LF to CRLF on
checkout and is the installer default on Windows, so without an explicit
attribute every one of those digests is wrong on a Windows clone — while
everyone involved did exactly the right thing. Measured before `.gitattributes`
existed, cloning with `core.autocrlf=true`:

    CLA.md      0dd37a0a… -> 0b9ece2b…
    LICENSE     cfc7749b… -> 3ddf9be5…
    plugins     14 of 14 trust-lock entries mismatched, so botainer would
                refuse to load its own bundled plugins

`* -text` in `.gitattributes` makes working-tree bytes equal committed bytes
everywhere, which turns the whole class from "detect and warn" into "cannot
happen". These tests exist so that guarantee cannot be silently withdrawn.

WHY THESE ASK GIT RATHER THAN READ THE FILE. Grepping `.gitattributes` for a
pattern proves the line is present, not that it takes effect: attributes are
resolved from a stack (repo file, nested directories, $GIT_DIR/info/attributes,
core.attributesFile) and a later rule can override an earlier one. `git
check-attr` reports the resolution git will actually apply, which is the
property we need. Presence is not effect.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Files whose bytes are the subject of a published claim. Adding a new digest
# claim anywhere should add a row here.
HASHED_PATHS = ["CLA.md", "LICENSE"]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout


def _is_git_checkout() -> bool:
    try:
        _git("rev-parse", "--git-dir")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


requires_git = pytest.mark.skipif(
    not _is_git_checkout(),
    reason="not a git checkout (e.g. an unpacked sdist) — nothing to resolve",
)


@requires_git
@pytest.mark.parametrize("path", HASHED_PATHS)
def test_git_will_not_convert_line_endings_for_a_hashed_file(path: str) -> None:
    """git's own resolution, not a grep of .gitattributes."""
    out = _git("check-attr", "text", "--", path).strip()
    # Format: "<path>: text: <value>". `-text` resolves to "unset".
    value = out.rsplit(":", 1)[-1].strip()
    assert value == "unset", (
        f"git resolves `text` to {value!r} for {path}, not 'unset'.\n\n"
        f"  {out}\n\n"
        "That means git may rewrite this file's line endings on checkout, and "
        "its sha256 then differs from the digest this project publishes — on "
        "Windows, by default, with nobody at fault. Restore `* -text` in "
        ".gitattributes (see the comment there for what each digest costs)."
    )


@requires_git
def test_the_plugin_tree_is_covered_too() -> None:
    """The trust lock hashes plugin sources; a CRLF checkout failed all 14."""
    sample = sorted(p for p in (REPO / "plugins").glob("*/botainer-plugin.yaml"))
    assert sample, "no bundled plugin manifests found — has plugins/ moved?"
    for manifest in sample[:5]:
        rel = manifest.relative_to(REPO).as_posix()
        value = _git("check-attr", "text", "--", rel).strip().rsplit(":", 1)[-1].strip()
        assert value == "unset", (
            f"git may convert line endings for {rel} (text={value!r}). Every "
            "trust-lock entry is a hash of these bytes; converting them makes "
            "botainer reject its own bundled plugins."
        )


def test_no_hashed_file_contains_crlf_today() -> None:
    """The committed bytes must be the LF form the published digests describe.

    Complements the attribute check: `-text` preserves whatever is committed,
    so it guarantees consistency, not correctness. If a CRLF file were ever
    committed, `-text` would faithfully preserve CRLF everywhere and the
    published digest would be wrong on every platform instead of just Windows.
    """
    for rel in HASHED_PATHS:
        data = (REPO / rel).read_bytes()
        assert b"\r\n" not in data, (
            f"{rel} contains CRLF. Published digests describe the LF form; "
            "committing CRLF makes them wrong everywhere. Convert it back and "
            "re-check the digest quoted in CONTRIBUTING.md / pyproject.toml."
        )


def test_the_license_digest_claimed_in_pyproject_is_correct() -> None:
    """pyproject invites the reader to verify LICENSE — so verify it here.

    An invitation to check a digest is worse than no digest at all if the
    digest is stale: it looks like proof. Nothing pinned this claim before.
    """
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    # The claim lives in a comment and WRAPS, so "sha256" and the digest are on
    # separate lines with a `# ` between them. `\s+` alone found nothing and the
    # test reported the claim missing rather than checking it.
    m = re.search(r"sha256[\s#]*([0-9a-f]{8,})", pyproject)
    assert m, (
        "pyproject.toml no longer states a sha256 for LICENSE. If the claim "
        "was removed deliberately, remove this test in the same commit; if it "
        "was reworded, re-pin it. An unpinned digest claim drifts."
    )
    claimed = m.group(1)
    actual = hashlib.sha256((REPO / "LICENSE").read_bytes()).hexdigest()
    assert actual.startswith(claimed), (
        f"pyproject.toml claims LICENSE is sha256 {claimed}…, but it hashes to "
        f"{actual[:len(claimed)]}….\n\n"
        "pyproject tells readers this digest lets them confirm the Apache-2.0 "
        "text was not tampered with. A wrong one tells them it was."
    )
