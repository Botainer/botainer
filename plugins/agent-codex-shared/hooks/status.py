#!/usr/bin/env python3
"""agent-codex-shared status: report shared OpenAI credential state."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    state_root = Path(
        os.environ.get("BOTAINER_STATE_ROOT")
        or os.environ.get("MY_BOTAINER")
        or str(Path.home() / ".botainer")
    )
    shared_dir = state_root / "shared-auth" / "agent-codex"
    creds_file = shared_dir / "auth.json"

    if not creds_file.exists():
        print(f"shared openai credential: NOT PRESENT at {creds_file}")
        print("Run `botainer auth login --shared --agent codex` to log in.")
        return 1

    st = creds_file.stat()
    mode = oct(st.st_mode & 0o777)
    owner_ok = (st.st_uid == os.getuid())
    print(f"shared openai credential: {creds_file}")
    print(f"  mode: {mode} {'(secure)' if mode in ('0o600', '0o400') else '(TOO PERMISSIVE — chmod 600)'}")
    print(f"  owner uid {st.st_uid} {'match' if owner_ok else 'MISMATCH'}")
    print(f"  size: {st.st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
