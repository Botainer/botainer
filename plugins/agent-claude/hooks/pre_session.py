#!/usr/bin/env python3
"""agent-claude pre_session hook.

Runs on the host, as the user, before the container is launched.
Responsibilities:
1. Ensure ~/.botainer/state/<uuid>/data/agent-claude/profiles/<profile>/ exists.
2. Confirm `.credentials.json` is present (and warn if it's not — login is
   per-project at v0.1.0; if missing, return a structured refusal).
3. Emit a PluginContribution JSON on stdout: bind for credentials, env vars.

Output: JSON on stdout with shape:
  {
    "version": "plugin-contribution-v1",
    "binds": [...],
    "env": {...}
  }
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
        sys.stderr.write("agent-claude pre_session: no BOTAINER_PROJECT_UUID in env\n")
        return 1

    # Mutual exclusion with agent-claude-proxy: if the proxy plugin is
    # enabled for this session, skip the credential bind. The proxy
    # holds the real key on the host and the container only sees an
    # ephemeral token (via env vars contributed by start_proxy.py).
    # Mounting the real credentials would defeat the proxy mode's
    # security property.
    proxy_enabled = (
        os.environ.get("BOTAINER_PLUGINS_ENABLED", "")
        .split(",")
    )
    if "agent-claude-proxy" in proxy_enabled:
        sys.stderr.write(
            "[agent-claude] agent-claude-proxy is enabled; skipping "
            "credential bind (proxy mode handles auth host-side)\n"
        )
        contribution = {
            "version": "plugin-contribution-v1",
            "binds": [],
            "env": {},
        }
        sys.stdout.write(json.dumps(contribution))
        return 0

    creds_dir = state_root / "state" / uid / "data" / "agent-claude" / "profiles" / profile
    creds_dir.mkdir(parents=True, exist_ok=True)
    creds_file = creds_dir / ".credentials.json"
    if not creds_file.exists():
        sys.stderr.write(
            f"agent-claude: no credentials at {creds_file}; "
            f"run `botainer plugin agent-claude login` first.\n"
        )
        return 2
    # Successful contribution: bind the credentials dir to /home/agent/.claude rw
    # (Claude refreshes OAuth tokens in-place; ro would go stale).
    contribution = {
        "version": "plugin-contribution-v1",
        "binds": [
            {
                "source": str(creds_dir),
                "target": "/home/agent/.claude",
                "mode": "rw",
                "provenance_detail": "Claude OAuth credentials (per-project)",
                "self_test": "SELFTEST_EXTRA_BIND",
            }
        ],
        "env": {},
    }
    sys.stdout.write(json.dumps(contribution))
    return 0


if __name__ == "__main__":
    sys.exit(main())
