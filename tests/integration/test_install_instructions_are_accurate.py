"""No shipped surface may offer `pip install botainer` as a working command.

The PyPI name is not registered and nothing is published under it. Until that
changes, every `pip install botainer` / `pipx install botainer` in a shipped
file is an instruction that fails — and the worst instance was in the text
`botainer setup` prints automatically on a user's very first run, so the first
thing a new user was told to type was the one command guaranteed to error.

The surfaces are ENUMERATED rather than derived. That is defensible here, unlike
the ship-list, because this is a short closed set — the places that tell someone
how to install — and a reviewer can check the list is complete by eye. If a new
install surface appears, add it here in the same commit.

When botainer IS published, this test is what tells you where to update: delete
it and the greps below name every file that needs the new instruction.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

INSTALL_SURFACES = [
    "README.md",
    "GETTING_STARTED.md",
    "GETTING_STARTED-HPC.md",
    "TROUBLESHOOTING.md",
    "DEPLOY.md",
    "docs/USER-WORKFLOWS.md",
    "botainer/cli/help_install.py",
    "tools/pkg/install-hpc.sh",
]

# `pip install botainer` / `pipx install botainer`, but NOT `pip install -e .`
# or `pip install -e <path>`, which are the instructions that actually work.
PROMISES_PYPI = re.compile(r"\bpip(?:x)?\s+install\s+(?:--user\s+)?botainer\b")

# Wording that makes the mention a warning rather than an instruction. A line is
# allowed to contain the command only if the surrounding three lines disown it.
DISOWNED = re.compile(
    r"not on PyPI|NOT on PyPI|will not work|No matching distribution|"
    r"not registered|nothing is published|is intended",
    re.I,
)


@pytest.mark.parametrize("rel", INSTALL_SURFACES)
def test_pypi_install_is_never_offered_as_working(rel):
    path = REPO / rel
    assert path.exists(), (
        f"{rel} is listed as an install surface but does not exist. Either the "
        "file moved (update this list) or it was deleted (drop the entry)."
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    offences = []
    for n, line in enumerate(lines):
        if not PROMISES_PYPI.search(line):
            continue
        window = "\n".join(lines[max(0, n - 3):n + 4])
        if not DISOWNED.search(window):
            offences.append(f"{rel}:{n + 1}: {line.strip()}")
    assert not offences, (
        "these lines offer a PyPI install that does not exist:\n  "
        + "\n  ".join(offences)
        + "\n\nSay what works today (install from a clone) and state plainly "
        "that the package is not published."
    )


def test_the_pattern_would_catch_a_regression():
    """Anti-vacuous: prove the regex fires on the shape it is meant to stop,
    and stays quiet on the editable installs that are the real instruction."""
    for bad in ("pipx install botainer", "pip install botainer",
                "    python -m pip install --user botainer"):
        assert PROMISES_PYPI.search(bad), f"pattern missed {bad!r}"
    for good in ("pipx install -e .", "pip install -e ~/opt/botainer",
                 "pip install -e .  # editable until published",
                 "~/opt/botainer-venv/bin/pip install -e ~/opt/botainer"):
        assert not PROMISES_PYPI.search(good), f"false positive on {good!r}"


def test_at_least_one_surface_states_the_situation():
    """Silence is not neutrality. Someone who types the obvious command must find
    an answer, so at least one surface has to say the package is unpublished."""
    said = [
        rel for rel in INSTALL_SURFACES
        if DISOWNED.search((REPO / rel).read_text(encoding="utf-8"))
    ]
    assert said, (
        "no install surface says botainer is unpublished. A user who runs "
        "`pip install botainer` and gets 'No matching distribution found' has "
        "nowhere to learn why."
    )
