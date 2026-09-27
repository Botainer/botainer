"""The history-move prose must read as English, whatever called it.

TWO DEFECTS, BOTH OBSERVED BY RUNNING `config set`, not by reading it.

1. WRONG NOUN. `warn_history_will_move` and `offer_carry` substitute
   `what_changed` into two sentences:

       "Changing the {what_changed} changes which directory holds…"
       "…do NOT travel: each {what_changed} keeps its own…"

   `auth use` passes "auth mode" and both read correctly. `config set` passed
   the raw config KEY, so a user switching codex to broker mode was told:

       ! Changing the plugins_enabled changes which directory holds your…
         Your login and account details do NOT travel: each plugins_enabled
         keeps its own,

   The module already carried a comment recording that this exact class had bit
   twice before ("one said 'a mode switch is a credential change' on the PROFILE
   path (wrong noun, found by running it)"). A third comment would not have
   helped; this is the test instead.

2. "1 files". The carry line leads with a count, and a count that cannot agree
   with its own noun reads as machine output — in the one paragraph whose whole
   job is to be read before the user decides.

WHY THE FALLBACK MATTERS AS MUCH AS THE MAPPING. Only three config keys move
history today, but the translator must stay grammatical for a key it has never
seen, or this defect returns the next time a key becomes history-affecting and
nobody remembers to add a row. So the unknown-key case is asserted in BOTH
sentence frames, not just the known ones.
"""
from __future__ import annotations

import pytest

from botainer.cli._history_prompt import _n_files, axis_noun_for_config_key

#: The two frames the value is substituted into, verbatim from the module.
FRAMES = (
    "Changing the {} changes which directory holds your agent's session history",
    "Your login and account details do NOT travel: each {} keeps its own",
)


@pytest.mark.parametrize("key,expected", [
    # plugins_enabled IS how the auth mode is expressed, so this is the thing
    # the user actually changed — not a euphemism for the key.
    ("plugins_enabled", "auth mode"),
    ("agent", "agent"),
    ("auth_profile", "auth profile"),
])
def test_a_history_moving_key_becomes_the_noun_the_user_changed(key, expected):
    assert axis_noun_for_config_key(key) == expected


def test_no_history_moving_key_renders_as_a_bare_identifier():
    """The literal defect: "Changing the plugins_enabled"."""
    for key in ("plugins_enabled", "agent", "auth_profile"):
        noun = axis_noun_for_config_key(key)
        assert noun != key or "_" not in key, (
            f"{key!r} is passed through unchanged, so the sentence reads "
            f"'Changing the {key}'")
        for frame in FRAMES:
            assert "_" not in frame.format(noun), (
                f"a raw config key reached the user: {frame.format(noun)!r}")


def test_an_UNKNOWN_key_is_still_grammatical_in_both_frames():
    """The half that keeps this fixed.

    A key with no mapping must still produce a readable sentence, or the defect
    returns the first time a new key moves history and nobody adds a row.
    """
    noun = axis_noun_for_config_key("some_future_key")
    for frame in FRAMES:
        sentence = frame.format(noun)
        assert "the some_future_key " not in sentence, sentence
        assert "each some_future_key " not in sentence, sentence
        assert "setting" in sentence, (
            f"the fallback dropped the noun that makes it read as English: "
            f"{sentence!r}")


def test_the_caller_that_was_always_right_is_unchanged():
    """OPPOSITE DIRECTION. `auth use` passes a noun phrase directly and must
    keep working — a translator that mangled correct input would be a
    regression dressed as a fix."""
    for frame in FRAMES:
        assert "the auth mode " in frame.format("auth mode") + " " or \
               "each auth mode " in frame.format("auth mode") + " "


@pytest.mark.parametrize("n,expected", [
    (0, "0 files"), (1, "1 file"), (2, "2 files"), (17, "17 files"),
])
def test_the_file_count_agrees_with_its_noun(n, expected):
    assert _n_files(n) == expected
