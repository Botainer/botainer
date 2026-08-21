"""Plugin manifest schema and parsing.

`botainer-plugin.yaml` at the plugin's root declares: name, version, runtimes,
commands, image, hooks, capabilities, contributes (mount target prefixes,
sidecars, etc.), config_schema, depends_on, tier.

Per codex HIGH 9: minimal provenance = (tree SHA + image digest lock). Recorded
in `installed.lock` after install, not in the manifest itself.

Per codex M10: third-party plugins must be declarative (no hooks) unless
explicit consent.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from botainer.core.refusal import RefusalCategory, Refused

# Module-top so Field() in PluginManifest below can reference it.
# Tied to PLUGIN_MANIFEST_API_SUPPORTED via assert later in the file.
DEFAULT_PLUGIN_MANIFEST_API = "botainer-plugin-v1"

MANIFEST_FILENAME = "botainer-plugin.yaml"


_RESERVED_NAME_PREFIXES = ("agent-", "botainer-")
_FIRST_PARTY_RESERVED = {
    "agent-claude",
    "agent-claude-proxy",
    "agent-claude-shared",
    "agent-codex",
    "agent-codex-shared",
    "agent-codex-proxy",
    "agent-gemini",
    "agent-augment",
    "agent-aider",
    "credential-proxy",
    "git",
    "hpc-launcher",
    "hpc-modules",
    "hpc-pool",
    "nudge",
    "web-ports",
    "wolfram-sidecar",
}


def _validate_relative_script_path(v: str) -> str:
    """Per sharp-edges #10: hook/command scripts must be relative paths inside
    the plugin tree. Absolute paths or `..` traversal are refused at
    manifest-parse time."""
    from pathlib import PurePosixPath

    if not v:
        raise ValueError("script path is empty")
    if v.startswith("/"):
        raise ValueError(f"script path {v!r} must be relative (not absolute)")
    if "\x00" in v or "\n" in v:
        raise ValueError(f"script path {v!r} contains NUL or newline")
    if ".." in PurePosixPath(v).parts:
        raise ValueError(f"script path {v!r} contains '..' (traversal)")
    return v


class HookDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    when: str
    script: str
    timeout_seconds: int = 30

    @field_validator("when")
    @classmethod
    def _validate_when(cls, v: str) -> str:
        if v not in (
            "pre_session",
            "post_session",
            "host_pre_launch",
        ):
            # #215: `pre_request` / `post_request` were declared here
            # but had no dispatcher in composition. They depend on the
            # credential-proxy infrastructure (§B1 — deferred to v0.2).
            # Removed in; will be re-added with a real
            # dispatcher when proxy lands.
            raise ValueError(f"invalid hook timing: {v!r}")
        return v

    @field_validator("script")
    @classmethod
    def _validate_script(cls, v: str) -> str:
        return _validate_relative_script_path(v)


class CommandDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    script: str
    help: str = ""

    @field_validator("script")
    @classmethod
    def _validate_script(cls, v: str) -> str:
        return _validate_relative_script_path(v)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        # AUDIT (MEDIUM): a command name becomes a click subcommand
        # verb (e.g. `botainer plugin <name>`); constrain it like the plugin
        # name (DN-027 §2) so a manifest can't inject an odd/colliding verb.
        import re
        if not re.fullmatch(r"[a-z][a-z0-9-]*", v):
            raise ValueError(f"command name {v!r} must match ^[a-z][a-z0-9-]*$")
        return v


class ImageDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = "registry"  # registry | dockerfile | apptainer-def
    tag: str | None = None  # name@sha256:... when source=registry
    dockerfile: str | None = None  # relative to plugin root when source=dockerfile
    definition: str | None = None  # for apptainer-def

    @field_validator("dockerfile", "definition")
    @classmethod
    def _validate_build_path(cls, v: str | None) -> str | None:
        # AUDIT (MEDIUM): cli/image.py builds `plugin_dir / dockerfile`
        # and its docstring CLAIMED "the relative-path validator on the manifest
        # field already refuses '..' traversal" — but there was no such
        # validator, so an absolute or '..'-traversing value escaped the plugin
        # root. Apply the same relative-path guard as hook/command scripts.
        if v is None:
            return v
        return _validate_relative_script_path(v)


class SidecarDecl(BaseModel):
    """Sidecar declaration. v0.1: `contributes.sidecars` is REFUSED at
    compose time (commit 20f752e — #95/#291). The schema is kept so
    plugin manifests that DO declare sidecars surface a clear refusal
    rather than a load-time crash. Removed fields:
    - `own_capabilities`: zero readers; v0.2 cap-grant surface, not v0.1.
    - `channel`: zero readers; v0.2 sidecar IPC config, not v0.1."""

    model_config = ConfigDict(extra="forbid")
    name: str
    runtime: str  # container | host_helper
    image: str | None = None
    command: list[str] = Field(default_factory=list)


class EntrypointWrapDecl(BaseModel):
    """A wrap that prepends to the agent's entrypoint argv.

    For example, the agent-claude plugin contributes an inner-layer
    wrap that injects AGENT_HINTS.md into Claude's system prompt.
    Multiple plugins may each contribute one wrap; composition layers
    them by (layer, plugin-name).

    layer ordering:
      "outer"   — runs first in the chain (strace, an in-container
                  multiplexer, ...). No bundled v0.1 plugin currently
                  uses this layer; reserved for third-party use.
      "default" — neither agent-specific nor session-framework. Default.
      "inner"   — closest to the actual agent binary (agent-claude prompt
                  injection, agent-codex API-key import). Examples: agent-claude.

    Within the same layer, plugins compose in alphabetical order for
    determinism. The final argv is:

        wraps[0] wraps[1] ... wraps[-1] entrypoint command

    where wraps[0] is outermost (first to run). This matters when an
    outer-layer wrap and an inner-layer wrap are both enabled: the
    outer wrap must be the actual session leader (the docker
    --entrypoint / apptainer exec target), not buried as argv to a
    deeper wrap. Implementation-review MEDIUM 13.

    §A19: the historical use of templates (`${tmux_socket_path}`) to
    inject in-container tmux socket paths was retired when nudge was
    migrated to a host-side screen wrap. The launcher no longer
    substitutes template placeholders in wrap commands; the wrap
    command must be a fully-resolved argv at manifest authoring time.
    """

    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(default_factory=list)
    layer: str = Field(default="default")

    @field_validator("command")
    @classmethod
    def _validate_command(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("entrypoint_wrap command must not be empty")
        for arg in v:
            if "\x00" in arg or "\n" in arg:
                raise ValueError(
                    f"entrypoint_wrap arg {arg!r} contains NUL or newline"
                )
        return v

    @field_validator("layer")
    @classmethod
    def _validate_layer(cls, v: str) -> str:
        if v not in {"outer", "default", "inner"}:
            raise ValueError(
                f"entrypoint_wrap layer must be one of outer/default/inner; got {v!r}"
            )
        return v


_MCP_NAME_RE = re.compile(r"\A[A-Za-z0-9_.-]+\Z")
_MCP_ENV_KEY_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class McpServerDecl(BaseModel):
    """An MCP (Model Context Protocol) server a plugin provides to the agent.

    The launcher writes this into the agent's MCP config ONLY when the plugin is
    enabled, so an MCP tool is a GOVERNED, capability-surfaced grant — not an
    ungoverned hand-edit of the agent's raw config. The server process runs INSIDE
    the §4 cage (same isolation as the agent) and inherits the plugin's trust
    (tier + trust-lock); a third-party plugin's MCP server is a supply-chain grant
    to surface at session start. Maps to Claude Code's `mcpServers` shape
    ({command, args, env}). Data-flow discipline: these become an argv + env for
    the server process — never a shell string — so every field is charset-guarded."""
    model_config = ConfigDict(extra="forbid")
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        if not (isinstance(v, str) and _MCP_NAME_RE.match(v)):
            raise ValueError(
                f"mcp_servers name {v!r} must match [A-Za-z0-9_.-]+")
        return v

    @field_validator("command")
    @classmethod
    def _v_command(cls, v: str) -> str:
        # argv[0] for the server. Reject a leading '-' (would parse as a flag —
        # arg-injection) and control chars (config → argv, never shell).
        if not (isinstance(v, str) and v):
            raise ValueError("mcp_servers command must be a non-empty string")
        if v[0] == "-" or any(ord(c) < 0x20 for c in v):
            raise ValueError(
                f"mcp_servers command {v!r}: no leading dash / control chars")
        return v

    @field_validator("args")
    @classmethod
    def _v_args(cls, v: list[str]) -> list[str]:
        for a in v:
            if not isinstance(a, str) or any(ord(c) < 0x20 for c in a):
                raise ValueError(
                    f"mcp_servers arg {a!r}: must be a control-char-free string")
        return v

    @field_validator("env")
    @classmethod
    def _v_env(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if not (isinstance(k, str) and _MCP_ENV_KEY_RE.match(k)):
                raise ValueError(
                    f"mcp_servers env key {k!r} is not a valid identifier")
            if not isinstance(val, str) or any(ord(c) < 0x20 for c in val):
                raise ValueError(
                    f"mcp_servers env value for {k!r} has a control char")
        return v


class ContributesDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mount_target_prefixes: list[str] = Field(default_factory=list)
    sidecars: list[SidecarDecl] = Field(default_factory=list)
    # MCP servers this plugin provides to the agent (governed — see McpServerDecl).
    mcp_servers: list[McpServerDecl] = Field(default_factory=list)
    # HUMAN-facing CLI tips (distinct from agent_hints_section, which is for the
    # AGENT). Lead with the CAPABILITY this plugin offers + how to activate it, and
    # POINT to docs for the security surface — a tip must NOT render a safety verdict
    # ("safe"/"safer"/"protects"): a one-liner can't carry the caveats, so a verdict
    # misleads (see botainer/tips.py "RULE"). They join the pool `botainer` shows one
    # of after each command (drawn from all INSTALLED plugins, so a tip can advertise
    # a not-yet-enabled capability); full list: `botainer tips`.
    user_tips: list[str] = Field(default_factory=list)

    @field_validator("user_tips")
    @classmethod
    def _validate_user_tips(cls, v: list[str]) -> list[str]:
        import unicodedata

        for t in v:
            if not isinstance(t, str) or not t.strip():
                raise ValueError("user_tips entries must be non-empty strings")
            # Printed to a terminal — reject ALL control (Cc: C0/C1/DEL) and format
            # (Cf: bidi overrides like U+202E, zero-width joiners) chars except TAB.
            # A C1 CSI (\x9b) or a bidi override can corrupt/spoof the rendered
            # footer, and the SGR reset in render_footer doesn't neutralise them
            # (Fable-5 L1). Only ordinary printable text is allowed.
            for c in t:
                if c != "\t" and unicodedata.category(c) in ("Cc", "Cf"):
                    raise ValueError(
                        f"user_tips entry {t!r} has a control/format char {c!r}")
            if len(t) > 300:
                raise ValueError("user_tips entries must be <= 300 chars (keep them short)")
            # The RULE above, actually enforced. It was prose in this comment and
            # in tips.py's docstring, and checked by nothing — so a plugin tip
            # reading "The viewer is SAFER and PROTECTS your clipboard" was
            # accepted unmodified. BOTH of the maintainer's hand-caught verdicts
            # were in a plugin manifest, not in BASE_TIPS, so this
            # is the surface where it actually happened.
            #
            # A FILTER backing up a structural gap, not a guarantee: a tip is a
            # `str`, so a verdict stays as representable as a fact and no word
            # list closes that ("redundant", "you don't need to worry about").
            # See tips.py's _VERDICT_WORDS block for the structural fix.
            from botainer.tips import verdict_words_in
            hits = verdict_words_in(t)
            if hits:
                raise ValueError(
                    f"user_tips entry renders a SAFETY VERDICT ({', '.join(hits)}): "
                    f"{t!r}\n"
                    f"A one-liner cannot carry the caveats a security claim needs, "
                    f"so a verdict there misleads. State a FACT and POINT at the "
                    f"security surface instead — e.g. 'the caged agent acts "
                    f"autonomously; see docs/BROWSER.md'. See botainer/tips.py RULE.")
        return v

    @field_validator("mount_target_prefixes")
    @classmethod
    def _validate_prefixes(cls, v: list[str]) -> list[str]:
        # AUDIT (MEDIUM, defense-in-depth): a prefix is the envelope
        # bind contributions are checked against (composition.py). The mount
        # normalizer + the hook-bind '..'/NUL guard already backstop derived
        # targets, but reject a NUL/newline/'..' prefix at manifest-parse time
        # too, for consistency with the script/image path validators.
        from pathlib import PurePosixPath
        for p in v:
            if not isinstance(p, str) or not p:
                raise ValueError("mount_target_prefixes entries must be non-empty strings")
            if "\x00" in p or "\n" in p:
                raise ValueError(f"mount_target_prefix {p!r} contains NUL/newline")
            if ".." in PurePosixPath(p).parts:
                raise ValueError(f"mount_target_prefix {p!r} contains '..' (traversal)")
        return v
    entrypoint_wrap: EntrypointWrapDecl | None = None
    # Architecture review #1: plugins own their AGENT_HINTS section.
    # The launcher used to hardcode if-branches per plugin name in
    # agent_hints.py — a layering violation that meant every new plugin
    # required touching launcher core. Now: each plugin declares the
    # markdown section the launcher splices into AGENT_HINTS.md when
    # the plugin is enabled. Empty string = no section.
    #
    # Recognized template tokens (resolved per-session by the launcher):
    #   ${port_forwards_list}  — bullet list of forwarded ports (web-ports)
    #   ${env_files_list}      — bullet list of env-file paths (hpc-modules)
    agent_hints_section: str = ""
    # Capability-summary entry: single multi-line block the launcher
    # adds to render_multiline() when the plugin is enabled. Same
    # template tokens as agent_hints_section.
    capability_summary_block: str = ""


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apiVersion: str = DEFAULT_PLUGIN_MANIFEST_API
    name: str
    version: str
    description: str = ""
    license: str = ""
    maintainer: str = ""
    botainer_min_version: str = "0.1.0a0"
    runtimes: list[str] = Field(default_factory=lambda: ["docker"])
    tier: str = "third-party"  # first-party | community-verified | third-party
    kind: str = "agent"  # agent | service-sidecar | service-host | policy-overlay

    def enabling_is_inert(self) -> bool:
        """True when listing this plugin in `plugins_enabled` changes nothing.

        `plugins_enabled` selects which plugins COMPOSE — composition.py:368
        intersects it with the installed set and from there runs hooks and
        applies binds/env/sidecars. A plugin declaring none of those is
        therefore unaffected by being enabled.

        `hpc-launcher` is the case (user report): they ran cluster
        jobs successfully without it in `plugins_enabled` and could not tell
        whether that was a mistake. It is not — jobs key off `job_profiles`,
        and the in-container `botainer-job` is bind-sourced from the INSTALLED
        plugin. But `botainer plugin list` said "installed, not enabled", which
        reads as a switch left off, and `botainer init` generated a template
        telling users to enable it.

        Derived from what the plugin DECLARES, not from its name or `kind`, so
        a future plugin that grows a hook stops being inert automatically.
        """
        c = self.contributes
        return not (
            self.hooks                      # nothing runs at any lifecycle point
            or c.sidecars                   # nothing is launched alongside
            or c.entrypoint_wrap            # the exec line is untouched
            or c.mcp_servers                # no MCP wiring
            or c.agent_hints_section        # the agent is told nothing extra
            or c.capability_summary_block   # the user is shown nothing extra
            or c.user_tips
        )
        # NB mount_target_prefixes is deliberately NOT in this list: it is an
        # ENVELOPE (permission to bind under a prefix), not a contribution. A
        # plugin declaring only an envelope contributes nothing by being
        # enabled — which is exactly hpc-launcher's shape.
    trust_required: str = "declarative"  # declarative | hooked
    commands: list[CommandDecl] = Field(default_factory=list)
    image: ImageDecl | None = None
    hooks: list[HookDecl] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    contributes: ContributesDecl = Field(default_factory=ContributesDecl)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)

    # Auth-family / auth-mode (AUTH-PRODUCT-PLAN.md §1, §8).
    # When set, this plugin is part of an auth family with three modes:
    #   isolated:  per-project credential file (today's default)
    #   shared:    host-wide credential dir, mounted into container
    #   proxy:     host-wide credential dir, container sees ephemeral token
    # Exactly ONE plugin per family may be enabled per project (the
    # launcher enforces this at compose time via the mutually_exclusive_with
    # field below).
    auth_family: str = ""  # "" (no auth) | "anthropic" | "openai" | ...
    auth_mode: str = ""    # "" (no auth) | "isolated" | "shared" | "proxy" | "broker"

    # If this plugin is part of an auth family, the OTHER plugins in
    # the same family. Enabling two plugins from the same family is
    # refused at compose time.
    mutually_exclusive_with: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[a-z][a-z0-9-]*", v):
            raise ValueError(f"plugin name {v!r} must match ^[a-z][a-z0-9-]*$")
        return v

    @field_validator("runtimes")
    @classmethod
    def _validate_runtimes(cls, v: list[str]) -> list[str]:
        # AUDIT (MEDIUM): runtimes was unvalidated (empty list or
        # arbitrary strings accepted) AND unconsumed. DN-027 §2: non-empty
        # list of known runtimes. (Compose now also REFUSES enabling a plugin
        # whose runtimes excludes the session runtime — see composition.py.)
        if not v:
            raise ValueError("runtimes must be a non-empty list (e.g. [docker, apptainer])")
        allowed = {"docker", "apptainer"}
        bad = [r for r in v if r not in allowed]
        if bad:
            raise ValueError(
                f"runtimes {bad!r} not in {sorted(allowed)} (mock is a test-only "
                f"runtime and is never declared by a plugin)"
            )
        return v

    @field_validator("auth_mode")
    @classmethod
    def _validate_auth_mode(cls, v: str) -> str:
        if v and v not in {"isolated", "shared", "proxy", "broker"}:
            raise ValueError(
                f"auth_mode {v!r} must be one of: '', 'isolated', 'shared', "
                f"'proxy', 'broker'"
            )
        return v


# Task #254 + #271: apiVersion + botainer_min_version enforcement.
# Was: PluginManifest accepted any apiVersion + any botainer_min_version.
# Now: load_manifest refuses incompatible plugins before they reach the
# composition pipeline.

PLUGIN_MANIFEST_API_SUPPORTED = frozenset({"botainer-plugin-v1"})
# Task #130: PluginManifest.apiVersion default = DEFAULT_PLUGIN_MANIFEST_API
# (defined at module top so pydantic Field can reference it without
# forward-ref). Tie ensures: when we add botainer-plugin-v2 to
# PLUGIN_MANIFEST_API_SUPPORTED, this assert catches forgetting to also
# update the parser's default.
assert DEFAULT_PLUGIN_MANIFEST_API in PLUGIN_MANIFEST_API_SUPPORTED
# MUST equal pyproject.toml's `version`. It drifted — this said 0.1.0a3 while
# pyproject said 0.1.0a1 — and nothing noticed, because _parse_semver_tuple
# drops the prerelease suffix so both read as (0, 1, 0). Inert until the day
# someone bumps the minor in one file only. tests/unit/test_version_is_single_sourced.py
# now pins them together, so the comment is enforced rather than hoped for.
BOTAINER_LAUNCHER_VERSION = "0.1.0a4"


def _parse_semver_tuple(v: str) -> tuple[int, int, int]:
    """Best-effort: '0.1.0a3' -> (0, 1, 0). Ignores prerelease suffix."""
    import re

    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def load_manifest(plugin_dir: Path) -> PluginManifest:
    path = plugin_dir / MANIFEST_FILENAME
    if not path.exists():
        raise Refused(
            RefusalCategory.PLUGIN_MANIFEST_INVALID,
            f"no {MANIFEST_FILENAME} in {plugin_dir}",
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise Refused(RefusalCategory.PLUGIN_MANIFEST_INVALID, f"yaml parse: {exc}") from exc
    if not isinstance(raw, dict):
        raise Refused(
            RefusalCategory.PLUGIN_MANIFEST_INVALID, "manifest top-level must be a mapping"
        )
    try:
        manifest = PluginManifest.model_validate(raw)
    except Exception as exc:
        raise Refused(RefusalCategory.PLUGIN_MANIFEST_INVALID, f"schema: {exc}") from exc

    # Task #254: apiVersion compatibility check.
    if manifest.apiVersion not in PLUGIN_MANIFEST_API_SUPPORTED:
        raise Refused(
            RefusalCategory.PLUGIN_MANIFEST_INVALID,
            f"plugin {manifest.name!r} declares apiVersion={manifest.apiVersion!r}; "
            f"this launcher supports {sorted(PLUGIN_MANIFEST_API_SUPPORTED)}",
        )

    # Task #271: botainer_min_version check.
    plugin_needs = _parse_semver_tuple(manifest.botainer_min_version)
    launcher_has = _parse_semver_tuple(BOTAINER_LAUNCHER_VERSION)
    if plugin_needs > launcher_has:
        raise Refused(
            RefusalCategory.PLUGIN_MANIFEST_INVALID,
            f"plugin {manifest.name!r} requires botainer "
            f">= {manifest.botainer_min_version}; this launcher is "
            f"{BOTAINER_LAUNCHER_VERSION}",
        )
    return manifest


def check_reserved_name(name: str, *, source_is_first_party: bool) -> None:
    """Reserved names (`agent-*`, `botainer-*`, hardcoded first-party list) can only
    be installed from first-party sources.
    """
    if name in _FIRST_PARTY_RESERVED and not source_is_first_party:
        raise Refused(
            RefusalCategory.PLUGIN_NAME_RESERVED,
            f"plugin name {name!r} is reserved for first-party plugins",
        )
    for prefix in _RESERVED_NAME_PREFIXES:
        if name.startswith(prefix) and not source_is_first_party:
            raise Refused(
                RefusalCategory.PLUGIN_NAME_RESERVED,
                f"plugin name {name!r} uses reserved prefix {prefix!r}",
            )


def declared_commands(installed_plugin: object) -> dict[str, Any]:
    """For lazy CLI wiring. Returns {verb: click.Command}.

    Loads the plugin's manifest, finds `commands:`, and builds a click
    Command per entry that exec's the declared `script:` with a
    standard BOTAINER_* env. Used by `botainer plugin <name> <verb>`.

    The script gets:
      BOTAINER_PROJECT_ROOT     — current cwd (or walked-up project root)
      BOTAINER_PROJECT_UUID     — UUID of the project (if found)
      BOTAINER_STATE_DIR        — host state root
      BOTAINER_PROFILE          — profile name (default: "default")
      BOTAINER_PLUGIN           — plugin name
      BOTAINER_COMMAND          — verb being invoked
    """
    import os
    import subprocess as _sub
    import sys
    from pathlib import Path as _Path

    import click as _click

    plugin_dir = getattr(installed_plugin, "plugin_dir", None)
    plugin_name = getattr(installed_plugin, "name", None)
    if plugin_dir is None or plugin_name is None:
        return {}
    try:
        manifest = load_manifest(plugin_dir)
    except Refused:
        return {}
    if not manifest.commands:
        return {}

    out: dict[str, Any] = {}
    for cmd_decl in manifest.commands:
        script_rel = cmd_decl.script
        script_path = plugin_dir / script_rel
        help_text = cmd_decl.help or f"Run plugin {plugin_name} `{cmd_decl.name}`"

        def _make_handler(
            _verb: str, _script: _Path, _plugin: str, _help: str
        ) -> Any:
            @_click.command(_verb, help=_help, context_settings={
                "ignore_unknown_options": True,
                "allow_extra_args": True,
            })
            @_click.pass_context
            def _handler(ctx: _click.Context) -> None:
                extra_args = list(ctx.args)
                # Walk up to find project root (for project-scoped commands).
                from botainer.cli._common import find_project_root
                from botainer.core import identity as _identity
                from botainer.core.refusal import Refused
                project_root = find_project_root() or _Path.cwd()
                try:
                    uid = _identity.read_project_id(project_root) or ""
                except (FileNotFoundError, OSError):
                    uid = ""
                # AC7 validator-parity audit: read_project_id
                # returns the raw .botainer/project-id with ZERO validation,
                # and this generic dispatcher exports it as
                # BOTAINER_PROJECT_UUID into EVERY plugin subprocess. The
                # hpc-launcher submit path trusts that env var and
                # interpolates it unquoted into the generated sbatch script —
                # a tampered (git-shareable) project-id was a confirmed HIGH
                # sbatch-injection → ACE on the login/compute node. Validate
                # at this source chokepoint so no plugin subprocess ever sees
                # a non-canonical uuid. Empty (no project-id) stays empty.
                if uid:
                    try:
                        uid = _identity._validate_uuid(uid)
                    except Refused as _exc:
                        _click.secho(f"refused: {_exc}", fg="red", err=True)
                        sys.exit(5)
                from botainer.state import dir as _state_dir
                # ensure_user_state_dir(create_if_missing=True) so the
                # plugin subprocess can find/create state if it needs to.
                _state_dir.ensure_user_state_dir(create_if_missing=True)
                # AC7 review defense-in-depth: when there is no
                # project-id (uid==""), subprocess_state_env(None) does NOT
                # set BOTAINER_PROJECT_UUID, so the `{**os.environ}` base below
                # would silently FORWARD any attacker-set BOTAINER_PROJECT_UUID
                # already in the launcher's environment — bypassing the
                # validation above. Drop it so the source layer never forwards
                # an unvalidated uuid (downstream __post_init__ also rejects).
                _inherited_uuid_drop = {} if uid else {"BOTAINER_PROJECT_UUID": ""}
                # Use the single helper. The previous code set
                # BOTAINER_STATE_DIR=paths.root (user-wide root), which
                # contradicted composition.py's usage (per-project) and
                # made any hook trying to derive a per-project path
                # silently wrong. Canonical now: STATE_ROOT = user-wide,
                # STATE_DIR = per-project.
                env = {
                    **os.environ,
                    **_inherited_uuid_drop,
                    **_state_dir.subprocess_state_env(uid or None),
                    "BOTAINER_PROJECT_ROOT": str(project_root),
                    "BOTAINER_PROFILE": os.environ.get("BOTAINER_PROFILE", "default"),
                    "BOTAINER_PLUGIN": _plugin,
                    "BOTAINER_COMMAND": _verb,
                }
                if not _script.exists():
                    _click.secho(
                        f"refused: plugin {_plugin}: command script "
                        f"{_script} not found",
                        fg="red",
                        err=True,
                    )
                    sys.exit(5)
                if _script.suffix == ".py":
                    # Always use the SAME Python interpreter as botainer
                    # itself, regardless of the script's executable bit.
                    # The shebang's `/usr/bin/env python3` resolves to the
                    # system interpreter on HPC clusters like Yale Grace
                    # — which typically does NOT have botainer's
                    # dependencies (pyyaml, pydantic, click) installed.
                    # Real-host failure: `botainer hpc submit`
                    # died with `ModuleNotFoundError: No module named
                    # 'yaml'` because the plugin's submit.py ran under
                    # /usr/bin/python3 instead of botainer's venv python.
                    argv = [sys.executable, str(_script), *extra_args]
                elif not os.access(_script, os.X_OK):
                    _click.secho(
                        f"refused: plugin {_plugin}: command script "
                        f"{_script} not executable",
                        fg="red",
                        err=True,
                    )
                    sys.exit(5)
                else:
                    argv = [str(_script), *extra_args]
                rc = _sub.call(argv, env=env)
                sys.exit(rc)

            return _handler

        out[cmd_decl.name] = _make_handler(cmd_decl.name, script_path, plugin_name, help_text)
    return out
