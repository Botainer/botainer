"""T1-6 regression: the *-shared pre_session hooks write README.shared-mode.txt
into the RW-into-container profile dir. A prior CAGED session can plant a
dangling symlink there to redirect this UNCAGED host write to an arbitrary path
(`if not exists(): write_text()` follows a dangling symlink). The hook must
create the README with O_EXCL|O_NOFOLLOW so a symlink at that path is refused,
never followed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_FAKE_CLAUDE_OAUTH = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-test-not-real",
        "refreshToken": "sk-ant-ort01-test-not-real",
        "expiresAt": 9999999999999,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }
}


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_shared_readme_write_refuses_planted_symlink(tmp_path: Path, agent: str) -> None:
    state = tmp_path / "state"
    uid = "11111111-1111-4111-8111-111111111111"
    hook = REPO / "plugins" / f"agent-{agent}-shared" / "hooks" / "pre_session.py"
    dotdir = ".claude" if agent == "claude" else ".codex"

    # Seed a valid shared credential so the hook progresses to the README stage.
    shared = state / "shared-auth" / f"agent-{agent}"
    shared.mkdir(parents=True)
    if agent == "claude":
        (shared / ".credentials.json").write_text(json.dumps(_FAKE_CLAUDE_OAUTH))
        os.chmod(shared / ".credentials.json", 0o600)
        (shared / ".claude.json").write_text("{}")
        os.chmod(shared / ".claude.json", 0o600)
    else:
        (shared / "auth.json").write_text(json.dumps({"api_key": "sk-test"}))
        os.chmod(shared / "auth.json", 0o600)

    per_project = state / "state" / uid / "data" / f"agent-{agent}" / "profiles" / "default"
    per_project.mkdir(parents=True)
    # A prior caged session plants a DANGLING symlink at the README path,
    # pointing at a host file that does not exist.
    escape_target = tmp_path / "PWNED-host-file"
    (per_project / "README.shared-mode.txt").symlink_to(escape_target)

    env = {
        **os.environ,
        "BOTAINER_PROJECT_UUID": uid,
        "BOTAINER_STATE_ROOT": str(state),
        "MY_BOTAINER": str(state),
        "BOTAINER_PROFILE": "default",
        "BOTAINER_PLUGINS_ENABLED": f"agent-{agent}-shared",
    }
    subprocess.run(
        [sys.executable, str(hook)], env=env, capture_output=True, text=True, timeout=30
    )

    # The security property: the dangling symlink was NOT followed, so the
    # attacker-named host file was NOT created.
    assert not escape_target.exists(), (
        f"agent-{agent}-shared pre_session followed a planted symlink and created "
        f"{escape_target} — host-write escape (T1-6)"
    )
    # Non-vacuous: the hook progressed past credential setup (it created the
    # in-container credential symlink), so it DID reach the README write region.
    cred_link = per_project / (".credentials.json" if agent == "claude" else "auth.json")
    assert cred_link.is_symlink(), (
        "hook did not reach the bind/README stage — test would be vacuous"
    )
