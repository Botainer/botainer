#!/usr/bin/env python3
"""Push a token refreshed INSIDE the container back to the shared store, at exit.

THE COUNTERPART THAT WAS NEVER WRITTEN. agent-claude-shared got this hook on
2026-07-28, for a bug reported from a real cluster session: shared mode symlinks
each project's credential into one shared file, but the agent refreshes with a
temp file plus `rename()`, and `rename()` REPLACES a symlink with a regular
file. The first in-container refresh detaches that project from the shared
store — it keeps the fresh token locally, the shared store keeps the stale one.

`pre_session` repairs that, but only at session START. So the repair landed the
next time you started THAT SAME project. Start a DIFFERENT project in between
and it symlinks to the stale shared file and reports a login failure, while the
first project carries on working. Two projects, identical config, opposite
behaviour, and nothing to explain it.

codex has had that bug the entire time, because the fix was applied to one
plugin and not its twin. That is the sibling-drift pattern (#136): three
hardenings claude received and codex did not, each found years later by
accident. The structural answer is one reconcile both plugins call; until that
lands, this hook calls `pre_session.reconcile_shared_credential` VERBATIM so the
two cannot disagree about what a valid back-fill is.

NO NEW TRUST SURFACE. Every check stays where it was: the same-account
`account_id` gate, the API-key-is-never-back-fillable rule, the fail-closed
lock. Nothing here decides what is valid — only WHEN the existing decision runs.

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
            "agent-codex-shared post_session: no BOTAINER_PROJECT_UUID\n")
        return 0          # nothing to reconcile; never fail a finished session

    per_project_dir = (
        state_root / "state" / uid / "data" / "agent-codex" / "profiles" / profile
    )
    shared_dir = state_root / "shared-auth" / "agent-codex"
    if not per_project_dir.is_dir() or not shared_dir.is_dir():
        return 0

    # BEST-EFFORT BY CONSTRUCTION. This runs AFTER the user's session ended, so
    # a non-zero exit would report a session that actually succeeded as failed,
    # and failing loudly cannot help them anyway. The reconcile is idempotent
    # and pre_session repeats it at next start, so the worst case of a failure
    # here is exactly the old behaviour.
    try:
        rc = reconcile_shared_credential(per_project_dir, shared_dir)
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write(
            f"agent-codex-shared post_session: could not reconcile the shared "
            f"login ({exc}). Your login is not lost — the next start of this "
            f"project repairs it.\n")
        return 0

    if rc != 0:
        sys.stderr.write(
            "agent-codex-shared post_session: the shared login was left "
            "unchanged (see the message above). The next start of this project "
            "will try again.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
