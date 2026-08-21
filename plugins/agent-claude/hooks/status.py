#!/usr/bin/env python3
"""agent-claude status — show credential file state + age."""
from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path


def main() -> int:
    uid = os.environ.get("BOTAINER_PROJECT_UUID", "")
    state_root = Path(os.environ.get("BOTAINER_STATE_ROOT", str(Path.home() / ".botainer")))
    profile = os.environ.get("BOTAINER_PROFILE", "default")
    if not uid:
        sys.stderr.write("status: BOTAINER_PROJECT_UUID env not set\n")
        return 1
    creds_dir = state_root / "state" / uid / "data" / "agent-claude" / "profiles" / profile
    creds = creds_dir / ".credentials.json"
    if not creds.exists():
        print(f"  agent-claude: NO credentials at {creds}")
        print("  → run `botainer plugin agent-claude login`")
        return 2
    st = creds.stat()
    mtime = datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc)
    age = datetime.datetime.now(datetime.timezone.utc) - mtime
    print(f"  agent-claude: credentials present at {creds}")
    print(f"    profile:    {profile}")
    print(f"    project:    {uid}")
    print(f"    size:       {st.st_size} bytes")
    print(f"    mtime:      {mtime.isoformat()}")
    print(f"    age:        {int(age.total_seconds() / 60)}m")
    print(f"    mode:       {oct(st.st_mode)[-3:]}")
    if (st.st_mode & 0o077) != 0:
        print("  WARNING: credentials file is world/group readable — re-run login to fix")
    return 0


if __name__ == "__main__":
    sys.exit(main())
