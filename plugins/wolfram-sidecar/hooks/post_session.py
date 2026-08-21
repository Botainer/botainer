#!/usr/bin/env python3
"""wolfram-sidecar post_session hook.

Reads the wolfram_proxy.pid from the session record, SIGTERMs the
proxy, and unlinks the socket + token files. Best-effort — never
fails the session teardown even if the proxy is already gone.
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
    except (OSError, json.JSONDecodeError):
        return 0

    rh = (record.get("runtime_handle") or {}).get("wolfram_proxy") or {}
    pid = rh.get("pid")
    sock_path = rh.get("socket_path")
    token_file = rh.get("token_file")

    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        # Give the proxy ~1s to clean up before we delete its files
        # out from under it.
        for _ in range(20):
            try:
                os.kill(pid, 0)        # exists?
            except (ProcessLookupError, OSError):
                break
            time.sleep(0.05)

    # Unlink the bind sources regardless of whether the proxy is alive.
    for p in (sock_path, token_file):
        if p:
            try:
                Path(p).unlink()
            except (OSError, FileNotFoundError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
