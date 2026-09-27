"""The carry's idea of "where history lives" must equal what the hooks BIND.

If these drift, the carry copies files between two directories no session ever
reads. That failure is silent and looks exactly like success: it reports "copied
412 files", and the next session is still empty. So this does not read the hook
source and compare strings — it RUNS each hook the way the plugin loader does
and reads the bind source out of the contribution it prints.

("Reading is NOT verification. Observe the running system." — CLAUDE.md, after
an evening lost to eight code-reading lenses agreeing with each other while one
`ls -la` settled the question.)

The broker hooks are not covered here: `start_broker.py` starts a broker, which
is not a thing a unit test may do. Their paths are pinned by
`test_history_prompt.py` instead, and the two are noted in the gap list below so
nobody reads this file as covering all four modes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from botainer.cli._history_prompt import history_dir_for

REPO = Path(__file__).resolve().parents[2]

# (plugin dir, mode as `auth use` names it, container path it binds)
RUNNABLE_PRE_SESSION_HOOKS = [
    ("agent-claude", "isolated", "/home/agent/.claude"),
    ("agent-codex", "isolated", "/home/agent/.codex"),
]

# Not runnable in a unit test, and why. Listed so the coverage gap is stated
# rather than implied by absence — an absent case reads as a covered one.
NOT_COVERED_HERE = {
    "agent-claude-broker": "start_broker.py launches a broker process",
    "agent-codex-broker": "start_broker.py launches a broker process",
    "agent-claude-shared": "needs a populated host-wide shared-auth store",
    "agent-codex-shared": "needs a populated host-wide shared-auth store",
}


def _seed_credential(plugin: str, creds_dir: Path) -> None:
    """Give the hook the credential it refuses to run without.

    Each hook checks for its OWN filename, so seeding the wrong one produces a
    refusal that looks like a path mismatch and would send someone hunting the
    wrong bug.
    """
    creds_dir.mkdir(parents=True, exist_ok=True)
    if plugin == "agent-claude":
        (creds_dir / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    else:
        (creds_dir / "api_key").write_text("sk-test")


@pytest.mark.parametrize("plugin,mode,container_path", RUNNABLE_PRE_SESSION_HOOKS)
@pytest.mark.parametrize("profile", ["default", "work"])
def test_the_hook_binds_the_directory_the_carry_targets(
        tmp_path, plugin, mode, container_path, profile) -> None:
    uid = "11111111-2222-3333-4444-555555555555"
    expected = history_dir_for(tmp_path, uid, plugin, mode, profile)
    _seed_credential(plugin, expected)

    env = dict(os.environ)
    env.update({
        "BOTAINER_PROJECT_UUID": uid,
        "BOTAINER_STATE_ROOT": str(tmp_path),
        "BOTAINER_PROFILE": profile,
        "BOTAINER_PLUGINS_ENABLED": plugin,
    })
    proc = subprocess.run(
        [sys.executable, str(REPO / "plugins" / plugin / "hooks" / "pre_session.py")],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, (
        f"{plugin} pre_session refused: {proc.stderr}")

    contribution = json.loads(proc.stdout)
    binds = [b for b in contribution["binds"] if b["target"] == container_path]
    assert binds, (
        f"{plugin} contributed no bind at {container_path}; it binds "
        f"{[b['target'] for b in contribution['binds']]}"
    )
    assert Path(binds[0]["source"]) == expected, (
        f"the carry would copy history into {expected}, but {plugin} binds "
        f"{binds[0]['source']} — every carry would silently target a directory "
        f"no session reads"
    )


def test_the_uncovered_modes_are_named_not_forgotten() -> None:
    """A coverage gap that is written down is a gap; an unwritten one is a lie.

    Fails if a new agent plugin with a pre_session hook appears and is neither
    exercised above nor listed as a stated exception — which is the moment
    someone would otherwise assume this file already covered it.
    """
    covered = {p for p, _, _ in RUNNABLE_PRE_SESSION_HOOKS}
    on_disk = {
        d.name for d in (REPO / "plugins").iterdir()
        if d.name.startswith("agent-") and (d / "hooks" / "pre_session.py").exists()
    }
    unaccounted = on_disk - covered - set(NOT_COVERED_HERE)
    assert not unaccounted, (
        f"agent plugins with a pre_session hook that this file neither runs "
        f"nor declares out of scope: {sorted(unaccounted)}"
    )
