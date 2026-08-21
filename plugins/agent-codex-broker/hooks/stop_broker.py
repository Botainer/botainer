#!/usr/bin/env python3
"""post_session hook: stop the Codex credential broker.

Reads the recorded broker pid from the session record, SIGTERMs it, waits
briefly for graceful shutdown, escalates to SIGKILL. TCP transport only (codex
has no unix socket), so there is no socket path to unlink. Mirror of
agent-claude-broker's stop_broker.py.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


def main() -> int:
    record_path = os.environ.get("BOTAINER_SESSION_RECORD_PATH")
    if not record_path:
        return 0
    record_path_p = Path(record_path)
    if not record_path_p.exists():
        return 0
    try:
        record = json.loads(record_path_p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    rh = record.get("runtime_handle") or {}
    broker = rh.get("broker")
    if not broker:
        return 0
    pid = broker.get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(60):  # up to 3s for a clean exit
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    print(f"[agent-codex-broker] stopped pid={pid}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
