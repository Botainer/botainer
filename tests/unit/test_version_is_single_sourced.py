"""The version must be the same number everywhere it is written down.

It was not. FOUR places disagreed:

    pyproject.toml                       0.1.0a4
    botainer/plugins/manifest.py         0.1.0a3   ("match pyproject" — it did not)
    botainer/__init__.py                 0.1.0a1   <- what `--version` PRINTED
    git tags                             v0.1.0a2, v0.1.0a3, on an abandoned line

The first version of this file caught two of the four. It missed the one a USER
actually sees, because it was written by listing the places drift was known to
have happened rather than by looking for the places it COULD happen — and
`botainer --version` reads `botainer.__version__`, which nothing here mentioned.

Found by installing the built wheel into a clean venv and running `botainer
--version`. Reading the source would never have found it: each copy is locally
correct, and the disagreement only exists between files.

`botainer/__init__.py` no longer declares a version — it reads the installed
package metadata, which pip writes from pyproject at build time. That makes the
drift UNREPRESENTABLE rather than detectable, which is the preferred shape. The
tests below cover what structure cannot: the manifest constant, which is a real
second copy, and any new literal someone adds later.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    m = re.search(r'^version = "([^"]+)"',
                  (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert m, "no version in pyproject.toml"
    return m.group(1)


def test_manifest_launcher_version_matches_pyproject() -> None:
    declared = _pyproject_version()
    body = (REPO / "botainer" / "plugins" / "manifest.py").read_text(encoding="utf-8")
    m = re.search(r'^BOTAINER_LAUNCHER_VERSION = "([^"]+)"', body, re.M)
    assert m, "BOTAINER_LAUNCHER_VERSION not found — did it move?"
    assert m.group(1) == declared, (
        f"BOTAINER_LAUNCHER_VERSION is {m.group(1)!r} but pyproject says "
        f"{declared!r}. They gate plugin compatibility together; a mismatch is "
        "invisible while only the prerelease suffix differs, and wrong the "
        "moment the minor does."
    )


def test_dunder_version_is_derived_not_declared() -> None:
    """`botainer/__init__.py` must not hardcode a version.

    A literal there outranks every other copy in visibility — it is what
    `--version` prints — and is the copy most likely to be forgotten, since
    nothing else in the package reads it.
    """
    body = (REPO / "botainer" / "__init__.py").read_text(encoding="utf-8")
    literal = re.search(r'^__version__ = "(\d[^"]*)"', body, re.M)
    assert not literal, (
        f"botainer/__init__.py hardcodes __version__ = {literal.group(1)!r} "
        "if it matched. Derive it from importlib.metadata instead: a literal "
        "here is what `botainer --version` prints, and it drifted to 0.1.0a1 "
        "while pyproject said 0.1.0a4."
    )
    assert "importlib.metadata" in body, (
        "botainer/__init__.py should read the installed package metadata so the "
        "version cannot disagree with pyproject."
    )


def test_dunder_version_actually_resolves() -> None:
    """Anti-vacuous: deriving is only better than declaring if it WORKS.

    A derivation that silently falls back to a placeholder would pass the test
    above while printing nonsense at users — worse than the literal it replaced.
    """
    import botainer

    assert botainer.__version__ == _pyproject_version(), (
        f"botainer.__version__ resolved to {botainer.__version__!r}, but "
        f"pyproject says {_pyproject_version()!r}. Either the package metadata "
        "is stale (reinstall) or the fallback path is being taken when it "
        "should not be."
    )


def test_no_other_version_literals_hide_in_the_package() -> None:
    """Catch the NEXT copy, not just the ones that already drifted.

    The first version of this file enumerated known drift sites, which is why it
    missed __init__.py. This looks for the shape instead.
    """
    declared = _pyproject_version()
    stem = declared.split("a")[0].split("b")[0].split("rc")[0]  # e.g. 0.1.0
    pattern = re.compile(rf'"{re.escape(stem)}(a|b|rc)?\d*"')
    allowed = {
        # The real second copy: plugin compatibility compares against it, so it
        # cannot be derived from metadata without importing at module scope in a
        # path that must stay import-light. Pinned by the first test above.
        "botainer/plugins/manifest.py",
    }
    offenders = []
    for path in sorted((REPO / "botainer").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel in allowed or "__pycache__" in rel:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # a comment recording history is not a copy
            if pattern.search(line):
                offenders.append(f"{rel}:{n}: {line.strip()[:90]}")
    assert not offenders, (
        "these hardcode the current version, making a fourth copy:\n  "
        + "\n  ".join(offenders)
        + "\n\nRead botainer.__version__ instead, or add the file to `allowed` "
        "with a reason and a test that pins it."
    )


def test_shipped_prose_states_the_version_pyproject_declares() -> None:
    """THE COPIES A READER SEES, which this file did not cover until 0.1.0a5.

    Its own docstring says the first version was written by listing the places
    drift was KNOWN to have happened rather than the places it COULD, and then
    it looked only inside `botainer/`. Three shipped documents also state the
    version in prose, to a reader, and nothing checked them:

        README.md      the Status line, the first fact on the front page
        SECURITY.md    "botainer is at vX", and the supported-versions table
        CHANGELOG.md   the heading of the newest release section

    None is derivable from package metadata — prose has to say a number — so
    unlike `__version__` this cannot be made unrepresentable, and a check is the
    honest fallback rather than a second-best. The failure it prevents is
    specific and bad: SECURITY.md's table is what tells a reporter whether the
    version they are running is supported, and a stale number there says "no"
    about the only version that exists.
    """
    declared = _pyproject_version()
    missing = []

    readme = (REPO / "README.md").read_text(encoding="utf-8")
    if f"`{declared}`" not in readme:
        missing.append("README.md — the **Status:** line")

    security = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    if f"v{declared}" not in security:
        missing.append("SECURITY.md — the 'botainer is at vX' sentence")
    if re.search(rf"^\|\s*{re.escape(declared)}\s*\|\s*yes\s*\|", security, re.M) is None:
        missing.append("SECURITY.md — the supported-versions table row")

    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    if re.search(rf"^## \[{re.escape(declared)}\]", changelog, re.M) is None:
        missing.append(f"CHANGELOG.md — no `## [{declared}]` section")

    assert not missing, (
        f"pyproject declares {declared!r}, but the shipped prose has not caught "
        f"up:\n  " + "\n  ".join(missing)
        + "\n\nA version bump is not one edit. These are read by people, not by "
        "code, so nothing else will notice."
    )


def test_the_version_is_a_valid_pep440_prerelease() -> None:
    """`0.1.0-alpha.1` is legal but PyPI rewrites it to `0.1.0a1`, so what you
    typed and what users see differ. Keep the normalised form in the file."""
    v = _pyproject_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+(a|b|rc)\d+|\d+\.\d+\.\d+", v), (
        f"version {v!r} is not a normalised PEP 440 release or prerelease. "
        "Use 0.1.0a4 / 0.1.0b1 / 0.1.0rc1 / 0.1.0 — not 0.1.0-alpha.4."
    )
