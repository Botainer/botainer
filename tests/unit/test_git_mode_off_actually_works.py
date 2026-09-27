"""The opt-out the git plugin's own refusal names must actually work.

THE DEFECT, measured 2026-08-31 by writing the config the error message tells
the user to write:

    refused: ... Remove them from .git/config, or set plugins.git.mode=off to
    opt out (you then own the risk).

    plugins:
      git:
        mode: off        <- YAML 1.1 parses this as the BOOLEAN False

`_read_mode` tested `mode in ("guarded", "off")`, False matched neither, and the
fallback returned "guarded". So the documented escape hatch silently did
nothing and the `if mode == "off"` branch was unreachable for anyone who
followed the instructions.

It failed SAFE, which is why it survived — and that is the trap. A remedy the
product NAMES and that quietly does nothing teaches the user the product is
broken, and the next thing they reach for is dropping `git` from
plugins_enabled, which removes the guard altogether (#201).

THE TEST THAT WOULD HAVE CAUGHT IT is this one: drive the DOCUMENTED spelling,
not the convenient one. A test written as `mode: "off"` (quoted) passes against
the broken code and proves nothing about what a user experiences.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK = (Path(__file__).resolve().parents[2]
        / "plugins" / "git" / "hooks" / "pre_session.py")


def _load():
    spec = importlib.util.spec_from_file_location("_git_pre_session", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _project(tmp_path: Path, mode_line: str) -> Path:
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text(
        "version: config-v1\nagent: claude\n"
        "plugins:\n  git:\n" + mode_line)
    return proj


def test_the_exact_spelling_the_refusal_tells_you_to_write(tmp_path) -> None:
    """`mode: off`, bare, as the error message dictates. The whole point."""
    mod = _load()
    proj = _project(tmp_path, "    mode: off\n")
    assert mod._read_mode(proj) == "off", (
        "the refusal message says `set plugins.git.mode=off to opt out`; if "
        "that exact text does not turn the guard off, the product is giving "
        "the user a remedy that does nothing"
    )


@pytest.mark.parametrize("spelling", [
    "    mode: off\n",        # YAML boolean False
    '    mode: "off"\n',      # the string
    "    mode: false\n",
    "    mode: no\n",
    "    mode: OFF\n",        # case
    "    mode: disabled\n",
])
def test_every_reasonable_way_to_say_off_means_off(tmp_path, spelling) -> None:
    assert _load()._read_mode(_project(tmp_path, spelling)) == "off"


@pytest.mark.parametrize("spelling", [
    "    mode: guarded\n",
    "    mode: on\n",
    "    mode: true\n",
])
def test_guarded_spellings_stay_guarded(tmp_path, spelling) -> None:
    assert _load()._read_mode(_project(tmp_path, spelling)) == "guarded"


def test_an_unrecognised_value_is_guarded_AND_says_so(tmp_path, capsys) -> None:
    """Silent fallback to the safe value is what hid the original bug.

    Guarded is the right OUTCOME; being quiet about it is what let a typo look
    like a working opt-out for as long as anyone cared to look.
    """
    mod = _load()
    assert mod._read_mode(_project(tmp_path, "    mode: gaurded\n")) == "guarded"
    assert "not a value I recognise" in capsys.readouterr().err


def test_no_config_and_no_mode_key_default_to_guarded(tmp_path) -> None:
    mod = _load()
    bare = tmp_path / "bare"
    bare.mkdir()
    assert mod._read_mode(bare) == "guarded"
    proj = tmp_path / "p2"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text("version: config-v1\n")
    assert mod._read_mode(proj) == "guarded"
