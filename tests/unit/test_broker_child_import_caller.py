"""Exercise hook-runner scrubbing and both real broker hook main functions.

Only process boundaries are controlled: the hook script runs in-process with
exactly the environment produced by run_hook, and Popen is stopped at dispatch.
No broker, socket, network or real credential is used. Import behavior of the
actual child prefixes is tested by test_host_self_import_isolation separately.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from botainer.plugins import hooks
from botainer.state import cluster_profile

REPO = Path(__file__).resolve().parents[2]


class CapturedChild(Exception):
    pass


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_real_hook_caller_preserves_isolation_and_scrubs_credentials(tmp_path, monkeypatch, agent):
    plugin = f"agent-{agent}-broker"
    hook = REPO / "plugins" / plugin / "hooks/start_broker.py"
    spec = importlib.util.spec_from_file_location(f"_{agent}_broker_import_caller", hook)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home, project = (tmp_path / name for name in ("home", "project"))
    state = home / "state"
    for directory in (home, project, state):
        directory.mkdir()
    project_uuid = "11111111-1111-4111-8111-111111111111"
    project_state = state / "state" / project_uuid
    session = project_state / "sessions" / "abcdef0123456789"
    session.mkdir(parents=True)
    record = session / "spec.json"
    record.write_text(json.dumps({"schema_version": 1,
        "session_id": session.name, "project_uuid": project_uuid,
        "project_root": str(project), "runtime": "docker", "runtime_handle": {},
        "spec": {"state_dir": str(project_state)}}))
    credentials = home / "synthetic-credential.json"
    credentials.write_text(json.dumps({"OPENAI_API_KEY": "sk-FAKE-not-a-real-key"}
        if agent == "codex" else {"claudeAiOauth": {
            "accessToken": "FAKE-not-a-real-token", "expiresAt": 4102444800000}}))
    credentials.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_BOTAINER", str(state))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic/unavailable-agent")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "HOST-MUST-NOT-LEAK")
    monkeypatch.setenv("OPENAI_API_KEY", "HOST-MUST-NOT-LEAK")
    monkeypatch.setenv("PYTHONPATH", str(project))
    monkeypatch.setattr(cluster_profile, "active_profile", lambda: None)
    monkeypatch.setattr(module, "_free_tcp_port", lambda _host: 49152)
    captured = {}

    def capture_child(argv, **kwargs):
        captured["argv"], captured["kwargs"] = argv, kwargs
        raise CapturedChild

    def execute_hook(argv, *, env, **kwargs):
        # run_hook still decides the script/interpreter and scrubs ambient
        # environment. We preserve that exact boundary while avoiding a real
        # helper spawn and dependence on a globally installed test checkout.
        assert argv == [sys.executable, str(hook)]
        assert "PYTHONPATH" not in env
        assert "SSH_AUTH_SOCK" not in env
        assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
        with patch.dict(os.environ, env, clear=True):
            return module.main()

    monkeypatch.setattr(module.subprocess, "Popen", capture_child)
    monkeypatch.setattr(hooks.subprocess, "run", execute_hook)
    with pytest.raises(CapturedChild):
        hooks.run_hook(plugin_name=plugin, hook_when="pre_session", script_path=hook,
            agent_writable_roots=[project], env={
                "MY_BOTAINER": str(state), "BOTAINER_STATE_ROOT": str(state),
                "BOTAINER_PROJECT_UUID": project_uuid,
                "BOTAINER_SESSION_RECORD_PATH": str(record),
                "BOTAINER_SESSION_SCRATCH": str(session),
                "BOTAINER_TESTING": "1",
                "BOTAINER_BROKER_CREDS_PATH_OVERRIDE": str(credentials)})
    assert captured["argv"] == [sys.executable, "-I", "-B", "-m", "botainer.broker.daemon_main"]
    env = captured["kwargs"]["env"]
    assert env["BOTAINER_BROKER_CREDENTIAL_FILE"] == str(credentials)
    assert env["BOTAINER_STATE_ROOT"] == str(state)
    assert env["BOTAINER_PROJECT_UUID"] == project_uuid
    assert env["BOTAINER_LAUNCHER_PID"] == str(os.getppid())
    assert "SSH_AUTH_SOCK" not in env
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert "HOST-MUST-NOT-LEAK" not in env.values()
    assert captured["kwargs"]["start_new_session"] is True
