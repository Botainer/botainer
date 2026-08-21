#!/usr/bin/env python3
"""agent-claude-shared status: show shared credential state on this host."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    state_root = Path(
        os.environ.get("BOTAINER_STATE_ROOT")
        or os.environ.get("MY_BOTAINER")
        or str(Path.home() / ".botainer")
    )
    shared_dir = state_root / "shared-auth" / "agent-claude"
    creds_file = shared_dir / ".credentials.json"

    if not creds_file.exists():
        print(f"shared anthropic credential: NOT PRESENT at {creds_file}")
        print("Run `botainer auth login --shared --agent claude` to log in.")
        return 1

    st = creds_file.stat()
    mode = oct(st.st_mode & 0o777)
    owner_ok = (st.st_uid == os.getuid())
    print(f"shared anthropic credential: {creds_file}")
    print(f"  mode: {mode} {'(secure)' if mode in ('0o600', '0o400') else '(TOO PERMISSIVE — chmod 600)'}")
    print(f"  owner uid {st.st_uid} {'match' if owner_ok else 'MISMATCH (refuse to mount)'}")
    print(f"  size: {st.st_size} bytes")
    # Try to extract expires_at without leaking the token.
    try:
        d = json.loads(creds_file.read_text())
        oauth = d.get("claudeAiOauth", {}) if isinstance(d, dict) else {}
        if isinstance(oauth, dict) and "expiresAt" in oauth:
            from datetime import datetime, timezone
            exp = int(oauth["expiresAt"]) // 1000  # ms → s
            exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
            print(f"  access token expires: {exp_dt.isoformat()}")
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
