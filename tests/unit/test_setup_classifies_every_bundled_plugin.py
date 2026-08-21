"""`botainer setup` must classify and describe every bundled plugin.

`botainer setup` is the FIRST command a new user runs, and it prints a line per
bundled plugin with a `[default]` / `[opt-in]` tag. The tag comes from two
hand-maintained sets in `cli/setup.py`, and a plugin in neither falls through to
a literal `[?]`.

`browser` shipped in neither, so a first run printed:

    ✓ browser        v0.1.0    [?]  tree-sha sha256:3c4dd8eb0496...

next to thirteen classified plugins — the one line offering no answer, on the
one screen where a user has no context to guess from. Two more (both brokers,
one of them the recommended-secure auth mode) had no `--interactive`
description.

Found by installing the built wheel into a clean venv and running `setup`. It
is invisible to reading, because each list is internally consistent; only the
gap between a list and the plugin set is wrong, and nothing compared them.

The lists STAY lists — the classification is a genuine editorial judgement, not
something derivable from a manifest. What changes is that a gap now fails here
instead of reaching a user.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from botainer.plugins.builtin import BUILTIN_PLUGIN_NAMES

SETUP_PY = Path(__file__).resolve().parents[2] / "botainer" / "cli" / "setup.py"


def _literal_set(name: str) -> set[str]:
    """Extract a `NAME = {...}` set literal, one-line or multi-line.

    The first version used a lazy `.*?` up to `\\n    }`, which silently ran
    PAST a one-line literal to the close of the NEXT set — so DEFAULT_ENABLED
    returned the union of both and every membership check passed vacuously.
    Balancing the brace is the fix; a helper that reports the wrong set makes a
    test worse than no test.
    """
    body = SETUP_PY.read_text(encoding="utf-8")
    start = re.search(rf"^\s*{name} = \{{", body, re.M)
    assert start, f"{name} not found in cli/setup.py — did it move or get renamed?"
    i = start.end() - 1
    depth = 0
    for j in range(i, len(body)):
        if body[j] == "{":
            depth += 1
        elif body[j] == "}":
            depth -= 1
            if depth == 0:
                return set(re.findall(r'"([a-z][a-z0-9-]+)"', body[i:j]))
    raise AssertionError(f"{name} literal is not brace-balanced")


def _described() -> set[str]:
    body = SETUP_PY.read_text(encoding="utf-8")
    m = re.search(r"PLUGIN_DESCRIPTIONS = \{(.*?)\n    \}", body, re.S)
    assert m, "PLUGIN_DESCRIPTIONS not found in cli/setup.py"
    return set(re.findall(r'"([a-z][a-z0-9-]+)":', m.group(1)))


@pytest.mark.parametrize("plugin", sorted(BUILTIN_PLUGIN_NAMES))
def test_every_bundled_plugin_is_classified(plugin: str) -> None:
    """No plugin may reach a user's screen tagged `[?]`.

    Agent plugins are exempt from the static sets: `_effective_role` resolves
    them against `policy.default_auth_mode` and always returns default/opt-in,
    never `?`. Every other plugin needs an explicit entry.
    """
    if plugin.startswith("agent-"):
        pytest.skip("agent plugins are classified dynamically by auth mode")
    classified = _literal_set("DEFAULT_ENABLED") | _literal_set("OPT_IN")
    assert plugin in classified, (
        f"{plugin!r} is bundled but appears in neither DEFAULT_ENABLED nor "
        "OPT_IN in cli/setup.py, so `botainer setup` prints `[?]` beside it on "
        "a new user's first command. Add it to whichever set is true."
    )


@pytest.mark.parametrize("plugin", sorted(BUILTIN_PLUGIN_NAMES))
def test_every_bundled_plugin_has_a_description(plugin: str) -> None:
    assert plugin in _described(), (
        f"{plugin!r} is bundled but has no entry in PLUGIN_DESCRIPTIONS, so "
        "`botainer setup --interactive` offers it with no explanation of what "
        "it does."
    )


def test_the_sets_do_not_name_plugins_that_no_longer_exist() -> None:
    """The other direction: a removed plugin left in a set is a stale claim.

    Cheap to check, and it is how the lists rot in the opposite direction —
    entries accumulate for things that shipped once and were deleted.
    """
    stale = (_literal_set("DEFAULT_ENABLED") | _literal_set("OPT_IN")
             | _described()) - set(BUILTIN_PLUGIN_NAMES)
    assert not stale, (
        f"cli/setup.py classifies or describes {sorted(stale)}, which are not "
        "bundled plugins. Remove the entries."
    )


def test_the_parse_actually_found_the_lists() -> None:
    """Anti-vacuous: a regex that matched nothing would pass every test above.

    `_literal_set` asserts the block exists, but an empty block would still
    parse. This pins that the sets have real content.
    """
    assert len(_literal_set("OPT_IN")) >= 5, "OPT_IN parsed as near-empty"
    assert len(_described()) >= 10, "PLUGIN_DESCRIPTIONS parsed as near-empty"
    assert len(BUILTIN_PLUGIN_NAMES) >= 10, "no bundled plugins discovered"


def test_the_changelog_plugin_count_is_right() -> None:
    """A counted claim in a shipped doc rots the next time a plugin lands.

    The first draft of CHANGELOG.md said "Eleven bundled plugins, off unless
    enabled". Both halves were wrong — there are fourteen, and two are on after
    `botainer init`. Written from memory of an older tree, believed because it
    sounded right.
    """
    changelog = SETUP_PY.parents[2] / "CHANGELOG.md"
    if not changelog.is_file():
        pytest.skip("no CHANGELOG.md in this tree")
    words = {10: "Ten", 11: "Eleven", 12: "Twelve", 13: "Thirteen",
             14: "Fourteen", 15: "Fifteen", 16: "Sixteen", 17: "Seventeen"}
    n = len(BUILTIN_PLUGIN_NAMES)
    expected = words.get(n, str(n))
    body = changelog.read_text(encoding="utf-8")
    claimed = re.search(r"\b(\w+) bundled plugins\b", body)
    assert claimed, "CHANGELOG.md no longer states a bundled-plugin count"
    assert claimed.group(1) == expected, (
        f"CHANGELOG.md says {claimed.group(1)!r} bundled plugins; there are "
        f"{n} ({expected}). Update the sentence, or drop the count."
    )


def test_the_default_enabled_claim_matches_the_set() -> None:
    """The same sentence claims WHICH plugins are on by default."""
    changelog = SETUP_PY.parents[2] / "CHANGELOG.md"
    if not changelog.is_file():
        pytest.skip("no CHANGELOG.md in this tree")
    body = changelog.read_text(encoding="utf-8")
    defaults = _literal_set("DEFAULT_ENABLED")
    assert defaults == {"agent-claude", "git"}, (
        f"DEFAULT_ENABLED is now {sorted(defaults)}; CHANGELOG.md describes it "
        "as 'the Claude agent and protected git mode'. Update the prose."
    )
    assert "off unless enabled" not in body, (
        "CHANGELOG.md claims every plugin is off unless enabled, but "
        f"{sorted(defaults)} are on after `botainer init`."
    )
