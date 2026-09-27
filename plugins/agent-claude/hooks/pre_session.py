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
    # A SHARED-mode hook leaves this project's credential as a SYMLINK to an
    # in-container path, which dangles on the host. `exists()` FOLLOWS the link
    # and reads False, so without this branch the refusal below says "no
    # credentials; run login" — and that login cannot write through the
    # dangling link either, because the login container has no /shared-auth
    # bound. The user is sent round a loop with no exit, while the README this
    # project wrote says the dangling links are "by design. Don't 'fix' them."
    #
    # `botainer auth use` now clears these on the way out of shared mode, so
    # this is the BACKUP for the gap that structure does not cover: a mode
    # changed by hand-editing config.yaml never runs that command.
    if creds_file.is_symlink() and not creds_file.exists():
        sys.stderr.write(
            # THE FIRST LINE CARRIES THE REMEDY ON PURPOSE. `dry-run
            # --include-hooks` lists a failing hook by its HEADLINE only, so a
            # first line that merely states the problem leaves that surface
            # with nothing to act on. `start` renders headline + full tail.
            f"agent-claude: leftover SHARED-mode credential link — clear it with "
            f"`botainer auth use isolated --family anthropic` (your shared "
            f"login is untouched).\n"
            f"Detail: this project is in isolated mode, but its credential\n"
            f"is a leftover SHARED-mode symlink into the container:\n"
            f"    {creds_file}\n"
            f"    -> {os.readlink(creds_file)}\n"
            f"That path only exists inside a running shared-mode container, so\n"
            f"nothing here can read it and a login cannot write through it.\n"
            f"That command removes the leftover link only; the shared\n"
            f"credential it names is not touched. Then run the login for\n"
            f"this project.\n"
        )
        return 2

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
