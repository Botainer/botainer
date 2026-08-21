#!/usr/bin/env python3
"""post_session hook: stop the credential broker.

Reads the recorded broker pid + socket path from the session record, sends
SIGTERM, waits briefly for graceful shutdown, escalates to SIGKILL, then unlinks
the socket if it remains. Mirror of agent-claude-proxy's stop_proxy.py.
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
    sock_path = broker.get("socket_path")

    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        # Wait up to 3s for clean exit.
        for _ in range(60):
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

    if sock_path:
        sp = Path(sock_path)
        try:
            sp.unlink()
        except (FileNotFoundError, OSError):
            pass
        # If the socket lived in a short per-session runtime dir that
        # start_broker created (because the session path overflowed the unix
        # sun_path limit), remove that dir too. Only the botainer-brk-* dirs we
        # create are touched — never the session scratch dir.
        parent = sp.parent
        if parent.name.startswith("botainer-brk-"):
            try:
                parent.rmdir()
            except (FileNotFoundError, OSError):
                pass
    print(f"[agent-claude-broker] stopped pid={pid}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
