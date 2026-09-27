#!/usr/bin/env python3
"""Credential proxy for the Claude Code agent.

Listens on a unix socket. Validates an ephemeral session token in the
request's Authorization header. Forwards the request to the configured
upstream (default https://api.anthropic.com) with the user's real API
key substituted in. Streams the response back. Audits every request.

Stdlib-only (no aiohttp/requests/httpx dependency) so the proxy can
ship as a single self-contained script that the launcher invokes via
plugins/agent-claude-proxy/hooks/start_proxy.py.

Environment expected (set by start_proxy.py):
  BOTAINER_PROXY_SOCKET_PATH    — unix socket to listen on
  BOTAINER_PROXY_EPHEMERAL_TOKEN — ephemeral token the container will present
  BOTAINER_PROXY_REAL_CREDS_PATH — host file with the real API key
                                    (one of: $REAL_KEY env, JSON
                                    {api_key: ...}, OAuth profile dir)
  BOTAINER_PROXY_UPSTREAM        — upstream URL (default https://api.anthropic.com)
  BOTAINER_PROXY_AUDIT_LOG       — append-only audit log path
  BOTAINER_PROXY_MAX_REQUEST_BYTES — request body size limit
  BOTAINER_PROXY_RATE_LIMIT_RPS  — soft per-second rate cap
  BOTAINER_PROXY_REDACT_BODIES   — "1" to strip bodies from audit

Signals:
  SIGTERM / SIGINT — clean shutdown; remove socket file before exit.

Audit log line format (JSONL):
  {"ts": ISO8601, "method": "POST", "path": "/v1/messages", "status":
   200, "request_bytes": N, "response_bytes": N, "kind": "forwarded"}
  Status -1 == proxy-refused (token rejected, body too large, etc.).
"""

from __future__ import annotations

import errno
import http.client
import json
import os
import secrets
import signal
import socketserver
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# ────────── config (loaded once from env) ──────────


class ProxyConfig:
    # Sharp-edges review HIGH 5: hostname allowlist on the upstream URL.
    # Without this, a stale BOTAINER_PROXY_UPSTREAM env (from user's
    # shell, direnv, CI) silently redirects api.anthropic.com requests
    # to attacker-controlled HTTP — and the real API key is forwarded
    # to whatever upstream is configured. Allowlist: production
    # Anthropic + loopback (for testing) only.
    _ALLOWED_UPSTREAM_HOSTS: frozenset[str] = frozenset({
        "api.anthropic.com",
        "127.0.0.1",
        "localhost",
        "::1",
    })

    def __init__(self) -> None:
        self.socket_path = Path(os.environ["BOTAINER_PROXY_SOCKET_PATH"])
        self.ephemeral_token = os.environ["BOTAINER_PROXY_EPHEMERAL_TOKEN"]
        self.real_creds_path = Path(os.environ["BOTAINER_PROXY_REAL_CREDS_PATH"])
        self.upstream = os.environ.get(
            "BOTAINER_PROXY_UPSTREAM", "https://api.anthropic.com"
        )
        self.audit_log = Path(os.environ["BOTAINER_PROXY_AUDIT_LOG"])
        self.max_request_bytes = int(
            os.environ.get("BOTAINER_PROXY_MAX_REQUEST_BYTES", "1048576")
        )
        self.rate_limit_rps = int(
            os.environ.get("BOTAINER_PROXY_RATE_LIMIT_RPS", "60")
        )
        self.redact_bodies = (
            os.environ.get("BOTAINER_PROXY_REDACT_BODIES", "0") == "1"
        )
        # Parse upstream once.
        self._up = urlparse(self.upstream)
        if self._up.scheme not in ("http", "https"):
            raise ValueError(f"upstream {self.upstream!r} must be http or https")
        host = (self._up.hostname or "").lower()
        if host not in self._ALLOWED_UPSTREAM_HOSTS:
            raise ValueError(
                f"upstream host {host!r} not in allowlist "
                f"{sorted(self._ALLOWED_UPSTREAM_HOSTS)}. The proxy will "
                f"only forward to Anthropic's production API or local "
                f"testing endpoints. If you need a different upstream, "
                f"file an issue."
            )
        # Refuse plain http:// to non-loopback hosts (HTTPS-only for
        # production; http allowed for testing against loopback).
        if self._up.scheme == "http" and host not in {
            "127.0.0.1", "localhost", "::1"
        }:
            raise ValueError(
                f"upstream {self.upstream!r}: http:// is only allowed for "
                f"loopback testing. Use https:// for api.anthropic.com."
            )

    def upstream_host(self) -> str:
        return self._up.hostname or ""

    def upstream_port(self) -> int:
        return self._up.port or (443 if self._up.scheme == "https" else 80)

    def upstream_scheme(self) -> str:
        return self._up.scheme

    def load_real_credential(self) -> tuple[str, str]:
        """Read the real credential from disk; return (kind, value).

        kind ∈ {"api_key", "oauth_access_token"}. The handler uses kind
        to pick the right upstream auth header:
          - api_key            → x-api-key: <value>
          - oauth_access_token → Authorization: Bearer <value>

        Supported file formats:

        - Plain string (the file's contents are the key, stripped).
          → ("api_key", <stripped>)
        - JSON with {"api_key": "..."} or {"key": "..."} or {"token": "..."}.
          → ("api_key", <value>)
        - JSON with {"claudeAiOauth": {"accessToken": "...", ...}} —
          the shape `claude /login` writes for Claude Pro/Max OAuth.
          → ("oauth_access_token", <accessToken>)

        Per DN-009 Gap 2: previously the
        proxy fell through to `return raw` for the OAuth shape, sending
        the entire JSON blob as x-api-key. The READ half is fixed here;
        the REFRESH half (Gap 3) is still v0.1.x.

        internal design note DN-042 finding #1: validate file
        ownership + mode bits before reading. A buggy write that left
        the file world-readable would silently leak the credential to
        anyone with shell on the host; refuse loudly instead.
        """
        try:
            st = os.stat(self.real_creds_path)
        except OSError as exc:
            raise RuntimeError(
                f"credential file {self.real_creds_path}: {exc}"
            ) from exc
        if st.st_uid != os.getuid():
            raise RuntimeError(
                f"credential file {self.real_creds_path}: owned by uid "
                f"{st.st_uid}, expected {os.getuid()}. Refusing to read."
            )
        # 0o077 = "any bit other than user-rwx". 0o600/0o400 are fine;
        # 0o604, 0o660, 0o644 are not.
        if (st.st_mode & 0o077) != 0:
            raise RuntimeError(
                f"credential file {self.real_creds_path}: mode "
                f"{oct(st.st_mode & 0o777)} is broader than 0600. "
                f"Refusing to read; tighten with `chmod 600`."
            )
        raw = self.real_creds_path.read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                # OAuth shape (Claude Pro/Max, Claude Code `claude /login`):
                #   {"claudeAiOauth": {"accessToken": "...",
                #                       "refreshToken": "...",
                #                       "expiresAt": <epoch_ms>, ...}}
                oauth = obj.get("claudeAiOauth")
                if isinstance(oauth, dict):
                    access = oauth.get("accessToken")
                    if isinstance(access, str) and access.strip():
                        return ("oauth_access_token", access.strip())
                # API-key shape (direct Anthropic console keys).
                for key_name in ("api_key", "key", "token"):
                    key = obj.get(key_name)
                    if isinstance(key, str) and key.strip():
                        return ("api_key", key.strip())
                # JSON that we recognize as a dict but don't understand
                # — refuse rather than send the blob.
                raise RuntimeError(
                    f"credential file {self.real_creds_path} is JSON but "
                    f"contains neither an `api_key`/`key`/`token` field "
                    f"nor a `claudeAiOauth.accessToken`. Re-run "
                    f"`botainer auth login --agent claude` (or switch to "
                    f"isolated mode) to write a recognized shape."
                )
        # Plain-string fallback: assume it's an API key.
        return ("api_key", raw)


# ────────── rate limiting (sliding-window per-second) ──────────


class _RateLimiter:
    def __init__(self, rps: int) -> None:
        self.rps = max(1, rps)
        self.window: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - 1.0
        while self.window and self.window[0] < cutoff:
            self.window.popleft()
        if len(self.window) >= self.rps:
            return False
        self.window.append(now)
        return True


# ────────── audit ──────────

# Hash-chained audit log. Each entry carries `prev_hash` = sha256 of the
# previous entry's serialized form (or 64 zero hex chars for the first).
# Verifying the chain end-to-end detects truncation, reordering, or
# in-place edits — the only way to forge the chain is to compute a fresh
# hash for every subsequent line, which is detectable if the user knows
# the expected starting state. Not a defense against an attacker who can
# rewrite the whole file in one pass without prior reads, but useful
# against accidental truncation and casual tampering.
#
# Per internal design note DN-042 finding #2.

import hashlib

_PREV_HASH_ZERO = "0" * 64


def _read_last_hash(path: Path) -> str:
    """Read the prev_hash for the next audit entry.

    Reads the last line of the log; returns its sha256. If the log is
    missing or empty, returns the all-zero starting hash.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return _PREV_HASH_ZERO
            # Walk back up to 64 KiB looking for the last newline.
            read_back = min(65536, size)
            f.seek(size - read_back)
            chunk = f.read()
            lines = chunk.split(b"\n")
            # Last non-empty line:
            for line in reversed(lines):
                if line.strip():
                    return hashlib.sha256(line).hexdigest()
            return _PREV_HASH_ZERO
    except OSError:
        return _PREV_HASH_ZERO


def _audit(cfg: ProxyConfig, **fields: object) -> None:
    entry: dict[str, object] = {"ts": datetime.now(timezone.utc).isoformat()}
    entry.update(fields)
    entry["prev_hash"] = _read_last_hash(cfg.audit_log)
    try:
        fd = os.open(
            str(cfg.audit_log),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            mode=0o600,
        )
        try:
            # sort_keys for deterministic hashing across re-reads.
            os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        # Audit is best-effort; don't fail requests because the log can't write.
        pass


def verify_audit_chain(audit_log: Path) -> tuple[bool, str]:
    """Verify the hash chain integrity of an audit log.

    Returns (ok, detail). Suitable for `botainer doctor` to run when
    asked. Linear scan; one pass.
    """
    if not audit_log.exists():
        return True, "audit log not present (no sessions yet)"
    expected_prev = _PREV_HASH_ZERO
    with open(audit_log, "rb") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.rstrip(b"\n")
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                return False, f"line {lineno}: JSON decode error: {exc}"
            actual_prev = entry.get("prev_hash")
            if actual_prev != expected_prev:
                return False, (
                    f"line {lineno}: prev_hash mismatch "
                    f"(expected {expected_prev[:16]}..., got "
                    f"{str(actual_prev)[:16]}...). The log was truncated, "
                    "reordered, or edited."
                )
            expected_prev = hashlib.sha256(line).hexdigest()
    return True, f"chain verified ({audit_log})"


# ────────── HTTP handler ──────────


def _make_handler(cfg: ProxyConfig, rate_limiter: _RateLimiter):
    # The real key is loaded fresh each request so credential file
    # rotation works without restarting the proxy. (Cheap; the file
    # is small and the read is local.)
    class _Handler(BaseHTTPRequestHandler):
        # Silence the default per-request stderr noise; we audit ourselves.
        def log_message(self, format: str, *args: object) -> None:
            return

        def _refuse(self, status: int, reason: str) -> None:
            body = json.dumps({"error": reason}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self) -> bool:
            auth = self.headers.get("Authorization", "")
            scheme, _, presented = auth.partition(" ")
            if not (scheme in ("Bearer", "bearer") and presented):
                # Anthropic CLI sometimes uses x-api-key header.
                presented = self.headers.get("x-api-key", "")
            if not presented:
                return False
            return secrets.compare_digest(presented, cfg.ephemeral_token)

        def _handle_request(self) -> None:
            if not rate_limiter.allow():
                _audit(
                    cfg, kind="rate_limited", method=self.command,
                    path=self.path, status=-1,
                )
                self._refuse(429, "rate-limited")
                return
            if not self._check_auth():
                _audit(
                    cfg, kind="auth_rejected", method=self.command,
                    path=self.path, status=-1,
                )
                self._refuse(403, "invalid session token")
                return
            content_length = int(self.headers.get("Content-Length", "0") or 0)
            if content_length > cfg.max_request_bytes:
                _audit(
                    cfg, kind="body_too_large", method=self.command,
                    path=self.path, request_bytes=content_length, status=-1,
                )
                self._refuse(413, f"body exceeds {cfg.max_request_bytes}")
                return
            body = self.rfile.read(content_length) if content_length else b""

            # Build headers for upstream: copy all except hop-by-hop +
            # Authorization (we substitute) + Host (let http.client set).
            HOP_BY_HOP = {
                "connection", "keep-alive", "proxy-authenticate",
                "proxy-authorization", "te", "trailer", "transfer-encoding",
                "upgrade", "host", "authorization", "x-api-key",
            }
            up_headers = {}
            for k, v in self.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    up_headers[k] = v
            # Pick the right auth header for the credential kind. The
            # READ half of proxy OAuth support: API keys go in x-api-key,
            # OAuth access tokens go in Authorization: Bearer. Refresh
            # (the WRITE half) is v0.1.x; if the upstream returns 401
            # the handler currently passes it through and the agent
            # surfaces the failure.
            try:
                cred_kind, real_value = cfg.load_real_credential()
            except RuntimeError as exc:
                _audit(
                    cfg, kind="credential_load_failed",
                    method=self.command, path=self.path, status=-1,
                    error=str(exc),
                )
                self._refuse(503, f"credential load failed: {exc}")
                return
            if cred_kind == "oauth_access_token":
                up_headers["Authorization"] = f"Bearer {real_value}"
                # Also send the standard OAuth-required version header
                # the Claude Code client expects to be there. (The
                # in-container client sets `anthropic-beta` and
                # `anthropic-version` already; we leave those alone.)
            else:
                up_headers["x-api-key"] = real_value
            # Anthropic versioning header: pass through whatever the
            # container sent if any (the CLI sets it).

            # Forward.
            conn_cls = (
                http.client.HTTPSConnection
                if cfg.upstream_scheme() == "https"
                else http.client.HTTPConnection
            )
            try:
                conn = conn_cls(cfg.upstream_host(), cfg.upstream_port(),
                                timeout=120)
                conn.request(self.command, self.path, body=body,
                             headers=up_headers)
                response = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                _audit(
                    cfg, kind="upstream_error", method=self.command,
                    path=self.path, status=-1, error=str(exc),
                )
                self._refuse(502, f"upstream error: {exc}")
                return

            # Stream response back.
            try:
                self.send_response(response.status)
                # Filter out hop-by-hop response headers.
                for k, v in response.getheaders():
                    if k.lower() not in HOP_BY_HOP:
                        self.send_header(k, v)
                self.end_headers()
                bytes_out = 0
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    bytes_out += len(chunk)
                self.wfile.flush()
            finally:
                conn.close()

            audit_extra: dict[str, object] = {
                "kind": "forwarded",
                "method": self.command,
                "path": self.path,
                "status": response.status,
                "request_bytes": content_length,
                "response_bytes": bytes_out,
            }
            # Task #300: was a 4 KiB preview written per request, growing
            # the audit log unbounded over time AND leaking request
            # content (user prompts can contain credentials). Now we
            # record bytes + sha256-prefix only; recovery isn't possible
            # but you can answer 'was the same request sent twice' and
            # 'how much did the agent send'. Set BOTAINER_PROXY_AUDIT_BODIES=1
            # to opt back into the 4 KiB preview if needed for debugging.
            import hashlib as _hashlib
            import os as _os
            if not cfg.redact_bodies and body:
                if _os.environ.get("BOTAINER_PROXY_AUDIT_BODIES", "0") == "1":
                    audit_extra["request_body_preview"] = body[:4096].decode(
                        "utf-8", errors="replace"
                    )
                else:
                    audit_extra["request_body_sha8"] = (
                        _hashlib.sha256(body).hexdigest()[:8]
                    )
            _audit(cfg, **audit_extra)

        # Route every HTTP method through the same handler.
        do_GET = _handle_request
        do_POST = _handle_request
        do_PUT = _handle_request
        do_DELETE = _handle_request
        do_PATCH = _handle_request
        do_HEAD = _handle_request

    return _Handler


# ────────── unix-socket HTTP server ──────────


class _UnixHTTPServer(socketserver.UnixStreamServer):
    """A unix-socket variant of HTTPServer.

    Threading is intentionally NOT used at v0.1.0: a single agent
    pipelines requests one-at-a-time normally, and reading the real
    credential per request avoids stale-cache concerns. If we need
    parallel agent processes later we can switch to ThreadingMixIn.
    """

    allow_reuse_address = False

    def server_bind(self) -> None:
        # Remove a stale socket file before binding.
        try:
            os.unlink(self.server_address)  # type: ignore[arg-type]
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        super().server_bind()
        # Restrict to owner so no other host process can talk to the proxy.
        try:
            os.chmod(self.server_address, 0o600)  # type: ignore[arg-type]
        except OSError:
            pass


def _start_launcher_watchdog(launcher_pid: int, server) -> None:
    """#108: shut down the proxy if the launcher process disappears.

    The proxy is spawned with `start_new_session=True` so it survives
    a clean launcher exit + post_session SIGTERM via stop_proxy.py.
    The disaster case is the launcher dying *without* running
    post_session — SIGKILL, OOM, panic, system reboot. In that case
    nothing reaps the proxy: it stays alive forever holding the UNIX
    socket, and the next `botainer start` fails with "address in
    use" on the same socket path.

    This watchdog runs in a daemon thread that polls the launcher PID
    every 30s. When the launcher is gone (os.kill returns ESRCH),
    it triggers a clean shutdown by calling server.shutdown() —
    same code path as SIGTERM.

    Cross-platform: os.kill(pid, 0) works on Linux + macOS + BSD.
    No Linux-only PR_SET_PDEATHSIG required.

    PID-reuse race: on a system that rapidly recycles PIDs, a
    different process could land on the same PID before we notice.
    Theoretical but not real — typical PID space is 32768+, recycle
    takes minutes-to-hours on busy systems. If it ever bites, we'll
    add a /proc/<pid>/stat ctime check (Linux only).
    """
    import threading

    def _watch() -> None:
        while True:
            time.sleep(30)
            try:
                os.kill(launcher_pid, 0)
            except ProcessLookupError:
                sys.stderr.write(
                    f"[proxy] launcher pid {launcher_pid} is gone; "
                    f"shutting down (orphan guard)\n"
                )
                threading.Thread(target=server.shutdown, daemon=True).start()
                return
            except PermissionError:
                # Process exists but we can't signal it — fine, still alive.
                continue

    t = threading.Thread(target=_watch, daemon=True, name="launcher-watchdog")
    t.start()


def main() -> int:
    cfg = ProxyConfig()
    rate = _RateLimiter(cfg.rate_limit_rps)
    handler = _make_handler(cfg, rate)
    cfg.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    server = _UnixHTTPServer(str(cfg.socket_path), handler)  # type: ignore[arg-type]

    def _shutdown(signum: int, _frame: object) -> None:
        """SIGTERM handler. MUST NOT call `server.shutdown()` on this thread.

        `socketserver.shutdown()` sets a flag and then BLOCKS until
        `serve_forever()` acknowledges it. A signal handler runs ON the thread
        that is inside `serve_forever` (or inside a request it dispatched), so
        the thing it waits for cannot happen: the documented CPython deadlock.
        The proxy then ignores SIGTERM entirely.

        OBSERVED, not reasoned: a `timeout 5 python3 proxy.py` from an unrelated
        measurement was still alive **3h29m** later, parked in
        `futex_wait_queue`, socket not unlinked, no `proxy_stopped` audit record.
        `timeout` had sent SIGTERM at five seconds.

        Bounded today only by luck of layering — `stop_proxy.py` escalates to
        SIGKILL after 3s, and whole-proxy auth mode is refused at v0.1.0, so no
        user reaches it. Neither of those is a reason to keep a handler that
        cannot run.

        The fix is the shape the #108 watchdog in this same file already uses,
        and the one `plugins/wolfram-sidecar/proxy.py` has always used: get off
        the serving thread. `shutdown()` runs on a throwaway thread; this
        handler returns immediately, `serve_forever()` unblocks, and the
        `finally` in `main()` does the unlink and the audit record on the way
        out — so cleanup happens exactly once, on one path, rather than being
        duplicated here.
        """
        sys.stderr.write(f"[proxy] received signal {signum}; shutting down\n")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # #108: launcher-death watchdog. start_proxy.py passes the
    # launcher's PID via BOTAINER_LAUNCHER_PID; if absent, we skip
    # the watchdog (e.g. when the proxy is launched standalone for
    # manual testing).
    launcher_pid_str = os.environ.get("BOTAINER_LAUNCHER_PID", "")
    if launcher_pid_str:
        try:
            launcher_pid = int(launcher_pid_str)
        except ValueError:
            sys.stderr.write(
                f"[proxy] ignoring malformed BOTAINER_LAUNCHER_PID="
                f"{launcher_pid_str!r}\n"
            )
        else:
            _start_launcher_watchdog(launcher_pid, server)

    _audit(cfg, kind="proxy_started",
           pid=os.getpid(), socket=str(cfg.socket_path),
           upstream=cfg.upstream)
    sys.stderr.write(
        f"[proxy] listening on {cfg.socket_path}; "
        f"upstream={cfg.upstream}; pid={os.getpid()}\n"
    )
    try:
        server.serve_forever()
    finally:
        _audit(cfg, kind="proxy_stopped", pid=os.getpid())
        try:
            cfg.socket_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
