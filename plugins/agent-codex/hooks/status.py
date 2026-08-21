#!/usr/bin/env python3
"""agent-codex status: show credential validity for this project."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    uid = os.environ.get("BOTAINER_PROJECT_UUID", "")
    state_root = Path(os.environ.get("BOTAINER_STATE_ROOT", str(Path.home() / ".botainer")))
    profile = os.environ.get("BOTAINER_PROFILE", "default")
    if not uid:
        sys.stderr.write("status: no BOTAINER_PROJECT_UUID\n")
        return 1
    creds_file = (
        state_root / "state" / uid / "data" / "agent-codex"
        / "profiles" / profile / "api_key"
    )
    if not creds_file.exists():
        print(f"profile={profile}: no API key. "
              f"Run `botainer plugin agent-codex login`.")
        return 1
    mode = creds_file.stat().st_mode & 0o777
    print(f"profile={profile}: key present ({creds_file}, mode={oct(mode)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
