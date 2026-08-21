#!/usr/bin/env python3
"""post_session hook: stop the credential proxy.

Reads the recorded proxy pid + socket path from spec.json, sends
SIGTERM, waits briefly for graceful shutdown, then unlinks the socket
if it remains.
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
    proxy = rh.get("proxy")
    if not proxy:
        return 0
    pid = proxy.get("pid")
    sock_path = proxy.get("socket_path")

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
        try:
            Path(sock_path).unlink()
        except (FileNotFoundError, OSError):
            pass
    print(f"[agent-claude-proxy] stopped pid={pid}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
