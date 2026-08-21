"""Distribution audit: the two release paths must not disagree.

botainer has TWO ways to produce a distribution:
  1. the wheel/sdist, governed by pyproject.toml's
     [tool.hatch.build.targets.sdist] `exclude` list;
  2. tools/pkg/build-distrib.sh, which copies a directory tree.

They drifted. `build-distrib.sh` did `cp -a docs` (the whole tree) and explicitly
copied AGENTS.md, while pyproject excludes `docs/PROTOCOLS/`,
`docs/REVIEW-PROTOCOL.md` and `AGENTS.md` as internal Claude-session process docs
— documents addressed to "every future Claude session" with dangling references
into private/, handoff/ and tools/dev/. Verified in the existing staging dir:
dist/0.1.0a1/ contained all three, and the leak check printed a pass because it
only tested TOP-LEVEL names.

Publishing is irreversible, so this asserts the invariant rather than the symptom:
anything the sdist excludes must also be refused by the script's leak check.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _sdist_excludes() -> set[str]:
    body = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"\[tool\.hatch\.build\.targets\.sdist\].*?^exclude = \[(.*?)^\]",
                      body, re.S | re.M)
    assert block, "could not find the sdist exclude list in pyproject.toml"
    # strip comment lines first — the block carries an explanatory comment that
    # itself contains a quoted phrase
    body_lines = [ln.split("#", 1)[0] for ln in block.group(1).splitlines()]
    items = re.findall(r'"([^"]+)"', "\n".join(body_lines))
    # normalise: drop globs (handled structurally) and trailing slashes
    out = set()
    for it in items:
        if "*" in it:
            continue
        out.add(it.rstrip("/"))
    return out


def _leakcheck_forbidden() -> set[str]:
    body = (REPO / "tools" / "pkg" / "build-distrib.sh").read_text(encoding="utf-8")
    m = re.search(r"for forbidden in (.*?); do", body, re.S)
    assert m, "could not find the leak-check list in build-distrib.sh"
    raw = m.group(1).replace("\\\n", " ")
    return {tok.rstrip("/") for tok in raw.split() if tok and tok != "\\"}


def test_leak_check_covers_everything_the_sdist_excludes() -> None:
    missing = _sdist_excludes() - _leakcheck_forbidden()
    assert not missing, (
        f"build-distrib.sh's leak check does not refuse {sorted(missing)}, which "
        f"pyproject.toml excludes from the sdist. The two release paths must not "
        f"disagree — publishing is irreversible.")


def test_build_script_does_not_copy_internal_process_docs() -> None:
    body = (REPO / "tools" / "pkg" / "build-distrib.sh").read_text(encoding="utf-8")
    top_files = re.search(r"TOP_FILES=\((.*?)\)", body, re.S)
    assert top_files, "TOP_FILES list not found"
    assert '"AGENTS.md"' not in top_files.group(1), (
        "build-distrib.sh copies AGENTS.md, which pyproject excludes")
    assert "DIST_PRUNE=(" in body, (
        "build-distrib.sh must prune docs/PROTOCOLS + docs/REVIEW-PROTOCOL.md, "
        "which ride in with `cp -a docs`")
