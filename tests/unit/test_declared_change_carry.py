"""#192: a HAND-EDIT of config.yaml must not silently strand the history.

`auth use` and `config set` both warn and offer to carry the agent's config dir
when a change relocates it. Editing `.botainer/config.yaml` by hand and running
`botainer start` carried NOTHING — and that is the route botainer's own
`--auth-profile` help text recommends for a persistent change. The user's agent
looks like it lost its transcripts; they are sitting in the old directory.

`state/declared.py` shipped the DETECTION on 2026-09-01, deliberately
record-only, with the prompting policy split out "to land separately". It never
did: until the function under test here, `declared` was imported by nothing but
its own test. These pin the consuming half.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from botainer.cli import _history_prompt
from botainer.state import declared


def _project(tmp_path: Path, **cfg) -> Path:
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True, exist_ok=True)
    (proj / ".botainer" / "config.yaml").write_text(yaml.safe_dump(cfg))
    (proj / ".botainer" / "project-id").write_text("u" * 32)
    return proj


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the state root at tmp_path and hand back the project state dir."""
    from botainer.core import identity
    root = tmp_path / "state-root"
    proj_state = root / "state" / ("u" * 32)
    proj_state.mkdir(parents=True)

    # THE REAL ProjectPaths, not a stand-in. The stand-in here used to be:
    #
    #     class _Proj:
    #         def __init__(self, r): self.root = r
    #
    # and `ProjectPaths` has NO `root` attribute — it has `base`, `data_dir`,
    # `home_dir`, `locks_dir`, `meta_path`, `packages_dir`, `scratch_dir`,
    # `sessions_dir`. So the fixture INVENTED the API the code under test used,
    # the production `proj.root` raised AttributeError, the bare
    # `except Exception: return` swallowed it, and these tests stayed green for
    # the entire life of a feature that had never run once (#224).
    #
    # A fixture that defines the interface cannot detect the caller using a
    # wrong one. Using the real object makes that class of drift impossible
    # rather than merely noticed.
    # No stand-in and no patching of the state layer at all: point MY_BOTAINER
    # at the tmp root and let the REAL ensure_user_state_dir / ProjectPaths run.
    monkeypatch.setenv("MY_BOTAINER", str(root))
    monkeypatch.setattr(identity, "read_project_id", lambda p: "u" * 32)
    return root, proj_state


def _history_at(state_root: Path, mode: str, profile: str) -> Path:
    d = _history_prompt.history_dir_for(state_root, "u" * 32, "agent-claude",
                                        mode, profile)
    d.mkdir(parents=True, exist_ok=True)
    (d / "history.jsonl").write_text('{"real": "history"}\n')
    return d


def test_a_first_start_says_nothing_and_records(wired, tmp_path, capsys):
    """No previous record means "I do not know what config said last time".
    Reporting a change there would fire on every project's first start."""
    root, proj_state = wired
    proj = _project(tmp_path, agent="claude", profile="default")
    _history_prompt.offer_carry_for_declared_change(proj, can_prompt=False)
    assert capsys.readouterr().err == ""
    assert declared.read(proj_state).values["profile"] == "default"


def test_a_hand_edited_profile_is_noticed_and_offered(wired, tmp_path, capsys):
    root, proj_state = wired
    declared.write_if_changed(proj_state, {"agent": "claude",
                                           "profile": "default",
                                           "auth_mode": "shared"})
    _history_at(root, "shared", "default")          # real history to strand
    proj = _project(tmp_path, agent="claude", profile="work")

    _history_prompt.offer_carry_for_declared_change(proj, can_prompt=False)
    err = capsys.readouterr().err
    assert "You changed profile" in err
    assert "still in the old" in err, "must say the history is not gone"
    assert "botainer config set profile work" in err, "must give the way out"
    # And it recorded, so the same edit is not re-reported on every start.
    assert declared.read(proj_state).values["profile"] == "work"


def test_a_switch_to_broker_mode_is_noticed(wired, tmp_path, capsys):
    """broker-state/<p> vs profiles/<p> is the OTHER path component, and the
    one a hand-edit of plugins_enabled changes."""
    root, proj_state = wired
    declared.write_if_changed(proj_state, {"agent": "claude",
                                           "profile": "default",
                                           "auth_mode": "shared"})
    _history_at(root, "shared", "default")
    proj = _project(tmp_path, agent="claude", profile="default",
                    plugins_enabled=["git", "agent-claude-broker"])

    _history_prompt.offer_carry_for_declared_change(proj, can_prompt=False)
    err = capsys.readouterr().err
    assert "You changed auth_mode" in err
    assert "broker-state" in err, "must name the directory it moved to"


def test_an_agent_switch_says_fresh_start_and_never_offers_a_carry(
        wired, tmp_path, capsys):
    """The two agents' config dirs share no format — copying either into the
    other is junk the tool then has to survive reading."""
    root, proj_state = wired
    declared.write_if_changed(proj_state, {"agent": "claude",
                                           "profile": "default",
                                           "auth_mode": "shared"})
    _history_at(root, "shared", "default")
    proj = _project(tmp_path, agent="codex", profile="default")

    _history_prompt.offer_carry_for_declared_change(proj, can_prompt=False)
    err = capsys.readouterr().err
    assert "FRESH history" in err
    assert "comes back if you switch back" in err, "must say nothing is deleted"
    assert "config set" not in err, "must not offer a cross-agent carry"


def test_no_history_to_strand_means_no_noise(wired, tmp_path, capsys):
    """A switch in a project whose old directory is empty has stranded
    nothing. Saying so anyway is the warn-that-fires-every-time failure."""
    root, proj_state = wired
    declared.write_if_changed(proj_state, {"agent": "claude",
                                           "profile": "default",
                                           "auth_mode": "shared"})
    proj = _project(tmp_path, agent="claude", profile="work")
    _history_prompt.offer_carry_for_declared_change(proj, can_prompt=False)
    assert capsys.readouterr().err == ""


def test_an_unreadable_project_never_raises(wired, tmp_path):
    """This runs on the launch path. A failure to NOTICE must never be a
    failure to START."""
    _history_prompt.offer_carry_for_declared_change(
        tmp_path / "does-not-exist", can_prompt=False)
