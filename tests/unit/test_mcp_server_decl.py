"""Governed MCP-server declarations (McpServerDecl) — the mechanism a plugin uses
to give the agent an MCP tool as a capability-surfaced grant (browser plugin +
"handle MCPs better"). Data-flow discipline: these become an argv+env
for the server process, never a shell string, so every field is charset-guarded."""
from __future__ import annotations

import pytest

from botainer.plugins.manifest import (
    ContributesDecl,
    McpServerDecl,
    PluginManifest,
)


def test_valid_mcp_server() -> None:
    m = McpServerDecl(
        name="browser", command="npx",
        args=["-y", "@playwright/mcp@latest", "--headless"],
        env={"PLAYWRIGHT_BROWSERS_PATH": "/packages/.pw"},
    )
    assert m.name == "browser" and m.command == "npx"
    assert m.args[1] == "@playwright/mcp@latest"


def test_contributes_carries_mcp_servers() -> None:
    c = ContributesDecl(mcp_servers=[{"name": "browser", "command": "npx"}])
    assert len(c.mcp_servers) == 1 and c.mcp_servers[0].name == "browser"


def test_default_no_mcp_servers_and_existing_manifests_unaffected() -> None:
    """Additive field: a manifest that never mentions mcp_servers still parses,
    defaulting to an empty list (no behavior change for existing plugins)."""
    pm = PluginManifest(name="x", version="0.1.0")
    assert pm.contributes.mcp_servers == []


def test_name_charset_enforced() -> None:
    with pytest.raises(ValueError, match="name"):
        McpServerDecl(name="bad name", command="x")


def test_command_no_leading_dash_or_control() -> None:
    """argv[0]: a leading '-' would parse as a flag (arg-injection)."""
    with pytest.raises(ValueError, match="command"):
        McpServerDecl(name="ok", command="-rm")
    with pytest.raises(ValueError, match="command"):
        McpServerDecl(name="ok", command="x\ny")


def test_args_reject_control_chars() -> None:
    with pytest.raises(ValueError, match="arg"):
        McpServerDecl(name="ok", command="x", args=["fine", "bad\x00"])


def test_env_key_identifier_and_value_no_control() -> None:
    with pytest.raises(ValueError, match="env key"):
        McpServerDecl(name="ok", command="x", env={"1BAD": "y"})
    with pytest.raises(ValueError, match="env value"):
        McpServerDecl(name="ok", command="x", env={"OK": "line\nbreak"})


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValueError):
        McpServerDecl(name="ok", command="x", surprise="nope")
