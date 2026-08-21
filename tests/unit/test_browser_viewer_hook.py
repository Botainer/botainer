"""Unit tests for the re-architected in-agent browser VIEWER hook
(plugins/browser/hooks/start_viewer.py). The viewer now runs INSIDE the agent
container — no helper container, no CDP. The hook: mints a token (0600, bound RO),
signals the entrypoint (env), and rewrites the mcp_server to HEADED (drop
--headless). Imports the plugin module directly (outside the botainer package)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


sv = _load("plugins/browser/hooks/start_viewer.py", "browser_start_viewer_v2")


# ───────────────────────── viewer_enabled ─────────────────────────
@pytest.mark.parametrize("cfg,expected", [
    ({}, False), ({"viewer": False}, False), ({"viewer": True}, True),
    ({"viewer": "true"}, True), ({"viewer": "yes"}, True),
    ({"viewer": "0"}, False), ({"viewer": "off"}, False),
])
def test_viewer_enabled(cfg, expected):
    assert sv.viewer_enabled(cfg) is expected


# ─────────────────────── rewrite_mcp_for_headed ───────────────────────
def _mcp(args):
    return json.dumps({"mcpServers": {"browser": {
        "command": "playwright-mcp", "args": args, "env": {}}}})


def test_headed_rewrite_drops_only_headless_keeps_sandbox_and_exec_path():
    src = _mcp(["--headless", "--no-sandbox", "--isolated",
               "--executable-path", "/usr/bin/chromium"])
    out = json.loads(sv.rewrite_mcp_for_headed(src))
    args = out["mcpServers"]["browser"]["args"]
    assert "--headless" not in args                    # headed now
    assert "--no-sandbox" in args                      # cage still needs it
    assert "--executable-path" in args                 # keep the browser path
    assert args[args.index("--executable-path") + 1] == "/usr/bin/chromium"
    assert "--isolated" in args
    assert "--viewport-size" in args                   # match the virtual screen


def test_headed_rewrite_noop_without_browser_server():
    src = json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}})
    assert sv.rewrite_mcp_for_headed(src) == src


# ──────────────────────────── main() ────────────────────────────
def _setup(tmp_path, monkeypatch, *, viewer, runtime,
           storage_state=None, create_auth=True, viewer_mode=None):
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    vv = "true" if viewer else "false"
    cfg = f"plugins:\n  browser:\n    viewer: {vv}\n"
    if viewer_mode is not None:
        cfg += f"    viewer_mode: {viewer_mode}\n"
    if storage_state is not None:
        cfg += f"    storage_state: {storage_state}\n"
        if create_auth:
            authp = proj / storage_state
            authp.parent.mkdir(parents=True, exist_ok=True)
            authp.write_text('{"cookies": [], "origins": []}')
    (proj / ".botainer" / "config.yaml").write_text(cfg)
    sid = "abc1234567890def"
    state = tmp_path / "state"
    sess = state / "sessions" / sid
    sess.mkdir(parents=True)
    # the mcp-servers.json the launcher composed (headless by default)
    (sess / "mcp-servers.json").write_text(_mcp(
        ["--headless", "--no-sandbox", "--isolated",
         "--executable-path", "/usr/bin/chromium"]))
    rec = {"schema_version": 1, "session_id": sid, "project_root": str(proj),
           "state_dir": str(state), "runtime": runtime}
    recp = sess / "spec.json"
    recp.write_text(json.dumps(rec))
    monkeypatch.setenv("BOTAINER_SESSION_RECORD_PATH", str(recp))
    monkeypatch.setattr(sv, "_SUN_PATH_MAX", 100000)   # long pytest tmp path
    return proj, sess, recp


def test_main_headless_default_empties_contribution(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, viewer=False, runtime="docker")
    assert sv.main() == 0
    contribution = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert contribution["env"] == {} and contribution["binds"] == []


def test_main_docker_viewer_arms_env_token_and_headed_mcp(tmp_path, monkeypatch, capsys):
    """LEGACY docker viewer: token file + in-container noVNC.

    `viewer_mode` is explicit because the default is gateway, which
    authenticates with an RFB password rather than this token file. The
    token-file mechanism is legacy's, so the test asks for legacy.
    """
    _proj, sess, _recp = _setup(tmp_path, monkeypatch, viewer=True, runtime="docker",
                                viewer_mode="legacy")
    assert sv.main() == 0
    contribution = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    env = contribution["env"]
    assert env["BOTAINER_BROWSER_VIEWER"] == "1"        # entrypoint starts stack
    assert env["BOTAINER_VIEWER_MODE"] == "tcp"         # docker transport
    assert env["BOTAINER_VIEWER_TOKEN_FILE"] == "/run/viewer-token"
    # token bound RO, never via env
    tok_bind = next(b for b in contribution["binds"] if b["target"] == "/run/viewer-token")
    assert tok_bind["mode"] == "ro"
    assert not any(k for k in env if "TOKEN" in k and k != "BOTAINER_VIEWER_TOKEN_FILE")
    # the mcp-servers.json was rewritten to headed
    args = json.loads((sess / "mcp-servers.json").read_text())["mcpServers"]["browser"]["args"]
    assert "--headless" not in args and "--no-sandbox" in args


def test_main_apptainer_viewer_uses_unix_socket_dir(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, viewer=True, runtime="apptainer")
    assert sv.main() == 0
    contribution = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert contribution["env"]["BOTAINER_VIEWER_MODE"] == "unix"   # NO TCP on HPC
    assert contribution["env"]["BOTAINER_VIEWER_SOCKET_DIR"] == "/run/viewer"
    assert any(b["target"] == "/run/viewer" for b in contribution["binds"])


def test_main_refuses_when_mcp_servers_missing(tmp_path, monkeypatch, capsys):
    _proj, sess, _ = _setup(tmp_path, monkeypatch, viewer=True, runtime="docker")
    (sess / "mcp-servers.json").unlink()               # browser mcp not enabled
    assert sv.main() == 2
    assert "mcp-servers.json not found" in capsys.readouterr().err


# ───────────────────── gateway mode (Track B, B1) ─────────────────────
# The default is GATEWAY. Absent config selects it; only an explicit `legacy`
# selects legacy. The hook is the loud validator, so anything else raises
# (test_viewer_mode_refuses_unknown below) rather than resolving.
@pytest.mark.parametrize("cfg,expected", [
    ({}, "gateway"), ({"viewer_mode": "legacy"}, "legacy"),
    ({"viewer_mode": " LEGACY "}, "legacy"),
    ({"viewer_mode": "gateway"}, "gateway"),
    ({"viewer_mode": " GATEWAY "}, "gateway"),
])
def test_viewer_mode_parsing(cfg, expected):
    assert sv.viewer_mode(cfg) == expected


@pytest.mark.parametrize("bad", ["bogus", True, 1, ["gateway"]])
def test_viewer_mode_refuses_unknown(bad):
    with pytest.raises(ValueError, match="viewer_mode"):
        sv.viewer_mode({"viewer_mode": bad})


def test_main_docker_gateway_arms_rfb_pass_not_token(tmp_path, monkeypatch, capsys):
    _proj, sess, recp = _setup(tmp_path, monkeypatch, viewer=True,
                               runtime="docker", viewer_mode="gateway")
    assert sv.main() == 0
    contribution = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    env = contribution["env"]
    assert env["BOTAINER_BROWSER_VIEWER"] == "1"
    assert env["BOTAINER_VIEWER_GATEWAY"] == "1"       # RFB-only stack
    assert env["BOTAINER_VIEWER_MODE"] == "tcp"
    assert env["BOTAINER_VIEWER_RFB_PASS_FILE"] == "/run/viewer-rfb-pass"
    # RFB password bound RO; the secret itself NEVER on the env
    pass_bind = next(b for b in contribution["binds"]
                     if b["target"] == "/run/viewer-rfb-pass")
    assert pass_bind["mode"] == "ro"
    pass_content = Path(pass_bind["source"]).read_text().strip()
    assert len(pass_content) == 8                      # VNC auth uses 8 bytes
    assert pass_content not in json.dumps(env)
    # NO legacy websockify token in gateway mode (the laptop mints its own)
    assert "BOTAINER_VIEWER_TOKEN_FILE" not in env
    assert not any(b["target"] == "/run/viewer-token" for b in contribution["binds"])
    # handle records gateway coords for `botainer plugin browser gateway`
    rec = json.loads(recp.read_text())
    handle = rec["runtime_handle"]["browser_viewer"]
    assert handle["mode"] == "gateway"
    assert handle["container_rfb_port"] == 5900
    assert handle["rfb_pass_file"] == pass_bind["source"]
    # still headed: the human watches the same Chromium
    args = json.loads((sess / "mcp-servers.json").read_text())["mcpServers"]["browser"]["args"]
    assert "--headless" not in args


def test_main_apptainer_gateway_records_rfb_socket(tmp_path, monkeypatch, capsys):
    _proj, _sess, recp = _setup(tmp_path, monkeypatch, viewer=True,
                                runtime="apptainer", viewer_mode="gateway")
    assert sv.main() == 0
    contribution = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert contribution["env"]["BOTAINER_VIEWER_MODE"] == "unix"   # no TCP on HPC
    assert contribution["env"]["BOTAINER_VIEWER_GATEWAY"] == "1"
    assert any(b["target"] == "/run/viewer" for b in contribution["binds"])
    handle = json.loads(recp.read_text())["runtime_handle"]["browser_viewer"]
    assert handle["mode"] == "gateway"
    assert handle["rfb_sock"].endswith("/viewer/vnc.sock")     # ssh -L target (host side)
    assert handle["rfb_sock_container"] == "/run/viewer/vnc.sock"
    assert "novnc_sock" not in handle                  # no in-container noVNC


def test_main_refuses_unknown_viewer_mode(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, viewer=True, runtime="docker",
           viewer_mode="sideways")
    assert sv.main() == 2
    assert "viewer_mode" in capsys.readouterr().err


# ───────────────── credential handoff (storage-state, Track A) ─────────────────
def test_resolve_storage_state():
    import pathlib
    root = pathlib.Path("/proj")
    assert sv.resolve_storage_state({}, root) is None            # unset
    assert sv.resolve_storage_state({"storage_state": ""}, root) is None


def test_resolve_storage_state_relative_and_missing(tmp_path):
    (tmp_path / ".botainer").mkdir()
    auth = tmp_path / ".botainer" / "browser-auth.json"
    auth.write_text("{}")
    got = sv.resolve_storage_state(
        {"storage_state": ".botainer/browser-auth.json"}, tmp_path)
    assert got == auth.resolve()                                 # relative → project
    import pytest
    with pytest.raises(FileNotFoundError):                       # set but absent
        sv.resolve_storage_state({"storage_state": "nope.json"}, tmp_path)


def test_rewrite_mcp_add_storage_state_idempotent():
    src = _mcp(["--headless", "--no-sandbox", "--isolated"])
    out = sv.rewrite_mcp_add_storage_state(src, "/run/browser-auth.json")
    args = json.loads(out)["mcpServers"]["browser"]["args"]
    assert args[args.index("--storage-state") + 1] == "/run/browser-auth.json"
    # idempotent — a second pass doesn't duplicate the flag
    again = sv.rewrite_mcp_add_storage_state(out, "/run/browser-auth.json")
    assert json.loads(again)["mcpServers"]["browser"]["args"].count("--storage-state") == 1


def test_rewrite_mcp_add_storage_state_noop_without_browser():
    src = json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}})
    assert sv.rewrite_mcp_add_storage_state(src, "/run/browser-auth.json") == src


def test_main_handoff_only_binds_ro_and_adds_flag(tmp_path, monkeypatch, capsys):
    """viewer:false + storage_state set → RO bind + --storage-state, no viewer env."""
    _proj, sess, _ = _setup(tmp_path, monkeypatch, viewer=False, runtime="docker",
                            storage_state=".botainer/browser-auth.json")
    assert sv.main() == 0
    contribution = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "BOTAINER_BROWSER_VIEWER" not in contribution["env"]   # no viewer
    b = next(x for x in contribution["binds"] if x["target"] == "/run/browser-auth.json")
    assert b["mode"] == "ro"
    args = json.loads((sess / "mcp-servers.json").read_text())["mcpServers"]["browser"]["args"]
    assert args[args.index("--storage-state") + 1] == "/run/browser-auth.json"
    assert "--headless" in args                                   # still headless (no viewer)


def test_main_handoff_plus_viewer_does_both(tmp_path, monkeypatch, capsys):
    _proj, sess, _ = _setup(tmp_path, monkeypatch, viewer=True, runtime="docker",
                            storage_state=".botainer/browser-auth.json")
    assert sv.main() == 0
    contribution = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert contribution["env"]["BOTAINER_BROWSER_VIEWER"] == "1"  # viewer armed
    assert any(x["target"] == "/run/browser-auth.json" for x in contribution["binds"])
    args = json.loads((sess / "mcp-servers.json").read_text())["mcpServers"]["browser"]["args"]
    assert "--storage-state" in args and "--headless" not in args  # handoff + headed


def test_main_refuses_when_storage_state_file_missing(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, viewer=False, runtime="docker",
           storage_state="nope.json", create_auth=False)
    assert sv.main() == 2
    assert "does not exist" in capsys.readouterr().err


# ── HIGH-1 symlink-escape guards (Fable-5) ──
def test_resolve_storage_state_refuses_symlink(tmp_path):
    import os
    real = tmp_path / "real-auth.json"; real.write_text("{}")
    os.symlink(real, tmp_path / "link.json")
    with pytest.raises(ValueError):                     # final component is a symlink
        sv.resolve_storage_state({"storage_state": "link.json"}, tmp_path)


def test_resolve_storage_state_refuses_dir_symlink_escape(tmp_path):
    import os
    proj = tmp_path / "proj"; proj.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "auth.json").write_text('{"cookie": "secret"}')
    os.symlink(outside, proj / "d")                     # intermediate dir symlink OUT
    with pytest.raises(ValueError):                     # resolves outside the project
        sv.resolve_storage_state({"storage_state": "d/auth.json"}, proj)


def test_resolve_storage_state_rejects_non_string(tmp_path):
    with pytest.raises(ValueError):                     # YAML `no`/`off` → bool
        sv.resolve_storage_state({"storage_state": False}, tmp_path)


def test_handoff_binds_a_copy_not_the_users_file(tmp_path, monkeypatch, capsys):
    """Fable-5 HIGH-1: the bind SOURCE is the host-private session-dir copy, not the
    user's (agent-writable) file — closes the TOCTOU."""
    _proj, sess, _ = _setup(tmp_path, monkeypatch, viewer=False, runtime="docker",
                            storage_state=".botainer/browser-auth.json")
    assert sv.main() == 0
    contribution = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    b = next(x for x in contribution["binds"] if x["target"] == "/run/browser-auth.json")
    assert b["source"] == str(sess / "browser-auth.json")   # the copy, in session dir
    assert (sess / "browser-auth.json").exists()


def test_browser_manifest_declares_all_contributed_bind_prefixes():
    """Fable-5 HIGH-2 pin: every target the hook binds must be in the envelope, or
    the pre_session bind-envelope refuses it."""
    from pathlib import Path

    from botainer.plugins.manifest import load_manifest
    m = load_manifest(Path(__file__).resolve().parents[2] / "plugins/browser")
    mtp = set(m.contributes.mount_target_prefixes)
    assert {"/run/viewer-token", "/run/viewer", "/run/browser-auth.json"} <= mtp
