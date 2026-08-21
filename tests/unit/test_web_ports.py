"""Tests for the web-ports plugin + PortForward + Docker -p rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import composition
from botainer.core.refusal import Refused
from botainer.core.spec import (
    AgentRendering,
    Bind,
    BindMode,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    PortForward,
    Provenance,
    SessionSpec,
)

# ────────── PortForward model ──────────


def test_port_forward_defaults_to_loopback() -> None:
    pf = PortForward(container_port=8888, host_port=8888)
    assert pf.host_bind == "127.0.0.1"
    assert pf.label == ""


def test_port_forward_refuses_out_of_range() -> None:
    with pytest.raises(Exception, match="65535|greater"):
        PortForward(container_port=70000, host_port=70000)
    with pytest.raises(Exception):
        PortForward(container_port=0, host_port=0)


def test_port_forward_is_frozen() -> None:
    pf = PortForward(container_port=8888, host_port=8888)
    with pytest.raises(Exception):
        pf.host_port = 9999  # type: ignore[misc]


# ────────── _resolve_port_forwards ──────────


def _fake_config(plugins_enabled: list[str], plugins: dict | None = None):
    from botainer.core import config as config_module

    return config_module.ProjectConfig(
        agent="claude",
        profile="default",
        runtime="auto",
        image=None,
        plugins_enabled=plugins_enabled,
        plugins=plugins or {},
    )


def test_resolve_port_forwards_no_plugin_no_forwards() -> None:
    cfg = _fake_config([])
    out = composition._resolve_port_forwards(cfg, set())
    assert out == ()


def test_resolve_port_forwards_plugin_enabled_no_ports() -> None:
    cfg = _fake_config(["web-ports"], {"web-ports": {"ports": []}})
    out = composition._resolve_port_forwards(cfg, {"web-ports"})
    assert out == ()


def test_resolve_port_forwards_int_form() -> None:
    cfg = _fake_config(
        ["web-ports"], {"web-ports": {"ports": [8888, 7860]}}
    )
    out = composition._resolve_port_forwards(cfg, {"web-ports"})
    assert len(out) == 2
    assert out[0].container_port == 8888
    assert out[0].host_port == 8888
    assert out[0].host_bind == "127.0.0.1"
    assert out[1].container_port == 7860


def test_resolve_port_forwards_object_form() -> None:
    cfg = _fake_config(
        ["web-ports"],
        {"web-ports": {"ports": [
            {"container": 8501, "host": 9000, "label": "streamlit"},
        ]}},
    )
    out = composition._resolve_port_forwards(cfg, {"web-ports"})
    assert len(out) == 1
    assert out[0].container_port == 8501
    assert out[0].host_port == 9000
    assert out[0].label == "streamlit"


def test_resolve_port_forwards_refuses_zero_zero_zero_zero_bind() -> None:
    cfg = _fake_config(
        ["web-ports"],
        {"web-ports": {"ports": [
            {"container": 8888, "host_bind": "0.0.0.0"},
        ]}},
    )
    with pytest.raises(Refused, match="0.0.0.0"):
        composition._resolve_port_forwards(cfg, {"web-ports"})


def test_resolve_port_forwards_refuses_localhost_name() -> None:
    """Independent-read F-3: `localhost` is name-resolved via /etc/hosts
    and can differ from 127.0.0.1 on misconfigured hosts. Restrict the
    allowlist to literal loopback IPs only."""
    cfg = _fake_config(
        ["web-ports"],
        {"web-ports": {"ports": [
            {"container": 8888, "host_bind": "localhost"},
        ]}},
    )
    with pytest.raises(Refused, match="localhost"):
        composition._resolve_port_forwards(cfg, {"web-ports"})


def test_resolve_port_forwards_refuses_duplicate_host_port() -> None:
    cfg = _fake_config(
        ["web-ports"],
        {"web-ports": {"ports": [
            {"container": 8888, "host": 9000},
            {"container": 8501, "host": 9000},
        ]}},
    )
    with pytest.raises(Refused, match="forwarded twice"):
        composition._resolve_port_forwards(cfg, {"web-ports"})


def test_resolve_port_forwards_refuses_missing_container_key() -> None:
    cfg = _fake_config(
        ["web-ports"], {"web-ports": {"ports": [{"host": 8888}]}}
    )
    with pytest.raises(Refused, match="container"):
        composition._resolve_port_forwards(cfg, {"web-ports"})


def test_resolve_port_forwards_refuses_non_int_non_dict() -> None:
    cfg = _fake_config(
        ["web-ports"], {"web-ports": {"ports": ["not-a-port"]}}
    )
    with pytest.raises(Refused, match="int or object"):
        composition._resolve_port_forwards(cfg, {"web-ports"})


def test_resolve_port_forwards_refuses_non_list_ports() -> None:
    cfg = _fake_config(
        ["web-ports"], {"web-ports": {"ports": "not-a-list"}}
    )
    with pytest.raises(Refused, match="must be a list"):
        composition._resolve_port_forwards(cfg, {"web-ports"})


# ────────── browser viewer noVNC forward (S4) ──────────


@pytest.mark.parametrize("val,expected", [
    (True, True), (False, False), ("true", True), ("on", True),
    ("yes", True), ("1", True), ("0", False), ("off", False), (None, False),
])
def test_browser_viewer_on_parsing(val, expected) -> None:
    plugins = {"browser": {} if val is None else {"viewer": val}}
    cfg = _fake_config(["browser"], plugins)
    assert composition._browser_viewer_on(cfg) is expected


def test_browser_viewer_on_missing_plugin_is_false() -> None:
    assert composition._browser_viewer_on(_fake_config([])) is False


def test_resolve_port_forwards_browser_viewer_docker_adds_novnc() -> None:
    """LEGACY viewer + docker → a loopback noVNC forward on 6080.

    `viewer_mode` is explicit because the DEFAULT is gateway, which publishes
    5900 RFB instead (the test below). This one is specifically about the
    legacy forward, so it must ask for legacy.
    """
    cfg = _fake_config(
        ["browser"], {"browser": {"viewer": True, "viewer_mode": "legacy"}})
    out = composition._resolve_port_forwards(cfg, {"browser"}, "docker")
    assert len(out) == 1
    pf = out[0]
    assert pf.container_port == 6080
    assert pf.host_bind == "127.0.0.1"
    assert 1 <= pf.host_port <= 65535
    assert pf.label == "browser viewer (noVNC)"


def test_resolve_port_forwards_browser_viewer_apptainer_no_forward() -> None:
    """Apptainer refuses port forwards; compose must NOT add the noVNC one on
    HPC (it's reached over a node-local unix socket + ssh -L instead)."""
    cfg = _fake_config(["browser"], {"browser": {"viewer": True}})
    assert composition._resolve_port_forwards(cfg, {"browser"}, "apptainer") == ()


def test_resolve_port_forwards_browser_headless_no_forward() -> None:
    """Default (viewer:false / unset) browser adds no forward — headless."""
    cfg = _fake_config(["browser"], {"browser": {"viewer": False}})
    assert composition._resolve_port_forwards(cfg, {"browser"}, "docker") == ()
    cfg2 = _fake_config(["browser"], {})
    assert composition._resolve_port_forwards(cfg2, {"browser"}, "docker") == ()


def test_resolve_port_forwards_browser_viewer_not_enabled_no_forward() -> None:
    """viewer:true in config but the plugin isn't enabled → no forward."""
    cfg = _fake_config([], {"browser": {"viewer": True}})
    assert composition._resolve_port_forwards(cfg, set(), "docker") == ()


def test_resolve_port_forwards_web_ports_and_browser_viewer_coexist() -> None:
    """web-ports forward + browser viewer forward compose together on docker,
    and the viewer's auto-picked host port never collides with a web port."""
    cfg = _fake_config(
        ["web-ports", "browser"],
        {"web-ports": {"ports": [{"container": 8501, "host": 9000}]},
         "browser": {"viewer": True}},
    )
    out = composition._resolve_port_forwards(
        cfg, {"web-ports", "browser"}, "docker")
    labels = {pf.label for pf in out}
    # No viewer_mode set, so this is the DEFAULT (gateway) forward. The subject
    # of this test is port-collision avoidance, not which mode — so it follows
    # whatever the default is rather than pinning one.
    assert "browser viewer (RFB)" in labels
    viewer = next(pf for pf in out if pf.label == "browser viewer (RFB)")
    web = next(pf for pf in out if pf.container_port == 8501)
    assert viewer.host_port != web.host_port           # no collision


def test_free_loopback_port_dodges_avoid_set() -> None:
    port = composition._free_loopback_port(avoid=set())
    assert 1 <= port <= 65535
    # a port we just asked for is excluded on the next ask
    assert composition._free_loopback_port(avoid={port}) != port


# ────────── browser viewer GATEWAY forward (Track B, B1) ──────────


@pytest.mark.parametrize("val,expected", [
    (None, "gateway"),                       # absent -> the default
    ("legacy", "legacy"), (" LEGACY ", "legacy"),   # only an EXPLICIT legacy
    ("gateway", "gateway"), ("GATEWAY", "gateway"), (" gateway ", "gateway"),
    # A bad value must NOT buy the weaker posture. compose is the quiet copy
    # (the hook raises); quiet must still mean gateway, or a typo silently
    # downgrades you to the mode where the container serves your browser's code.
    ("bogus", "gateway"), (True, "gateway"),
])
def test_browser_viewer_mode_parsing(val, expected) -> None:
    plugins = {"browser": {} if val is None else {"viewer_mode": val}}
    cfg = _fake_config(["browser"], plugins)
    assert composition._browser_viewer_mode(cfg) == expected


def test_resolve_port_forwards_gateway_publishes_rfb_not_novnc() -> None:
    """gateway mode: the container speaks ONLY RFB — the forward is 5900
    (authenticated x11vnc), NOT the in-container noVNC 6080."""
    cfg = _fake_config(
        ["browser"], {"browser": {"viewer": True, "viewer_mode": "gateway"}})
    out = composition._resolve_port_forwards(cfg, {"browser"}, "docker")
    assert len(out) == 1
    pf = out[0]
    assert pf.container_port == 5900
    assert pf.host_bind == "127.0.0.1"
    assert pf.label == "browser viewer (RFB)"
    assert not any(p.container_port == 6080 for p in out)


def test_resolve_port_forwards_gateway_apptainer_still_no_forward() -> None:
    cfg = _fake_config(
        ["browser"], {"browser": {"viewer": True, "viewer_mode": "gateway"}})
    assert composition._resolve_port_forwards(cfg, {"browser"}, "apptainer") == ()


def test_resolve_port_forwards_gateway_without_viewer_no_forward() -> None:
    """viewer_mode alone doesn't arm anything — viewer: true is the grant."""
    cfg = _fake_config(["browser"], {"browser": {"viewer_mode": "gateway"}})
    assert composition._resolve_port_forwards(cfg, {"browser"}, "docker") == ()


# ────────── Docker adapter -p rendering ──────────


def _spec(**kw) -> SessionSpec:
    workspace_bind = Bind(
        source="/host/workspace",
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="workspace",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_WORKSPACE_RW",
    )
    defaults: dict = dict(
        session_id="web-1",
        project_uuid="u",
        project_root="/p",
        state_dir="/s",
        runtime="docker",
        image="img@sha256:" + "0" * 64,
        mount_plan=MountPlan(binds=(workspace_bind,)),
        network=NetworkSpec(mode=NetworkMode.INTERNET),
    )
    defaults.update(kw)
    return SessionSpec(**defaults)


def test_docker_render_emits_p_flags() -> None:
    from botainer.adapters.docker import DockerAdapter

    spec = _spec(port_forwards=(
        PortForward(container_port=8888, host_port=8888),
        PortForward(container_port=7860, host_port=7860, label="gradio"),
    ))
    argv = DockerAdapter().render_argv(spec)
    # Should contain -p flags.
    p_args = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert "127.0.0.1:8888:8888" in p_args
    assert "127.0.0.1:7860:7860" in p_args


def test_docker_render_no_p_when_no_port_forwards() -> None:
    from botainer.adapters.docker import DockerAdapter

    spec = _spec()
    argv = DockerAdapter().render_argv(spec)
    assert "-p" not in argv


def test_docker_refuses_network_none_with_port_forwards() -> None:
    """`--network=none` + -p is a Docker error; we refuse at compose."""
    from botainer.adapters.docker import DockerAdapter

    spec = _spec(
        network=NetworkSpec(mode=NetworkMode.NONE),
        port_forwards=(PortForward(container_port=8888, host_port=8888),),
    )
    with pytest.raises(Refused, match="network.mode=none \\+ inbound port forwards"):
        DockerAdapter().render_argv(spec)


def test_apptainer_refuses_port_forwards() -> None:
    """Apptainer can't forward ports; refuse with hint."""
    from botainer.adapters.apptainer import ApptainerAdapter

    workspace_bind = Bind(
        source="/host/workspace",
        target="/workspace",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="workspace",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_WORKSPACE_RW",
    )
    spec = SessionSpec(
        session_id="ap-1",
        project_uuid="u",
        project_root="/p",
        state_dir="/s",
        runtime="apptainer",
        image="/path/img.sif",
        mount_plan=MountPlan(binds=(workspace_bind,)),
        network=NetworkSpec(mode=NetworkMode.NONE),
        port_forwards=(PortForward(container_port=8888, host_port=8888),),
    )
    with pytest.raises(Refused, match="does not support container port forwarding"):
        ApptainerAdapter().validate(spec)


# ────────── Capability summary surfaces port forwards ──────────


def test_capability_summary_shows_port_forwards() -> None:
    from botainer.inspect import capability_summary

    spec = _spec(port_forwards=(
        PortForward(container_port=8888, host_port=8888, label="jupyter"),
    ))
    out = capability_summary.render_multiline(spec)
    assert "Web ports forwarded" in out
    assert "http://127.0.0.1:8888" in out
    assert "jupyter" in out
    assert "host shell access" in out


def test_capability_summary_json_includes_port_forwards() -> None:
    import json

    from botainer.inspect import capability_summary

    spec = _spec(port_forwards=(
        PortForward(container_port=8888, host_port=8888, label="jupyter"),
    ))
    parsed = json.loads(capability_summary.render_json(spec))
    assert len(parsed["port_forwards"]) == 1
    assert parsed["port_forwards"][0]["container_port"] == 8888
    assert parsed["port_forwards"][0]["label"] == "jupyter"


# ────────── Plugin manifest loads ──────────


def test_web_ports_plugin_loads() -> None:
    from botainer.plugins.manifest import load_manifest

    repo_plugins = Path(__file__).resolve().parents[2] / "plugins"
    m = load_manifest(repo_plugins / "web-ports")
    assert m.name == "web-ports"
    assert m.tier == "first-party"
    assert "docker" in m.runtimes
    assert "apptainer" not in m.runtimes  # web-ports refuses apptainer
