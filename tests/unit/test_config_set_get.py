"""Tests for `botainer config get/set` and `botainer policy set`.

Per CLI rework: users should be able to mutate config/policy via CLI
rather than only by editing YAML by hand.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from botainer.cli.config_cmd import config


def _make_project(tmp_path: Path, body: str) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".botainer").mkdir()
    (proj / ".botainer" / "project-id").write_text(
        "11111111-1111-4111-8111-111111111111\n"
    )
    (proj / ".botainer" / "config.yaml").write_text(body)
    return proj


def test_config_explain_does_not_crash(tmp_path: Path, monkeypatch) -> None:
    """Regression (audit): `config explain` read
    `cfg.network.host_services` — a field removed in #175 — so EVERY run crashed
    with AttributeError. Exercise the network block (incl. endpoints) end-to-end."""
    proj = _make_project(
        tmp_path,
        "agent: claude\nnetwork:\n  mode: internet\n"
        "  endpoints: [api.anthropic.com]\n",
    )
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    result = CliRunner().invoke(config, ["explain"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "Network:" in result.output
    assert "api.anthropic.com" in result.output   # endpoints still rendered


def test_config_get_simple_key(tmp_path: Path, monkeypatch) -> None:
    proj = _make_project(tmp_path, "agent: claude\nnetwork:\n  mode: none\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(config, ["get", "agent"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "claude"


def test_config_get_dotted_key(tmp_path: Path, monkeypatch) -> None:
    proj = _make_project(tmp_path, "network:\n  mode: internet\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(config, ["get", "network.mode"])
    assert result.exit_code == 0
    assert result.output.strip() == "internet"


def test_config_get_missing_key_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    proj = _make_project(tmp_path, "agent: claude\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(config, ["get", "nonexistent"])
    assert result.exit_code == 1


def test_config_set_with_yes_persists(tmp_path: Path, monkeypatch) -> None:
    proj = _make_project(tmp_path, "agent: claude\nnetwork:\n  mode: none\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(config, ["set", "--yes", "network.mode", "internet"])
    assert result.exit_code == 0, result.output
    cfg_text = (proj / ".botainer" / "config.yaml").read_text()
    import yaml
    data = yaml.safe_load(cfg_text)
    assert data["network"]["mode"] == "internet"


def test_config_set_noop_when_already_set(tmp_path: Path, monkeypatch) -> None:
    proj = _make_project(tmp_path, "agent: claude\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(config, ["set", "agent", "claude"])
    assert result.exit_code == 0, result.output
    assert "nothing to change" in result.output


def test_config_set_yaml_value_parsing(tmp_path: Path, monkeypatch) -> None:
    """Values parse as YAML so '[a, b]' becomes a list, 'true' becomes bool."""
    proj = _make_project(tmp_path, "agent: claude\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(
        config, ["set", "--yes", "plugins_enabled", "[agent-claude, git]"]
    )
    assert result.exit_code == 0, result.output
    import yaml
    data = yaml.safe_load((proj / ".botainer" / "config.yaml").read_text())
    assert data["plugins_enabled"] == ["agent-claude", "git"]


def test_config_set_refuses_without_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(config, ["set", "--yes", "foo", "bar"])
    assert result.exit_code != 0
    assert "not inside a botainer project" in result.output


def test_policy_set_with_yes_persists(tmp_path: Path, monkeypatch) -> None:
    from botainer.cli.policy import policy
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    # Create policy.yaml in valid v1 shape so the new schema-validation
    # path (codex 45#6) doesn't refuse the round-trip.
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)
    (state_root / "policy.yaml").write_text(
        "version: policy-v1\n"
    )
    runner = CliRunner()
    result = runner.invoke(
        policy, ["set", "--yes", "default_auth_mode", "shared"]
    )
    assert result.exit_code == 0, result.output
    import yaml
    data = yaml.safe_load((state_root / "policy.yaml").read_text())
    assert data["default_auth_mode"] == "shared"


def test_policy_set_refuses_unknown_key(tmp_path: Path, monkeypatch) -> None:
    """Codex 45#6: setting a key that isn't in SitePolicy refuses with
    a schema error rather than silently writing a broken policy."""
    from botainer.cli.policy import policy
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)
    (state_root / "policy.yaml").write_text("version: policy-v1\n")
    runner = CliRunner()
    result = runner.invoke(
        policy, ["set", "--yes", "totally_made_up_field", "true"]
    )
    assert result.exit_code == 2
    assert "invalid" in result.output.lower() or "refused" in result.output.lower()


def test_policy_set_refuses_invalid_auth_mode_value(
    tmp_path: Path, monkeypatch
) -> None:
    """default_auth_mode only accepts isolated/shared/proxy/empty."""
    from botainer.cli.policy import policy
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)
    (state_root / "policy.yaml").write_text("version: policy-v1\n")
    runner = CliRunner()
    result = runner.invoke(
        policy, ["set", "--yes", "default_auth_mode", "shred"]
    )
    assert result.exit_code == 2, result.output


def test_policy_get_returns_value(tmp_path: Path, monkeypatch) -> None:
    from botainer.cli.policy import policy
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)
    (state_root / "policy.yaml").write_text(
        "version: v1\ndefault_auth_mode: shared\n"
    )
    runner = CliRunner()
    result = runner.invoke(policy, ["get", "default_auth_mode"])
    assert result.exit_code == 0
    assert result.output.strip() == "shared"


def test_policy_set_refuses_without_policy_file(tmp_path: Path, monkeypatch) -> None:
    from botainer.cli.policy import policy
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(policy, ["set", "--yes", "k", "v"])
    assert result.exit_code == 2
    assert "no policy.yaml" in result.output
