"""Tests for plugin enable/disable preserving YAML comments.

Implementation-review HIGH 6: previously yaml.safe_dump round-trip
stripped user comments from .botainer/config.yaml. Surgical block
update preserves them.
"""
from __future__ import annotations

from pathlib import Path

from botainer.plugins import lifecycle


def _make_config(tmp_path: Path, body: str) -> Path:
    cfg_dir = tmp_path / ".botainer"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "config.yaml"
    cfg.write_text(body)
    return tmp_path


def test_enable_preserves_inline_comments_outside_block(tmp_path: Path) -> None:
    """Top-of-file and trailing comments survive `plugin enable`."""
    project = _make_config(tmp_path, """\
# This project's botainer config. Edit by hand or via `botainer plugin`.
# Network: keep at none unless you really need internet.
network:
  mode: none

plugins_enabled:
  - agent-claude
  - git

# Below: project-specific knobs.
secrets_profile: default
""")
    lifecycle.enable(project, "nudge")
    out = (project / ".botainer" / "config.yaml").read_text()
    assert "# This project's botainer config" in out
    assert "# Network: keep at none" in out
    assert "# Below: project-specific knobs." in out
    assert "- nudge" in out
    assert "secrets_profile: default" in out


def test_disable_preserves_inline_comments_outside_block(tmp_path: Path) -> None:
    project = _make_config(tmp_path, """\
# Top comment
plugins_enabled:
  - agent-claude
  - git
  - nudge

other_section:
  key: value  # explanation
""")
    lifecycle.disable(project, "nudge")
    out = (project / ".botainer" / "config.yaml").read_text()
    assert "# Top comment" in out
    assert "- nudge" not in out
    assert "- agent-claude" in out
    assert "- git" in out
    assert "key: value  # explanation" in out


def test_enable_no_existing_block_appends(tmp_path: Path) -> None:
    """Configs without `plugins_enabled:` get a fresh block appended."""
    project = _make_config(tmp_path, """\
network:
  mode: none
""")
    lifecycle.enable(project, "git")
    out = (project / ".botainer" / "config.yaml").read_text()
    assert "plugins_enabled:" in out
    assert "- git" in out
    assert "network:" in out


def test_enable_idempotent(tmp_path: Path) -> None:
    project = _make_config(tmp_path, """\
plugins_enabled:
  - agent-claude
""")
    lifecycle.enable(project, "agent-claude")  # already enabled
    out = (project / ".botainer" / "config.yaml").read_text()
    # Don't add a duplicate
    assert out.count("- agent-claude") == 1


def test_disable_empties_to_empty_list(tmp_path: Path) -> None:
    """Disabling the last plugin leaves a valid (empty) list."""
    project = _make_config(tmp_path, """\
plugins_enabled:
  - git
""")
    lifecycle.disable(project, "git")
    out = (project / ".botainer" / "config.yaml").read_text()
    # Still parses as valid YAML with empty list
    import yaml
    parsed = yaml.safe_load(out)
    assert parsed.get("plugins_enabled") == []
