"""History carry reports settings files withheld from the transfer.

Codex mount modes and broker mode use different history directories. Carrying
transcripts between them does not mean every setting is safe to transfer:
broker-generated `config.toml` can contain routing to a per-session broker.
Copying agent-authored endpoint configuration alongside a real credential would
change where that credential is sent.

`plan.withheld` records each excluded path and reason. These tests check that
the command prints this information, rather than promising to carry all settings
and silently leaving a file behind."""
from __future__ import annotations

import json
import subprocess

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli._history_prompt import history_dir_for
from botainer.cli.config_cmd import config as config_cmd
from botainer.cli.init import init
from botainer.state import dir as state_dir


@pytest.fixture
def codex_project(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    state_dir.ensure_user_state_dir(create_if_missing=True)
    from botainer.plugins import builtin as plugin_builtin
    plugin_builtin.install_all_builtin()

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    monkeypatch.chdir(proj)
    first = CliRunner().invoke(init, ["--agent", "codex", "--non-interactive"])
    assert first.exit_code == 0, f"fixture init failed:\n{first.output}"

    cfg = proj / ".botainer" / "config.yaml"
    d = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    d["plugins_enabled"] = ["agent-codex-shared", "git"]
    cfg.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")

    uid = (proj / ".botainer" / "project-id").read_text(encoding="utf-8").strip()
    root = tmp_path / "state"
    src = history_dir_for(root, uid, "agent-codex-shared", "shared", "default")
    src.mkdir(parents=True, exist_ok=True)

    def _switch_to_broker():
        return CliRunner().invoke(
            config_cmd,
            ["set", "plugins_enabled", "[agent-codex-broker, git]", "--yes"])

    return src, _switch_to_broker


def test_a_withheld_settings_file_is_named(codex_project):
    """THE DEFECT. `config.toml` stayed and the output never mentioned it."""
    src, switch = codex_project
    (src / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    (src / "history.jsonl").write_text(json.dumps({"a": 1}) + "\n",
                                       encoding="utf-8")

    res = switch()

    assert "config.toml" in res.output, (
        f"a file was left behind and the output never named it:\n{res.output}")
    assert "NOT moved" in res.output, (
        f"nothing marks the file as deliberately withheld:\n{res.output}")


def test_the_reason_travels_with_the_name(codex_project):
    """A name with no reason invites the user to 'fix' it by copying the file
    across by hand — which is exactly the credential-adjacent move the carry
    refuses to make for them."""
    src, switch = codex_project
    (src / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    (src / "history.jsonl").write_text("{}\n", encoding="utf-8")

    res = switch()

    assert "describes the mode it lives in" in res.output, (
        f"the withheld file is named without a reason:\n{res.output}")


def test_a_carry_with_nothing_withheld_stays_quiet(codex_project):
    """THE OPPOSITE DIRECTION. Most switches withhold nothing, and a heading
    with an empty list under it is the scenery that trains people to skim."""
    src, switch = codex_project
    (src / "history.jsonl").write_text("{}\n", encoding="utf-8")
    (src / "sessions").mkdir(exist_ok=True)
    (src / "sessions" / "rollout.jsonl").write_text("{}\n", encoding="utf-8")

    res = switch()

    assert "NOT moved" not in res.output, (
        f"the withheld heading printed with nothing to list:\n{res.output}")


def test_transcripts_really_do_cross(codex_project):
    """THE REASSURING HALF, and it must not regress: the fear was that sessions
    were being lost. They are not — including whole subdirectories."""
    src, switch = codex_project
    (src / "sessions" / "2026" / "09").mkdir(parents=True, exist_ok=True)
    (src / "sessions" / "2026" / "09" / "rollout-a.jsonl").write_text(
        "{}\n", encoding="utf-8")
    (src / "config.toml").write_text("model = 'x'\n", encoding="utf-8")

    switch()

    moved = src.parent.parent / "broker-state" / "default"
    assert (moved / "sessions" / "2026" / "09" / "rollout-a.jsonl").is_file(), (
        f"a transcript did not cross the switch; {sorted(p.name for p in moved.rglob('*'))}")


def test_the_count_agrees_with_its_own_noun(codex_project):
    """"Moved 1 files" — the one line in this module that did not use the
    helper written for exactly this, so a single switch printed "3 files" in
    the plan and "1 files" in the result."""
    src, switch = codex_project
    (src / "history.jsonl").write_text("{}\n", encoding="utf-8")

    res = switch()

    assert "1 files" not in res.output, (
        f"the result line still disagrees with its own count:\n{res.output}")
    assert "Moved 1 file " in res.output or "Moved 1 file(" in res.output, (
        f"the singular case does not report at all:\n{res.output}")
