#!/usr/bin/env python3
"""wolfram-sidecar pre_session hook.

Mints a per-launch token, writes it mode 0600 to the session scratch,
spawns the host-side proxy as a detached subprocess listening on a
unix socket also in the session scratch, and emits a contribution
that the launcher will merge into the SessionSpec:

  - bind <session>/wolfram-proxy.sock → /run/wolfram-proxy.sock
  - bind <session>/wolfram-token      → /run/wolfram-token (ro)
  - bind <plugin>/client.py           → /usr/local/bin/wolframscript (ro)
  - env  WOLFRAM_PROXY_SOCKET=/run/wolfram-proxy.sock
  - env  WOLFRAM_PROXY_TOKEN_FILE=/run/wolfram-token

Proxy lifecycle: pid recorded in the session record under
runtime_handle.wolfram_proxy.pid. post_session.py reads it and
SIGTERMs the proxy.

Exit codes:
  0 — success
  1 — config / sandbox / wolframscript-missing problem (refused;
      session must not start with a broken wolfram setup)
  2 — proxy spawn failed
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PROXY_SCRIPT = PLUGIN_DIR / "proxy.py"
CLIENT_SCRIPT = PLUGIN_DIR / "client.py"
SANDBOX_PROFILE = PLUGIN_DIR / "sandbox" / "wolfram-sandbox.sb"
SANDBOX_INIT = PLUGIN_DIR / "sandbox" / "wolfram-sandbox-init.wl"


def _read_plugin_config(project_root: Path) -> dict[str, Any]:
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return (data.get("plugins") or {}).get("wolfram-sidecar") or {}


def main() -> int:
    record_path = os.environ.get("BOTAINER_SESSION_RECORD_PATH")
    if not record_path:
        print("[wolfram-sidecar] BOTAINER_SESSION_RECORD_PATH unset",
              file=sys.stderr)
        return 1
    record_path_p = Path(record_path)
    if not record_path_p.exists():
        print(f"[wolfram-sidecar] record file missing: {record_path_p}",
              file=sys.stderr)
        return 1
    record = json.loads(record_path_p.read_text(encoding="utf-8"))
    if record.get("schema_version") != 1:
        print(
            f"[wolfram-sidecar] unsupported schema_version "
            f"{record.get('schema_version')!r}",
            file=sys.stderr,
        )
        return 1

    session_id = record["session_id"]
    state_dir = Path(record["state_dir"])
    session_scratch = Path(
        os.environ.get(
            "BOTAINER_SESSION_SCRATCH",
            str(state_dir / "sessions" / session_id),
        )
    )
    session_scratch.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Per-project config.
    plugin_cfg = _read_plugin_config(Path(record["project_root"]))
    wolframscript_path = str(plugin_cfg.get("wolframscript_path") or "wolframscript")
    timeout_s = int(plugin_cfg.get("timeout_seconds") or 300)
    max_bytes = int(plugin_cfg.get("max_request_bytes") or 1048576)
    require_sandbox = bool(
        plugin_cfg.get("require_sandbox", True)
    )

    # Refuse early if the host has no wolframscript at all. The proxy
    # would also catch this, but failing pre_session gives the user a
    # better message before the session starts.
    if not shutil.which(wolframscript_path):
        print(
            f"[wolfram-sidecar] refused: wolframscript not on PATH "
            f"(looked for {wolframscript_path!r}).\n"
            f"  - On macOS: install Mathematica or wolframscript and "
            f"activate it.\n"
            f"  - To use a custom path, set "
            f"plugins.wolfram-sidecar.wolframscript_path in .botainer/config.yaml.\n"
            f"  - To disable the plugin: remove `wolfram-sidecar` from "
            f"plugins_enabled.\n",
            file=sys.stderr,
        )
        return 1

    # Mint per-launch token.
    token = secrets.token_urlsafe(32)
    token_file = session_scratch / "wolfram-token"
    sock_path = session_scratch / "wolfram-proxy.sock"
    audit_log = session_scratch / "wolfram-proxy-audit.jsonl"

    # Refuse to overwrite a symlinked token file (parity with v0.0.x H4).
    if token_file.is_symlink():
        print(f"[wolfram-sidecar] refusing to write through symlink at "
              f"{token_file}",
              file=sys.stderr)
        return 1
    try:
        if token_file.exists():
            token_file.unlink()
    except OSError:
        pass
    fd = os.open(
        str(token_file),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "w") as f:
        f.write(token)

    # Spawn the proxy. Detach from this hook (start_new_session=True)
    # so the proxy survives until post_session SIGTERMs it.
    proxy_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "TZ": os.environ.get("TZ", ""),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        # Proxy config.
        "BOTAINER_PROXY_SOCKET_PATH": str(sock_path),
        "BOTAINER_PROXY_TOKEN_FILE": str(token_file),
        "BOTAINER_PROXY_WOLFRAMSCRIPT": wolframscript_path,
        "BOTAINER_PROXY_TIMEOUT": str(timeout_s),
        "BOTAINER_PROXY_MAX_REQUEST_BYTES": str(max_bytes),
        "BOTAINER_PROXY_REQUIRE_SANDBOX": "1" if require_sandbox else "0",
        "BOTAINER_PROXY_SANDBOX_PROFILE": str(SANDBOX_PROFILE),
        "BOTAINER_PROXY_SANDBOX_INIT": str(SANDBOX_INIT),
        "BOTAINER_PROXY_AUDIT_LOG": str(audit_log),
    }
    try:
        proc = subprocess.Popen(
            [sys.executable, str(PROXY_SCRIPT)],
            env=proxy_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            # Keep stderr — if the proxy refuses (e.g., no sandbox),
            # we want to see why before deciding to fail pre_session.
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"[wolfram-sidecar] proxy spawn failed: {exc}", file=sys.stderr)
        token_file.unlink(missing_ok=True)
        return 2

    # Wait briefly for the socket to appear OR the proxy to exit.
    # Up to 3s. If the proxy exits early it's refusing (sandbox check
    # likely); surface its stderr.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if sock_path.exists():
            break
        if proc.poll() is not None:
            err = ""
            try:
                err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            except OSError:
                pass
            print(
                f"[wolfram-sidecar] proxy exited early (rc={proc.returncode}); "
                f"stderr:\n{err}",
                file=sys.stderr,
            )
            token_file.unlink(missing_ok=True)
            return 1
        time.sleep(0.05)
    else:
        # No early exit AND no socket — give up.
        try:
            proc.terminate()
        except OSError:
            pass
        print(
            "[wolfram-sidecar] proxy did not bind socket within 3s; "
            "aborting session start.",
            file=sys.stderr,
        )
        token_file.unlink(missing_ok=True)
        return 2

    # Record pid in the session record under runtime_handle so post_session
    # can find it.
    rh = record.setdefault("runtime_handle", {})
    rh["wolfram_proxy"] = {
        "pid": proc.pid,
        "socket_path": str(sock_path),
        "token_file": str(token_file),
        "audit_log": str(audit_log),
        "started_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }
    tmp = record_path_p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, record_path_p)

    # Emit contribution: three binds + two env vars.
    # mount_target_prefixes in the manifest covers all three targets.
    print(json.dumps({
        "version": "plugin-contribution-v1",
        "kind": "pre_session",
        "binds": [
            {
                "source": str(sock_path),
                "target": "/run/wolfram-proxy.sock",
                "mode": "unix-socket",
            },
            {
                "source": str(token_file),
                "target": "/run/wolfram-token",
                "mode": "ro",
            },
            {
                "source": str(CLIENT_SCRIPT),
                "target": "/usr/local/bin/wolframscript",
                "mode": "ro",
            },
        ],
        "env": {
            "WOLFRAM_PROXY_SOCKET": "/run/wolfram-proxy.sock",
            "WOLFRAM_PROXY_TOKEN_FILE": "/run/wolfram-token",
        },
        "proxy_pid": proc.pid,
        "audit_log": str(audit_log),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
