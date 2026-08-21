"""The hash a contributor signs must be the hash of the CLA they were shown.

HOW A SIGNATURE IS PROVED, AND WHY THIS TEST EXISTS.

A contributor signs by posting a comment on their own pull request:

    I have read the Botainer Contributor License Agreement v1.0
    (sha256 <digest>) and I hereby sign it

That sentence is the record. It lives in GitHub's pull-request conversation —
authored by their account, timestamped by GitHub, edit-flagged in the UI,
deletion-logged in the org audit log. Critically it lives OUTSIDE anything this
project controls: `main` is regenerated wholesale by an rsync export, so a file
in the repository is a poor place to keep evidence, but a PR comment is not
ours to rewrite.

The first version of that sentence said only "the Botainer Contributor License
Agreement". It identified no particular text. The signature JSON the bot keeps
records a name, an account id, a pull request and a date — no document, no
digest — and `path-to-document` pointed at a git TAG, which the steward can
move with `git tag -f`. So there was no way to establish WHAT anyone had agreed
to; only that they had agreed to something.

Naming a version and a digest in the contributor's own words fixes that without
depending on any file we control.

WHAT THIS TEST GUARDS. The digest is a literal in the workflow, so it is a
second copy of a fact about CLA.md — the same shape as the version and the
viewer-mode default, both of which drifted. Editing the agreement without
updating the sentence would leave contributors signing a digest that matches
nothing, which is worse than no digest at all: it looks like proof and is not.

If this test fails, the agreement changed. That is not a reason to update the
hash and move on — changed text is a NEW VERSION. Bump the version in CLA.md,
bump it in the sentence, update the digest, and use a fresh signatures file so
each signature maps to the text that was actually shown. CLA.md §10 already
says a revised version binds only contributions submitted after acceptance.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CLA = REPO / "CLA.md"
WORKFLOW = REPO / ".github" / "workflows" / "cla.yml"


CONTRIBUTING = REPO / "CONTRIBUTING.md"


def _sentence() -> str:
    """The sentence a contributor is asked to post — from CONTRIBUTING.md.

    CONTRIBUTING is the authority because it is what a contributor actually
    reads. Agreement is manual: there is no bot in the published repository,
    so the workflow file is not the operative surface and is checked only as a
    consistency matter when present.
    """
    body = CONTRIBUTING.read_text(encoding="utf-8")
    # The sentence is a blockquote and WRAPS, so it is two `> ` lines. Matching
    # one line silently captured half of it — including the half without the
    # digest, which is the half that matters.
    m = re.search(r"((?:^> .*\n)*^> I have read the Botainer.*\n(?:^> .*\n)*)",
                  body, re.M)
    assert m, (
        "CONTRIBUTING.md no longer quotes the sign sentence as a blockquote. "
        "That sentence is what a contributor posts; it must be findable."
    )
    text = " ".join(line.lstrip("> ").strip() for line in m.group(1).splitlines())
    i = text.index("I have read the Botainer")
    return " ".join(text[i:].split())


def test_the_signed_sentence_names_a_version_and_a_digest() -> None:
    s = _sentence()
    assert re.search(r"\bv\d+\.\d+\b", s), (
        f"the sign sentence names no version: {s!r}. Without one, a signature "
        "does not identify which text was agreed to."
    )
    assert re.search(r"sha256 [0-9a-f]{16,}", s), (
        f"the sign sentence carries no sha256: {s!r}. The signature record "
        "stores no document, so the digest in this sentence is the only thing "
        "tying an acceptance to specific bytes."
    )


def test_the_digest_is_the_digest_of_the_current_cla() -> None:
    digest = hashlib.sha256(CLA.read_bytes()).hexdigest()
    claimed = re.search(r"sha256 ([0-9a-f]+)", _sentence()).group(1)
    assert digest.startswith(claimed), (
        f"the sign sentence claims sha256 {claimed}…, but CLA.md hashes to "
        f"{digest[:len(claimed)]}…\n\n"
        "CLA.md changed. Changed text is a NEW VERSION — bump the version in "
        "CLA.md and in the sentence, update the digest, and move the bot to a "
        "fresh signatures file so old signatures keep pointing at the text "
        "they were actually shown."
    )


def test_the_signed_version_is_the_version_the_cla_declares() -> None:
    """The sentence's version must equal CLA.md's own version line.

    NOT redundant with the digest test, and the reason is that test's own
    failure message. Bump CLA.md's version line and its bytes change, so
    `test_the_digest_is_the_digest_of_the_current_cla` fails and tells the
    editor to update the digest. They update the digest. Green. The sentence
    now reads `v1.0 (sha256 <digest of the v1.1 text>)` — internally
    consistent, and wrong about which version was agreed to.

    That matters more than a stale string: CLA §10 makes acceptance
    version-scoped, so the version is what decides WHICH contributions a
    signature covers. Naming the wrong one does not clearly cover anything.
    """
    body = CLA.read_text(encoding="utf-8")
    m = re.search(r"^\*\*Version\s+([0-9]+\.[0-9]+)", body, re.M)
    assert m, (
        "CLA.md no longer opens with a `**Version X.Y` line. That line is what "
        "the sign sentence's version refers to; without it there is nothing for "
        "a contributor to be agreeing with."
    )
    declared = m.group(1)
    signed = re.search(r"\bv([0-9]+\.[0-9]+)\b", _sentence())
    assert signed, f"the sign sentence names no version: {_sentence()!r}"
    assert signed.group(1) == declared, (
        f"CLA.md declares Version {declared}, but the sentence a contributor is "
        f"asked to post says v{signed.group(1)}.\n\n"
        "Acceptance is version-scoped (CLA §10). Update the version in "
        "CONTRIBUTING.md's blockquote — and in the workflow, which the next "
        "test pins to it. If the TEXT changed too, that is a new version: use "
        "a fresh signatures file so old signatures keep pointing at the text "
        "they were actually shown."
    )


def test_the_unshipped_workflow_agrees_with_contributing() -> None:
    """The bot is not published, but the file is kept ready — keep it in step.

    If it is ever switched on, a workflow asking for a different sentence than
    CONTRIBUTING documents would leave contributors typing the documented one
    and the job never firing: the PR sits blocked with no explanation.
    """
    if not WORKFLOW.is_file():
        pytest.skip("no CLA workflow in this tree")
    body = WORKFLOW.read_text(encoding="utf-8")
    sentence = _sentence()
    for label, pattern in (("custom-pr-sign-comment",
                            r"custom-pr-sign-comment:\s*'([^']+)'"),
                           ("the job's if: condition", r"^\s*if:\s*(.+)$")):
        m = re.search(pattern, body, re.M)
        assert m, f"{label} not found in the CLA workflow"
        assert sentence in m.group(1), (
            f"{label} does not match the sentence CONTRIBUTING.md documents.\n"
            f"  CONTRIBUTING: {sentence!r}"
        )


def test_the_document_url_is_pinned() -> None:
    """`path-to-document` must not float on a branch.

    A branch ref shows whatever the tip says today, so a contributor who signs
    in January and a reader who checks in June can see different documents. A
    tag is better and a commit SHA is best — a tag is movable with `git tag -f`,
    a SHA is not.
    """
    if not WORKFLOW.is_file():
        pytest.skip("no CLA workflow in this tree")
    body = WORKFLOW.read_text(encoding="utf-8")
    m = re.search(r"path-to-document:\s*'([^']+)'", body)
    assert m, "path-to-document not found"
    url = m.group(1)
    assert "/blob/main/" not in url and "/blob/master/" not in url, (
        f"path-to-document points at a branch tip: {url}. It must reference a "
        "fixed tag or commit, or the document can change under a signature."
    )
