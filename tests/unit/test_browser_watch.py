"""`botainer browser watch` — the pure instruction renderer (the rest is the agent
entrypoint that starts the in-container viewer). Imports the plugin command module
directly. Re-architected: token rides the ws PATH; docker host port
comes from spec.port_forwards; URL is 127.0.0.1 not localhost."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_WATCH = Path(__file__).resolve().parents[2] / "plugins/browser/commands/watch.py"
_spec = importlib.util.spec_from_file_location("browser_watch", _WATCH)
watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch)  # type: ignore[union-attr]


def test_local_instructions_have_no_ssh_and_token_on_ws_path() -> None:
    out = watch.render_connect_instructions(
        is_local=True, node="", login=None,
        socket_path="", token="TOK123", local_port=6080)
    assert "ssh" not in out.lower()
    assert "http://127.0.0.1:6080/vnc.html" in out
    assert "localhost" not in out                       # never localhost
    # token rides the ws PATH (websockify TokenFile requires it), not a bare query
    assert "path=websockify%3Ftoken%3DTOK123" in out
    assert "BLANK until the agent opens its first page" in out
    assert "REQUIRED" in out                             # docker: token ENFORCED
    # the trust-inversion security warning is ALWAYS present (in the right place)
    assert "SECURITY" in out and "UNTRUSTED sandbox" in out
    assert "THROWAWAY / private browser" in out


def test_local_liveness_warns_when_not_listening() -> None:
    out = watch.render_connect_instructions(
        is_local=True, node="", login=None, socket_path="", token="T",
        local_port=6080, listening=False)
    assert "nothing is accepting connections" in out
    assert "UNTRUSTED sandbox" in out          # warning still shown on this branch


def test_local_no_liveness_warning_when_listening() -> None:
    out = watch.render_connect_instructions(
        is_local=True, node="", login=None, socket_path="", token="T",
        local_port=6080, listening=True)
    assert "nothing is accepting connections" not in out


def test_remote_instructions_forward_the_0700_socket_over_ssh() -> None:
    out = watch.render_connect_instructions(
        is_local=False, node="node042", login="grace-login",
        socket_path="/run/viewer/novnc.sock", token="TOK123", local_port=6080)
    # jump host + local-forward of the socket (no routable port on the node)
    assert "ssh -J grace-login node042 -L 6080:/run/viewer/novnc.sock" in out
    assert "path=websockify%3Ftoken%3DTOK123" in out
    assert "http://127.0.0.1:6080/vnc.html" in out
    # HPC caveat (M3): token is ADVISORY, not enforced; transport is provisional
    assert "ADVISORY" in out
    assert "PROVISIONAL" in out
    assert "REQUIRED by websockify" not in out
    assert "UNTRUSTED sandbox" in out          # warning shown on the remote branch too


def test_read_token_rejects_escape_injection(tmp_path) -> None:
    """Fable-5 defense-in-depth: a token file with terminal escapes must be
    refused (the token is embedded into OSC-8/ANSI output)."""
    good = tmp_path / "good"
    good.write_text("Abc_123-xyz\n")
    assert watch._read_token(str(good)) == "Abc_123-xyz"
    bad = tmp_path / "bad"
    bad.write_text("has\x1b]8;;evil\x1b\\stuff")
    with pytest.raises(ValueError):
        watch._read_token(str(bad))


def test_remote_without_login_omits_jump() -> None:
    out = watch.render_connect_instructions(
        is_local=False, node="node042", login=None,
        socket_path="/run/viewer/novnc.sock", token="T", local_port=7000)
    assert "ssh node042 -L 7000:/run/viewer/novnc.sock" in out
    assert "-J" not in out


# ── docker host port comes from spec.port_forwards ──
def test_docker_host_novnc_port_from_labelled_forward() -> None:
    spec = {"port_forwards": [
        {"container_port": 8501, "host_port": 9000, "label": "streamlit"},
        {"container_port": 6080, "host_port": 54321,
         "label": "browser viewer (noVNC)"},
    ]}
    assert watch.docker_host_novnc_port(spec) == 54321


def test_docker_host_novnc_port_falls_back_to_container_6080() -> None:
    spec = {"port_forwards": [
        {"container_port": 6080, "host_port": 40000, "label": ""},
    ]}
    assert watch.docker_host_novnc_port(spec) == 40000


def test_docker_host_novnc_port_none_when_absent() -> None:
    assert watch.docker_host_novnc_port({"port_forwards": []}) is None
    assert watch.docker_host_novnc_port({}) is None


def test_render_kwargs_tcp_uses_spec_host_port() -> None:
    kw = watch.render_kwargs_from_handle(
        {"transport": "tcp", "container_novnc_port": 6080},
        login=None, host_novnc_port=54321)
    assert kw["is_local"] is True and kw["local_port"] == 54321


def test_render_kwargs_tcp_raises_without_host_port() -> None:
    with pytest.raises(KeyError):
        watch.render_kwargs_from_handle(
            {"transport": "tcp"}, login=None, host_novnc_port=None)


def test_main_refuses_when_no_viewer_running(monkeypatch, capsys) -> None:
    for var in ("BOTAINER_BROWSER_TOKEN_FILE", "BOTAINER_BROWSER_VIEWER_SOCK",
                "BOTAINER_BROWSER_LOCAL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("BOTAINER_STATE_DIR", raising=False)
    assert watch.main([]) == 3
    assert "no running viewer" in capsys.readouterr().err


def test_main_gateway_mode_redirects_to_gateway_command(monkeypatch, capsys) -> None:
    """A gateway-mode session has no in-container noVNC to render a URL for —
    watch must point at `botainer plugin browser gateway`, not fail."""
    for var in ("BOTAINER_BROWSER_TOKEN_FILE", "BOTAINER_BROWSER_VIEWER_SOCK",
                "BOTAINER_BROWSER_LOCAL"):
        monkeypatch.delenv(var, raising=False)

    class _FakeRecord:
        extra_runtime_handle = {"browser_viewer": {"mode": "gateway",
                                                   "runtime": "docker",
                                                   "transport": "tcp"}}
        spec = {}

    monkeypatch.setattr(watch, "_load_running_viewer_record", lambda: _FakeRecord())
    assert watch.main([]) == 0
    out = capsys.readouterr().out
    assert "botainer plugin browser gateway" in out
    assert "vnc.html" not in out                       # no legacy URL rendered


def test_main_reads_token_file_and_prints(monkeypatch, capsys, tmp_path) -> None:
    tok = tmp_path / "tok"
    tok.write_text("SECRET-TOKEN\n")
    monkeypatch.setenv("BOTAINER_BROWSER_TOKEN_FILE", str(tok))
    monkeypatch.setenv("BOTAINER_BROWSER_VIEWER_SOCK", "/run/viewer/novnc.sock")
    monkeypatch.setenv("BOTAINER_BROWSER_NODE", "node042")
    monkeypatch.setenv("BOTAINER_BROWSER_LOGIN", "grace-login")
    monkeypatch.delenv("BOTAINER_BROWSER_LOCAL", raising=False)
    assert watch.main([]) == 0
    out = capsys.readouterr().out
    assert "path=websockify%3Ftoken%3DSECRET-TOKEN" in out
    assert "ssh -J grace-login node042 -L 6080:/run/viewer/novnc.sock" in out
