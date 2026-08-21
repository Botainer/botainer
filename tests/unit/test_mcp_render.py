"""render_mcp_servers: enabled plugins' governed MCP servers → Claude Code shape."""
from __future__ import annotations

import pytest

from botainer.core.refusal import Refused
from botainer.plugins.manifest import PluginManifest
from botainer.plugins.mcp import mcp_capability_lines, render_mcp_servers


def _man(name: str, servers: list[dict]) -> PluginManifest:
    return PluginManifest(
        name=name, version="0.1.0",
        contributes={"mcp_servers": servers},
    )


def test_none_yields_empty() -> None:
    """No mcp_servers anywhere → {} so composition writes no MCP config at all."""
    assert render_mcp_servers([_man("git", [])]) == {}
    assert render_mcp_servers([]) == {}


def test_renders_claude_shape() -> None:
    m = _man("browser", [{"name": "browser", "command": "npx",
                          "args": ["-y", "@playwright/mcp"], "env": {"X": "1"}}])
    out = render_mcp_servers([m])
    assert out == {"mcpServers": {"browser": {
        "command": "npx", "args": ["-y", "@playwright/mcp"], "env": {"X": "1"}}}}


def test_merges_across_plugins() -> None:
    out = render_mcp_servers([
        _man("a", [{"name": "browser", "command": "npx"}]),
        _man("b", [{"name": "search", "command": "search-mcp"}]),
    ])
    assert set(out["mcpServers"]) == {"browser", "search"}


def test_duplicate_name_across_plugins_refused() -> None:
    """Governed: a later plugin cannot silently shadow an earlier server's name."""
    with pytest.raises(Refused, match="named 'browser'"):
        render_mcp_servers([
            _man("a", [{"name": "browser", "command": "npx"}]),
            _man("b", [{"name": "browser", "command": "other"}]),
        ])


def test_capability_lines_name_the_tool_and_plugin() -> None:
    lines = mcp_capability_lines([_man("browser",
                                       [{"name": "browser", "command": "npx"}])])
    assert len(lines) == 1
    assert "browser" in lines[0] and "plugin browser" in lines[0]
    assert mcp_capability_lines([_man("git", [])]) == []
