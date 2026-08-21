"""End-to-end SessionSpec composition with the mock adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import composition, identity
from botainer.core import config as config_module


def _prepare_project(tmp_path: Path) -> Path:
    from tests.conftest import append_image_to_config
    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    append_image_to_config(proj)
    return proj


def test_compose_yields_valid_session_spec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert spec.project_root == str(proj.resolve())
    assert spec.runtime == "mock"
    # Three core binds at minimum.
    targets = {b.target for b in spec.mount_plan.binds}
    assert {"/workspace", "/workspace/.botainer", "/workspace/.botainer/AGENT_ACCESS.txt"}.issubset(
        targets
    )
    # AGENT_ACCESS.txt was written to the session scratch.
    aas_bind = next(b for b in spec.mount_plan.binds if b.target.endswith("AGENT_ACCESS.txt"))
    assert Path(aas_bind.source).read_text().startswith("# AGENT_ACCESS")


def test_compose_delivers_governed_mcp_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plugin contributing an MCP server → the launcher writes mcp-servers.json,
    binds it RO at /workspace/.botainer/, and launches Claude with
    `--mcp-config <path> --strict-mcp-config` (browser feature wiring).
    render_mcp_servers (unit-tested separately) is stubbed here so the test targets
    the composition DELIVERY, not the collection."""
    import json

    import botainer.plugins.mcp as _mcp
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    monkeypatch.setattr(_mcp, "render_mcp_servers", lambda mans: {
        "mcpServers": {"browser": {
            "command": "npx", "args": ["-y", "@playwright/mcp"], "env": {}}}})
    spec = composition.compose_session(proj, runtime_choice="mock",
                                       identity_accept=False)
    # 1. the file is written + bound RO at the namespaced path
    b = next(b for b in spec.mount_plan.binds
             if b.target == "/workspace/.botainer/mcp-servers.json")
    data = json.loads(Path(b.source).read_text())
    assert data["mcpServers"]["browser"]["command"] == "npx"
    # 2. Claude is launched with the governed MCP config, strictly
    inner = spec.entrypoint_wraps[-1]
    assert "--mcp-config" in inner
    assert "/workspace/.botainer/mcp-servers.json" in inner
    assert "--strict-mcp-config" in inner


def test_compose_no_mcp_config_when_none_contributed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default project (no plugin contributes an MCP server) → no mcp-servers.json
    bind and no --mcp-config flag (the feature is inert until a plugin opts in)."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(proj, runtime_choice="mock",
                                       identity_accept=False)
    assert not any(b.target.endswith("mcp-servers.json")
                   for b in spec.mount_plan.binds)
    assert all("--mcp-config" not in w for w in spec.entrypoint_wraps)


def test_compose_does_not_perform_runtime_plugin_trust_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AUDIT (H9): runtime plugin-trust hashing is intentionally NOT
    performed (the launcher-written lock is the wrong integrity boundary; the
    correct one is install-time wheel-signature verification, v0.2). A prior
    Task #100 block re-added a DEAD, contradictory runtime verify_plugin() call
    (its tier filter was disjoint from verify_plugin's returns, so it never
    warned anyway). This pins the deliberate contract — compose yields NO
    plugin_trust_warnings — so the dead code can't silently creep back."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert spec.plugin_trust_warnings == ()


def test_compose_with_env_denylist_violation_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    cfg_path = proj / ".botainer" / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + "\nenv:\n  LD_PRELOAD: /tmp/evil.so\n")
    with pytest.raises(composition.Refused) as exc:
        composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert "env-var-denied" in str(exc.value)


def test_fresh_install_compose_does_not_refuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Re-audit #2 (CRITICAL onboarding) — END-TO-END guard. The state_dir unit
    test asserts the default-ceiling INVARIANT by proxy; this asserts the EFFECT:
    a brand-new install (write_default_policy on disk + init + compose) must
    compose WITHOUT a Refused. This is the only test that exercises the on-disk
    write_default_policy → load_user_policy → intersect → ceiling-check path that
    the bug lived in (the other compose tests rode the in-memory NetworkPolicy
    class default and could never have caught the original `none` regression).
    """
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    # setup: write the default USER policy to disk (the thing the bug was in).
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    state_dir.write_default_policy(paths.root, allow_tiers=["first-party"], force=True)
    # init + compose, exactly as a first run would.
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    # Must NOT raise Refused (the original bug: internet > ceiling none).
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert spec.network.mode.value == "internet"  # the documented frictionless default


def test_compose_with_endpoint_ip_allowlist_without_endpoints_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    cfg_path = proj / ".botainer" / "config.yaml"
    # Default config template writes 'mode: internet'; switch to the
    # mode that requires endpoints. Match either 'mode: internet' or
    # legacy 'mode: none' for compatibility with older templates.
    txt = cfg_path.read_text()
    for old in ("mode: internet", "mode: none"):
        if old in txt:
            txt = txt.replace(old, "mode: endpoint-ip-allowlist", 1)
            break
    cfg_path.write_text(txt)
    with pytest.raises(composition.Refused) as exc:
        composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert "api-only-requires-endpoints" in str(exc.value) or "requires endpoints" in str(exc.value)


def test_compose_refuses_plugin_not_supporting_session_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AUDIT (MEDIUM, HPC parity): a plugin's declared `runtimes`
    must be enforced — previously display-only, so a docker-only plugin
    (web-ports declares runtimes:[docker]) silently composed under apptainer.
    Enabling web-ports under apptainer must now refuse at compose."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    # A real .sif so apptainer image resolution passes and compose reaches the
    # plugin-runtimes check (otherwise it refuses earlier for a missing image).
    sif = tmp_path / "img.sif"
    sif.write_bytes(b"fake sif")
    cfg_path = proj / ".botainer" / "config.yaml"
    txt = cfg_path.read_text()
    # Enable web-ports (docker-only), pin an apptainer image, run under apptainer.
    txt = txt.replace("  - git", "  - git\n  - web-ports", 1)
    txt += f"\nimage: {sif}\n"
    cfg_path.write_text(txt)
    with pytest.raises(composition.Refused) as exc:
        composition.compose_session(proj, runtime_choice="apptainer", identity_accept=False)
    assert exc.value.category.value == "unsupported-runtime-feature", str(exc.value)
    assert "web-ports" in str(exc.value) and "does not support" in str(exc.value)


def test_compose_refuses_network_mode_above_site_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AUDIT (H4): the site-policy network ceiling
    (effective_policy.network.default_mode) was computed but NEVER enforced —
    a git-shareable config requesting `internet` was accepted even when the
    admin ceiling was `none` (fail-open egress). Here the admin policy lowers
    the ceiling to `none`; the default config (requests `internet`) must be
    refused with capability-denied-by-policy."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    # Admin deploys a restrictive ceiling (as /etc/botainer/policy.yaml would).
    from botainer.core import policy as policy_module
    from botainer.core.policy import NetworkPolicy, SitePolicy
    monkeypatch.setattr(
        policy_module, "load_site_policy",
        lambda: SitePolicy(network=NetworkPolicy(default_mode="none")),
    )
    with pytest.raises(composition.Refused) as exc:
        composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert "capability-denied-by-policy" in str(exc.value)
    assert "ceiling" in str(exc.value)


def test_compose_allows_network_mode_at_or_below_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The complement of the H4 enforcement: a project mode AT or BELOW the
    ceiling composes fine. Admin ceiling `endpoint-ip-allowlist`; config asks
    for the most-restrictive `none` (≤ ceiling) → accepted (no false-reject)."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    cfg_path = proj / ".botainer" / "config.yaml"
    txt = cfg_path.read_text()
    for old in ("mode: internet", "mode: none", "mode: endpoint-ip-allowlist"):
        if old in txt:
            txt = txt.replace(old, "mode: none", 1)
            break
    cfg_path.write_text(txt)
    from botainer.core import policy as policy_module
    from botainer.core.policy import NetworkPolicy, SitePolicy
    monkeypatch.setattr(
        policy_module, "load_site_policy",
        lambda: SitePolicy(network=NetworkPolicy(default_mode="endpoint-ip-allowlist")),
    )
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert spec.network.mode.value == "none"


def test_compose_with_mount_extra_in_allowlist_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    cfg_path = proj / ".botainer" / "config.yaml"
    cfg_path.write_text(
        cfg_path.read_text()
        + "\n"
        + "mounts:\n"
        + "  extra:\n"
        + "    - source: /host/data\n"
        + "      target: /data\n"
        + "      mode: ro\n"
        + "      reason: dataset for analysis\n"
    )
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert any(b.target == "/data" for b in spec.mount_plan.binds)


def test_compose_with_mount_extra_off_allowlist_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    cfg_path = proj / ".botainer" / "config.yaml"
    cfg_path.write_text(
        cfg_path.read_text()
        + "\n"
        + "mounts:\n"
        + "  extra:\n"
        + "    - source: /host/x\n"
        + "      target: /elsewhere\n"
        + "      mode: ro\n"
        + "      reason: oops\n"
    )
    with pytest.raises(composition.Refused) as exc:
        composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert "off-allowlist" in str(exc.value) or "denied" in str(exc.value)


def test_mock_adapter_can_render_and_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    argv = composition.render_argv(spec)
    assert argv[0] == "mock-runtime"
    handle = composition.launch(spec)
    assert handle.runtime == "mock"
    rc = composition.attach(handle)
    assert rc == 0


def test_compose_refuses_plugin_declaring_contributes_sidecars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tasks #95/#291: spec.sidecars launcher is not implemented at v0.1.
    A plugin manifest declaring `contributes.sidecars: [...]` must be
    REFUSED at compose time so the plugin can't silently never-launch.

    The host_helper pattern (pre_session hook spawns the helper subprocess)
    is the supported v0.1 mechanism; contributes.sidecars is reserved for
    a v0.2 launcher.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    # Install a synthetic plugin into the user state plugin dir that
    # declares contributes.sidecars (something composition will see).
    plugins_dir = tmp_path / "s" / "plugins" / "sidecar-claimer"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "botainer-plugin.yaml").write_text(
        "apiVersion: botainer-plugin-v1\n"
        "name: sidecar-claimer\n"
        "version: 0.1.0\n"
        "kind: agent\n"
        "trust_required: hooked\n"
        # tier: first-party in the MANIFEST is only a claim. Since audit S2
        # a self-declared tier no longer clears the compose-time
        # ceiling — an installed.lock entry is written below to grant it via the
        # real mechanism, so this test still reaches the SIDECARS refusal it is
        # actually exercising. (Before S2 this test passed BECAUSE of the bug:
        # a copied-in dir was trusted at whatever tier it claimed.)
        "tier: first-party\n"
        "contributes:\n"
        "  sidecars:\n"
        "    - name: my-helper\n"
        "      runtime: container\n"
        "      image: alpine:3.19\n"
        "      command: [sleep, infinity]\n"
    )
    # Grant first-party via the MECHANISM (an installed.lock entry), not the
    # manifest's self-declaration — see the tier note above.
    from botainer.plugins import provenance as _prov
    from botainer.state import dir as _sd
    _prov.append_lock(
        _sd.ensure_user_state_dir(create_if_missing=True).installed_lock_path,
        _prov.ProvenanceEntry(
            name="sidecar-claimer", version="0.1.0", source="test-fixture",
            tree_sha="sha256:0", image_digest=None, installed_at="t",
            tier="first-party"))
    # Enable the plugin in this project's config.
    cfg_path = proj / ".botainer" / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + "\nplugins_enabled:\n  - sidecar-claimer\n")
    with pytest.raises(composition.Refused) as exc:
        composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    msg = str(exc.value).lower()
    assert "sidecar" in msg
    assert "host_helper" in msg or "pre_session" in msg or "v0.1" in msg


def test_no_bundled_plugin_declares_contributes_sidecars() -> None:
    """Tasks #95/#291 invariant: bundled plugins MUST NOT declare
    contributes.sidecars at v0.1 since the launcher refuses them
    fail-closed. Sidecar functionality (wolfram, future host_helper
    plugins) lives in pre_session/post_session hooks instead."""
    plugins_root = Path(__file__).resolve().parents[2] / "plugins"
    assert plugins_root.is_dir(), f"plugins dir missing: {plugins_root}"
    import yaml as _yaml
    offenders: list[tuple[str, int]] = []
    for manifest_path in plugins_root.glob("*/botainer-plugin.yaml"):
        data = _yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        sidecars = (data.get("contributes") or {}).get("sidecars") or []
        if sidecars:
            offenders.append((manifest_path.parent.name, len(sidecars)))
    assert offenders == [], (
        f"Bundled plugins declaring contributes.sidecars (refused at compose, "
        f"would never launch): {offenders}. Use the host_helper hook pattern "
        f"(see plugins/wolfram-sidecar/hooks/pre_session.py)."
    )
