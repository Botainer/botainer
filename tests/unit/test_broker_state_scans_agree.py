"""Three copies of one scan must agree, or the copy is a liability.

`botainer.core.history_carry.credential_files_under` finds credential-named
files under a directory. Both broker hooks need the same answer at spawn time
and CANNOT import it: a hook runs as a subprocess with no guarantee that
`botainer` is on its path. So there are three implementations.

That is the arrangement this repo keeps getting hurt by. The whole broker-state
thread began because `auth status` looked somewhere `auth profiles` did not, and
the claude broker got a spawn warning that the codex broker did not — sibling
drift twice over, in the same subsystem, inside three weeks.

A comment saying "keep these in sync" is not a mechanism. This file is: it
drives all three against one fixture set and fails naming whichever disagrees.
The duplication stays (it is forced); diverging silently does not.

WHAT THE FIXTURES COVER, and why each is here rather than being an arbitrary
pile: the plain case, the NESTED case (the profile dir is bound whole, so depth
changes nothing about reachability), the `.pre-shared` BACKUP spelling (this
project's own docs say those are credentials), the codex filenames, a lock file
that must NOT match (the prefix rule in `_is_credential_name` would wrongly
claim it), and ordinary session state that must stay quiet.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_HOOKS = {
    "agent-claude-broker": REPO / "plugins/agent-claude-broker/hooks/start_broker.py",
    "agent-codex-broker": REPO / "plugins/agent-codex-broker/hooks/start_broker.py",
}


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"_hook_{path.parent.parent.name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tree(tmp_path):
    """One directory holding every shape the scan has to get right."""
    d = tmp_path / "broker-state" / "default"
    (d / "nested" / "deeper").mkdir(parents=True)

    (d / ".credentials.json").write_text("{}")            # claude OAuth
    (d / "auth.json").write_text("{}")                    # codex OAuth
    (d / "api_key").write_text("sk-fake")                 # codex key-paste
    (d / ".credentials.json.pre-shared").write_text("{}")  # the backup spelling
    (d / "nested" / "deeper" / "auth.json").write_text("{}")

    # MUST NOT match. The lock file is the one that matters: history_carry's
    # `_is_credential_name` has a PREFIX rule that would call it a credential,
    # and reporting a lock file as a leaked secret would make the real warning
    # unreadable.
    (d / ".credentials.json.lock").write_text("")
    (d / ".claude.json").write_text("{}")
    (d / "history.jsonl").write_text("")
    (d / "config.toml").write_text("")
    (d / "nested" / "settings.json").write_text("{}")
    return d


_EXPECTED = {
    ".credentials.json",
    ".credentials.json.pre-shared",
    "auth.json",
    "api_key",
    "nested/deeper/auth.json",
}


def _rel(root, paths):
    return {str(Path(p).relative_to(root)) for p in paths}


def test_the_library_scan_finds_exactly_the_credential_names(tree):
    """The reference answer the two copies are measured against."""
    from botainer.core.history_carry import credential_files_under

    assert _rel(tree, credential_files_under(tree)) == _EXPECTED


@pytest.mark.parametrize("plugin", sorted(_HOOKS))
def test_each_hook_agrees_with_the_library(plugin, tree):
    """THE POINT OF THIS FILE.

    If a hook's copy is edited and the library's is not — or the reverse — this
    says which one moved and how. Without it, the two drift apart exactly the
    way `auth status` and `auth profiles` did, and the first symptom is a
    credential nobody warned about.
    """
    from botainer.core.history_carry import credential_files_under

    hook = _load(_HOOKS[plugin])
    assert hasattr(hook, "_credential_names_under"), (
        f"{plugin} has no spawn-time scan at all. That was true of "
        f"agent-codex-broker for three weeks while the claude side had one.")

    theirs = _rel(tree, hook._credential_names_under(tree))
    ours = _rel(tree, credential_files_under(tree))

    assert theirs == ours, (
        f"{plugin}'s copy disagrees with "
        f"botainer.core.history_carry.credential_files_under.\n"
        f"  only the hook found: {sorted(theirs - ours)}\n"
        f"  only the library found: {sorted(ours - theirs)}")


@pytest.mark.parametrize("plugin", sorted(_HOOKS))
def test_each_hook_pins_the_same_filename_set(plugin):
    """The names themselves, not just the walk.

    A hook could match the library's shape and still be scanning for a
    different set. Pinned to the real registry so adding a credential filename
    to the product cannot leave either broker blind to it.
    """
    from botainer.core.history_carry import CREDENTIAL_FILENAMES

    hook = _load(_HOOKS[plugin])
    assert set(hook._CREDENTIAL_FILENAMES) == set(CREDENTIAL_FILENAMES), (
        f"{plugin}'s local copy of CREDENTIAL_FILENAMES has drifted from "
        f"botainer.core.history_carry's. A hook cannot import the real one, so "
        f"this test is the only thing holding them together.")


@pytest.mark.parametrize("plugin", sorted(_HOOKS))
def test_each_hook_is_QUIET_on_a_clean_directory(plugin, tmp_path):
    """The control, for both.

    `broker-state/` exists on every broker project and legitimately fills with
    session state. A scan keyed on the DIRECTORY rather than its CONTENTS would
    warn on every launch, and a warning that always fires is the scenery this
    project has a rule about.
    """
    d = tmp_path / "broker-state" / "default"
    (d / "sessions").mkdir(parents=True)
    (d / ".claude.json").write_text("{}")
    (d / "config.toml").write_text("")
    (d / "sessions" / "abc.jsonl").write_text("")

    hook = _load(_HOOKS[plugin])

    assert hook._credential_names_under(d) == [], (
        f"{plugin} would warn about an ordinary populated broker-state dir: "
        f"{hook._credential_names_under(d)}")


@pytest.mark.parametrize("plugin", sorted(_HOOKS))
def test_each_hook_survives_an_unreadable_subdirectory(plugin, tmp_path):
    """A reporting helper must never turn a permissions problem into a crash.

    This runs on the launch path. A traceback here does not just lose the
    warning — it takes the session with it.
    """
    import os
    d = tmp_path / "broker-state" / "default"
    locked = d / "locked"
    locked.mkdir(parents=True)
    (d / ".credentials.json").write_text("{}")
    os.chmod(locked, 0o000)
    try:
        hook = _load(_HOOKS[plugin])
        found = hook._credential_names_under(d)
        assert _rel(d, found) == {".credentials.json"}, (
            f"{plugin} lost the finding it COULD see, or crashed: {found}")
    finally:
        os.chmod(locked, 0o700)
