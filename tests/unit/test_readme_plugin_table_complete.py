"""The README's bundled-plugin table must list every bundled plugin.

It listed 9 of 14. Missing were the whole `-shared`/`-broker` auth-mode family
and `browser` — i.e. the mode botainer actually recommends for credentials
(`auth use broker`) and one of its largest capability surfaces. A user reading
the README could not learn that either existed.

This is drift, not a typo: plugins were added to `plugins/` over months and the
table was written once. So assert the relationship rather than the contents —
the table is checked against the directory listing, which is the thing that
actually determines what ships.

Deliberately one-directional in strictness: every bundled plugin must appear,
and the table may not invent one that doesn't exist. It says nothing about the
Purpose column, which is prose.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _bundled_plugin_names() -> set[str]:
    root = REPO / "plugins"
    return {p.name for p in root.iterdir()
            if p.is_dir() and (p / "botainer-plugin.yaml").is_file()}


def _readme_table_names() -> set[str]:
    body = (REPO / "README.md").read_text(encoding="utf-8")
    section = re.search(r"## What you get out of the box(.*?)(?=^## )", body, re.S | re.M)
    assert section, "README no longer has a 'What you get out of the box' section"
    return set(re.findall(r"^\| `([a-z0-9-]+)` \|", section.group(1), re.M))


def test_every_bundled_plugin_is_in_the_readme_table() -> None:
    missing = _bundled_plugin_names() - _readme_table_names()
    assert not missing, (
        f"README's bundled-plugin table omits {sorted(missing)}. A plugin that "
        f"ships but isn't documented is one the user cannot discover.")


def test_readme_table_lists_no_plugin_that_does_not_ship() -> None:
    phantom = _readme_table_names() - _bundled_plugin_names()
    assert not phantom, (
        f"README's table lists {sorted(phantom)}, which are not in plugins/. "
        f"Either the plugin was removed and the row wasn't, or it's a typo.")
