"""Render enabled plugins' governed MCP servers into Claude Code's config shape.

`contributes.mcp_servers` (McpServerDecl) is the GOVERNED way a plugin gives the
agent an MCP tool (see plugins/manifest.py + CAPABILITY-SURFACE §4at). This module
is the pure transform: enabled manifests → the `{"mcpServers": {...}}` dict Claude
Code reads. Who WRITES it into the agent's config dir (auth-mode-dependent) + who
surfaces it in the capability summary are separate composition steps; keeping the
transform pure makes it unit-testable and keeps the delivery decision isolated.
"""
from __future__ import annotations

from typing import Iterable

from botainer.core.refusal import RefusalCategory, Refused


def render_mcp_servers(manifests: Iterable) -> dict:
    """Merge the enabled plugins' `contributes.mcp_servers` into the Claude Code
    `mcpServers` mapping. Returns `{}` (NOT `{"mcpServers": {}}`) when no plugin
    contributes any, so composition can skip writing an MCP config entirely.

    GOVERNANCE: a duplicate server NAME across two enabled plugins is REFUSED — a
    later plugin must not silently shadow an earlier one's tool (the same
    fail-closed stance as the auth-family mutual-exclusion). Names are already
    charset-validated at manifest parse (McpServerDecl)."""
    servers: dict[str, dict] = {}
    owner: dict[str, str] = {}
    for man in manifests:
        for s in getattr(man.contributes, "mcp_servers", ()):
            if s.name in servers:
                raise Refused(
                    RefusalCategory.CAPABILITY_VALUE_INVALID,
                    f"two enabled plugins declare an MCP server named {s.name!r} "
                    f"({owner[s.name]!r} and {man.name!r}); refusing to let one "
                    f"silently shadow the other — disable one or rename.",
                )
            owner[s.name] = man.name
            servers[s.name] = {
                "command": s.command,
                "args": list(s.args),
                "env": dict(s.env),
            }
    return {"mcpServers": servers} if servers else {}


def mcp_capability_lines(manifests: Iterable) -> list[str]:
    """Plain-language capability-summary lines for the MCP tools this session grants
    the agent — so an MCP tool is VISIBLE at start, not a silent backdoor. Empty
    when none. (Composition splices these into the capability summary.)"""
    lines: list[str] = []
    for man in manifests:
        for s in getattr(man.contributes, "mcp_servers", ()):
            lines.append(f"  • MCP tool '{s.name}' (from plugin {man.name}) — the "
                         f"agent can call it; it runs inside the container cage.")
    return lines
