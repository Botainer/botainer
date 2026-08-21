#!/bin/sh
# agent-codex entrypoint wrapper: inject AGENT_HINTS so the Codex agent actually
# SEES what botainer set up (job tools, package paths, network mode, MPI).
#
# HOW (verified 2026-07): the current @openai/codex CLI builds its instruction
# chain each run from AGENTS.md files — a GLOBAL `$CODEX_HOME/AGENTS.md` (default
# ~/.codex) MERGED with any project AGENTS.md (never clobbered). So we write our
# hints into a marker-delimited block in the GLOBAL AGENTS.md, preserving the
# user's own instructions and any project AGENTS.md they mounted. Works for both
# `codex` (TUI) and `codex exec`, needs no flag/argv quoting, survives
# `exec codex "$@"`. (The old wrapper only printed the hints PATH to stderr — the
# agent never saw them. CODEX_INITIAL_CONTEXT_FILE was also dead; both removed.)
set -e

HINTS_FILE="/workspace/.botainer/AGENT_HINTS.md"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
GLOBAL_MD="$CODEX_DIR/AGENTS.md"
BEGIN='<!-- BEGIN BOTAINER HINTS (auto-generated each session; do not edit) -->'
END='<!-- END BOTAINER HINTS -->'

_inject() {  # $1 = target AGENTS.md file
    # mktemp yields an UNPREDICTABLE name. Do NOT fall back to a fixed path: this
    # dir persists across sessions and is agent-writable, so a prior compromised
    # session could pre-plant a symlink at a guessable name and the awk redirect
    # below would write THROUGH it (arbitrary-file clobber). If mktemp fails,
    # skip injection rather than write to a predictable path.
    _tmp="$(mktemp "$CODEX_DIR/.agents.XXXXXX")" || return 0
    # Drop any stale botainer block from a previous session (idempotent — the
    # dir may persist), keep everything else the user has. Only read a REGULAR,
    # non-symlink file: `[ -f ]` follows symlinks, so a prior session's planted
    # symlink at $1 would otherwise be read THROUGH (echoing its target's content
    # into the agent-visible AGENTS.md). `! -L` refuses that.
    if [ -f "$1" ] && [ ! -L "$1" ]; then
        awk -v b="$BEGIN" -v e="$END" '$0==b{s=1} !s{print} $0==e{s=0}' \
            "$1" > "$_tmp" || true
    fi
    { printf '\n%s\n' "$BEGIN"; cat "$HINTS_FILE"; printf '%s\n' "$END"; } >> "$_tmp"
    # mv replaces a symlink at $1 rather than following it — safe.
    mv "$_tmp" "$1"
}

if [ -r "$HINTS_FILE" ]; then
    mkdir -p "$CODEX_DIR"
    _inject "$GLOBAL_MD"
    # If the user has a GLOBAL AGENTS.override.md, Codex ignores the plain
    # AGENTS.md (only the first non-empty global file counts) — mirror the block
    # into the override too so the hints are always seen.
    [ -f "$CODEX_DIR/AGENTS.override.md" ] && _inject "$CODEX_DIR/AGENTS.override.md"
fi

# Read API key file into OPENAI_API_KEY if present (mount-mode auth). In BROKER
# mode no key file is mounted, so this is skipped and the sentinel OPENAI_API_KEY
# the broker provisioned survives. Quote the cat arg (the value is launcher-set,
# so this is robustness, not an attacker vector).
if [ -r "${OPENAI_API_KEY_FILE:-/home/agent/.openai/api_key}" ]; then
    export OPENAI_API_KEY="$(cat "${OPENAI_API_KEY_FILE:-/home/agent/.openai/api_key}")"
fi

# #53 / T0-2: codex's in-cage posture flags (--sandbox danger-full-access
# --ask-for-approval never) are appended to the agent argv by the LOGIN-NODE /
# launcher composition, NOT here, so they apply REGARDLESS of image version —
# this wrapper only forwards "$@" to codex. (danger-full-access is REQUIRED under
# apptainer: codex's own bwrap/landlock sandbox can't nest in the cage. The
# launcher chooses it; see composition.py + CAPABILITY-SURFACE §4ab.)
exec codex "$@"
