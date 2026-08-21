"""Tests for project config.yaml parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import config as config_module
from botainer.core.refusal import RefusalCategory, Refused


def _write(proj: Path, body: str) -> None:
    bot = proj / ".botainer"
    bot.mkdir(parents=True, exist_ok=True)
    (bot / "config.yaml").write_text(body)


def test_load_minimal_config(tmp_path: Path) -> None:
    _write(tmp_path, "agent: claude\nprofile: default\n")
    cfg = config_module.load_config(tmp_path)
    assert cfg.agent == "claude"
    assert cfg.profile == "default"


def test_missing_config_refuses(tmp_path: Path) -> None:
    with pytest.raises(Refused) as exc:
        config_module.load_config(tmp_path)
    assert exc.value.category == RefusalCategory.CONFIG_MISSING


def test_invalid_yaml_refuses(tmp_path: Path) -> None:
    _write(tmp_path, "agent: claude\n: bad-key\n")
    with pytest.raises(Refused) as exc:
        config_module.load_config(tmp_path)
    assert exc.value.category in (
        RefusalCategory.CONFIG_INVALID,
        RefusalCategory.CONFIG_SCHEMA_MISMATCH,
    )


def test_unknown_top_level_field_refuses(tmp_path: Path) -> None:
    _write(tmp_path, "agent: claude\nbogus_field: x\n")
    with pytest.raises(Refused) as exc:
        config_module.load_config(tmp_path)
    assert exc.value.category == RefusalCategory.CONFIG_SCHEMA_MISMATCH


def test_api_only_alias_normalized(tmp_path: Path) -> None:
    _write(tmp_path, "agent: claude\nnetwork:\n  mode: api-only\n")
    cfg = config_module.load_config(tmp_path)
    assert cfg.network.mode == "endpoint-ip-allowlist"


def test_write_initial_config_creates_file(tmp_path: Path) -> None:
    config_module.write_initial_config(tmp_path, agent="claude", force=False)
    p = tmp_path / ".botainer" / "config.yaml"
    assert p.exists()
    # And it should be a valid config.
    cfg = config_module.load_config(tmp_path)
    assert cfg.agent == "claude"


def test_write_initial_config_does_not_overwrite_without_force(tmp_path: Path) -> None:
    p = tmp_path / ".botainer" / "config.yaml"
    p.parent.mkdir()
    p.write_text("agent: existing\n")
    config_module.write_initial_config(tmp_path, agent="claude", force=False)
    cfg = config_module.load_config(tmp_path)
    assert cfg.agent == "existing"


def test_write_initial_config_hpc_runtime_preenables_hpc_plugins(tmp_path: Path) -> None:
    """HPC init pre-enables the plugin that NEEDS enabling, and only that one.

    #127 originally pre-enabled hpc-launcher AND hpc-modules. NARROWED
    after a user report: hpc-launcher declares no hooks and no
    container contributions (see PluginManifest.enabling_is_inert), so listing
    it changed nothing — while `plugin list` and this template together told
    users it was a switch they had to flip. hpc-modules has a host_pre_launch
    hook and genuinely must be enabled, so it stays.

    This NARROWS an assertion deliberately. The principle it protected — HPC
    init must produce a working config without hand-editing — is unweakened:
    submission is wired by the INSTALLED plugin plus `job_profiles`, neither of
    which this line affected. The settings block is still populated for both,
    which is the part that actually configures a submission.
    """
    proj = tmp_path / "hpcproj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=True, runtime="apptainer")
    body = (proj / ".botainer" / "config.yaml").read_text()
    assert "- hpc-modules" in body
    assert "- hpc-launcher" not in body, (
        "the template again tells users to enable hpc-launcher, which does "
        "nothing — that was the reported confusion")
    # The populated settings block (active YAML, not commented templates) is
    # unchanged: hpc-launcher's config IS read, it just is not enabled.
    assert "\nhpc-launcher:" in body or "  hpc-launcher:\n" in body
    assert "hpc-modules:" in body
    # Loadable.
    cfg = config_module.load_config(proj)
    assert "hpc-modules" in cfg.plugins_enabled


def test_write_initial_config_docker_runtime_does_not_enable_hpc_plugins(tmp_path: Path) -> None:
    # Symmetric guard: Docker init must NOT auto-enable HPC plugins.
    proj = tmp_path / "dockproj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=True, runtime="docker")
    body = (proj / ".botainer" / "config.yaml").read_text()
    cfg = config_module.load_config(proj)
    assert "hpc-launcher" not in cfg.plugins_enabled
    assert "hpc-modules" not in cfg.plugins_enabled
    # The commented-out template lines (with `# - hpc-launcher`) are fine; the
    # active `  - hpc-launcher` (no leading `#`) must be absent.
    assert "\n  - hpc-launcher\n" not in body
    assert "\n  - hpc-modules\n" not in body
