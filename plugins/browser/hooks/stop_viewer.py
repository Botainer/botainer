#!/usr/bin/env python3
"""post_session hook — browser viewer teardown.

Re-architected: the viewer now runs INSIDE the agent container (no
separate helper container), so there is NOTHING to stop — the whole stack dies
with the agent container. This hook only drops the per-session token file so a
leftover can't be reused. No-op for headless sessions."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    record_path = os.environ.get("BOTAINER_SESSION_RECORD_PATH")
    if not record_path or not Path(record_path).exists():
        return 0
    try:
        record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    handle = (record.get("runtime_handle") or {}).get("browser_viewer")
    if not isinstance(handle, dict):
        return 0  # headless session; nothing armed
    # legacy mints a ws token; gateway mints an RFB password — drop whichever
    # secret(s) the start hook recorded so a leftover can't be reused.
    for key in ("token_file", "rfb_pass_file"):
        path = handle.get(key)
        if path:
            try:
                os.unlink(str(path))
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
