"""A doc path in shipped code must resolve for someone who has only the package.

THE DEFECT. `botainer auth login --help` printed "Per <a plan document> §11" —
naming a file that lives in the project's private half and never ships. The one
pointer a reader was given is a dead end, and the filename itself discloses a
little of that private tree. The rule here is to cite internal design work by a
stable `DN-` id, and a dev-side check enforces it — but only for strings that
already LOOK like `DN-###`. A bare path sails past, which is how 26 of them
accumulated across 17 files.

AND THE HALF THAT IS EASY TO MISS: "is it under docs/?" is not the test.
`pyproject.toml` excludes several documents and one whole subdirectory of
`docs/` from the distribution, so shipped code citing one of those hands the
reader the same dead end while looking perfectly legitimate. The exclusion list
is read FROM pyproject here rather than copied, so this guard cannot drift from
what packaging does — copying it would make this the second source of truth the
repo keeps being bitten by.

WHAT THIS DOES NOT CHECK: prose inside `docs/` itself (a shipped doc may cite
another shipped doc, and the export scan covers that), and `tests/` (which do not
ship either). It is about the code in the package.
"""
from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10; provided by the dev extra.
    import tomli as tomllib

REPO = Path(__file__).resolve().parents[2]
SHIPPED_CODE_ROOTS = ("botainer", "plugins")
CODE_SUFFIXES = (".py", ".yaml", ".yml", ".sh")

# A citation looks like a doc filename: SCREAMING-KEBAB or Title_Case ending .md,
# optionally with a directory in front. Deliberately narrow — `README.md` in a
# sentence about the user's own project is not a citation of ours.
CITATION = re.compile(r"(?<![\w/.-])((?:[A-Za-z0-9_-]+/)*[A-Z][A-Za-z0-9_-]*\.md)")

# Cited by name in prose about the USER's own tree, not about ours: the
# per-repo agent-instruction files and a project README.
NOT_OURS = {"README.md", "CLAUDE.md", "AGENTS.md"}

# Files botainer WRITES at runtime, named in the code that writes or binds them.
# `AGENT_HINTS.md` is generated per session and bound into the container; a
# comment naming it is describing an artefact, not pointing at a document, so
# "does this ship?" is the wrong question to ask of it. Listed rather than
# pattern-matched, because the moment this becomes a shape ("anything with an
# underscore") it starts excusing real citations.
GENERATED_ARTEFACTS = {"AGENT_HINTS.md", "AGENT_ACCESS.md"}


def _excluded_from_the_distribution() -> list[str]:
    """The sdist/wheel exclude patterns, read from pyproject, never copied."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    out: list[str] = []
    tool = data.get("tool", {})
    for section in ("hatch", "setuptools", "flit", "poetry"):
        node = tool.get(section, {})
        out += _collect_excludes(node)
    return out


def _collect_excludes(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("exclude", "excludes") and isinstance(value, list):
                found += [str(v) for v in value]
            else:
                found += _collect_excludes(value)
    elif isinstance(node, list):
        for item in node:
            found += _collect_excludes(item)
    return found


def _ships(rel: str, excludes: list[str]) -> bool:
    """Does `rel` (repo-relative) end up in the distribution?"""
    if not (REPO / rel).is_file():
        return False
    for pattern in excludes:
        pat = pattern.rstrip("/")
        if rel == pat or rel.startswith(pat + "/"):
            return False
    return True


def _resolve(citation: str) -> str | None:
    """Repo-relative path for a citation, or None if nothing by that name exists."""
    if (REPO / citation).is_file():
        return citation
    name = Path(citation).name
    for candidate in REPO.rglob(name):
        if ".git" in candidate.parts or "github" in candidate.parts:
            continue
        if "worktrees" in candidate.parts or "__pycache__" in candidate.parts:
            continue
        return str(candidate.relative_to(REPO))
    return None


def test_no_shipped_file_cites_a_doc_the_package_does_not_contain():
    excludes = _excluded_from_the_distribution()
    assert excludes, (
        "no exclude patterns found in pyproject.toml — either packaging changed "
        "shape or this test is reading the wrong table, and it would pass "
        "vacuously either way")

    offenders = []
    scanned = 0
    for root in SHIPPED_CODE_ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            if path.suffix not in CODE_SUFFIXES or "__pycache__" in path.parts:
                continue
            scanned += 1
            rel_file = str(path.relative_to(REPO))
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for citation in CITATION.findall(line):
                    name = Path(citation).name
                    if name in NOT_OURS or name in GENERATED_ARTEFACTS:
                        continue
                    target = _resolve(citation)
                    if target is None:
                        offenders.append(
                            f"{rel_file}:{lineno} cites {citation!r} — no such "
                            f"file anywhere in the repo")
                    elif not _ships(target, excludes):
                        offenders.append(
                            f"{rel_file}:{lineno} cites {citation!r} -> "
                            f"{target} which does NOT ship")
    assert scanned > 100, (
        f"only {scanned} shipped files scanned — the walk is broken and this "
        f"test would assert nothing")
    assert not offenders, (
        "shipped code points a reader at a document the package does not "
        "contain. Cite it by its DN- id instead — the developer index maps ids "
        "to documents — and inline the substance:\n  "
        + "\n  ".join(offenders))
