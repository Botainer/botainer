#!/usr/bin/env python3
"""`botainer plugin browser gateway` — the TRUSTED laptop-side viewer gateway
(browser-strategy Track B). Runs on the USER'S machine: serves a vendored, pinned
noVNC under a strict CSP + a per-session token, and websockify-bridges it to the
container's x11vnc RFB (docker: a loopback-published RFB port; HPC: `ssh -L` to the
node's 0700 socket). The container serves NO code to the browser — it speaks only
RFB — which is what fixes the trust inversion.

STATUS: the PURE builders below (URL, CSP, ssh -L, websockify/browser argv) are
unit-tested here. `main()` is the runtime orchestration seam and is INTENTIONALLY
untested from the dev container (no docker/display/browser) — it is meant to be
finished + iterated in a host Claude Code session per DN-020.
Nothing here removes the existing in-container viewer; this is the additive target.

Security (from RESEARCH-leg 3, Fable-verified):
- Pin/vendor noVNC >= 0.6.2 (closes CVE-2017-18635); serve read-only.
- Strict CSP; `img-src data:` (upstream noVNC — NOT blob:, which is the KasmVNC WASM
  fork; VERIFY the bundle). 127.0.0.1/localhost, never ::1 (secure-context list).
- Mandatory token; prefer a `--unix-listen` socket (browsers can't dial it → no
  cross-site WebSocket hijacking); else loopback TCP + token + an Origin allowlist.
- NEVER a native VNC client against the untrusted container (native-code RFB
  decoder RCE class). Optionally run this whole gateway in a locked-down container.
"""
from __future__ import annotations

import argparse
import contextlib
import hmac
import importlib.util
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# The vendored noVNC under ../gateway_web/novnc — bump ONLY via the re-vendor
# procedure in ../gateway_web/PROVENANCE.md (two-channel fetch + hash + diff
# verification). 1.7.0 is upstream stable; >= 0.6.2 closes
# CVE-2017-18635. Hashes recorded at vendor time:
NOVNC_PIN = "1.7.0"
NOVNC_GITHUB_TARBALL_SHA256 = (
    "b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6")
NOVNC_NPM_INTEGRITY = (
    "sha512-ucEJOx4T2avIRCleodk7YobZj5O2Ga2AeLfQ69A/yjG9HHba2+PDgwSkN3"
    "FttrmG+70ZGx21sElNFouK13RzyA==")


def strict_csp(ws_port: int, host: str = "127.0.0.1") -> str:
    """The Content-Security-Policy for the locally-served noVNC page. Deny-by-
    default; only same-origin scripts; the ws origin listed explicitly; `data:`
    images (upstream noVNC's JPEG render path). Emitted as an HTTP RESPONSE header
    (not <meta> — need frame-ancestors)."""
    return "; ".join([
        "default-src 'none'",
        "script-src 'self'",
        f"connect-src ws://{host}:{ws_port}",
        "img-src 'self' data:",
        "style-src 'self'",
        "font-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ])


def novnc_url(port: int, token: str, host: str = "127.0.0.1") -> str:
    """The URL the user opens. Token rides the ws PATH (websockify TokenFile REQUIRES
    it). 127.0.0.1, never localhost/::1 mismatch. Minimal page (no defaults.json)."""
    return (f"http://{host}:{port}/vnc.html"
            f"?autoconnect=1&resize=scale&path=websockify%3Ftoken%3D{token}")


def ssh_forward_cmd(node: str, local_port: int, remote_socket: str,
                    jump: str | None = None) -> list[str]:
    """`ssh -L <local_port>:<remote 0700 socket> [-J <login>] <node>` — forwards a
    local TCP port to the compute node's RFB unix socket (OpenSSH >= 6.7). The
    gateway's websockify then targets 127.0.0.1:<local_port>."""
    cmd = ["ssh"]
    if jump:
        cmd += ["-J", jump]
    cmd += ["-L", f"{local_port}:{remote_socket}", node]
    return cmd


def websockify_cmd(web_dir: str | None, listen: str, target: str,
                   token_file: str | None = None) -> list[str]:
    """websockify argv. `listen` is `<0700-sock>` (preferred, browser-unreachable →
    no CSWSH) or `127.0.0.1:<port>`; `target` is the RFB endpoint (a unix socket or
    host:port). With a token_file, TokenFile-gates the ws upgrade. `web_dir` is
    None in the broker-fronted design (the trusted broker serves the noVNC page
    itself, with the CSP; websockify does ONLY the ws↔RFB byte bridge on its unix
    socket, which no browser can reach) — pass a dir only if websockify should
    also static-serve."""
    cmd = ["websockify"]
    if web_dir:
        cmd += ["--web", web_dir]
    if token_file:
        cmd += ["--token-plugin", "TokenFile", "--token-source", token_file]
    if listen.startswith("/"):                 # unix-socket listener
        cmd += [f"--unix-listen={listen}"]
    else:                                       # loopback TCP listener
        cmd += [listen]
    if target.startswith("/"):
        cmd += [f"--unix-target={target}"]
    else:
        cmd += [target]
    return cmd


def throwaway_browser_cmd(url: str, profile_dir: str,
                          browser: str = "chrome",
                          exe: str | None = None) -> list[str]:
    """Open the URL in a DEDICATED, throwaway profile — no shared cookies/extensions/
    sessions, so a hostile served page has no ambient creds and can't reach other
    127.0.0.1 apps as an authed origin. Delete the profile dir after. `exe`
    overrides argv[0] with a resolved absolute path (the flags — the security-
    relevant part — are fixed by `browser`; the platform resolves the binary,
    e.g. macOS's '/Applications/…/Google Chrome')."""
    if browser in ("chrome", "chromium", "google-chrome"):
        default_exe = {"chrome": "google-chrome", "chromium": "chromium",
                       "google-chrome": "google-chrome"}[browser]
        return [exe or default_exe, f"--user-data-dir={profile_dir}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--new-window", url]
    if browser == "firefox":
        return [exe or "firefox", "-no-remote", "-new-instance",
                "-profile", profile_dir, url]
    raise ValueError(f"unsupported browser {browser!r}")


_RFB_FORWARD_LABEL = "browser viewer (RFB)"          # composition.py (gateway mode)
_CONTAINER_RFB_PORT = 5900

# The gateway serves ONLY these vendored assets; anything else 404s. Content types
# are fixed here (not sniffed) so `X-Content-Type-Options: nosniff` is accurate.
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}


def gateway_web_dir() -> Path:
    """The vendored trusted client tree (`plugins/browser/gateway_web`), served
    read-only by the broker. Pinned noVNC lives under `novnc/`."""
    return Path(__file__).resolve().parents[1] / "gateway_web"


# ───────────────────────── HTTP head parsing (pure) ─────────────────────────
def parse_request_head(raw: bytes) -> tuple[str, str, dict[str, str]] | None:
    """Parse an HTTP/1.x request head into (method, target, headers-lowercased).
    Header VALUES keep case; keys are lowercased. Returns None on a malformed
    start line. Pure — unit-tested; the broker's only request parser."""
    text = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
    lines = text.split("\r\n")
    start = lines[0].split(" ")
    if len(start) < 2 or not start[0] or not start[1]:
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return start[0], start[1], headers


def origin_ok(headers: dict[str, str], expected_host: str) -> bool:
    """CSWSH + DNS-rebind kill for the ws upgrade. The browser MUST present both
    (a) Host == our loopback authority and (b) Origin == our exact http origin.
    A drive-by/rebound page fails (wrong Origin, or Host = the attacker name).
    Browsers ALWAYS send Origin on a ws handshake, so a missing Origin is
    rejected. Pure."""
    if headers.get("host") != expected_host:
        return False
    return headers.get("origin") == f"http://{expected_host}"


def token_ok(target: str, expected_token: str) -> bool:
    """Constant-time check of the ws-path `?token=` against the minted token."""
    got = parse_qs(urlparse(target).query).get("token", [""])[0]
    return bool(expected_token) and hmac.compare_digest(got, expected_token)


def resolve_static(target: str, web_dir: Path) -> tuple[Path, str] | None:
    """Map a request target to a vendored file, or None (404). `/` → vnc.html.
    Refuses path traversal (resolved path must stay under web_dir) and any
    extension not in `_MIME`. Pure-ish (stats the file)."""
    path = urlparse(target).path
    if path in ("", "/"):
        path = "/vnc.html"
    base = web_dir.resolve()
    full = (base / path.lstrip("/")).resolve()
    if base != full and base not in full.parents:
        return None                                   # traversal escape
    if not full.is_file() or full.suffix not in _MIME:
        return None
    return full, _MIME[full.suffix]


# ───────────────────────── the trusted broker ─────────────────────────
class _Broker(threading.Thread):
    """The ONLY browser-reachable listener (loopback TCP). Serves the vendored
    noVNC page with strict_csp() headers, and for the ws upgrade enforces
    Host+Origin (CSWSH) + the token before splicing raw bytes to websockify's
    0700 unix socket. The broker never parses WebSocket frames — after the
    header check it is a dumb TCP splice; websockify does the ws handshake +
    the ws↔RFB byte bridge. Container RFB bytes therefore never touch trusted
    parsing here."""

    def __init__(self, srv: socket.socket, web_dir: Path, token: str,
                 ws_sock: str, csp: str) -> None:
        super().__init__(daemon=True)
        self._srv = srv
        self._web_dir = web_dir
        self._token = token
        self._ws_sock = ws_sock
        self._csp = csp
        self.port = srv.getsockname()[1]
        self._host = f"127.0.0.1:{self.port}"
        self._stop = threading.Event()

    def run(self) -> None:
        self._srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,),
                             daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._srv.close()

    def _handle_conn(self, conn: socket.socket) -> None:
        # NB not `_handle` — threading.Thread has an internal `_handle`
        # (_thread._ThreadHandle) in Python 3.13 that would shadow the method.
        try:
            conn.settimeout(15)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 64 * 1024:
                    self._respond(conn, 431, "Request Header Fields Too Large")
                    return
            parsed = parse_request_head(buf)
            if parsed is None:
                self._respond(conn, 400, "Bad Request")
                return
            method, target, headers = parsed
            is_ws = (headers.get("upgrade", "").lower() == "websocket"
                     or urlparse(target).path == "/websockify")
            if is_ws:
                self._proxy_ws(conn, buf, target, headers)
            elif method == "GET":
                self._serve_static(conn, target)
            else:
                self._respond(conn, 405, "Method Not Allowed")
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                conn.close()

    def _serve_static(self, conn: socket.socket, target: str) -> None:
        resolved = resolve_static(target, self._web_dir)
        if resolved is None:
            self._respond(conn, 404, "Not Found")
            return
        full, ctype = resolved
        body = full.read_bytes()
        head = "\r\n".join([
            "HTTP/1.1 200 OK",
            f"Content-Type: {ctype}",
            f"Content-Length: {len(body)}",
            f"Content-Security-Policy: {self._csp}",
            "X-Content-Type-Options: nosniff",
            "Referrer-Policy: no-referrer",
            "Cross-Origin-Opener-Policy: same-origin",
            "Cross-Origin-Resource-Policy: same-origin",
            "Cache-Control: no-store",
            "Connection: close",
            "", "",
        ]).encode("latin-1")
        with contextlib.suppress(OSError):
            conn.sendall(head + body)
            conn.close()

    def _proxy_ws(self, conn: socket.socket, buf: bytes, target: str,
                  headers: dict[str, str]) -> None:
        if not origin_ok(headers, self._host):
            self._respond(conn, 403, "Forbidden (origin/host)")
            return
        if not token_ok(target, self._token):
            self._respond(conn, 403, "Forbidden (token)")
            return
        try:
            up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            up.connect(self._ws_sock)
        except OSError:
            self._respond(conn, 502, "Bad Gateway (websockify down)")
            return
        conn.settimeout(None)
        up.sendall(buf)                    # replay the head (+ any early ws bytes)
        self._splice(conn, up)

    @staticmethod
    def _splice(a: socket.socket, b: socket.socket) -> None:
        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    with contextlib.suppress(OSError):
                        s.shutdown(socket.SHUT_RDWR)
        t1 = threading.Thread(target=pump, args=(a, b), daemon=True)
        t2 = threading.Thread(target=pump, args=(b, a), daemon=True)
        t1.start(); t2.start(); t1.join(); t2.join()
        for s in (a, b):
            with contextlib.suppress(OSError):
                s.close()

    @staticmethod
    def _respond(conn: socket.socket, code: int, text: str) -> None:
        body = text.encode()
        resp = (f"HTTP/1.1 {code} {text}\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "X-Content-Type-Options: nosniff\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
                ).encode("latin-1") + body
        with contextlib.suppress(OSError):
            conn.sendall(resp)
            conn.close()


# ───────────────────────── runtime helpers ─────────────────────────
def docker_host_rfb_port(spec: dict) -> int | None:
    """The host loopback port compose published for the container RFB (gateway
    mode), read from the session record's `spec.port_forwards` — matched by the
    `browser viewer (RFB)` label, else by container_port 5900. Pure/testable."""
    forwards = spec.get("port_forwards") or []
    if not isinstance(forwards, (list, tuple)):
        return None
    by_label = [f for f in forwards if isinstance(f, dict)
                and f.get("label") == _RFB_FORWARD_LABEL]
    def _cport(f: dict) -> int:
        with contextlib.suppress(ValueError, TypeError):
            return int(f.get("container_port", 0))
        return 0
    by_port = [f for f in forwards if isinstance(f, dict)
               and _cport(f) == _CONTAINER_RFB_PORT]
    for f in (by_label or by_port):
        with contextlib.suppress(KeyError, ValueError, TypeError):
            return int(f["host_port"])
    return None


def _free_loopback_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _wait_for(pred, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_browser(preferred: str | None = None) -> tuple[str, str] | tuple[None, None]:
    """Resolve a real throwaway-capable browser on this host. macOS app bundles
    first (no PATH entry), then Linux PATH names. Returns (browser-kind, exe)."""
    mac = {
        "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "chromium": "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "firefox": "/Applications/Firefox.app/Contents/MacOS/firefox",
    }
    linux = {
        "chrome": ["google-chrome", "google-chrome-stable"],
        "chromium": ["chromium", "chromium-browser"],
        "firefox": ["firefox"],
    }
    order = ([preferred] if preferred else []) + ["chrome", "chromium", "firefox"]
    for kind in order:
        if kind not in mac:
            continue
        if os.path.exists(mac[kind]):
            return kind, mac[kind]
        for exe in linux[kind]:
            found = shutil.which(exe)
            if found:
                return kind, found
    return None, None


def websockify_available() -> bool:
    """True iff websockify can be launched — importable as a module under
    botainer's own interpreter (the preferred route, see
    `websockify_launch_argv`) or present as a console script on PATH. Lets the
    gateway fail UPFRONT with a clean install hint instead of after a spawn +
    timeout."""
    return (importlib.util.find_spec("websockify") is not None
            or shutil.which("websockify") is not None)


def websockify_launch_argv(cmd: list[str]) -> list[str]:
    """Turn `websockify_cmd(...)` output into an argv that actually launches.

    Prefers `sys.executable -m websockify` — the copy `pip install
    'botainer[gateway]'` put in botainer's OWN environment, i.e. the one whose
    version this project pins and tests. A bare-name `websockify` off PATH is
    the fallback for installs where the module isn't importable from this
    interpreter (a pipx/system split); it resolves to whatever PATH happens to
    say, which is neither pinned nor ours.

    Order matters beyond determinism: `websockify` bridges the browser to the
    VNC endpoint, so it sits on the gateway's data path. Reaching for our own
    interpreter first means the usual case never consults PATH at all.
    """
    if importlib.util.find_spec("websockify") is not None:
        return [sys.executable, "-m", "websockify", *cmd[1:]]
    return cmd


_WEBSOCKIFY_HINT = (
    "gateway mode needs websockify (the WebSocket↔RFB bridge that runs on your\n"
    "laptop). Install the gateway extra into botainer's own environment:\n"
    "    pip install 'botainer[gateway]'")


def _read_secret(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def _err(msg: str) -> None:
    sys.stderr.write(f"browser gateway: {msg}\n")


def _resolve_rfb_target(handle: dict, spec: dict, login: str | None,
                        cleanup: list) -> str | None:
    """Resolve the RFB endpoint the gateway's websockify targets, starting an
    `ssh -L` forward on HPC. Registers any child proc in `cleanup`. Returns a
    `host:port` string, or None (with a logged reason)."""
    transport = handle.get("transport")
    if transport == "tcp":                             # docker
        port = docker_host_rfb_port(spec) or handle.get("host_rfb_port")
        if not port:
            _err("docker RFB host port missing from spec.port_forwards — is this "
                 "a gateway-mode session that actually started?")
            return None
        if not _port_open("127.0.0.1", int(port)):
            _err(f"nothing is listening on 127.0.0.1:{port} (the container RFB). "
                 "Give `botainer start` a few seconds, or check the session is up.")
            return None
        return f"127.0.0.1:{int(port)}"
    # apptainer/HPC: forward the node-local 0700 RFB socket over ssh.
    node = handle.get("node")
    sock = handle.get("rfb_sock")
    if not node or not sock:
        _err("HPC gateway node/socket not recorded yet (job-start recording is a "
             "pending HPC-phase follow-up; see docs/CAPABILITY-SURFACE.md §4av).")
        return None
    # SSH OPTION-INJECTION GUARD (review HIGH): node is the ssh TRAILING
    # POSITIONAL — ssh treats any arg starting with '-' as an OPTION
    # (`-oProxyCommand=…` = laptop RCE), and `--` isn't reliably honored by the
    # OpenSSH client. The HPC session record is bound RW into the UNTRUSTED
    # container, so once job-start node-sync lands the cage could write this
    # field. Refuse a dash-leading node/socket/login (login rides `-J`). Fail
    # closed — this is the trusted-boundary the whole gateway exists to hold.
    for label, val in (("node", node), ("rfb_sock", sock), ("login/-J", login)):
        if val is not None and str(val).startswith("-"):
            _err(f"refusing an ssh {label} that starts with '-' ({val!r}) — "
                 "option-injection guard.")
            return None
    if handle.get("node_provisional"):
        _err(f"NOTE: node '{node}' is provisional (recorded on the login node at "
             "submit); if the ssh forward fails, substitute the real compute node.")
    local_port = _free_loopback_port()
    ssh_cmd = ssh_forward_cmd(str(node), local_port, str(sock), jump=login)
    ssh_cmd = [ssh_cmd[0], "-N", "-o", "ExitOnForwardFailure=yes", *ssh_cmd[1:]]
    _err(f"ssh -L 127.0.0.1:{local_port} → {node}:{sock} …")
    proc = subprocess.Popen(ssh_cmd)
    cleanup.append(lambda: proc.terminate())
    if not _wait_for(lambda: _port_open("127.0.0.1", local_port), timeout=15):
        _err("ssh forward to the compute node did not come up (site policy may "
             "forbid ssh-into-compute-node; the reverse-forward fallback is a "
             "tracked follow-up).")
        return None
    return f"127.0.0.1:{local_port}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="botainer plugin browser gateway",
        description="Open the running gateway-mode viewer through the trusted "
                    "laptop-side gateway (vendored noVNC + strict CSP + token).")
    ap.add_argument("--no-browser", action="store_true",
                    help="set up the gateway and print the URL; don't launch a "
                         "browser (leaves it running until Ctrl-C).")
    ap.add_argument("--browser", default=None,
                    help="chrome|chromium|firefox (default: autodetect).")
    ap.add_argument("--timeout", type=float, default=None,
                    help="exit after N seconds (for scripted tests).")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    # The gateway runs host-side; it reuses `watch`'s session-record loader
    # (same dir → on sys.path[0] under the plugin command dispatcher).
    try:
        import watch  # type: ignore
    except ImportError:
        _err("internal: cannot import the watch helper module.")
        return 3

    login = os.environ.get("BOTAINER_BROWSER_LOGIN") or None
    record = watch._load_running_viewer_record()
    if record is None:
        _err("no running viewer for this session. Set `plugins.browser.viewer: "
             "true` + `viewer_mode: gateway`, start a session, then re-run.")
        return 3
    handle = watch._viewer_handle(record) or {}
    if handle.get("mode") != "gateway":
        _err("this session's viewer is in LEGACY mode, not gateway. Use "
             "`botainer plugin browser watch`, or set `viewer_mode: gateway`.")
        return 3

    # Preflight the one non-stdlib dependency UPFRONT — before starting any ssh
    # forward, minting sockets, or spawning anything — so a missing dep fails
    # instantly with a clean install hint (not a leaked traceback after a
    # 10s wait). websockify is the laptop-side bridge (see pyproject gateway extra).
    if not websockify_available():
        _err(_WEBSOCKIFY_HINT)
        return 3

    spec = getattr(record, "spec", {}) or {}
    cleanup: list = []

    def _run_cleanup() -> None:
        for fn in reversed(cleanup):
            with contextlib.suppress(Exception):
                fn()

    try:
        rfb_target = _resolve_rfb_target(handle, spec, login, cleanup)
        if rfb_target is None:
            return 3

        rfb_pass_file = handle.get("rfb_pass_file")
        if not rfb_pass_file:
            _err("gateway session has no RFB password file recorded.")
            return 3
        try:
            rfb_pass = _read_secret(str(rfb_pass_file))
        except OSError as exc:
            _err(f"could not read the RFB password ({exc}).")
            return 3
        if not rfb_pass:
            _err("the RFB password file is empty; refusing.")
            return 3

        web_dir = gateway_web_dir()
        if not (web_dir / "vnc.html").is_file():
            _err(f"vendored gateway assets missing at {web_dir}.")
            return 3

        # Throwaway workspace: 0700 dir holds the websockify unix socket + the
        # browser profile; deleted on exit.
        workdir = tempfile.mkdtemp(prefix="botainer-gw-")
        cleanup.append(lambda: shutil.rmtree(workdir, ignore_errors=True))
        ws_sock = os.path.join(workdir, "ws.sock")
        profile_dir = os.path.join(workdir, "profile")

        # websockify: unix-listen (browser-unreachable) → the RFB target. No
        # --web (the broker serves the page) and no token (the broker gates).
        wcmd = websockify_launch_argv(websockify_cmd(None, ws_sock, rfb_target))
        ws_proc = subprocess.Popen(wcmd)
        cleanup.append(lambda: ws_proc.terminate())
        # Wait for the bridge socket, but bail the INSTANT websockify dies
        # (don't burn the full timeout on a process that already exited).
        ws_ok = False
        for _ in range(100):                          # ~10s ceiling
            if ws_proc.poll() is not None:
                break                                 # died — stop waiting now
            if os.path.exists(ws_sock):
                ws_ok = True
                break
            time.sleep(0.1)
        if not ws_ok:
            rc = ws_proc.poll()
            if rc is not None:
                _err(f"websockify exited immediately (code {rc}). {_WEBSOCKIFY_HINT}")
            else:
                _err("websockify did not create its bridge socket in time.")
            return 3

        # Broker: the single browser-reachable listener.
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(64)
        token = secrets.token_urlsafe(24)
        broker = _Broker(srv, web_dir, token, ws_sock, strict_csp(srv.getsockname()[1]))
        cleanup.append(broker.stop)
        broker.start()

        url = novnc_url(broker.port, token) + "#password=" + quote(rfb_pass, safe="")

        # Loud, persistent banner. THE #1 footgun (vs legacy `watch`): this
        # command IS the viewer server — it runs on your laptop and the page
        # only loads while this stays running. Stop it (Ctrl-C / close the
        # terminal) and the browser gets "unable to connect". So: show the URL
        # prominently (reopenable in any private window WHILE this runs), and
        # say plainly that it blocks on purpose and how to stop it.
        banner = (
            "\n"
            "╭──────────────────────────────────────────────────────────────╮\n"
            "│  botainer plugin browser gateway is RUNNING — KEEP THIS OPEN │\n"
            "╰──────────────────────────────────────────────────────────────╯\n"
            f"  Viewer URL (open in a PRIVATE/throwaway window):\n"
            f"    {url}\n\n"
            "  This command is the viewer server, running ON YOUR LAPTOP — the\n"
            "  page only loads while it keeps running. If you stop it (Ctrl-C,\n"
            "  or closing this terminal) the browser shows 'unable to connect'.\n"
            "  It stays in the foreground ON PURPOSE. Press Ctrl-C when done.\n"
        )
        print(banner)
        sys.stdout.flush()

        stop_evt = threading.Event()

        def _sig(_signum, _frame):
            stop_evt.set()
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGINT, _sig)
            signal.signal(signal.SIGTERM, _sig)

        if not args.no_browser:
            kind, exe = _find_browser(args.browser)
            if kind is None:
                _err("(no chrome/chromium/firefox auto-detected — open the URL "
                     "above yourself, in a private window.)")
            else:
                bcmd = throwaway_browser_cmd(url, profile_dir, browser=kind, exe=exe)
                try:
                    bproc = subprocess.Popen(bcmd)
                except OSError as exc:
                    # A failed browser launch must NOT crash the gateway — the
                    # server is already up and the URL is printed above; the user
                    # can open it manually. (Belt-and-suspenders: _find_browser
                    # only returns resolved paths, but PATH/bundle races happen.)
                    _err(f"couldn't auto-open {kind} ({exc}). Open the URL above "
                         "yourself, in a private window. The gateway is still up.")
                else:
                    # Terminate the browser BEFORE rmtree(workdir) removes its
                    # profile (review LOW/NIT): registered last → runs first in
                    # the reversed cleanup, so the throwaway profile promise holds
                    # and no browser is left writing a just-deleted dir.
                    cleanup.append(lambda: bproc.terminate())
                    _err(f"opened a throwaway {kind} window (also fine to use the "
                         "URL above). Ctrl-C here tears the gateway down.")

        # Wait until: signal, timeout, or websockify/broker death.
        deadline = (time.monotonic() + args.timeout) if args.timeout else None
        while not stop_evt.is_set():
            if ws_proc.poll() is not None:
                _err("websockify exited; tearing down.")
                break
            if not broker.is_alive():
                break
            if deadline and time.monotonic() >= deadline:
                break
            stop_evt.wait(0.5)
        return 0
    finally:
        _run_cleanup()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
