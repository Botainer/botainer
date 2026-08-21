#!/bin/bash
# botainer-viewer-start — the in-agent browser VIEWER stack.
#
# Runs INSIDE the agent container (started by entrypoint_wrap.sh when
# BOTAINER_BROWSER_VIEWER=1). Pipeline:  Xvfb (virtual screen) -> x11vnc (share
# it) -> websockify/noVNC (serve it as a web page). The agent's playwright-mcp
# launches a HEADED Chromium into the SAME DISPLAY separately (see the browser
# plugin) — this script does NOT launch Chromium. Human and agent then drive the
# one browser; the human reaches noVNC (docker: a loopback port; HPC: ssh -L to a
# node-local 0700 socket) and can watch + click + log in.
#
# Two transports (Fable-5 review C1/C2 — apptainer shares the node netns, so a TCP
# port there is reachable by node co-tenants and would hand them input control):
#   BOTAINER_VIEWER_MODE=tcp  (docker):  x11vnc on 127.0.0.1:5900 (private to the
#       container netns) + websockify on 0.0.0.0:<port> INSIDE the container with a
#       MANDATORY TokenFile token. It must bind 0.0.0.0 (not container-loopback):
#       docker publishes the port with `-p 127.0.0.1:<hostport>:<port>` on a bridge
#       network, which DNATs to the container's *bridge* IP — a listener on the
#       container's 127.0.0.1 is NOT on that IP and would refuse the connection. The
#       host side stays loopback-only (the `-p 127.0.0.1:` bind); the token gates
#       the ws upgrade; the container has its own netns. (A malicious page in the
#       agent's own Chromium could also hit the port — hence the mandatory token.)
#   BOTAINER_VIEWER_MODE=unix (apptainer): x11vnc AND websockify on 0700 UNIX
#       SOCKETS only — NO TCP anywhere. The socket perms are the boundary; the
#       token is advisory. Refuses to fall back to TCP.
set -euo pipefail

DISPLAY_NUM="${BOTAINER_VIEWER_DISPLAY_NUM:-99}"
GEOMETRY="${BOTAINER_VIEWER_GEOMETRY:-1280x1024x24}"
MODE="${BOTAINER_VIEWER_MODE:-}"                    # tcp (docker) | unix (apptainer)
GATEWAY="${BOTAINER_VIEWER_GATEWAY:-}"              # 1 = Track B: RFB-only stack
SOCK_DIR="${BOTAINER_VIEWER_SOCKET_DIR:-/run/viewer}"
TOKEN_FILE="${BOTAINER_VIEWER_TOKEN_FILE:-/run/viewer-token}"
RFB_PASS_FILE="${BOTAINER_VIEWER_RFB_PASS_FILE:-/run/viewer-rfb-pass}"
NOVNC_PORT="${BOTAINER_VIEWER_NOVNC_PORT:-6080}"    # legacy tcp mode only
VNC_PORT=5900                                       # tcp modes (container-local)
NOVNC_WEB="${BOTAINER_VIEWER_NOVNC_WEB:-/usr/share/novnc}"
export DISPLAY=":${DISPLAY_NUM}"

log() { echo "[botainer-viewer] $*" >&2; }

# FAIL CLOSED on the transport (Fable-5 M1): NEVER default to tcp. An unset MODE
# on an apptainer node would otherwise open TCP 5900/6080 on a shared, multi-tenant
# netns — reachable (and input-controllable) by co-tenants. The hook always sets
# BOTAINER_VIEWER_MODE; if it's missing, something is wrong — refuse, don't guess.
case "${MODE}" in
    tcp|unix) ;;
    *) log "FATAL: BOTAINER_VIEWER_MODE must be 'tcp' or 'unix' (got '${MODE:-<unset>}')" \
            "— refusing to guess a transport."; exit 1 ;;
esac

pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

# ── 1. Virtual screen. -nolisten tcp = X reachable only via its unix socket. ──
log "Xvfb ${DISPLAY} (${GEOMETRY})"
Xvfb "${DISPLAY}" -screen 0 "${GEOMETRY}" -nolisten tcp &
pids+=($!)
for _ in $(seq 1 60); do xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1 && break; sleep 0.1; done
xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1 \
    || { log "FATAL: Xvfb did not come up on ${DISPLAY}"; exit 1; }

# ── Track B GATEWAY mode: the container speaks ONLY RFB. ──
# No noVNC, no websockify, no web content served from the cage — the LAPTOP
# serves the (vendored, pinned) viewer page and bridges to this RFB endpoint
# (`botainer plugin browser gateway`). x11vnc must AUTHENTICATE here: unlike
# the legacy path (x11vnc private behind an in-container websockify token),
# this endpoint is published to the laptop (docker) / ssh-forwarded (HPC), so
# -nopw would hand view+input to any local process on the user's machine.
# FAIL CLOSED if the per-session password bind is missing.
if [ "${GATEWAY}" = "1" ]; then
    # Non-EMPTY, not merely readable (adversarial review MEDIUM-1): an empty
    # password file could degrade x11vnc to effectively unauthenticated, and
    # 5900 is already published by compose. Mirrors the legacy path's
    # non-empty token gate.
    RFB_PASS="$(head -n1 "${RFB_PASS_FILE}" 2>/dev/null | tr -d '\r\n')"
    [ -n "${RFB_PASS}" ] || {
        log "FATAL: gateway mode but no RFB password at ${RFB_PASS_FILE};"
        log "refusing to serve an unauthenticated RFB endpoint."
        exit 1
    }
    unset RFB_PASS    # only needed for the non-empty check; x11vnc reads the file
    if [ "${MODE}" = "unix" ]; then
        # HPC/apptainer: RFB on a 0700 unix socket only (no TCP on the shared
        # netns). The socket perms are the primary gate; the password ALSO
        # applies (it gates the laptop end of the user's ssh forward).
        mkdir -p "${SOCK_DIR}"; chmod 0700 "${SOCK_DIR}"
        VNC_SOCK="${SOCK_DIR}/vnc.sock"
        log "x11vnc (gateway) on unix socket ${VNC_SOCK} (no TCP, passwd required)"
        x11vnc -display "${DISPLAY}" -unixsock "${VNC_SOCK}" -rfbport 0 \
            -forever -shared -passwdfile "${RFB_PASS_FILE}" -quiet &
        vnc_pid=$!
        pids+=("${vnc_pid}")
        _up=0
        for _ in $(seq 1 100); do            # ~10s
            [ -S "${VNC_SOCK}" ] && { _up=1; break; }
            kill -0 "${vnc_pid}" 2>/dev/null || break
            sleep 0.1
        done
        [ "${_up}" = "1" ] \
            || { log "FATAL: gateway x11vnc socket ${VNC_SOCK} did not come up"; exit 1; }
        log "viewer READY (gateway RFB socket ${VNC_SOCK})"
    else
        # docker: RFB on ${VNC_PORT} inside the container netns. NOT -localhost:
        # docker publishes with `-p 127.0.0.1:<host>:5900` on a bridge network,
        # which DNATs to the container's bridge IP — a container-loopback
        # listener would refuse (same constraint as the legacy websockify
        # listen). Host exposure stays loopback-only via the publish; the
        # password gates it (mandatory, checked above).
        log "x11vnc (gateway) on ${VNC_PORT} (container netns; host publish is loopback; passwd required)"
        x11vnc -display "${DISPLAY}" -rfbport "${VNC_PORT}" \
            -forever -shared -passwdfile "${RFB_PASS_FILE}" -quiet &
        vnc_pid=$!
        pids+=("${vnc_pid}")
        _up=0
        for _ in $(seq 1 100); do            # ~10s
            (exec 3<>"/dev/tcp/127.0.0.1/${VNC_PORT}") 2>/dev/null \
                && { exec 3>&-; _up=1; break; }
            kill -0 "${vnc_pid}" 2>/dev/null || break
            sleep 0.1
        done
        [ "${_up}" = "1" ] \
            || { log "FATAL: gateway x11vnc did not listen on ${VNC_PORT}"; exit 1; }
        log "viewer READY (gateway RFB :${VNC_PORT})"
    fi
    wait "${vnc_pid}"      # block on the RFB endpoint; PID tracks its liveness
    exit $?
fi

TOKEN=""
[ -r "${TOKEN_FILE}" ] && TOKEN="$(tr -d '\r\n' < "${TOKEN_FILE}")"

if [ "${MODE}" = "unix" ]; then
    # ── HPC / apptainer: 0700 UNIX SOCKETS end to end, zero TCP listeners. ──
    mkdir -p "${SOCK_DIR}"; chmod 0700 "${SOCK_DIR}"
    VNC_SOCK="${SOCK_DIR}/vnc.sock"
    NOVNC_SOCK="${SOCK_DIR}/novnc.sock"
    # x11vnc on a unix socket ONLY (-rfbport 0 = no TCP). x11vnc >= 0.9.13.
    log "x11vnc on unix socket ${VNC_SOCK} (no TCP)"
    x11vnc -display "${DISPLAY}" -unixsock "${VNC_SOCK}" -rfbport 0 \
        -forever -shared -nopw -quiet &
    pids+=($!)
    for _ in $(seq 1 60); do [ -S "${VNC_SOCK}" ] && break; sleep 0.1; done
    [ -S "${VNC_SOCK}" ] || { log "FATAL: x11vnc unix socket not created"; exit 1; }
    # websockify on a unix socket, bridging to the x11vnc unix socket. Needs
    # websockify >= 0.12 for --unix-listen (Debian's 0.10 lacks it; the image
    # pip-installs a newer one). Token can't map to a unix target, and the 0700
    # socket is the real gate, so no token enforcement here (advisory only).
    log "noVNC on unix socket ${NOVNC_SOCK} (0700) — reach via ssh -L"
    websockify --web "${NOVNC_WEB}" \
        --unix-listen="${NOVNC_SOCK}" --unix-target="${VNC_SOCK}" &
    ws_pid=$!
    pids+=("${ws_pid}")
    # Health-check the HUMAN-FACING endpoint, not just the display (Fable-5 M4):
    # a viewer:true session whose noVNC never binds would otherwise look "ready"
    # while being un-watchable, with nothing to tell the user on HPC.
    _nv_up=0
    for _ in $(seq 1 100); do            # ~10s
        [ -S "${NOVNC_SOCK}" ] && { _nv_up=1; break; }
        kill -0 "${ws_pid}" 2>/dev/null || break
        sleep 0.1
    done
    [ "${_nv_up}" = "1" ] \
        || { log "FATAL: noVNC unix socket ${NOVNC_SOCK} did not come up"; exit 1; }
    log "viewer READY (unix socket ${NOVNC_SOCK})"
    wait "${ws_pid}"       # block on the endpoint; PID tracks its liveness
fi

# ── docker: TCP inside the container netns (private), token MANDATORY. ──
if [ -z "${TOKEN}" ]; then
    log "FATAL: no viewer token at ${TOKEN_FILE}; refusing to serve an "
    log "unauthenticated VNC on the docker path."
    exit 1
fi
log "x11vnc on 127.0.0.1:${VNC_PORT} (container-local)"
x11vnc -display "${DISPLAY}" -localhost -rfbport "${VNC_PORT}" \
    -forever -shared -nopw -quiet &
pids+=($!)
_vnc_up=0
for _ in $(seq 1 60); do
    (exec 3<>"/dev/tcp/127.0.0.1/${VNC_PORT}") 2>/dev/null \
        && { exec 3>&-; _vnc_up=1; break; }
    sleep 0.1
done
# Assert x11vnc actually bound (symmetry with the unix path). Without this,
# websockify would come up over a dead VNC target and the human would get a
# connected-but-black viewer with no loud failure.
[ "${_vnc_up}" = "1" ] || { log "FATAL: x11vnc did not listen on ${VNC_PORT}"; exit 1; }
# websockify with a TokenFile-ENFORCED token: a connection whose ?token= lookup
# fails is rejected. The token maps to the loopback x11vnc TARGET (container-local
# 127.0.0.1:5900). The LISTEN binds 0.0.0.0 so docker's `-p 127.0.0.1:<hostport>:
# <port>` publish (DNAT to the container bridge IP) can reach it — a container-
# loopback listen would refuse. The human's URL rides the token on the ws PATH
# (browser watch renders it).
TF="$(mktemp)"
printf '%s: 127.0.0.1:%s\n' "${TOKEN}" "${VNC_PORT}" > "${TF}"
chmod 0600 "${TF}"
log "noVNC on 0.0.0.0:${NOVNC_PORT} (container netns; host publish is loopback) — token REQUIRED"
websockify --web "${NOVNC_WEB}" \
    --token-plugin TokenFile --token-source "${TF}" \
    "0.0.0.0:${NOVNC_PORT}" &
ws_pid=$!
pids+=("${ws_pid}")
# Health-check the human-facing noVNC endpoint too (Fable-5 M4), not just the
# display — otherwise a websockify that fails to bind leaves a session that looks
# "viewer ready" but can't actually be watched.
_nv_up=0
for _ in $(seq 1 100); do            # ~10s
    (exec 3<>"/dev/tcp/127.0.0.1/${NOVNC_PORT}") 2>/dev/null \
        && { exec 3>&-; _nv_up=1; break; }
    kill -0 "${ws_pid}" 2>/dev/null || break
    sleep 0.1
done
[ "${_nv_up}" = "1" ] || { log "FATAL: noVNC did not listen on ${NOVNC_PORT}"; exit 1; }
log "viewer READY on 0.0.0.0:${NOVNC_PORT}"
wait "${ws_pid}"          # block on the endpoint; PID tracks its liveness
