"""Every id space has ONE definition per id, and every citation resolves.

WHY A TOOL. Ids are hand-assigned sequential letters with no allocator, so two
sessions appending independently pick the same one and nothing notices. That has
happened: eight §4x ids were defined twice in the security contract, and §4df was
handed out while a pending proposal had reserved it.

WHY THE EXISTING GUARD DOES NOT CATCH IT. It counts headings in the SHIPPED file.
The resolver accepts four sources, so an id defined as a heading and cited from a
stub — or reserved in a private proposal that has not landed — is invisible to it.

MEASURED, WHILE ALLOCATING THREE IDS TODAY. My first hand-rolled scan reported
§4du FREE while it was defined in the contract and cited in two tzar ledgers. Two
independent bugs, both of which this tool exists to make impossible:

  1. Non-recursive globs miss nested directories — two of the three sources
     for the id I was allocating lived one level deeper than my glob reached.
  2. A `§`-anchored regex finds every CITATION and no DEFINITION, because
     headings are written `## 4du.` with no `§`. For ALLOCATION that is exactly
     backwards — the definitions are the thing you must not collide with.

WHY THE SCAN LIVES IN A TEST AND THE CLI IMPORTS IT, not the reverse.
The dev-tooling directory is not part of the exported tree; `tests/` is. A guard that exists
only in the private repo is the weaker-of-the-pair defect this project already
records, and the ids it protects live in a SHIPPED document. So the scan is
here, where it runs for anyone who has the export, and the dev-side allocator
is a thin wrapper that imports it. One owner, two entry points.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: DEFINITIONS have ONE OWNER PER SPACE. My first version pattern-matched every
#: space across every file and reported six "duplicates" that were not: `## 4a.`
#: headings in unrelated planning docs, a DN citation in a test counted against
#: the index that defines it, and two ids counted twice because a TRIMMED COPY of
#: the contract lives in a distribution-rehearsal tree. A guard that cries wolf trains
#: everyone to ignore the channel — the defect this repo names by itself — so the
#: owner is named rather than inferred.
#: ONLY SPACES WHOSE DEFINITION SOURCE SHIPS. The other two id spaces are
#: defined in files the distribution excludes, so in an exported tree their
#: sources are simply absent — and a scan that finds no definitions calls every
#: citation dangling. This test would then FAIL for every user of the export,
#: which is the precise failure the exclude list warns about elsewhere: a tree
#: containing a test for code it does not have fails its own suite.
#:
#: So the shipped guard covers the shipped document, and the dev-side CLI adds
#: its own spaces by passing them in. A guard that is honest about its scope
#: beats one that claims more and breaks.
DEFINITION_SOURCES: dict[str, tuple[str, ...]] = {
    "§4x": ("docs/CAPABILITY-SURFACE.md",),
}

#: EXTRA SOURCES AND RESERVATIONS ARE PASSED IN, never named here. A non-public
#: path written into a shipped file is a dead end for its reader, and a gate in
#: this suite exists to catch exactly that — it caught this file on its first
#: run. The dev-side CLI knows its own paths and supplies them.

#: RESERVATIONS are definitions-in-waiting, and they are DECLARED, not guessed.
#: An id spoken for by a pending proposal is not free — §4df was handed to a new
#: section while a proposal was waiting to use it, invisible because the guard
#: reads only the shipped file.
#:
#: I tried inferring reservations from the proposals themselves and it cried
#: wolf twice: reading every heading in a proposal caught nothing (reservations
#: are often prose — "they reserve the ids 4dr and 4ds"), and reading every
#: CITATION turned each ordinary cross-reference into a false duplicate, 16 of
#: them. A proposal that says "this supersedes §4bb" is citing, not reserving,
#: and no regex distinguishes those. So the reservation is written down.

#: Citations may appear anywhere, RECURSIVELY. A non-recursive glob is how my
#: own first scan lost two of the three sources for the id it was allocating.
SEARCH_ROOTS = ("docs", "private", "tools", "tests", "botainer", "plugins")
SUFFIXES = (".md", ".py", ".txt", ".tsv", ".yaml", ".yml", ".sh")

#: (definition pattern, citation pattern) per space. Definitions are what an
#: allocator must avoid; citations are what an audit must be able to resolve.
SPACES: dict[str, tuple[re.Pattern, re.Pattern]] = {
    "§4x": (re.compile(r"^#{1,6}\s+(4[a-z]{1,3})\.", re.M),
            re.compile(r"§(4[a-z]{1,3})\b")),
}

#: Copies and history are not the contract. A distribution-rehearsal tree holds
#: a TRIMMED COPY of the shipped document, and an archive holds superseded
#: planning docs whose dangling ids are a fact about the past rather than a
#: defect to fix now. Counting either produced false duplicates.
EXCLUDE_PARTS = ("dist-prep", "archive", "__pycache__", ".git")


def _files():
    for root in SEARCH_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not (f.is_file() and f.suffix in SUFFIXES):
                continue
            if any(part in EXCLUDE_PARTS for part in f.parts):
                continue
            yield f


def reservations_from(path: Path) -> dict[str, str]:
    """id -> why, from a declared reservations file. Absent file = none.

    Passed in by the caller: the file lives outside the exported tree, and
    naming it here would put a path in a shipped file that its reader does not
    have.
    """
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            out[parts[0].lstrip("§")] = parts[1]
    return out


def scan(space: str, *, extra_sources: "tuple[str, ...]" = (),
         reservations: "dict[str, str] | None" = None
         ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(definitions, citations), each id -> the files it appears in."""
    define_re, cite_re = SPACES[space]
    defs: dict[str, list[str]] = {}
    cites: dict[str, list[str]] = {}

    for src in (REPO / p for p in
                list(DEFINITION_SOURCES.get(space, ())) + list(extra_sources)):
        if not src.is_file():
            continue
        rel = str(src.relative_to(REPO))
        # NOT set(): the real collision is EIGHT ids defined twice INSIDE one
        # file, and de-duplicating per file makes exactly that case invisible.
        # Measured — with set() here, injecting a second `## 4aa.` heading into
        # a fixture contract left the audit passing.
        for i in define_re.findall(src.read_text(errors="ignore")):
            defs.setdefault(i, []).append(rel)

    for i, why in (reservations or {}).items():
        if cite_re.fullmatch(("§" if space == "§4x" else "") + i) or \
           cite_re.fullmatch(i):
            defs.setdefault(i, []).append(f"reserved ({why})")

    for f in _files():
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        rel = str(f.relative_to(REPO))
        for i in set(cite_re.findall(text)):
            cites.setdefault(i, []).append(rel)
    return defs, cites


def _successor_4x(body: str) -> str:
    """4a -> 4b … 4z -> 4aa -> 4ab … 4az -> 4ba."""
    tail = body[1:]
    if len(tail) == 1:
        return "4" + (chr(ord(tail) + 1) if tail != "z" else "aa")
    head, last = tail[:-1], tail[-1]
    if last != "z":
        return "4" + head + chr(ord(last) + 1)
    return "4" + _successor_4x("4" + head)[1:] + "a"


def next_free(space: str, *, extra_sources: "tuple[str, ...]" = (),
              reservations: "dict[str, str] | None" = None) -> str:
    defs, cites = scan(space, extra_sources=extra_sources,
                       reservations=reservations)
    # RESERVED COUNTS AS TAKEN. An id spoken for by a pending proposal is not
    # free just because it has not landed — that is exactly how §4df was handed
    # to a new section while a proposal was waiting to use it.
    taken = set(defs) | set(cites)
    if space != "§4x":
        nums = [int(re.search(r"\d+", i).group()) for i in taken] or [0]
        prefix = "DN-" if space == "DN" else "EF-"
        width = 3 if space == "DN" else 1
        return f"{prefix}{max(nums) + 1:0{width}d}"
    order = sorted((i for i in taken), key=lambda s: (len(s), s))
    cand = order[-1] if order else "4a"
    while cand in taken:
        cand = _successor_4x(cand)
    return "§" + cand


def audit(*, spaces: "dict | None" = None,
          extra_sources: "dict[str, tuple[str, ...]] | None" = None,
          reservations: "dict[str, str] | None" = None) -> int:
    problems: list[str] = []
    for space in (spaces or SPACES):
        defs, cites = scan(space,
                           extra_sources=(extra_sources or {}).get(space, ()),
                           reservations=reservations)
        for i, files in sorted(defs.items()):
            # Two files defining one id, or one file defining it twice, are both
            # the collision this exists to stop.
            if len(files) > 1:
                problems.append(
                    f"{space}: {i!r} is DEFINED in {len(files)} places: "
                    + ", ".join(sorted(files)))
        dangling = sorted(set(cites) - set(defs))
        if dangling:
            problems.append(
                f"{space}: cited but never defined: {', '.join(dangling)}")
    for p in problems:
        print("  " + p)
    return 1 if problems else 0


# ---------------------------------------------------------------- assertions


def test_no_id_is_DEFINED_twice_in_any_space():
    """The collision that has actually happened, in the file it happened in.

    Eight §4x ids were once defined twice inside `docs/CAPABILITY-SURFACE.md`,
    four of them cited from elsewhere, because ids are hand-assigned sequential
    letters and two sessions appending independently pick the same one.
    """
    offenders: list[str] = []
    for space in SPACES:
        defs, _cites = scan(space)
        for i, files in sorted(defs.items()):
            if len(files) > 1:
                offenders.append(
                    f"{space}: {i!r} defined {len(files)}x in "
                    + ", ".join(sorted(files)))
    assert not offenders, (
        "an id is defined more than once. Two sections with the same id means "
        "every citation of it is ambiguous, and the reader cannot tell which "
        "one a cross-reference meant:\n  " + "\n  ".join(offenders))


#: Ids CITED in the shipped contract and DEFINED only in a register that does
#: not ship. A public reader following one of these finds nothing.
#:
#: INLINE, NOT A FILE IN THE DEV TREE, and that is the third time this lesson
#: landed in one commit: data a SHIPPED test needs must ship with it. A baseline
#: living outside the distribution is absent in an export, the test then sees
#: 35 unexplained dangling ids, and it fails for every user who is not me.
#:
#: BASELINED, NOT IGNORED. These predate this guard — the contract grew a public
#: half and a private register, and these ended up defined only in the private
#: one. A gate born red is a gate everyone learns to skip, and this project has
#: a row about a warn that fired on every commit until nobody read it. The list
#: is meant to SHRINK; a NEW one fails the test below.
_DANGLING_BASELINE = frozenset({
    "4o", "4p", "4q", "4t", "4u", "4v", "4aa", "4aq",
    "4ba", "4bd", "4bi", "4bj", "4bm", "4bn", "4bv", "4by",
    "4cc", "4cd", "4ci", "4cj", "4cl", "4cr", "4db", "4dd",
    "4df", "4dg", "4di", "4dj", "4dk", "4dl", "4dm", "4dn",
    "4do", "4dp", "4dq",
})


def test_no_NEW_citation_dangles():
    """A reference that resolves to nothing is worse than no reference.

    It tells a reader there is more and then wastes the trip. The shipped
    contract cites ids defined only in a register that does not ship, so a
    public reader following them finds nothing.

    The known set is recorded above with its reason; this fails on a NEW one.
    """
    known = _DANGLING_BASELINE
    dangling: list[str] = []
    for space in SPACES:
        defs, cites = scan(space)
        for i in sorted(set(cites) - set(defs)):
            if i in known:
                continue
            dangling.append(f"{space}: {i} cited in "
                            + ", ".join(sorted(cites[i])[:3]))
    assert not dangling, (
        "a NEW id is cited but never defined — a reader following it gets "
        "nothing:\n  " + "\n  ".join(dangling))


def test_the_scan_can_actually_FAIL(tmp_path, monkeypatch):
    """The check this test file exists to be, applied to itself.

    My first version of this scan de-duplicated per file with `set()`, which
    made a duplicate WITHIN one file invisible — and inside one file is exactly
    where the real eight-id collision happened. It passed a clean repo and
    would have passed the incident. So: build a fixture that DOES collide and
    require the scan to see it.
    """
    import sys as _sys
    monkeypatch.setattr(_sys.modules[__name__], "REPO", tmp_path, raising=False)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CAPABILITY-SURFACE.md").write_text(
        "## 4aa. First\n\n## 4ab. Second\n\n## 4aa. A COLLIDING second\n")
    defs, _ = scan("§4x")
    assert len(defs.get("4aa", [])) == 2, (
        f"the scan cannot see two definitions of one id inside a single file, "
        f"which is the shape the real collision took: {defs}")


def test_a_RESERVED_id_is_not_offered_as_free(tmp_path, monkeypatch):
    """`§4df` was handed to a new section while a pending proposal held it.

    The guard could not see the reservation because it read only the shipped
    file. Reservations are declared in a file now — inferring them from the
    proposals themselves turned every ordinary cross-reference into a false
    collision, sixteen of them, which is a guard that cries wolf.
    """
    import sys as _sys
    mod = _sys.modules[__name__]
    monkeypatch.setattr(mod, "REPO", tmp_path, raising=False)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CAPABILITY-SURFACE.md").write_text("## 4aa. Only one\n")
    res = tmp_path / "reservations.tsv"
    res.write_text("# id\twhy\n4ab\tpending proposal, awaiting the maintainer\n")

    assert next_free("§4x", reservations=reservations_from(res)) == "§4ac", (
        "an id reserved by a pending proposal was offered as free — which is "
        "exactly how a live reservation got handed to a new section")
