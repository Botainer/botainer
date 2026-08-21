"""A shipped file may not name a path in the private half of the repo.

The rule was written down long before anything checked it. The DN- index gate
(tools/dev/check-internal-design-index.sh) enforces two directions — a row
pointing at a missing document, and a DN- cited with no row — and its own header
opens by saying shipped code "may not name a `design/`, `private/` or `handoff/`
PATH". It never checked that. Ninety-three such citations shipped, to forty
distinct documents, one of which (`design/HPC-ONBOARDING-PLAN.md`) had already
moved and resolved to nothing even for someone holding the private tree.

Three separate problems, which is why the id scheme exists:

  * A public reader cannot follow it. It looks like a link and is a dead end.
  * It rots. Files move; ids do not.
  * The filename itself discloses. `handoff/POSTMORTEM-umbrella-bind.md` tells
    a reader there was an umbrella-bind incident before they have read a word
    about it.

This check lives in tests/ rather than tools/dev/ deliberately: tools/dev/ does
not ship, so a guard there protects the private repo and abandons the exported
one — the weaker-of-the-pair defect this project keeps rediscovering. Here it
runs in both.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path

import sys

import pytest

# pyproject declares `requires-python = ">=3.10"` and the PACKAGE honours it —
# only this file needs `tomllib`, which arrived in 3.11. Skipping on 3.10 keeps
# the declared floor genuinely runnable instead of quietly requiring 3.11 of
# everyone. If the floor moves to 3.11, delete this.
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="tomllib is 3.11+; botainer itself supports 3.10",
)


REPO = Path(__file__).resolve().parents[2]

# Top-level directories that exist only in the maintainer's tree. In the
# exported repo they are simply absent, which is what makes a citation of one
# a dead pointer rather than a private-but-followable one.
PRIVATE_ROOTS = ("private", "handoff", "design", "strategy")

# `foo/bar` where foo is a private root. The negative lookbehind is load-bearing
# twice over: it lets macOS's real `/private/etc`, `/private/var` and
# `/private/tmp` through (the wolfram sandbox profile and state/dir.py both need
# them), and it stops `tests/private` from matching here — that one is caught by
# its own pattern below, with its own message.
CITATION = re.compile(
    r"(?<![/\w.-])(" + "|".join(PRIVATE_ROOTS) + r")/[A-Za-z0-9_][A-Za-z0-9_/.-]*"
)
TESTS_PRIVATE = re.compile(r"(?<![/\w.-])tests/private/")

# A private document named WITHOUT a directory. `CITATION` above cannot see
# these — `FOLLOWUPS.md:` at the head of a docstring has no slash — and seven of
# them shipped for exactly that reason, including one that opened a test's
# docstring with the name of a tracker no reader can open.
#
# Each entry must be distinctive enough that the English word does not match:
# `HANDOFF-` keeps its hyphen because the browser plugin legitimately says
# "credential HANDOFF", and `BACKLOG` is deliberately absent for the same
# reason — an ordinary word cannot be a reliable marker.
PRIVATE_DOC_NAMES = [
    r"FOLLOWUPS(\.md)?\b",
    r"POSTMORTEM-[A-Za-z0-9_-]+",
    r"HANDOFF-[A-Za-z0-9_.-]+",
    r"SESSION-HANDOFF[A-Za-z0-9_.*-]*",
    r"ACKNOWLEDGED-RISKS(\.md)?\b",
    r"TESTING-AND-INTEGRATION(\.md)?\b",
    r"TRACK-B-BUILD-SPEC(\.md)?\b",
]
PRIVATE_DOC = re.compile(r"(?<![\w/-])(" + "|".join(PRIVATE_DOC_NAMES) + ")")

# Files whose JOB is to name these directories, because they are the rules that
# keep them out. Naming the thing you exclude is not a dangling pointer.
# Enumerated, never a glob: a pattern here would silently cover the next file
# that should have been caught.
DECLARES_THE_EXCLUSION = {
    ".gitignore",
    "pyproject.toml",
    "tools/pkg/build-distrib.sh",
    "tests/unit/test_dist_excludes_agree.py",
    "tests/private/test_release_paths_agree.py",
    "tests/integration/test_no_private_paths_in_shipped_files.py",
}

# WHICH FILES COUNT AS SHIPPED IS DERIVED, NOT LISTED.
#
# A hand-kept skip list here would be a second ship-list, and a second ship-list
# drifts from the first — the exact defect the public-export selector was
# rewritten to remove. pyproject.toml's sdist include/exclude is the one
# authority, and pyproject ships, so this works unchanged in the exported tree.
_ALWAYS_SKIP = {".git", ".pytest_cache", ".venv", ".mypy_cache", ".ruff_cache",
                "node_modules", "build", "dist", "__pycache__",
                # vendored upstream; not ours to annotate
                "novnc"}
_BINARY = {".pyc", ".sif", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff",
           ".woff2", ".gz", ".zip", ".pdf", ".so"}


def _matches(path: str, patterns: list[str]) -> bool:
    """Hatch-ish glob match; a bare directory name covers its whole subtree."""
    for pat in patterns:
        if path == pat or fnmatch.fnmatch(path, pat):
            return True
        base = pat.rstrip("/*")
        if base and (path == base or path.startswith(base + "/")):
            return True
    return False


def _ship_rules() -> tuple[list[str], list[str]]:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    sdist = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]
    include, exclude = list(sdist.get("include", [])), list(sdist.get("exclude", []))
    assert include, "pyproject's sdist include list is empty — refusing to guess"
    return include, exclude


def _candidate_files() -> list[Path]:
    """Every file the sdist would ship, read from pyproject rather than listed."""
    include, exclude = _ship_rules()
    out = []
    for p in REPO.rglob("*"):
        if not p.is_file() or p.suffix in _BINARY:
            continue
        rel = p.relative_to(REPO).as_posix()
        if any(part in _ALWAYS_SKIP for part in Path(rel).parts):
            continue
        if not _matches(rel, include) or _matches(rel, exclude):
            continue
        out.append(p)
    return out


def _scan(pattern: re.Pattern[str]) -> list[str]:
    hits = []
    for p in _candidate_files():
        rel = p.relative_to(REPO).as_posix()
        if rel in DECLARES_THE_EXCLUSION:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in pattern.finditer(line):
                hits.append(f"{rel}:{n}: {m.group(0)}")
    return hits


def test_no_private_tree_paths_in_shipped_files():
    hits = _scan(CITATION)
    assert not hits, (
        f"{len(hits)} shipped citation(s) name a path in the private half of "
        "the repo:\n  " + "\n  ".join(hits[:25])
        + ("\n  ..." if len(hits) > 25 else "")
        + "\n\nUse a stable id instead — `internal design note DN-0nn` — and add "
        "the row to tools/dev/internal-design-index.tsv. Inlining the substance "
        "beside the id is still required: the id says where the long form lives, "
        "it does not license a comment that explains nothing."
    )


def test_no_tests_private_paths_in_shipped_files():
    hits = _scan(TESTS_PRIVATE)
    assert not hits, (
        f"{len(hits)} shipped citation(s) name tests/private/, which no "
        "distribution ships:\n  " + "\n  ".join(hits[:25])
        + "\n\nSay what it is ('a maintainer-side suite that does not ship') "
        "rather than where it is."
    )


def test_no_private_doc_names_in_shipped_files():
    hits = _scan(PRIVATE_DOC)
    assert not hits, (
        f"{len(hits)} shipped citation(s) name a private document by bare "
        "filename:\n  " + "\n  ".join(hits[:25])
        + ("\n  ..." if len(hits) > 25 else "")
        + "\n\nA name with no directory is still a dead pointer, and it slips "
        "the path check above. Use a DN- id, or state the fact without naming "
        "the document."
    )


def test_the_scan_actually_reaches_the_shipped_tree():
    """Anti-vacuous: a walk that finds nothing would pass both tests above.

    The first version of the personal-info scanner passed while deriving no
    identity at all. A guard that can succeed by looking at nothing is not a
    guard.
    """
    files = {p.relative_to(REPO).as_posix() for p in _candidate_files()}
    for expected in ("botainer/core/composition.py", "README.md",
                     "docs/CAPABILITY-SURFACE.md"):
        assert expected in files, f"the walk never reached {expected}"
    assert len(files) > 300, f"only {len(files)} files walked; expected the tree"


@pytest.mark.parametrize("planted,pattern", [
    ("see handoff/POSTMORTEM-umbrella-bind.md for why", CITATION),
    ("per private/DESIGN-whatever.md §4", CITATION),
    ("moved to tests/private/test_baseline_ratchets.py", TESTS_PRIVATE),
    ("FOLLOWUPS.md: a state dir that resolves onto scratch", PRIVATE_DOC),
    ("known gaps documented in HANDOFF-2026-05-19.md", PRIVATE_DOC),
    ("Step B (#216, ACKNOWLEDGED-RISKS #1)", PRIVATE_DOC),
])
def test_patterns_catch_what_they_claim_to(planted, pattern):
    assert pattern.search(planted), f"pattern missed {planted!r}"


@pytest.mark.parametrize("benign", [
    '(literal "/private/etc/master.passwd")',      # macOS, wolfram sandbox
    'Path("/private/tmp").resolve()',              # macOS, state/dir.py
    "the design of the cage is capability-based",  # the English word
    "a well-designed/robust interface",
])
def test_patterns_do_not_fire_on_real_paths_or_prose(benign):
    assert not CITATION.search(benign), f"false positive on {benign!r}"


@pytest.mark.parametrize("benign", [
    "credential HANDOFF: the browser mcp reads a Playwright session",  # the word
    "CREDENTIAL HANDOFF (no viewer, no password",
    "the backlog of pending requests",
])
def test_doc_name_pattern_does_not_fire_on_ordinary_words(benign):
    assert not PRIVATE_DOC.search(benign), f"false positive on {benign!r}"
