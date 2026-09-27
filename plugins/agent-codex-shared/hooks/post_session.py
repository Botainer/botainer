#!/usr/bin/env python3
"""Reconcile a container-refreshed Codex credential at session exit.

A client can replace a credential symlink with a regular file when refreshing
through a temporary file and rename. The project then holds the newer token,
while the shared store retains the old one. Reconciliation at startup alone
leaves other projects using that older token until this project starts again.

This hook calls pre_session.reconcile_shared_credential, reusing its account
identity checks, API-key exclusion and locking. It is best-effort at exit;
a killed session runs no exit hook and must wait for the next startup repair.
This does not establish provider-side refresh-concurrency guarantees."""
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
