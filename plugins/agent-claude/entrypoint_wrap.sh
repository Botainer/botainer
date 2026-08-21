#!/bin/sh
# agent-claude entrypoint wrapper.
#
# Purpose: if AGENT_HINTS.md is mounted (always is, when the launcher
# composes the session), prime Claude Code to read it BEFORE responding
# to user input. Without this, the agent has no awareness of /packages,
# /scratch, the network policy, or the nudge channel.
#
# The wrapper:
# 1. If AGENT_HINTS.md is mounted, passes it to claude via the
#    `--append-system-prompt` flag (Claude Code 2.x).
# 2. Otherwise, exec's claude unchanged.
# 3. Either way, `exec`s — keeps PID 1 = claude so container lifecycle
#    is the agent's lifecycle.
set -e

# #53 / T0-2: the in-cage permission posture flag (claude
# --dangerously-skip-permissions) is appended to the agent argv by the
# LOGIN-NODE / launcher composition (botainer/core/composition.py), NOT here, so
# it applies REGARDLESS of image version — this wrapper only needs to forward
# "$@" to claude (which it already does below). Injecting the flag from
# first-party compose code (not the image, not a plugin) also keeps the
# command_append refusal (#290) intact. See CAPABILITY-SURFACE §4ab. The launcher
# still sets BOTAINER_AGENT_PERMISSIONS in the container env as an
# posture signal the agent can read; the wrapper deliberately does not act on it.

# Watchable browser VIEWER (opt-in). When the browser plugin's pre_session hook
# sets BOTAINER_BROWSER_VIEWER=1, start the in-container viewer stack (Xvfb ->
# x11vnc -> noVNC via /usr/local/bin/botainer-viewer-start) BEFORE claude, and
# export DISPLAY so playwright-mcp launches a HEADED Chromium into it (the human
# then watches/clicks over VNC). Fail LOUD if the display doesn't come up:
# playwright-mcp launches Chromium lazily on the first browser tool-call, so the
# display must be ready first, and a viewer:true session that silently can't be
# watched is worse than refusing (Fable-5 review H4).
if [ "${BOTAINER_BROWSER_VIEWER:-}" = "1" ]; then
    DISPLAY=":${BOTAINER_VIEWER_DISPLAY_NUM:-99}"
    export DISPLAY
    /usr/local/bin/botainer-viewer-start &
    _viewer_pid=$!
    _viewer_ok=0
    i=0
    while [ "$i" -lt 60 ]; do            # ~6s
        if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then _viewer_ok=1; break; fi
        kill -0 "$_viewer_pid" 2>/dev/null || break   # stack died -> stop waiting
        sleep 0.1
        i=$((i + 1))
    done
    if [ "$_viewer_ok" != "1" ]; then
        echo "[agent-claude] FATAL: the browser viewer stack did not come up on" \
             "$DISPLAY. Rebuild the agent image (\`botainer image build" \
             "agent-claude\`) so the viewer tools are present, or set" \
             "plugins.browser.viewer:false to run headless." >&2
        exit 1
    fi
    echo "[agent-claude] browser viewer ready on $DISPLAY —" \
         "run \`botainer plugin browser watch\` to open it." >&2
fi

HINTS_FILE="/workspace/.botainer/AGENT_HINTS.md"

if [ -r "$HINTS_FILE" ]; then
    # Read the hints; prepend a directive so the agent treats them
    # as session-bootstrapping context.
    HINTS_CONTENT="$(cat "$HINTS_FILE")"
    INSTRUCTION="Your container session has special environment hints in /workspace/.botainer/AGENT_HINTS.md — read them before responding to any user prompt. The hints document where packages persist, what network mode is active, and which botainer plugins are enabled. The full content follows:

$HINTS_CONTENT"
    exec claude --append-system-prompt "$INSTRUCTION" "$@"
fi

exec claude "$@"
