"""What a project's directory alias does with a real-world name.

The charset is an ALLOWLIST — only [A-Za-z0-9._-] can reach a path we build —
and that is not up for negotiation: widening it to Unicode means macOS NFD vs
Linux NFC producing two different byte strings for the same name, plus
case-folding and shell-quoting on every path we print.

What IS up for negotiation is how much information the allowlist throws away.
These pin the answer.
"""

from __future__ import annotations

import pytest

from botainer.state.dir import _sanitize_for_dirname as clean


@pytest.mark.parametrize("raw,expected", [
    # Accents fold to their base letters instead of becoming underscores.
    ("Café-Analyse", "Cafe-Analyse"),
    ("Müller_data", "Muller_data"),
    ("naïve café  résumé", "naive-cafe-resume"),
    ("Škoda", "Skoda"),
    # Whitespace reads better as a dash.
    ("my project", "my-project"),
    ("  leading and trailing  ", "leading-and-trailing"),
    # A run of separators collapses to its first character.
    ("my.project (v2)", "my.project-v2"),
    ("a///b", "a_b"),
    # Ordinary names pass through untouched.
    ("tesi-magistrale", "tesi-magistrale"),
    ("analysis_2026", "analysis_2026"),
    # Leading dot would make the alias hidden.
    (".hidden", "hidden"),
])
def test_readable_names_stay_readable(raw, expected):
    assert clean(raw) == expected


@pytest.mark.parametrize("raw", ["数据分析", "проект", "ניתוח", "", "   ", "!!!", "---"])
def test_nothing_usable_falls_back_to_a_word_not_punctuation(raw):
    """A row of underscores is a bug; `project` is a fallback.

    KNOWN LIMIT, pinned deliberately: a name in a script with no Latin
    equivalent degrades to `project-<short-uuid>` and the uuid does all the
    identifying. Transliterating properly needs a dependency we do not want.
    The real fix is letting people SET the alias; this test exists so that
    degradation stays visible and intentional rather than being discovered by
    a user whose whole project list reads `_-1a2b3c4d`.
    """
    assert clean(raw) == "project"


def test_output_is_always_in_the_allowlist():
    hostile = [
        "../../etc/passwd", "a/b/c", "name with\ttab", "nul", "CON",
        "\x00null", "emoji 🎉 name", "'; rm -rf /", "$(whoami)", "--flag",
        "‮RTL-override", "a" * 500,
    ]
    for raw in hostile:
        out = clean(raw)
        assert out, f"{raw!r} produced an empty name"
        assert all(c.isalnum() or c in "._-" for c in out), f"{raw!r} -> {out!r}"
        assert "/" not in out and ".." not in out
        assert len(out) <= 65          # 64 + a possible reserved-name `_`
        assert not out.startswith("-")  # never looks like a CLI flag


def test_windows_reserved_basenames_are_prefixed():
    for raw in ("con", "CON", "prn", "aux", "nul", "com1", "lpt9"):
        assert clean(raw).startswith("_")


def test_distinct_names_do_not_needlessly_merge():
    # Sanitizing collapses information; make sure it does not collapse
    # everyday distinctions. (Non-Latin scripts DO merge — see the fallback
    # test above; that is the documented limit.)
    names = ["analysis", "analysis_2", "analysis-2", "Analysis",
             "my project", "my-project"]
    out = [clean(n) for n in names]
    # `my project` and `my-project` intentionally converge; the rest must not.
    assert len(set(out)) == len(names) - 1
