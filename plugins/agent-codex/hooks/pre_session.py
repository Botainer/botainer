#!/usr/bin/env python3
"""agent-codex pre_session hook.

Same shape as agent-claude/hooks/pre_session.py but for OpenAI Codex CLI.
MOUNT mode only at v0.1.x (real OPENAI_API_KEY visible to agent). Proxy
mode comes with the generic credential-proxy plugin per
DN-030.

Output: PluginContribution JSON on stdout with the credential bind +
the OPENAI_API_KEY env directive.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    uid = os.environ.get("BOTAINER_PROJECT_UUID", "")
    state_root = Path(os.environ.get("BOTAINER_STATE_ROOT", str(Path.home() / ".botainer")))
    profile = os.environ.get("BOTAINER_PROFILE", "default")
    if not uid:
        sys.stderr.write("agent-codex pre_session: no BOTAINER_PROJECT_UUID in env\n")
        return 1

    # Mutual exclusion with credential-proxy (planned; not yet shipped).
    enabled = os.environ.get("BOTAINER_PLUGINS_ENABLED", "").split(",")
    if "credential-proxy" in enabled:
        # When the generic proxy ships, skip the credential bind here;
        # the proxy hook contributes ANTHROPIC_API_KEY-like ephemeral tokens.
        sys.stderr.write(
            "[agent-codex] credential-proxy enabled; skipping credential bind\n"
        )
        sys.stdout.write(json.dumps({"version": "plugin-contribution-v1", "binds": [], "env": {}}))
        return 0

    creds_dir = state_root / "state" / uid / "data" / "agent-codex" / "profiles" / profile
    creds_dir.mkdir(parents=True, exist_ok=True)
    api_key_file = creds_dir / "api_key"
    auth_file = creds_dir / "auth.json"          # OAuth, written by codex (#62)

    if not api_key_file.exists() and not auth_file.exists():
        sys.stderr.write(
            f"agent-codex: no credential for this project at {creds_dir}\n"
            f"  Run `botainer auth login --agent codex` and choose either:\n"
            f"    1) OAuth   — your ChatGPT/Codex subscription (writes auth.json)\n"
            f"    2) API key — billed per token (writes api_key)\n"
        )
        return 2

    # ONE bind, at codex's own config dir, announced via CODEX_HOME.
    #
    # It used to bind at /home/agent/.openai (ro) and contribute nothing, which
    # worked ONLY because the image bakes
    # OPENAI_API_KEY_FILE=/home/agent/.openai/api_key and entrypoint_wrap.sh
    # cats it into OPENAI_API_KEY. That is fine for a key and useless for OAuth:
    # codex reads auth.json from CODEX_HOME, so an OAuth credential delivered to
    # /home/agent/.openai would never be found (the shared-mode bug of the same
    # morning, one plugin over).
    #
    # rw, not ro: codex writes session state into CODEX_HOME, and OAuth tokens
    # are refreshed in place. Read-only would break both. The credential is
    # per-project, so rw grants the agent nothing it cannot already read.
    binds = [
        {
            "source": str(creds_dir),
            "target": "/home/agent/.codex",
            "mode": "rw",
            "provenance_detail": (
                "OpenAI Codex credentials + session state (per-project, "
                "ISOLATED mode). Not visible to any other project."
            ),
            "self_test": "SELFTEST_EXTRA_BIND",
        }
    ]
    env = {"CODEX_HOME": "/home/agent/.codex"}

    # Point the entrypoint's key-reader at the new location. Without this it
    # would keep looking at the image default (/home/agent/.openai/api_key),
    # which nothing binds any more — so an API-key project would silently lose
    # its key the moment the bind target moved.
    if api_key_file.exists():
        env["OPENAI_API_KEY_FILE"] = "/home/agent/.codex/api_key"

    contribution = {
        "version": "plugin-contribution-v1",
        "binds": binds,
        "env": env,
    }
    sys.stdout.write(json.dumps(contribution))
    return 0


if __name__ == "__main__":
    sys.exit(main())
