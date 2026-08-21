"""An agent plugin that BINDS a config dir must also SAY WHERE IT IS.

The bug this file exists to stop, reported: "I logged in. started the
session and codex asks me to log in. Something is fucked up."

The login had worked. The credential was on disk, 0600, and `agent-codex-shared`
bound it correctly to /home/agent/.codex. But nothing told codex to look there —
the hook contributed `"env": {}` and the codex image bakes no CODEX_HOME. Codex
resolved its default ~/.codex against the session HOME (/home/user), found an
empty directory, and asked the user to log in again. The credential was two
inches away and invisible.

THE CONTRACT: for every agent plugin, the directory it binds for credentials
must be ANNOUNCED to the agent, and the announced path must be the one bound.

"Announced" deliberately spans two layers, because the plugins legitimately use
both and a test that knew only one would be wrong about half of them:

  agent-claude          image bakes CLAUDE_CONFIG_DIR   hook env {}   -> fine
  agent-claude-shared   hook contributes it                          -> fine
  agent-codex           hook contributes CODEX_HOME (#62)            -> fine
  agent-codex-shared    hook contributes CODEX_HOME                  -> fine
                        (before the fix: NEITHER layer -> the bug)

An earlier version of this file checked only the hook. That would have passed
agent-claude (announced by its image) for the wrong reason and, worse, taught
the next author that hook-level announcement is the only mechanism. The failure
mode is not "the hook forgot" — it is "NOBODY said", which is a question about
the whole delivery path.

These tests RUN THE HOOKS as subprocesses and read what they actually emit.
Reading the source would have agreed that codex was fine, because `"env": {}`
looks like a deliberate "no env needed" rather than an omission.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PLUGINS = _REPO / "plugins"


class AgentPlugin:
    """One agent auth plugin, the credential it demands, and where it lives."""

    def __init__(self, name: str, image: str, cred_filename: str, payload,
                 env_var: str, scope: str) -> None:
        self.name = name
        self.image = image              # plugin dir holding Dockerfile/.def
        self.cred_filename = cred_filename
        self.payload = payload          # dict -> JSON, str -> raw bytes
        self.env_var = env_var          # what the agent CLI reads
        self.scope = scope              # "shared" | "project"

    def __repr__(self) -> str:
        return self.name


_CLAUDE_CRED = {"claudeAiOauth": {"accessToken": "a" * 40,
                                  "refreshToken": "r" * 40,
                                  "expiresAt": 9_999_999_999_000}}
_CODEX_CRED = {"tokens": {"access_token": "a" * 40,
                          "refresh_token": "r" * 40,
                          "account_id": "acct_1"}}

PLUGINS = [
    AgentPlugin("agent-claude", "agent-claude", ".credentials.json",
                _CLAUDE_CRED, "CLAUDE_CONFIG_DIR", "project"),
    AgentPlugin("agent-claude-shared", "agent-claude", ".credentials.json",
                _CLAUDE_CRED, "CLAUDE_CONFIG_DIR", "shared"),
    # ISOLATED codex, OAuth — the path #62 added. Before it, isolated mode
    # accepted only an API key, so choosing per-project isolation forced
    # per-token API billing instead of the subscription.
    AgentPlugin("agent-codex", "agent-codex", "auth.json",
                _CODEX_CRED, "CODEX_HOME", "project"),
    AgentPlugin("agent-codex-shared", "agent-codex", "auth.json",
                _CODEX_CRED, "CODEX_HOME", "shared"),
]


def _parse_image_env(plugin_dir: Path) -> dict[str, str]:
    """ENV baked into the Dockerfile, honouring backslash continuations."""
    text = (plugin_dir / "Dockerfile").read_text().replace("\\\n", " ")
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("ENV "):
            continue
        for token in s[4:].split():
            if "=" in token:
                k, _, v = token.partition("=")
                out[k.strip()] = v.strip().strip("\"'")
    return out


def _write_cred(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(data, encoding="utf-8")
    path.chmod(0o600)               # the hooks refuse anything looser


def _run_hook(plugin: AgentPlugin, tmp_path: Path) -> dict:
    """Run the plugin's pre_session hook against a throwaway state root."""
    state_root = tmp_path / "botainer"
    project_uuid = str(uuid.uuid4())
    # The agent family owns the on-disk dir name; "-shared" is an auth MODE,
    # not a separate agent, so both modes read agent-<family>.
    family = plugin.name.replace("-shared", "")

    if plugin.scope == "shared":
        _write_cred(state_root / "shared-auth" / family / plugin.cred_filename,
                    plugin.payload)
    else:
        _write_cred(state_root / "state" / project_uuid / "data" / family
                    / "profiles" / "default" / plugin.cred_filename,
                    plugin.payload)

    proc = subprocess.run(
        [sys.executable,
         str(_PLUGINS / plugin.name / "hooks" / "pre_session.py")],
        env={**os.environ,
             "BOTAINER_STATE_ROOT": str(state_root),
             "MY_BOTAINER": str(state_root),
             "BOTAINER_PROJECT_UUID": project_uuid,
             "BOTAINER_PROFILE": "default"},
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"{plugin.name} hook refused a valid 0600 credential "
        f"(exit {proc.returncode}): {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _announced(plugin: AgentPlugin, contribution: dict) -> tuple[str | None, str]:
    """Where the agent will look, and which layer said so."""
    hook_env = contribution.get("env", {})
    if plugin.env_var in hook_env:
        return hook_env[plugin.env_var], "hook"
    image_env = _parse_image_env(_PLUGINS / plugin.image)
    if plugin.env_var in image_env:
        return image_env[plugin.env_var], "image"
    return None, "nobody"


@pytest.mark.parametrize("plugin", PLUGINS, ids=repr)
def test_the_bound_config_dir_is_also_announced(plugin, tmp_path) -> None:
    """THE contract. A bind is delivery; the env var is discovery."""
    contribution = _run_hook(plugin, tmp_path)
    announced, layer = _announced(plugin, contribution)

    assert announced is not None, (
        f"{plugin.name} binds a config dir but NOTHING sets {plugin.env_var} — "
        f"not the hook, not the image. The agent resolves its default path "
        f"against HOME and finds nothing, so the session asks the user to log "
        f"in again despite a successful login. "
        f"hook env={contribution.get('env')!r}")

    targets = [b["target"] for b in contribution.get("binds", [])]
    assert announced in targets, (
        f"{plugin.name}: {plugin.env_var} (set by the {layer}) points at "
        f"{announced!r}, which is not one of the bound dirs ({targets}). The "
        f"agent would be sent to a path that does not exist in the container.")


@pytest.mark.parametrize("plugin", PLUGINS, ids=repr)
def test_the_announced_dir_is_writable_by_the_agent(plugin, tmp_path) -> None:
    """Both CLIs write into their config dir — session state, history, and a
    refreshed OAuth token. Announcing a read-only dir turns "cannot find" into
    "cannot save", a subtler version of the same failure."""
    contribution = _run_hook(plugin, tmp_path)
    announced, _ = _announced(plugin, contribution)
    bind = next(b for b in contribution["binds"] if b["target"] == announced)
    assert bind["mode"] == "rw", (
        f"{plugin.name} announces {announced} as the config dir but binds it "
        f"{bind['mode']}; the agent cannot write its own state or refresh its "
        f"token there.")


@pytest.mark.parametrize("plugin", PLUGINS, ids=repr)
def test_the_credential_is_reachable_at_the_announced_path(
        plugin, tmp_path) -> None:
    """Follow the announced path on the host side and find the credential.

    This is the check that needs no knowledge of which var each CLI reads: go
    where the agent is told to go, and see whether the thing it needs is there.
    """
    contribution = _run_hook(plugin, tmp_path)
    announced, _ = _announced(plugin, contribution)
    bind = next(b for b in contribution["binds"] if b["target"] == announced)

    entry = Path(bind["source"]) / plugin.cred_filename
    assert entry.exists() or entry.is_symlink(), (
        f"nothing named {plugin.cred_filename} at the announced config dir "
        f"({announced} -> {bind['source']}); the agent finds an empty directory")

    # Shared mode links the per-project entry at the host-wide credential. The
    # link target is a CONTAINER path, unresolvable from here, but it must at
    # least point inside another bind rather than dangle nowhere.
    if entry.is_symlink():
        target = os.readlink(entry)
        others = [b["target"] for b in contribution["binds"]
                  if b["target"] != announced]
        assert any(target.startswith(t) for t in others), (
            f"{plugin.name}: {plugin.cred_filename} links to {target!r}, not "
            f"inside any bind ({others}) — it will dangle in the container")


def test_codex_specifically_sets_CODEX_HOME(tmp_path) -> None:
    """Named regression for the reported bug, so `pytest -k codex` shows it.

    The value is not arbitrary: agent-codex-broker already uses
    /home/agent/.codex. Two codex plugins disagreeing about where codex lives
    would strand the credential for anyone switching auth mode.
    """
    for plugin in [p for p in PLUGINS if p.image == "agent-codex"]:
        announced, _ = _announced(plugin, _run_hook(plugin, tmp_path / plugin.name))
        assert announced == "/home/agent/.codex", (
            f"{plugin.name} announces {announced!r}; every codex auth mode must "
            f"agree on /home/agent/.codex")


def test_isolated_codex_accepts_an_api_key_too(tmp_path) -> None:
    """#62 added OAuth to isolated mode; it must not have removed the API key.

    The key is a RAW file, because the image's entrypoint does
    `export OPENAI_API_KEY="$(cat "$OPENAI_API_KEY_FILE")"` — JSON-wrapping it
    would export the JSON blob as the key. The hook must therefore also
    re-point OPENAI_API_KEY_FILE at the new bind, or an API-key project would
    silently lose its credential when the bind target moved.
    """
    plugin = AgentPlugin("agent-codex", "agent-codex", "api_key",
                         "sk-test-key-value\n", "CODEX_HOME", "project")
    contribution = _run_hook(plugin, tmp_path)
    env = contribution["env"]
    assert env.get("OPENAI_API_KEY_FILE") == "/home/agent/.codex/api_key", (
        f"api-key project: entrypoint would still read the image default "
        f"(/home/agent/.openai/api_key), which nothing binds any more. "
        f"env={env!r}")
    announced = env["CODEX_HOME"]
    assert (Path(next(b["source"] for b in contribution["binds"]
                      if b["target"] == announced)) / "api_key").exists()


def test_isolated_codex_refuses_when_it_has_no_credential_at_all(
        tmp_path) -> None:
    """And says how to get one — naming BOTH methods and what they bill."""
    state_root = tmp_path / "botainer"
    (state_root / "state" / str(uuid.uuid4())).mkdir(parents=True)
    proc = subprocess.run(
        [sys.executable,
         str(_PLUGINS / "agent-codex" / "hooks" / "pre_session.py")],
        env={**os.environ,
             "BOTAINER_STATE_ROOT": str(state_root),
             "MY_BOTAINER": str(state_root),
             "BOTAINER_PROJECT_UUID": str(uuid.uuid4()),
             "BOTAINER_PROFILE": "default"},
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    err = proc.stderr.lower()
    assert "auth login" in err, "did not say how to fix it"
    assert "oauth" in err and "api key" in err, "did not name both methods"
