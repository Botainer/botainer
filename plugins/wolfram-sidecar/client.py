#!/usr/bin/env python3
"""wolfram-sidecar in-container `wolframscript` shim.

Bind-mounted at `/usr/local/bin/wolframscript` inside the container.
Forwards all argv to the host-side proxy over a unix-domain socket
and prints the response.

Differs from v0.0.x's wolfram-proxy-client.py only in transport: v0.0.x
opened a TCP socket to `host.docker.internal:51839`; v0.1.x uses the
unix socket bind-mounted by the pre_session hook (default
`/run/wolfram-proxy.sock`).

`--proxy-timeout N` is stripped from argv before forwarding; sets a
per-request timeout (seconds), capped at the proxy's global limit.
"""
from __future__ import annotations

import json
import os
import socket
import sys


def _resolve_socket_path() -> str:
    return os.environ.get(
        "WOLFRAM_PROXY_SOCKET",
        "/run/wolfram-proxy.sock",
    )


def _resolve_token() -> str | None:
    token_path = os.environ.get(
        "WOLFRAM_PROXY_TOKEN_FILE",
        "/run/wolfram-token",
    )
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def main() -> int:
    if not sys.argv[1:]:
        sys.stderr.write(
            "Usage: wolframscript [args...]\n"
            "  --proxy-timeout N   Per-request timeout in seconds.\n"
            "  -code 'expr'        Evaluate Wolfram expression.\n"
            "  -file <path>        REFUSED at the proxy layer; use -code\n"
            "                      with explicit Get[\"...\"].\n"
        )
        return 1

    args = sys.argv[1:]
    timeout = None
    if "--proxy-timeout" in args:
        idx = args.index("--proxy-timeout")
        if idx + 1 < len(args):
            try:
                timeout = int(args[idx + 1])
            except ValueError:
                pass
            args = args[:idx] + args[idx + 2:]
        else:
            args = args[:idx]

    sock_path = _resolve_socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(5)
        sock.connect(sock_path)
        sock.settimeout(None)
    except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
        sys.stderr.write(
            f"Error: wolframscript proxy not reachable at {sock_path}: {exc}\n"
            f"Make sure plugins_enabled in .botainer/config.yaml contains\n"
            f"`wolfram-sidecar` and the session was started fresh.\n"
            f"(The proxy is spawned by pre_session; if it died, restart\n"
            f"the session with `botainer stop && botainer start` on the host.)\n"
        )
        return 1

    request: dict[str, object] = {"args": args}
    if timeout is not None:
        request["timeout"] = timeout
    token = _resolve_token()
    if token:
        request["auth"] = token

    try:
        sock.sendall(json.dumps(request).encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    finally:
        sock.close()

    try:
        response = json.loads(b"".join(chunks))
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"Error: proxy returned malformed response: {exc}\n"
            f"Raw bytes: {b''.join(chunks)[:200]!r}\n"
        )
        return 1

    if response.get("stdout"):
        sys.stdout.write(response["stdout"])
    if response.get("stderr"):
        sys.stderr.write(response["stderr"])
    return int(response.get("returncode", 1))


if __name__ == "__main__":
    sys.exit(main())
