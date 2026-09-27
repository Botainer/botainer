#!/usr/bin/env python3
"""Push a token refreshed INSIDE the container back to the shared store, at exit.

Shared mode symlinks the
per-project `.credentials.json` at `/shared-auth/agent-claude/.credentials.json`
so every project uses ONE file — the same way several native `claude` sessions
share `~/.claude`. But Claude Code refreshes the token with a temp file plus
`rename()`, and `rename()` REPLACES a symlink with a regular file. The first
in-container refresh therefore detaches that project from the shared store: it
keeps the fresh token locally, `/shared-auth/` keeps the stale one.

`pre_session` already repairs this — but only at session START, so the repair
landed the next time you started THAT SAME project. Start a NEW project instead
and it symlinks to the stale shared file and reports "login expired", while the
old project carries on working. Two projects, identical config, opposite
behaviour, nothing in the UI to explain it.

This runs the SAME reconcile at session EXIT, so a refresh propagates as soon as
the session that produced it ends.

NO NEW TRUST SURFACE: it calls `pre_session.reconcile_shared_credential`
verbatim, including the #158 anti-poisoning checks (`sk-ant-oat*`/`sk-ant-ort*`
prefixes, >=64 chars, <=60-day expiry delta) that stop a hostile in-container
agent planting a forged credential for every other project. Nothing here decides
what is valid; only WHEN the existing check runs.

RESIDUAL GAP, stated rather than implied: a session killed outright (scancel,
OOM, node failure) runs no exit hook, so its refresh still waits for that
project's next start. Bounded and self-healing — unlike before, where it waited
indefinitely.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Hooks execute as `python <abs path to this file>`, so this file's directory is
# sys.path[0] and the sibling module imports cleanly.
from pre_session import reconcile_shared_credential  # noqa: E402


def main() -> int:
    uid = os.environ.get("BOTAINER_PROJECT_UUID", "")
    state_root = Path(
        os.environ.get("BOTAINER_STATE_ROOT", str(Path.home() / ".botainer")))
    profile = os.environ.get("BOTAINER_PROFILE", "default")
    if not uid:
        sys.stderr.write(
            "agent-claude-shared post_session: no BOTAINER_PROJECT_UUID\n")
        return 0          # nothing to reconcile; never fail a finished session

    per_project_dir = (
        state_root / "state" / uid / "data" / "agent-claude" / "profiles" / profile
    )
    shared_dir = state_root / "shared-auth" / "agent-claude"
    if not per_project_dir.is_dir() or not shared_dir.is_dir():
        return 0

    # BEST-EFFORT BY CONSTRUCTION. This runs AFTER the user's session ended, so
    # a non-zero exit would report a session that actually succeeded as failed,
    # and failing loudly cannot help them anyway. The reconcile is idempotent
    # and pre_session repeats it at next start, so the worst case of a failure
    # here is exactly the old behaviour.
    try:
        outcome = reconcile_shared_credential(per_project_dir, shared_dir)
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write(
            f"agent-claude-shared post_session: could not reconcile the shared "
            f"credential ({exc}). Your login is not lost — the next start of "
            f"this project repairs it.\n")
        return 0

    if outcome == "backfilled":
        sys.stderr.write(
            "agent-claude-shared: refreshed Anthropic token copied back to the "
            "shared store; other projects will pick it up.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
