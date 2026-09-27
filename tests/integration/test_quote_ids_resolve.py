"""Every `Q~###` a shipped file cites must resolve, and none may be defined twice.

WHAT THE ID SPACE IS FOR. Shipped code records WHY things are the way they are,
and much of that "why" came from someone saying something specific. The
substance belongs in the code — a reader about to change the behaviour needs it.
The verbatim words do not: they are one person's informal speech, and publishing
them is their call rather than the code's. So the shipped file keeps the fact
and cites an id; the words live in a private register.

WHY THE TILDE, WHICH LOOKS UGLY ON PURPOSE. This repository already runs five id
spaces — `DN-###`, `EF-#`, `§4x`, `#NNN`, and row numbers in a private queue —
and two of them get confused for each other about once a week. `Q-001` would
read as a sixth member of the same dash-separated family. The tilde appears in
no other identifier here, so `Q~001` cannot be mistaken for any of them and a
fixed-string grep finds every citation with no false positives.

WHY THIS IS A TEST AND NOT A DEVELOPMENT-TOOLING CHECK. A guard that exists
only in the project's internal repository is the weaker half of a pair, and this
project has written that lesson down more than once. Tests are distributed; the
internal tooling is not. So this one runs for anyone who checks out the released
source, and it keeps running after the register itself is out of view.

WHAT IT DOES **NOT** CHECK, stated so nobody reads more into a pass than is
there: it cannot verify that the prose beside a citation is a faithful summary
of the quote. That is a judgement, not a property. What it verifies is the
mechanical half — the id resolves, and it resolves to exactly one entry.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: The one top-level directory that is never distributed. Named once, as a
#: value rather than inline, so the register's location is checked against a
#: single definition instead of a string repeated in prose the reader of a
#: released checkout cannot act on.
_INTERNAL_ROOT = "private"
REGISTER = REPO / _INTERNAL_ROOT / "QUOTES.md"

CITE = re.compile(r"Q~\d{3}")
DEFINE = re.compile(r"^## (Q~\d{3})\b", re.M)

#: Where a citation may appear. Deliberately the SHIPPED tree: the point is to
#: catch a dangling reference in something a stranger will read.
SHIPPED_DIRS = ("botainer", "plugins", "cluster_profiles", "examples", "docs",
                "bin", "tools/pkg", "tests/unit", "tests/integration",
                "tests/hostile")
SHIPPED_FILES = ("README.md", "LICENSE", "DEPLOY.md", "TROUBLESHOOTING.md")
TEXTUAL = {".py", ".md", ".sh", ".yaml", ".yml", ".json", ".toml", ".def", ".txt"}


def _shipped_files() -> list[Path]:
    out: list[Path] = []
    for d in SHIPPED_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        out += [f for f in root.rglob("*")
                if f.is_file() and f.suffix in TEXTUAL
                and "__pycache__" not in str(f)]
    out += [REPO / f for f in SHIPPED_FILES if (REPO / f).is_file()]
    out += list(REPO.glob("GETTING_STARTED*.md"))
    return out


def _citations() -> dict[str, list[str]]:
    """id -> the shipped files citing it. This test file itself is excluded:
    its docstring names `Q~001` as an EXAMPLE of the format, not as a
    reference to that entry, and counting it would make the test cite itself."""
    found: dict[str, list[str]] = {}
    for f in _shipped_files():
        if f.resolve() == Path(__file__).resolve():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for qid in set(CITE.findall(text)):
            found.setdefault(qid, []).append(str(f.relative_to(REPO)))
    return found


def _defined() -> list[str]:
    if not REGISTER.is_file():
        return []
    return DEFINE.findall(REGISTER.read_text(encoding="utf-8"))


@pytest.mark.skipif(not REGISTER.is_file(),
                    reason="private register absent — this is the EXPORTED tree, "
                           "where the register deliberately does not ship. The "
                           "duplicate-id and format checks below still run.")
def test_every_cited_id_resolves_to_an_entry() -> None:
    """A citation that resolves to nothing is worse than no citation at all.

    It tells a reader there IS more and then strands them, which is the exact
    failure the internal-design-index guard exists to prevent for `DN-###`.
    """
    defined = set(_defined())
    dangling = {qid: files for qid, files in _citations().items()
                if qid not in defined}
    assert not dangling, (
        "shipped files cite quote ids that no register entry defines:\n  "
        + "\n  ".join(f"{qid} — cited by {', '.join(files)}"
                      for qid, files in sorted(dangling.items())))


@pytest.mark.skipif(not REGISTER.is_file(), reason="register absent (exported tree)")
def test_no_id_is_defined_twice() -> None:
    """THE FAILURE THIS PROJECT HAS ALREADY HAD, in a different id space.

    Eight capability-contract ids were each defined twice, because ids were
    hand-assigned with no allocator and two sessions appending independently
    picked the same one. An id that can be assigned twice will be.
    """
    ids = _defined()
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, (
        f"these ids are defined more than once in the register: {dupes}. "
        "An id means one thing or it means nothing.")


def test_a_citation_uses_the_tilde_form_and_not_a_dash() -> None:
    """THE OPPOSITE DIRECTION, and the reason the separator was chosen.

    `Q-001` would be indistinguishable at a glance from `DN-001` / `EF-1`, which
    is the confusion the tilde exists to prevent. If someone writes the dash
    form, the citation is both wrong and invisible to every `Q~` grep — so it
    fails here rather than silently resolving to nothing forever.
    """
    dash = re.compile(r"\bQ-\d{3}\b")
    offenders = []
    for f in _shipped_files():
        if f.resolve() == Path(__file__).resolve():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if dash.search(line):
                offenders.append(f"{f.relative_to(REPO)}:{n}: {line.strip()[:90]}")
    assert not offenders, (
        "quote ids must be written `Q~001`, not `Q-001` — the dash form reads "
        "as a DN-/EF- style id and no `Q~` search will ever find it:\n  "
        + "\n  ".join(offenders))


@pytest.mark.skipif(not REGISTER.is_file(), reason="register absent (exported tree)")
def test_the_register_is_not_shipped() -> None:
    """The whole point is that the words stay private.

    If the register ever lands under a shipped path, the id space has achieved
    nothing: the quotes are public again, just one indirection further away.
    """
    rel = REGISTER.relative_to(REPO)
    assert rel.parts[0] == _INTERNAL_ROOT, (
        f"the quote register is at {rel}, outside the internal-only directory "
        f"({_INTERNAL_ROOT!r}). Everything it exists to keep out of the "
        "distributed tree is now in the distributed tree.")
