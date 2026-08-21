"""Broker request handling + a unix-socket HTTP server.

Ported from the proven spike at
``DN-015/broker/daemon.py``.

Security-relevant behavior, all enforced here so it is testable in isolation:

1. The upstream (host/scheme/port) is PINNED and must be https by default.
   The client cannot redirect where the real token is sent (SSRF).
2. Redirects are NEVER followed — a 3xx from the upstream is returned
   unfollowed so the injected credential is never re-sent to a second hop.
3. Only allowlisted path prefixes are forwarded (default ``/v1/``).
4. ALL client-supplied credential + hop-by-hop headers are stripped
   (including anything the client listed in ``Connection:``); the real
   credential is injected host-side by the keystore.
5. The response returned to the client is sanitized: only ``Content-Type``
   comes back, never an upstream auth header.
6. The request handler is hardened against malformed framing / body DoS
   (chunked refused, ``Content-Length`` validated, body-size cap, worker cap).

The keystore contract (see :class:`Keystore`): ``outbound_authorization()``
returns the value for the outbound ``Authorization`` header (or ``None``),
and an optional ``outbound_headers()`` returns a dict of extra headers to
inject host-side (e.g. an ``x-api-key`` style upstream). A keystore that
cannot produce a credential raises — the handler fails CLOSED with a 502
that carries no secret.
"""

from __future__ import annotations

import contextlib
import http.server
import os
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping, Protocol, runtime_checkable

from botainer.core.refusal import Refused

CREDENTIAL_HEADERS = ("authorization", "x-api-key", "proxy-authorization", "cookie")
HOP_BY_HOP = (
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "te", "trailer", "proxy-connection", "proxy-authenticate",
)
ALLOWED_METHODS = ("GET", "POST")
MAX_BODY_BYTES = 10 * 1024 * 1024
_WORKER_LIMIT = threading.Semaphore(64)
# Hard cap on concurrent handler THREADS (audit MEDIUM): the worker
# semaphore above only bounds requests being FORWARDED, and it is acquired after
# ThreadingMixIn has already spawned a thread per connection — so a connection
# flood spawned unbounded host threads. This caps threads at accept time.
MAX_CONNECTIONS = 128
# Whole-request body-read deadline (audit MEDIUM): the per-recv socket
# timeout does not bound a slowloris trickling one byte per timeout window. This
# is a wall-clock ceiling on reading the request body.
BODY_READ_DEADLINE_S = 60.0
# Wall-clock ceiling on reading the request LINE + headers (sharp-edges MED
#). The `timeout = 30` socket timeout is per-recv, so a byte-per-29s
# trickle keeps the connection (and its capped slot) alive forever — a header
# slowloris. On a shared HPC node another local user could open MAX_CONNECTIONS
# such connections (the slot is taken before the sentinel check) and deny the
# victim its broker. A watchdog shuts the socket down if the request isn't
# parsed within this bound.
HEADER_PHASE_DEADLINE_S = 10.0
_READ_CHUNK = 65536


@runtime_checkable
class Keystore(Protocol):
    """What the daemon needs from a credential source."""

    def outbound_authorization(self) -> str | None:
        """The ``Authorization`` value for the broker→upstream hop (or None)."""
        ...  # pragma: no cover - protocol


class BrokerError(Exception):
    """A per-request refusal, surfaced to the CLIENT as an HTTP status.

    Distinct from :class:`botainer.core.refusal.Refused`: ``Refused`` is a
    hard host-side refusal (e.g. no credential available — the daemon should
    not have started / the request cannot be authenticated); ``BrokerError``
    is the daemon telling the in-container client "no" for THIS request.
    """

    def __init__(self, status: int, msg: str) -> None:
        self.status = status
        self.msg = msg
        super().__init__(f"{status}: {msg}")


def _read_body_bounded(rfile: object, n: int, *, deadline: float) -> bytes:
    """Read up to ``n`` request-body bytes under a wall-clock ``deadline``.

    Uses ``read1`` (returns after one underlying recv, possibly < requested) so a
    trickled body is noticed between chunks instead of blocking a full-buffer
    fill — a slowloris can't hold a worker slot forever (audit MEDIUM).
    Raises ``TimeoutError`` on the deadline or a stalled/timed-out read."""
    if not n:
        return b""
    buf = bytearray()
    read1 = getattr(rfile, "read1", None)
    while len(buf) < n:
        if time.monotonic() >= deadline:
            raise TimeoutError("request body read exceeded the deadline")
        want = min(n - len(buf), _READ_CHUNK)
        try:
            chunk = read1(want) if read1 else rfile.read(want)  # type: ignore[operator]
        except OSError as exc:  # socket.timeout (⊂ OSError) or a broken read
            raise TimeoutError(f"request body read stalled: {exc}") from exc
        if not chunk:
            break  # client closed early — forward what we got
        buf += chunk
    return bytes(buf)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow ANY redirect. Returning None means the 3xx is surfaced
    as an HTTPError instead of being followed with the injected credential
    attached (the cross-origin auth-leak the prototype red-team found)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


# Empty ProxyHandler → ignore the broker HOST's ambient HTTPS_PROXY/ALL_PROXY env
# (audit LOW): egress goes straight to the pinned upstream, not through
# whatever proxy the operator's shell happened to export.
_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def _connection_listed(headers: Mapping[str, str]) -> set[str]:
    """Header names the client nominated as hop-by-hop via ``Connection:``.
    Case-insensitive on BOTH the header name and the listed tokens (a client
    spelling ``CONNECTION:`` must not slip its nominated headers past the
    stripper)."""
    listed: set[str] = set()
    for k, v in headers.items():
        if k.lower() == "connection" and v:
            for tok in v.split(","):
                listed.add(tok.strip().lower())
    return listed


class _CapMixin:
    """Bound concurrent handler threads at accept time. Over the cap, the
    connection is closed immediately WITHOUT spawning a thread — so a connection
    flood can't exhaust host threads/FDs (the per-request worker semaphore is too
    late for that, running only after the thread is already spawned)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[misc]
        self._conn_sem = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request: object, client_address: object) -> None:
        if not self._conn_sem.acquire(blocking=False):
            try:
                self.shutdown_request(request)  # type: ignore[attr-defined]
            except Exception:
                pass
            return
        t = threading.Thread(
            target=self._run_capped, args=(request, client_address), daemon=True)
        t.start()

    def _run_capped(self, request: object, client_address: object) -> None:
        try:
            self.process_request_thread(request, client_address)  # type: ignore[attr-defined]
        finally:
            self._conn_sem.release()


def _merge_anthropic_beta(clean: dict[str, str], value: str) -> None:
    """Ensure ``value`` is present in the ``anthropic-beta`` header WITHOUT
    clobbering any beta flags the client already sent (Claude Code sends its own
    feature betas). Dedupes, preserves order."""
    existing_key = next((k for k in clean if k.lower() == "anthropic-beta"), None)
    existing = clean.pop(existing_key) if existing_key else ""
    seen: list[str] = []
    for tok in (t.strip() for t in f"{existing},{value}".split(",")):
        if tok and tok not in seen:
            seen.append(tok)
    clean["anthropic-beta"] = ", ".join(seen)


def _prepare_upstream_request(
    *,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    keystore: Keystore,
    pinned_upstream: str,
    method: str,
    allowed_prefixes: tuple[str, ...],
    allow_insecure_upstream: bool,
    required_client_token: str | None,
) -> urllib.request.Request:
    """Validate + sanitize the request and inject the real credential, returning
    the outbound ``Request`` (guarantees (0)-(4)). Raises ``BrokerError`` on any
    refusal and propagates ``Refused`` from the keystore (fail-closed). Both the
    buffered ``handle_request`` and the streaming server path go through here so
    the security invariants live in ONE place.

    ``required_client_token``: TCP-loopback transport (Docker Desktop) — the
    caller MUST present ``Authorization: Bearer <token>`` (the per-session
    sentinel) or the request is refused; a loopback port has no uid boundary.
    Unix-socket transport leaves this ``None`` (0600 socket IS the boundary)."""
    up = urllib.parse.urlsplit(pinned_upstream)
    if up.scheme not in ("http", "https") or not up.netloc:
        raise BrokerError(500, "pinned_upstream must be an absolute http(s) URL")
    if up.scheme != "https" and not allow_insecure_upstream:
        raise BrokerError(
            500, "refusing non-https upstream (set allow_insecure_upstream for local tests)"
        )
    if method not in ALLOWED_METHODS:
        raise BrokerError(405, f"method {method!r} not allowed")

    # (0) transport access control (TCP only): constant-time compare.
    if required_client_token is not None:
        incoming_auth = ""
        for _k, _v in headers.items():
            if _k.lower() == "authorization":
                incoming_auth = _v or ""
                break
        import hmac as _hmac
        # Compare on BYTES: a non-ASCII Authorization value would make the str
        # form of compare_digest raise TypeError (audit LOW). encode
        # with errors="ignore" so a hostile non-ASCII guess simply mismatches.
        want = f"Bearer {required_client_token}".encode()
        got = incoming_auth.encode("utf-8", "ignore")
        if not _hmac.compare_digest(got, want):
            raise BrokerError(403, "broker access denied")

    # (3) scope: reject absolute URLs and anything off the path allowlist.
    if "://" in path or path.startswith("//"):
        raise BrokerError(400, "absolute URLs are not permitted")
    scope = path.split("?", 1)[0]
    lowered = scope.lower()
    # Reject encoded separators AND encoded-percent (%25…), so a double-encoded
    # `%252e`/`%252f` can't slip the single-decode check to a backend that
    # decodes twice (audit LOW).
    if ("%2e" in lowered or "%2f" in lowered or "%5c" in lowered
            or "%25" in lowered or "\\" in scope):
        raise BrokerError(400, "encoded path separators are not permitted")
    if "." in scope.split("/") or ".." in scope.split("/"):
        raise BrokerError(400, "dot segments are not permitted in the path")
    if not any(scope.startswith(p) for p in allowed_prefixes):
        raise BrokerError(403, f"path {scope!r} not in allowlist {allowed_prefixes}")

    # (4) strip every client credential + hop-by-hop header (incl. Connection-listed)
    drop = {h.lower() for h in CREDENTIAL_HEADERS} | {h.lower() for h in HOP_BY_HOP}
    drop |= _connection_listed(headers)
    # Force IDENTITY encoding upstream: the broker streams raw bytes and (for
    # security) passes only Content-Type/Content-Encoding back. If the client's
    # Accept-Encoding stayed, a gzip/br response body would reach the client
    # mislabeled → garbage / "400 <binary>". Dropping it makes upstream reply
    # uncompressed. (Content-Encoding IS also passed through below, belt-and-
    # suspenders, in case an upstream compresses regardless.)
    drop.add("accept-encoding")
    # Canonicalize to LOWERCASE keys (sharp-edges HIGH). If we kept the
    # client's original case, a case-variant (`Chatgpt-Account-Id`) would COEXIST in
    # the dict with a host-injected canonical key (`chatgpt-account-id`); both
    # capitalize to the same name in urllib.add_header, and the later one wins — so
    # a client could OVERRIDE the injected `chatgpt-account-id` (spoof the account)
    # or strip the forced `anthropic-beta` flag by ordering duplicate case-variants.
    # One lowercase key per header name means the host injection below is the only
    # writer, authoritative.
    clean: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk not in drop:
            clean[lk] = v  # a later case-variant overwrites, never coexists

    # The swap, host-side: whatever the client sent (a sentinel, junk, nothing)
    # was stripped above; the REAL credential is injected here.
    injected = keystore.outbound_authorization()
    if injected is not None:
        clean["authorization"] = injected
    # auth_style seam: the keystore may need extra headers (e.g. the OAuth
    # `anthropic-beta: oauth-2025-04-20` flag required for a subscription Bearer
    # to be accepted). anthropic-beta is MERGED (Claude Code's feature betas are
    # preserved); everything else is SET authoritatively (client copy can't survive
    # — keys are lowercased above, so this overwrites any client value).
    extra = getattr(keystore, "outbound_headers", None)
    if callable(extra):
        for k, v in (extra() or {}).items():
            if k.lower() == "anthropic-beta":
                _merge_anthropic_beta(clean, v)
            else:
                clean[k.lower()] = v

    # (1)(2) forward to the PINNED upstream (opener refuses redirects).
    url = pinned_upstream.rstrip("/") + path
    req = urllib.request.Request(url, data=body if body else None, method=method)
    for k, v in clean.items():
        req.add_header(k, v)
    return req


def handle_request(
    *,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    keystore: Keystore,
    pinned_upstream: str,
    method: str = "POST",
    allowed_prefixes: tuple[str, ...] = ("/v1/",),
    allow_insecure_upstream: bool = False,
    required_client_token: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Buffered forward: return ``(status, {Content-Type}, body)``. The server
    STREAMS instead (see make_handler); this stays for tests + non-streaming
    callers. Response is sanitized: only Content-Type is passed back."""
    req = _prepare_upstream_request(
        path=path, headers=headers, body=body, keystore=keystore,
        pinned_upstream=pinned_upstream, method=method,
        allowed_prefixes=allowed_prefixes,
        allow_insecure_upstream=allow_insecure_upstream,
        required_client_token=required_client_token,
    )
    try:
        with _OPENER.open(req, timeout=300) as r:
            ct = r.headers.get("Content-Type", "application/json")
            return r.status, {"Content-Type": ct}, r.read()
    except urllib.error.HTTPError as e:
        ct = e.headers.get("Content-Type", "application/json") if e.headers else "application/json"
        return e.code, {"Content-Type": ct}, e.read()


class _UnixHTTPServer(_CapMixin, socketserver.ThreadingMixIn,
                      socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, handler: type) -> None:
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        # LOW-1: create the socket already 0600 by dropping group/other in the
        # umask across the bind, closing the create→chmod window (the parent dir
        # is 0700 host-private too, so this is belt-and-suspenders).
        old_umask = os.umask(0o077)
        try:
            super().__init__(socket_path, handler)
        finally:
            os.umask(old_umask)
        # Owner-only: the socket is the identity boundary (topology, not a
        # token); don't let another local uid talk to the broker.
        os.chmod(socket_path, 0o600)


class _TCPHTTPServer(_CapMixin, socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Loopback TCP transport for Docker Desktop (macOS/Windows), where a host
    unix socket cannot be bind-mounted across the VM boundary. The container
    reaches this via ``host.docker.internal:<port>``. Access is gated by the
    per-session sentinel (``required_client_token`` in the handler), so binding
    loopback does NOT expose the broker to other local processes that lack it."""
    daemon_threads = True
    allow_reuse_address = True


def make_handler(
    keystore: Keystore,
    pinned_upstream: str,
    allowed_prefixes: tuple[str, ...] = ("/v1/",),
    allow_insecure_upstream: bool = False,
    required_client_token: str | None = None,
    debug_log: str | None = None,
) -> type:
    """Build the BaseHTTPRequestHandler subclass bound to this keystore/upstream.

    The handler STREAMS the upstream response (Claude Code streams every request
    via SSE; a buffered reply breaks its parser with "Failed to parse JSON").
    On a success it streams chunk-by-chunk with the upstream Content-Type and
    ``Connection: close``; on an upstream 4xx/5xx it buffers + returns the (small
    JSON) error so the client parses it. ``debug_log`` (host-only path) records
    per-request upstream status / content-type / body-head for diagnosis; it
    never logs request headers (which carry the injected real credential)."""

    # Response-body heads are model output; retaining them host-side is opt-in
    # (audit LOW). Status/content-type/phase are always logged (no
    # content). Set BOTAINER_BROKER_DEBUG_BODIES=1 to also capture body heads.
    debug_bodies = os.environ.get("BOTAINER_BROKER_DEBUG_BODIES") == "1"

    def _dbg(**fields: object) -> None:
        if not debug_log:
            return
        if not debug_bodies:
            fields.pop("head", None)  # drop model-output snippets unless opted in
        try:
            import json as _json
            # 0600 from creation (L1): the log retains response-body heads (model
            # output) host-side; don't inherit a 0644 umask. O_NOFOLLOW refuses a
            # planted symlink at the path.
            fd = os.open(debug_log,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                         0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(_json.dumps(fields) + "\n")
        except OSError:
            pass

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30

        def handle_one_request(self) -> None:
            # Bound the request-line + header read with an ABSOLUTE deadline (the
            # socket timeout is only per-recv). A watchdog force-closes the socket
            # if the request isn't parsed in time; `_serve` cancels it once headers
            # are in hand, so a legit slow UPSTREAM isn't affected.
            self._hdr_wd = threading.Timer(
                HEADER_PHASE_DEADLINE_S, self._hdr_deadline)
            self._hdr_wd.daemon = True
            self._hdr_wd.start()
            try:
                super().handle_one_request()
            finally:
                self._hdr_wd.cancel()

        def _hdr_deadline(self) -> None:
            with contextlib.suppress(Exception):
                self.connection.shutdown(socket.SHUT_RDWR)

        def _reply(self, status: int, hdrs: dict[str, str], resp: bytes) -> None:
            # An error reply before the body is drained would desync a keep-alive
            # connection (leftover bytes parsed as the next request). Close on
            # every error status (sharp-edges LOW).
            if status >= 400:
                self.close_connection = True
            self.send_response(status)
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def _serve(self) -> None:
            # Headers are parsed by the time do_POST/do_GET dispatch here — stop
            # the header-phase watchdog so it can't fire during the upstream call.
            _wd = getattr(self, "_hdr_wd", None)
            if _wd is not None:
                _wd.cancel()
            if not _WORKER_LIMIT.acquire(blocking=False):
                return self._reply(503, {"Content-Type": "text/plain"}, b"broker busy")
            try:
                # (6) reject unsupported/ambiguous framing rather than mis-forwarding
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    return self._reply(
                        400, {"Content-Type": "text/plain"}, b"chunked not supported"
                    )
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return self._reply(
                        400, {"Content-Type": "text/plain"}, b"bad Content-Length"
                    )
                if n < 0:
                    return self._reply(
                        400, {"Content-Type": "text/plain"}, b"negative Content-Length"
                    )
                if n > MAX_BODY_BYTES:
                    return self._reply(
                        413, {"Content-Type": "text/plain"}, b"body too large"
                    )
                try:
                    body = _read_body_bounded(
                        self.rfile, n,
                        deadline=time.monotonic() + BODY_READ_DEADLINE_S)
                except TimeoutError:
                    return self._reply(
                        408, {"Content-Type": "text/plain"}, b"request body timeout"
                    )

                # Validate + inject (guarantees 0-4). Errors here reply cleanly.
                try:
                    req = _prepare_upstream_request(
                        path=self.path,
                        headers=dict(self.headers),
                        body=body,
                        keystore=keystore,
                        pinned_upstream=pinned_upstream,
                        method=self.command,
                        allowed_prefixes=allowed_prefixes,
                        allow_insecure_upstream=allow_insecure_upstream,
                        required_client_token=required_client_token,
                    )
                except BrokerError as e:
                    _dbg(path=self.path, phase="refused", status=e.status, msg=e.msg)
                    return self._reply(
                        e.status, {"Content-Type": "text/plain"}, e.msg.encode()
                    )
                except Refused:
                    _dbg(path=self.path, phase="credential_unavailable", status=502)
                    return self._reply(
                        502, {"Content-Type": "text/plain"},
                        b"broker credential unavailable",
                    )

                # Open the upstream. A 4xx/5xx is an HTTPError → buffer its JSON
                # error body (small) and reply so the client can parse it.
                try:
                    r = _OPENER.open(req, timeout=300)
                except urllib.error.HTTPError as e:
                    eb = e.read()
                    ct = (e.headers.get("Content-Type", "application/json")
                          if e.headers else "application/json")
                    _dbg(path=self.path, phase="upstream_error", status=e.code,
                         content_type=ct, head=eb[:400].decode("utf-8", "replace"))
                    return self._reply(e.code, {"Content-Type": ct}, eb)
                except Exception as ex:  # URLError, timeout, TLS, …
                    _dbg(path=self.path, phase="upstream_exception", err=repr(ex))
                    return self._reply(
                        502, {"Content-Type": "text/plain"}, b"upstream error"
                    )

                # Success: STREAM the body through, sanitized (Content-Type only).
                try:
                    ct = r.headers.get("Content-Type", "application/json")
                    ce = r.headers.get("Content-Encoding")
                    _dbg(path=self.path, phase="upstream_ok", status=r.status,
                         content_type=ct, content_encoding=ce)
                    self.send_response(r.status)
                    self.send_header("Content-Type", ct)
                    if ce:  # belt-and-suspenders: don't mislabel a compressed body
                        self.send_header("Content-Encoding", ce)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    logged_head = False
                    while True:
                        chunk = r.read(8192)
                        if not chunk:
                            break
                        if not logged_head:
                            _dbg(path=self.path, phase="body_head",
                                 head=chunk[:400].decode("utf-8", "replace"))
                            logged_head = True
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    self.close_connection = True
                except Exception as ex:
                    # Headers already sent — can't send a fresh status; drop the
                    # connection (the client sees a truncated stream).
                    _dbg(path=self.path, phase="stream_error", err=repr(ex))
                    self.close_connection = True
                finally:
                    r.close()
            finally:
                _WORKER_LIMIT.release()

        do_POST = _serve
        do_GET = _serve

        def log_message(self, *a: object) -> None:  # noqa: D102 - silence stdlib logging
            pass

    return _Handler


def serve_unix(
    socket_path: str,
    keystore: Keystore,
    pinned_upstream: str,
    *,
    allowed_prefixes: tuple[str, ...] = ("/v1/",),
    allow_insecure_upstream: bool = False,
    debug_log: str | None = None,
) -> _UnixHTTPServer:
    """Bind a threading HTTP server on ``socket_path``. Caller runs
    ``serve_forever()`` (typically on a thread or as the daemon main loop).

    Unix transport: the 0600 socket + 0700 parent dir ARE the access boundary,
    so no ``required_client_token`` is used (only the owner uid can connect)."""
    handler = make_handler(
        keystore,
        pinned_upstream,
        allowed_prefixes=allowed_prefixes,
        allow_insecure_upstream=allow_insecure_upstream,
        debug_log=debug_log,
    )
    return _UnixHTTPServer(socket_path, handler)


def serve_tcp(
    host: str,
    port: int,
    keystore: Keystore,
    pinned_upstream: str,
    *,
    required_client_token: str,
    allowed_prefixes: tuple[str, ...] = ("/v1/",),
    allow_insecure_upstream: bool = False,
    debug_log: str | None = None,
) -> _TCPHTTPServer:
    """Bind a threading HTTP server on ``(host, port)`` — the Docker-Desktop
    transport. ``required_client_token`` (the per-session sentinel) is MANDATORY:
    a loopback TCP port has no uid boundary, so every request must present it.
    Caller runs ``serve_forever()``. Bind to ``127.0.0.1`` for Docker Desktop
    (reached from the container via ``host.docker.internal``)."""
    if not required_client_token:
        raise ValueError("serve_tcp requires a non-empty required_client_token")
    handler = make_handler(
        keystore,
        pinned_upstream,
        allowed_prefixes=allowed_prefixes,
        allow_insecure_upstream=allow_insecure_upstream,
        required_client_token=required_client_token,
        debug_log=debug_log,
    )
    return _TCPHTTPServer((host, port), handler)


class UnixHTTPConnection:
    """Minimal client for talking HTTP over a unix socket (tests + probes)."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Return ``(status, response_headers_lowercased, body)``."""
        import http.client

        outer = self

        class _C(http.client.HTTPConnection):
            def connect(self) -> None:  # noqa: D102
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(outer.socket_path)
                self.sock = s

        c = _C("localhost")
        c.request(method, path, body=body, headers=dict(headers or {}))
        r = c.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data

    def post(
        self,
        path: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        return self.request("POST", path, headers=headers, body=body)
