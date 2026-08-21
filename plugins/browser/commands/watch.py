#!/usr/bin/env python3
"""`botainer plugin browser watch` — print how to open the live browser viewer.

Re-architected: the viewer stack (Xvfb -> x11vnc -> noVNC) now runs
INSIDE the agent's own container (no separate helper container, no CDP). The agent
drives its OWN local HEADED Chromium; only the VNC *view* crosses to the human.
This command finds the running session's viewer coordinates and prints the exact
one-liner to reach it:

  - docker (laptop): noVNC is published on a 127.0.0.1 host port (chosen at
    compose, recorded in `spec.port_forwards`). Open the URL directly — no SSH.
  - apptainer (HPC): noVNC listens on a node-local 0700 unix socket (no routable
    port on the shared node). Reach it with `ssh -L <port>:<socket> <node>`, via a
    jump host if needed, then open the URL. A node-neighbor can't touch the 0700
    socket. NOTE: the compute node + host-side socket path are only known at JOB
    START (compose runs on the login node) — a node-local splice is the HPC-phase
    follow-up; until then the printed remote command is best-effort.

Connection guards, in order of what actually enforces access TODAY:
  - PRIMARY: the transport. HPC = a 0700 unix socket reached only through YOUR ssh
    forward (no routable port on the node). Laptop = a 127.0.0.1 publish on your
    own single-user machine.
  - SECONDARY: a per-session ~192-bit token that rides the noVNC WebSocket PATH
    (`path=websockify?token=...`). On DOCKER this is ENFORCED — websockify's
    TokenFile plugin rejects a wrong/absent token. On HPC/apptainer the unix-socket
    transport has NO TokenFile (the token can't map to a unix target), so there the
    token is ADVISORY and the 0700 socket perms are the only gate.
The classic VNC password is NOT relied on either way.

Only the rendering is here (pure + unit-tested); starting the viewer is the agent
entrypoint's job (BOTAINER_BROWSER_VIEWER=1 -> botainer-viewer-start).
"""
from __future__ import annotations

import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

# In-container noVNC port (matches BOTAINER_VIEWER_NOVNC_PORT / viewer-start.sh /
# the "browser viewer (noVNC)" PortForward composed in _resolve_port_forwards).
_CONTAINER_NOVNC_PORT = 6080
_VIEWER_FORWARD_LABEL = "browser viewer (noVNC)"


def _viewer_url(local_port: int, token: str) -> str:
    """The noVNC URL. The token rides the WebSocket PATH — websockify's TokenFile
    plugin reads `?token=` off the ws path and REJECTS a wrong/absent one, so this
    is an enforced gate, not advisory. `%3F`/`%3D` encode the `?`/`=` so noVNC
    passes `websockify?token=<T>` as the literal ws path. 127.0.0.1 (never
    `localhost`, which can resolve to ::1 and miss a v4-only publish)."""
    return (
        f"http://127.0.0.1:{local_port}/vnc.html"
        f"?autoconnect=1&resize=scale&path=websockify%3Ftoken%3D{token}"
    )


def _linkify(url: str, hyperlink: bool) -> str:
    """Make the URL clickable via the OSC-8 terminal-hyperlink escape when we're
    on a TTY that the caller says supports it — so the user can just click it
    instead of copy-pasting. The URL is ALSO the visible text, so terminals
    without OSC-8 support (e.g. macOS Terminal.app) still show the plain URL and
    auto-linkifiers can pick it up. Off (plain) by default → clean for pipes/tests."""
    if not hyperlink:
        return url
    return f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\"


def _security_warning(color: bool) -> str:
    """The trust-inversion warning shown EVERY time the link is created. Opening the
    viewer connects the user's OWN browser to a server running INSIDE the agent's
    container — an untrusted sandbox. The container owns its end, so NOTHING set
    server-side (clipboard rules, etc.) is a real boundary against a compromised
    agent/page; the browser you connect WITH is served code by the container and is
    the real trust surface. Say so, loudly, in red on a TTY. (User directive
: connect-your-client-to-a-container-served-server capabilities MUST
    carry a very clear warning in a very clear place. The proper fix — a host-side
    TRUSTED renderer/gateway that serves known-good assets and brokers only the
    pixel/input stream — is a deferred TODO; until then this warning is the guard.)"""
    red = "\x1b[1;31m" if color else ""
    off = "\x1b[0m" if color else ""
    body = (
        "━━━━━━━━━━━━━━ ⚠  SECURITY — READ BEFORE OPENING  ⚠ ━━━━━━━━━━━━━━\n"
        "Opening this connects YOUR browser to a server running INSIDE the agent's\n"
        "container — an UNTRUSTED sandbox. The container controls its own end, so\n"
        "NOTHING set there (clipboard rules, etc.) is a real boundary. The page you\n"
        "load is code the container serves to your browser; a compromised agent or web\n"
        "page could try to read your clipboard (it crosses BOTH ways), reach other\n"
        "services on your machine (127.0.0.1), pop file/device permission dialogs,\n"
        "phish you, or attack the browser itself. Your BROWSER's sandbox is the real\n"
        "protection here — the container's settings are not.\n"
        "  • Open it in a THROWAWAY / private browser window, not your main profile\n"
        "    (clicking opens your DEFAULT browser — paste into a private window if you\n"
        "    care). Best on a machine without sensitive 127.0.0.1 services running.\n"
        "  • Decline any clipboard / file / device permission prompts it raises; don't\n"
        "    copy secrets while it's open.\n"
        "  • Do NOT point a native VNC client at it (those auto-sync the clipboard and\n"
        "    have a history of malicious-server→client exploits).\n"
        "If you only need to SEE the page, asking Claude for a screenshot avoids the\n"
        "live browser↔container connection entirely (it's a read-only image). That's\n"
        "LOWER-RISK than this interactive viewer, but NOT risk-free: any file the\n"
        "agent produces is untrusted, so opening it on your host is its own (smaller)\n"
        "trust boundary.\n"
        "Proceed only if you accept driving an untrusted container's browser.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return f"{red}{body}{off}"


def render_connect_instructions(
    *,
    is_local: bool,
    node: str,
    login: str | None,
    socket_path: str,
    token: str,
    local_port: int = 6080,
    listening: bool | None = None,
    hyperlink: bool = False,
    color: bool = False,
) -> str:
    """The user-facing instructions for reaching the viewer. Pure so it's testable.

    `listening` is a best-effort liveness hint for the local case: True/False from
    a loopback probe, or None if not probed. `hyperlink` wraps the URL in an OSC-8
    terminal hyperlink (clickable); `color` red-highlights the security warning.
    Both default off so pipes/tests get clean plain text."""
    sec = _security_warning(color) + "\n"          # ALWAYS shown, both transports
    url = _linkify(_viewer_url(local_port, token), hyperlink)
    # The viewer's screen is BLANK until Chromium actually launches — playwright-mcp
    # starts it lazily on the agent's FIRST browser tool-call. Say so, so an empty
    # black VNC canvas doesn't read as "broken".
    blank = ("The view is BLANK until the agent opens its first page (Chromium "
             "starts on the\nfirst browser action) — an empty screen is normal "
             "until then.")
    if is_local:
        warn = ""
        if listening is False:
            warn = (
                "NOTE: nothing is accepting connections on that port yet. The "
                "viewer starts\nwith the agent container — give it a few seconds "
                "after `botainer start`, or\ncheck the session is running "
                "(`botainer status`).\n"
            )
        return (
            f"{sec}"
            "Live browser viewer (local / docker):\n"
            f"{warn}"
            f"  open:  {url}\n"
            f"{blank}\n"
            "The viewer is on your own machine's loopback (127.0.0.1). The token "
            "in the URL\nis REQUIRED by websockify; still, keep the port on "
            "loopback (don't forward or\npublish it) — that loopback bind is the "
            "primary gate."
        )
    # Remote (HPC/apptainer): forward the 0700 socket over SSH (via the login/jump
    # host if given), so the local port on YOUR laptop maps to the node-local sock.
    jump = f"-J {login} " if login else ""
    # HPC-parity audit (C1): the node is unknown when the session was
    # composed on the LOGIN node (sbatch compose-at-submit) — it is only settled
    # once the job lands. Emitting `ssh -L <port>:<sock>` with no host produced a
    # malformed command that fails confusingly. State the fact and point at where
    # the real node appears instead (loud > silently broken).
    if not node:
        step1 = (
            "  1) on your laptop:  ssh <compute-node> "
            f"-L {local_port}:{socket_path}\n"
            "     ⚠ the compute node is NOT known yet — this session was composed\n"
            "       before the job landed. Get the node with `botainer status` (or\n"
            "       `squeue -j <jobid> -o %N`) and substitute it above.\n"
        )
    else:
        step1 = f"  1) on your laptop:  ssh {jump}{node} -L {local_port}:{socket_path}\n"
    return (
        f"{sec}"
        "Live browser viewer (remote / HPC):\n"
        "  ⚠ HPC viewer transport is PROVISIONAL: the compute node + node-local\n"
        "    socket path are only finalized at job start, so the node/path below\n"
        "    may need substituting (see docs/CAPABILITY-SURFACE.md §4av).\n"
        f"{step1}"
        f"  2) open in your browser:  {url}\n"
        f"{blank}\n"
        "Access is gated by the 0700 socket (no open port on the node) reached "
        "only through\nYOUR ssh forward. On HPC the URL token is ADVISORY only (no "
        "TokenFile on the unix\ntransport — the socket perms are the real gate), so "
        "keep your ssh forward private.\nClose the SSH connection to close the viewer."
    )


def _read_token(token_file: str) -> str:
    """Read the per-session token the host hook wrote (0600); never invent one.

    The token is embedded verbatim into the noVNC URL, which is wrapped in OSC-8 /
    ANSI escapes and printed to the operator's terminal. `secrets.token_urlsafe`
    only ever emits `[A-Za-z0-9_-]`, so REJECT anything else (Fable-5 defense in
    depth): a token file carrying `\\x1b` could otherwise inject terminal escapes.
    Not agent-reachable today (the token bind is read-only), but this keeps it safe
    against the tracked HPC node-local token/socket splice."""
    with open(token_file, encoding="utf-8") as fh:
        token = fh.read().strip()
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("viewer token has an unexpected format; refusing")
    return token


def _port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    """Best-effort: is something accepting TCP connections on host:port? Used only
    to enrich the local message; never fatal (probe errors -> None upstream)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def docker_host_novnc_port(spec: dict[str, Any]) -> int | None:
    """Find the browser-viewer noVNC host port compose published, from the session
    record's serialized `spec.port_forwards`. Match the labelled forward first,
    else fall back to the one targeting the in-container noVNC port. Returns None
    if absent (headless, or apptainer where no forward is added). Pure/testable."""
    forwards = spec.get("port_forwards") or []
    if not isinstance(forwards, (list, tuple)):
        return None
    by_label = [f for f in forwards if isinstance(f, dict)
                and f.get("label") == _VIEWER_FORWARD_LABEL]
    by_port = [f for f in forwards if isinstance(f, dict)
               and int(f.get("container_port", 0)) == _CONTAINER_NOVNC_PORT]
    for f in (by_label or by_port):
        try:
            return int(f["host_port"])
        except (KeyError, ValueError, TypeError):
            continue
    return None


def render_kwargs_from_handle(
    handle: dict[str, Any],
    *,
    login: str | None,
    host_novnc_port: int | None = None,
    default_port: int = 6080,
) -> dict[str, Any]:
    """Map a `runtime_handle.browser_viewer` record (+ the docker host port read
    from spec.port_forwards) into render_connect_instructions kwargs. Pure. Raises
    KeyError/ValueError on a malformed handle so the caller can fall through.

      docker    → {transport: tcp,  container_novnc_port, token_file}
                  host port comes from spec.port_forwards (host_novnc_port)
      apptainer → {transport: unix, novnc_sock, node, token_file}
    """
    transport = handle.get("transport")
    if transport == "tcp":
        port = host_novnc_port or handle.get("novnc_port")
        if not port:
            raise KeyError("docker viewer host port missing from spec.port_forwards")
        return {
            "is_local": True, "node": "", "login": None,
            "socket_path": "", "local_port": int(port),
        }
    # Remote apptainer: forward the node-local 0700 socket.
    return {
        "is_local": False,
        "node": str(handle.get("node") or ""),
        "login": login,
        "socket_path": str(handle["novnc_sock"]),
        "local_port": default_port,
    }


def _load_running_viewer_record() -> Any | None:
    """Find the running session's record (the one with a browser viewer armed).

    Runs on the host/login node (not in the container), so it reads the same
    per-project session records `botainer status`/`nudge` use. Best-effort:
    returns None on any resolution failure so the command degrades cleanly."""
    state_dir = os.environ.get("BOTAINER_STATE_DIR")
    if not state_dir:
        return None
    sessions_dir = Path(state_dir) / "sessions"
    if not sessions_dir.is_dir():
        return None
    try:
        from botainer.state import session_record
        from botainer.state.liveness import is_session_alive
    except Exception:  # pragma: no cover - botainer import path
        return None
    try:
        records = session_record.list_sessions(sessions_dir)
    except Exception:
        return None
    with_viewer = [r for r in records if _viewer_handle(r) is not None]
    if not with_viewer:
        return None
    # Prefer a session that is actually alive; else the most-recent record.
    for r in with_viewer:
        try:
            if is_session_alive(r):
                return r
        except Exception:
            continue
    return with_viewer[0]


def _viewer_handle(record: Any) -> dict[str, Any] | None:
    # list_sessions returns SessionRecord; unknown runtime_handle keys are
    # preserved under extra_runtime_handle (session_record forward-compat).
    extra = getattr(record, "extra_runtime_handle", None) or {}
    h = extra.get("browser_viewer")
    return h if isinstance(h, dict) else None


def _viewer_from_env() -> dict[str, Any] | None:
    """Explicit override (tests + power users): BOTAINER_BROWSER_* env vars."""
    token_file = os.environ.get("BOTAINER_BROWSER_TOKEN_FILE", "")
    socket_path = os.environ.get("BOTAINER_BROWSER_VIEWER_SOCK", "")
    is_local = os.environ.get("BOTAINER_BROWSER_LOCAL", "") == "1"
    if not token_file or not (socket_path or is_local):
        return None
    return {
        "token_file": token_file,
        "render": {
            "is_local": is_local,
            "node": os.environ.get("BOTAINER_BROWSER_NODE", ""),
            "login": os.environ.get("BOTAINER_BROWSER_LOGIN") or None,
            "socket_path": socket_path,
            "local_port": int(os.environ.get("BOTAINER_BROWSER_LOCAL_PORT", "6080")),
        },
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    login = os.environ.get("BOTAINER_BROWSER_LOGIN") or None

    # 1. Explicit env override (used by tests + targeting a specific viewer).
    env_v = _viewer_from_env()
    if env_v is not None:
        token_file = env_v["token_file"]
        render = env_v["render"]
    else:
        # 2. The real path: the running session's record.
        record = _load_running_viewer_record()
        if record is None:
            sys.stderr.write(
                "browser watch: no running viewer for this session.\n"
                "Set `plugins.browser.viewer: true` (+ `network.mode: internet`), "
                "start a session, then run this again.\n"
            )
            return 3
        handle = _viewer_handle(record) or {}
        if handle.get("mode") == "gateway":
            # Track B: there is no in-container noVNC to point at — the viewer
            # is served by the trusted laptop-side gateway instead.
            print(
                "This session's viewer runs in GATEWAY mode (the container "
                "exposes only an\nauthenticated RFB stream; your laptop serves "
                "the viewer page). Open it with:\n\n"
                "  botainer plugin browser gateway\n\n"
                # Fact + pointer, no verdict (tips RULE): the pixels shown are
                # still fully container-controlled even in gateway mode.
                "What you'll see is the AGENT's browser — container-produced, "
                "untrusted\ncontent. Tradeoffs: docs/BROWSER.md.")
            return 0
        token_file = str(handle.get("token_file", ""))
        host_port = docker_host_novnc_port(getattr(record, "spec", {}) or {})
        try:
            render = render_kwargs_from_handle(
                handle, login=login, host_novnc_port=host_port)
        except (KeyError, ValueError) as exc:
            sys.stderr.write(f"browser watch: malformed viewer record: {exc}\n")
            return 3

    if not token_file:
        sys.stderr.write("browser watch: viewer record has no token file.\n")
        return 3
    try:
        token = _read_token(token_file)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"browser watch: could not read viewer token: {exc}\n")
        return 3

    # Best-effort liveness probe for the local (docker) case only — enriches the
    # message; never fatal. (Remote is behind SSH; probing here would be wrong.)
    listening = None
    if render.get("is_local"):
        listening = _port_listening(int(render["local_port"]))
    # Red-highlight the security warning + make the URL a clickable OSC-8 hyperlink
    # only on a TTY (keeps pipes/tests plain). The warning TEXT is always present.
    tty = sys.stdout.isatty()
    print(render_connect_instructions(
        token=token, listening=listening, hyperlink=tty, color=tty, **render))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
