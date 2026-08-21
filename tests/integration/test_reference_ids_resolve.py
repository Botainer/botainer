"""A cross-reference must resolve, or say plainly that it cannot.

This repo cites four kinds of internal identifier from shipped files:

    §4x        a section of docs/CAPABILITY-SURFACE.md
    DN-###     an internal design note
    EF-#       an entry in the external-facts register
    #NNN       an item in botainer's internal issue tracker

Only the first two are checkable from inside a distribution, and only those are
checked here. The rules they encode are the same one CLAUDE.md states for DN-:

    "A reference that resolves to nothing is worse than no reference."

WHY THIS IS A TEST AND NOT ANOTHER SHELL GATE. It replaces one. The check began
life as tools/dev/check-surface-section-ids.sh, which meant the rule was
enforced only in the private repo and only at commit time — and the whole
personal-info episode two commits earlier was about a check existing in one of
two places and being weaker in the one that mattered. As a test it runs on every
commit (the full suite is a pre-commit gate) AND inside the exported tree, where
export-public.sh runs the suite before anything is committable. One
implementation, both surfaces.

WHAT WENT WRONG WITHOUT IT. docs/CAPABILITY-SURFACE.md defines 111 §4x sections.
On, EIGHT ids were defined TWICE on unrelated entries — §4cq was both
"credential messages rewritten" and "/scratch may leave the state root" — and
four of the eight were cited from elsewhere, so those citations resolved to
whichever entry the reader happened to reach first. Ids are hand-assigned
sequential letters with no allocator, so two sessions appending independently
pick the same one and nothing notices.
"""

from __future__ import annotations

import collections
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SURFACE = REPO / "docs" / "CAPABILITY-SURFACE.md"

#: `#NNN` in a shipped file points at a tracker the reader does not have. That
#: is tolerated — the substance is always inlined beside the reference, the id
#: is provenance — but it MUST be disclosed rather than left looking like a
#: GitHub issue number. This is the sentence that does it.
DISCLOSURE_MARKER = "internal issue tracker"


def _tracked() -> list[str]:
    return subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True, text=True, check=True,
    ).stdout.split()


def _readable(rel: str) -> str | None:
    try:
        return (REPO / rel).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


#: The change history moved to a maintainer's register on, leaving a
#: one-line stub per entry in the public contract's appendix. So an id resolves
#: three ways, and all three are legitimate:
#:
#:   ## 4x.        a contract section, in full, in the public file
#:   - **§4x** —   a stub in the public appendix; long form is internal
#:   ## 4x.        the full entry, in the private register (this repo only)
#:
#: The stub is what keeps the reference resolvable for a public reader: the id
#: names something, with a title and a date, and the page says plainly that the
#: detail is not distributed and nothing above depends on it.
REGISTER = REPO / "private" / "CAPABILITY-SURFACE-REGISTER.md"


def _defined_sections() -> list[str]:
    """Full sections in the public contract — the set that must be unique."""
    return re.findall(r"^## (4[a-z]{0,2})\.", SURFACE.read_text(encoding="utf-8"), re.M)


def _resolvable_sections() -> set[str]:
    """Every id a `§4x` citation may legitimately point at."""
    surface = SURFACE.read_text(encoding="utf-8")
    ids = set(re.findall(r"^## (4[a-z]{0,2})\.", surface, re.M))
    ids |= set(re.findall(r"^- \*\*§(4[a-z]{0,2})\*\*", surface, re.M))
    if REGISTER.exists():  # absent in an exported tree, by design
        ids |= set(re.findall(r"^## (4[a-z]{0,2})\.", REGISTER.read_text(encoding="utf-8"), re.M))
    return ids


def test_no_capability_section_id_is_defined_twice() -> None:
    """Two entries under one id make every citation to it ambiguous."""
    ids = _defined_sections()
    assert ids, "no §4x sections found — this test would pass vacuously"
    dupes = {k: n for k, n in collections.Counter(ids).items() if n > 1}
    if dupes:
        lines = []
        text = SURFACE.read_text(encoding="utf-8").splitlines()
        for sid in sorted(dupes):
            for n, line in enumerate(text, 1):
                if line.startswith(f"## {sid}."):
                    lines.append(f"    CAPABILITY-SURFACE.md:{n}: {line[:88]}")
    assert not dupes, (
        f"{len(dupes)} section id(s) defined more than once: {sorted(dupes)}\n"
        + "\n".join(lines)
        + "\n  Give all but one a free id (4d*, 4e*, …) and update whatever cited "
          "the renumbered entry."
    )


def test_every_capability_section_citation_resolves() -> None:
    """A §4x that names no section is a pointer to nothing.

    Resolving to a STUB counts. The public contract keeps one line per
    moved-out register entry precisely so that every id still names something.
    """
    known = _resolvable_sections()
    dangling: dict[str, list[str]] = collections.defaultdict(list)
    for rel in _tracked():
        body = _readable(rel)
        if body is None:
            continue
        for n, line in enumerate(body.splitlines(), 1):
            for sid in re.findall(r"§(4[a-z]{1,2})\b", line):
                if sid not in known:
                    dangling[sid].append(f"{rel}:{n}")
    assert not dangling, (
        "§4x citations that resolve to no section:\n"
        + "\n".join(f"    §{k} — {', '.join(v[:4])}" for k, v in sorted(dangling.items()))
    )


def test_citations_are_actually_present() -> None:
    """Guards the two tests above against passing on an empty search.

    If the citation syntax ever changes, the checks would find nothing to check
    and report success. This fails instead.
    """
    n = sum(
        len(re.findall(r"§4[a-z]{1,2}\b", body))
        for body in (_readable(r) for r in _tracked())
        if body
    )
    assert n > 20, (
        f"only {n} §4x citations found across the tree — either the citation "
        "syntax changed or this test is no longer looking at anything"
    )


def test_internal_tracker_ids_are_disclosed_not_disguised() -> None:
    """`#160` in a shipped file must not read as a GitHub issue number.

    804 citations of 185 distinct tracker ids appear across 187 shipped files.
    They are provenance — the substance is stated beside every one of them, per
    CLAUDE.md's rule for DN- — but a reader of the public repo has no tracker,
    and `#160` looks exactly like an issue in the repo they are standing in. The
    fix is the same as for design notes: say plainly that there is more,
    that they do not have it, and that nothing here depends on it.

    So: if shipped files cite tracker ids, a shipped doc must disclose what they
    are. This asserts the disclosure exists and is reachable, not that all 804
    citations were rewritten.
    """
    citing = [
        rel for rel in _tracked()
        if rel.startswith(("botainer/", "plugins/", "docs/"))
        and (body := _readable(rel)) is not None
        and re.search(r"(?<![\w#])#\d{1,3}\b", body)
        and not re.search(r"#[0-9a-fA-F]{6}\b", body)
    ]
    if not citing:
        return  # nothing to disclose

    disclosed = [
        rel for rel in _tracked()
        if rel.startswith(("docs/", "README"))
        and (body := _readable(rel)) is not None
        and DISCLOSURE_MARKER in body
    ]
    assert disclosed, (
        f"{len(citing)} shipped files cite bare `#NNN` tracker ids (e.g. "
        f"{citing[0]}), and no shipped doc explains what they are. To a reader "
        "of the public repo those look like issue numbers in the repo they are "
        f"standing in. Add the phrase {DISCLOSURE_MARKER!r} to a shipped doc "
        "saying the tracker is internal, that the substance is inlined beside "
        "each reference, and that nothing depends on having it."
    )


def test_every_private_register_entry_has_a_public_stub() -> None:
    """The stub is the whole reason the split works — so check the stub.

    Without this, `test_every_capability_section_citation_resolves` passes in
    THIS repo even with a stub deleted, because the id still resolves against
    the private register sitting next to it. A public reader has no register:
    for them the id would name nothing. Caught by deleting a stub and watching
    the citation test stay green.

    Skipped in an exported tree, where the register is absent by design.
    """
    if not REGISTER.exists():
        return
    in_register = set(re.findall(r"^## (4[a-z]{0,2})\.",
                                 REGISTER.read_text(encoding="utf-8"), re.M))
    assert in_register, "the register defines no sections — this would pass vacuously"
    stubs = set(re.findall(r"^- \*\*§(4[a-z]{0,2})\*\*",
                           SURFACE.read_text(encoding="utf-8"), re.M))
    missing = sorted(in_register - stubs)
    assert not missing, (
        f"{len(missing)} register entries have no stub in the public contract: "
        f"{missing}\n  Each moved-out entry needs one line in the change-register "
        "appendix, or its id names nothing for anyone outside this repo."
    )
