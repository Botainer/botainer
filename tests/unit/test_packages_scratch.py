"""Tests for /packages + /scratch mounts (Phase 2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import composition, identity
from botainer.core import config as config_module
from botainer.state import dir as state_dir


def _prepare_project(tmp_path: Path) -> Path:
    from tests.conftest import append_image_to_config

    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    append_image_to_config(proj)
    return proj


def test_packages_dir_created_at_ensure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    proj = state_dir.ensure_project_dirs(paths, "11111111-1111-1111-1111-111111111111")
    assert proj.packages_dir.exists()
    assert proj.scratch_dir.exists()
    # Per-language subdirs
    assert (proj.packages_dir / "pip").exists()
    assert (proj.packages_dir / "julia_depot").exists()


def test_compose_includes_packages_and_scratch_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    targets = {b.target for b in spec.mount_plan.binds}
    assert "/packages" in targets
    assert "/scratch" in targets


def test_packages_is_writable_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    pkg = next(b for b in spec.mount_plan.binds if b.target == "/packages")
    from botainer.core.spec import BindMode
    assert pkg.mode == BindMode.RW


def test_scratch_is_writable_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    scr = next(b for b in spec.mount_plan.binds if b.target == "/scratch")
    from botainer.core.spec import BindMode
    assert scr.mode == BindMode.RW


def test_compose_includes_writable_home_bind_and_HOME_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The container runs as the host uid (no home in the image) → HOME=/ breaks
    npm/npx/pip and Claude Code spills into /tmp. Compose must bind a writable
    /home/user (rw) and set HOME to it. Fable-5 reviewed."""
    from botainer.core.spec import BindMode
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    home = next((b for b in spec.mount_plan.binds if b.target == "/home/user"), None)
    assert home is not None, "missing writable /home/user bind"
    assert home.mode == BindMode.RW
    # source is the per-project state home dir (NOT the host home)
    assert home.source.endswith("/home")
    assert spec.env.values.get("HOME") == "/home/user"


@pytest.mark.parametrize("bad_key", [
    "GCONV_PATH", "GLIBC_TUNABLES", "HOSTALIASES", "LD_PROFILE",  # exec-injection
    "CLAUDE_CONFIG_DIR", "PIP_TARGET", "HOME",                   # managed-routes
])
def test_config_env_refuses_execinjection_and_managed_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_key: str
) -> None:
    """sharp-edges HIGH (3 audits): the git-shareable config `env:`
    must be gated by the SAME hardcoded exec-injection + managed-route sets the
    hook channels use — not just the (weaker) policy denylist. A config setting
    GCONV_PATH/CLAUDE_CONFIG_DIR/etc. would run code at container start or redirect
    the agent's config lookup."""
    from botainer.core.refusal import Refused
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    cfg = proj / ".botainer" / "config.yaml"
    cfg.write_text(cfg.read_text() + f"\nenv:\n  {bad_key}: /workspace/evil\n")
    with pytest.raises(Refused):
        composition.compose_session(proj, runtime_choice="mock", identity_accept=False)


def test_expanded_env_denylist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Per sharp-edges F5: expanded denylist includes LD_AUDIT, NODE_OPTIONS, etc."""
    from botainer.core.policy import CapabilitiesPolicy
    cap = CapabilitiesPolicy()
    # Spot-check new entries
    assert "LD_AUDIT" in cap.env_var_denylist
    assert "NODE_OPTIONS" in cap.env_var_denylist
    assert "JULIA_LOAD_PATH" in cap.env_var_denylist
    assert "BASH_ENV" in cap.env_var_denylist
    assert "GIT_SSH_COMMAND" in cap.env_var_denylist
    # Original ones still present
    assert "LD_PRELOAD" in cap.env_var_denylist
    assert "PYTHONPATH" in cap.env_var_denylist
