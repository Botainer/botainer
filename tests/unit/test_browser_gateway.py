"""`botainer plugin browser gateway` — the pure, design-stable builders for the
trusted laptop-side viewer gateway (Track B). The runtime orchestration (main()) is
finished/iterated in a host session and is not tested here."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GW = Path(__file__).resolve().parents[2] / "plugins/browser/commands/gateway.py"
_spec = importlib.util.spec_from_file_location("browser_gateway", _GW)
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)  # type: ignore[union-attr]


def test_strict_csp_locks_it_down():
    csp = gw.strict_csp(6080)
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp
    assert "connect-src ws://127.0.0.1:6080" in csp   # explicit ws origin
    assert "img-src 'self' data:" in csp              # upstream noVNC (data:, NOT blob:)
    assert "blob:" not in csp
    assert "frame-ancestors 'none'" in csp


def test_novnc_url_token_on_ws_path_and_loopback():
    url = gw.novnc_url(6080, "TOK123")
    assert url.startswith("http://127.0.0.1:6080/vnc.html")
    assert "path=websockify%3Ftoken%3DTOK123" in url  # token on the ws path
    assert "localhost" not in url and "::1" not in url


def test_ssh_forward_cmd_with_and_without_jump():
    assert gw.ssh_forward_cmd("node042", 5901, "/run/x/vnc.sock") == \
        ["ssh", "-L", "5901:/run/x/vnc.sock", "node042"]
    assert gw.ssh_forward_cmd("node042", 5901, "/run/x/vnc.sock", jump="login") == \
        ["ssh", "-J", "login", "-L", "5901:/run/x/vnc.sock", "node042"]


def test_websockify_cmd_unix_listen_preferred():
    cmd = gw.websockify_cmd("/novnc", "/run/gw.sock", "/run/vnc.sock", token_file="/t")
    assert "--unix-listen=/run/gw.sock" in cmd and "--unix-target=/run/vnc.sock" in cmd
    assert cmd[cmd.index("--token-source") + 1] == "/t"


def test_websockify_cmd_tcp_listen_and_target():
    cmd = gw.websockify_cmd("/novnc", "127.0.0.1:6080", "127.0.0.1:5900")
    assert "127.0.0.1:6080" in cmd and "127.0.0.1:5900" in cmd
    assert "--token-plugin" not in cmd                # no token file → no TokenFile


def test_throwaway_browser_cmd_isolated_profile():
    cmd = gw.throwaway_browser_cmd("http://127.0.0.1:6080/vnc.html", "/tmp/p", "chrome")
    assert "--user-data-dir=/tmp/p" in cmd and "--disable-extensions" in cmd
    assert cmd[-1] == "http://127.0.0.1:6080/vnc.html"
    ff = gw.throwaway_browser_cmd("http://x", "/tmp/p", "firefox")
    assert "-profile" in ff and "/tmp/p" in ff
    with pytest.raises(ValueError):
        gw.throwaway_browser_cmd("http://x", "/tmp/p", "safari")
