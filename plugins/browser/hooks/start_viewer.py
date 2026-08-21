#!/usr/bin/env python3
"""pre_session hook — arm the in-agent browser VIEWER (opt-in).

Re-architected (Fable-5-reviewed, see
DN-018). The OLD design started a
SEPARATE helper container and made the agent reach its Chromium over CDP —
unreliable on every platform (it died on the user's Mac). The NEW design runs the
whole viewer stack INSIDE the agent's own container: the agent drives its OWN
local HEADED Chromium (no cross-container control), and only the VNC *view*
crosses to the human. So this hook no longer starts any container. It:

  1. mints a per-session token (0600 file, bound RO at /run/viewer-token — NEVER
     via --env, which is visible in `ps` on a shared apptainer node),
  2. signals the agent entrypoint to start the viewer stack (env
     BOTAINER_BROWSER_VIEWER=1 + the transport mode),
  3. rewrites the session mcp-servers.json to DROP `--headless` (so playwright-mcp
     launches Chromium HEADED into the viewer's display) — keeping --no-sandbox +
     --executable-path,
  4. (apptainer) contributes a node-local 0700 socket dir bound at /run/viewer,
  5. records viewer coordinates so `botainer plugin browser watch` can print how
     to reach it.

Default (headless) sessions never set BOTAINER_BROWSER_VIEWER, so the entrypoint
does nothing and the mcp_server keeps --headless.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - botainer ships pyyaml
    yaml = None  # type: ignore[assignment]

_SUN_PATH_MAX = 100  # sockaddr_un.sun_path ceiling (108 Linux / 104 mac), margin


# ─────────────────────────── pure, unit-tested ───────────────────────────
def viewer_enabled(plugin_cfg: dict[str, Any]) -> bool:
    """True iff the project opted into the watchable viewer. Default headless."""
    val = plugin_cfg.get("viewer", False)
    return val is True or (
        isinstance(val, str) and val.strip().lower() in {"1", "true", "yes", "on"}
    )


def viewer_mode(plugin_cfg: dict[str, Any]) -> str:
    """`plugins.browser.viewer_mode`: 'gateway' (default) or 'legacy'.

    gateway — the container exposes ONLY an authenticated RFB endpoint and your
              laptop serves the vendored noVNC page (`botainer plugin browser
              gateway`). The container serves no code to your browser.
    legacy  — DEPRECATED. noVNC is served by the container, so your browser
              runs code the untrusted container supplied.

    This is the LOUD validator: a value that is neither raises. The default
    mirrors botainer-plugin.yaml's `viewer_mode.default`, which is the
    authority; a test pins the copies together.
    """
    # Default mirrors botainer-plugin.yaml's `viewer_mode.default`, which is
    # the authority; a test pins the three copies together.
    val = plugin_cfg.get("viewer_mode", "gateway")
    if not isinstance(val, str) or val.strip().lower() not in {"legacy", "gateway"}:
        raise ValueError(
            f"plugins.browser.viewer_mode must be 'legacy' or 'gateway'; got "
            f"{val!r}.")
    return val.strip().lower()


def rewrite_mcp_for_headed(mcp_json_text: str, viewport: str = "1280,1024") -> str:
    """Make the `browser` MCP server launch a HEADED Chromium into the viewer's
    display: drop ONLY `--headless` and set `--viewport-size` to match the virtual
    screen. Everything else — crucially `--no-sandbox` (the cage needs it) and
    `--executable-path /usr/bin/chromium` (the image's browser) — is preserved.

    Returns the text unchanged if there is no `browser` server. Pure/testable."""
    data = json.loads(mcp_json_text)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or "browser" not in servers:
        return mcp_json_text
    browser = servers["browser"]
    args = [a for a in (browser.get("args") or []) if a != "--headless"]
    if "--viewport-size" not in args:
        args += ["--viewport-size", viewport]
    browser["args"] = args
    return json.dumps(data, indent=2, sort_keys=True)


# Container-side path the human's captured session is bound RO at, and where
# playwright-mcp reads it via `--storage-state`. Declared in the plugin manifest's
# contributes.mount_target_prefixes (envelope).
_STORAGE_STATE_TARGET = "/run/browser-auth.json"


def resolve_storage_state(
    plugin_cfg: dict[str, Any], project_root: Path
) -> Path | None:
    """Credential-handoff (Track A): the human logs into a site in THEIR OWN
    browser and hands the resulting Playwright storage-state file to the agent's
    browser — no viewer, no in-container login (which would keylog the password).
    `plugins.browser.storage_state: <path>` names that file (absolute, or relative
    to the project). Returns the validated host path, or None if unset. Raises on a
    bad/unsafe value. Pure/testable.

    SECURITY (Fable-5 HIGH-1): a caged agent can WRITE an agent-writable auth path
    (anything in the project except the RO `/workspace/.botainer`). Without these
    guards it could swap the file for a symlink to a host credential (e.g. the broker
    OAuth refresh token under `~/.botainer/...`) and get it bound in on the next run,
    defeating broker isolation. So: refuse a symlinked path, and for a PROJECT-
    RELATIVE path (the agent-writable case) require the resolved target to stay
    INSIDE the project. (main() additionally COPIES the content into the host-private
    session dir and binds the copy, closing the queue-wait/concurrent TOCTOU.)"""
    raw = plugin_cfg.get("storage_state")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"plugins.browser.storage_state must be a string path; got "
            f"{type(raw).__name__} ({raw!r}). NB unquoted YAML `no`/`off`/`yes` "
            f"parse to booleans — quote the path.")
    if not raw.strip():
        return None
    raw = raw.strip()
    given = Path(raw).expanduser()
    relative = not given.is_absolute()
    p = (project_root / given) if relative else given
    if p.is_symlink():
        raise ValueError(
            f"plugins.browser.storage_state {raw!r} is a symlink; refusing "
            f"(symlink-escape guard).")
    if not p.exists():
        raise FileNotFoundError(
            f"plugins.browser.storage_state points at {raw!r} which does not exist "
            f"({p}). Capture it with `botainer plugin browser login <url>` first.")
    real = p.resolve()
    if not real.is_file():
        raise FileNotFoundError(
            f"plugins.browser.storage_state {raw!r} is not a file ({real}).")
    if relative and not real.is_relative_to(project_root.resolve()):
        raise ValueError(
            f"plugins.browser.storage_state {raw!r} resolves OUTSIDE the project "
            f"({real}); refusing (symlink-escape guard). Use an absolute path if you "
            f"deliberately mean a file outside the project.")
    return real


def rewrite_mcp_add_storage_state(mcp_json_text: str, container_path: str) -> str:
    """Add `--storage-state <container_path>` to the `browser` MCP server so
    playwright-mcp loads the handed-over session. Idempotent (won't duplicate the
    flag). Returns text unchanged if there is no `browser` server. Pure/testable."""
    data = json.loads(mcp_json_text)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or "browser" not in servers:
        return mcp_json_text
    browser = servers["browser"]
    args = list(browser.get("args") or [])
    if "--storage-state" not in args:
        args += ["--storage-state", container_path]
    browser["args"] = args
    return json.dumps(data, indent=2, sort_keys=True)


# ─────────────────────────────── impl ───────────────────────────────
def _read_plugin_config(project_root: Path) -> dict[str, Any]:
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return (data.get("plugins") or {}).get("browser") or {}


def _err(msg: str) -> None:
    print(f"[browser] {msg}", file=sys.stderr)


def _contribution(env: dict[str, str], binds: list[dict]) -> None:
    print(json.dumps({
        "version": "plugin-contribution-v1", "kind": "pre_session",
        "env": env, "binds": binds,
    }))


def main() -> int:
    record_path = os.environ.get("BOTAINER_SESSION_RECORD_PATH")
    if not record_path or not Path(record_path).exists():
        _err("BOTAINER_SESSION_RECORD_PATH missing")
        return 1
    record_path_p = Path(record_path)
    record = json.loads(record_path_p.read_text(encoding="utf-8"))
    if record.get("schema_version") != 1:
        _err(f"unsupported schema_version {record.get('schema_version')!r}")
        return 1

    project_root = Path(record["project_root"])
    plugin_cfg = _read_plugin_config(project_root)
    viewer = viewer_enabled(plugin_cfg)
    try:
        mode = viewer_mode(plugin_cfg)
        storage_host = resolve_storage_state(plugin_cfg, project_root)
    except (FileNotFoundError, ValueError) as exc:
        _err(str(exc))
        return 2
    if not viewer and storage_host is None:
        _contribution({}, [])          # headless, no handoff — nothing to arm
        return 0

    session_id = record["session_id"]
    state_dir = Path(record.get("state_dir")
                     or record.get("spec", {}).get("state_dir", ""))
    if not state_dir or not state_dir.exists():
        _err("state_dir missing from record")
        return 1
    session_dir = state_dir / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    env: dict[str, str] = {}
    binds: list[dict] = []

    # Track A — credential HANDOFF: the browser mcp reads a Playwright session via
    # --storage-state (added below). Independent of the viewer; the human's password
    # never enters the container. We COPY the content into the host-private session
    # dir (O_NOFOLLOW read) and bind THAT copy RO — never the user's file directly —
    # so a caged agent can't swap the source for a symlink to a host credential in a
    # queue-wait/concurrent-session window (Fable-5 HIGH-1; resolve_storage_state
    # already refused symlinks + project-escape at validation).
    if storage_host is not None:
        auth_copy = session_dir / "browser-auth.json"
        try:
            sfd = os.open(str(storage_host), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                data = os.read(sfd, 16 * 1024 * 1024)  # cap; storage-state is small
            finally:
                os.close(sfd)
            dfd = os.open(str(auth_copy),
                          os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(dfd, data)
            finally:
                os.close(dfd)
        except OSError as exc:
            _err(f"could not stage storage_state ({exc}). Refusing.")
            return 2
        binds.append({
            "source": str(auth_copy), "target": _STORAGE_STATE_TARGET, "mode": "ro",
            "provenance_detail": "browser: handed-over session (copied to session dir, RO)",
        })

    # Viewer (opt-in): per-session secret + display transport. What the secret
    # IS depends on the mode:
    #   legacy  → the ~192-bit websockify TokenFile token (in-container noVNC).
    #   gateway → an x11vnc RFB password (VNC auth truncates at 8 bytes; 8
    #             urlsafe chars ≈ 47 bits — a second gate behind the loopback
    #             publish, replacing -nopw now that RFB is laptop-reachable).
    #             The in-container stack serves NO web content in this mode;
    #             the laptop gateway mints its own ws token per run.
    # Both are 0600, O_NOFOLLOW, bound RO under /run/ — NEVER via --env (env is
    # visible in `ps`/inspect on a shared node).
    handle: dict[str, Any] | None = None
    if viewer:
        runtime = str(record.get("runtime") or "docker")
        env["BOTAINER_BROWSER_VIEWER"] = "1"          # entrypoint starts the stack
        handle = {"runtime": runtime, "mode": mode}
        if mode == "gateway":
            rfb_pass = secrets.token_urlsafe(6)        # exactly 8 urlsafe chars
            rfb_pass_file = session_dir / "viewer-rfb-pass"
            fd = os.open(str(rfb_pass_file),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, (rfb_pass + "\n").encode())
            finally:
                os.close(fd)
            env["BOTAINER_VIEWER_GATEWAY"] = "1"       # RFB-only stack, no noVNC
            env["BOTAINER_VIEWER_RFB_PASS_FILE"] = "/run/viewer-rfb-pass"
            binds.append({
                "source": str(rfb_pass_file), "target": "/run/viewer-rfb-pass",
                "mode": "ro",
                "provenance_detail":
                    "browser viewer (gateway): per-session RFB password (0600, RO)",
            })
            handle["rfb_pass_file"] = str(rfb_pass_file)
        else:
            token = secrets.token_urlsafe(24)
            token_file = session_dir / "viewer.token"
            fd = os.open(str(token_file),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, (token + "\n").encode())
            finally:
                os.close(fd)
            env["BOTAINER_VIEWER_TOKEN_FILE"] = "/run/viewer-token"
            binds.append({
                "source": str(token_file), "target": "/run/viewer-token", "mode": "ro",
                "provenance_detail": "browser viewer: per-session token (0600, RO)",
            })
            handle["token_file"] = str(token_file)

        if runtime == "apptainer":
            # HPC: unix sockets end-to-end on a node-local 0700 dir bound at
            # /run/viewer (a socket in the container's private --containall /tmp is
            # invisible to sshd; Fable-5 H5). NOTE: the compute node + resolved
            # socket path are recorded at JOB START (not here — this hook runs on the
            # LOGIN node under compose-at-submit; Fable-5 H6). The host dir here is a
            # placeholder under the session dir; a node-local $SLURM_TMPDIR splice is
            # the follow-up for the HPC test phase.
            sock_dir = session_dir / "viewer"
            sock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if len(str(sock_dir / "novnc.sock")) > _SUN_PATH_MAX:
                _err(f"viewer socket path too long (> {_SUN_PATH_MAX}); shorter state dir")
                return 2
            env["BOTAINER_VIEWER_MODE"] = "unix"
            env["BOTAINER_VIEWER_SOCKET_DIR"] = "/run/viewer"
            binds.append({
                "source": str(sock_dir), "target": "/run/viewer", "mode": "rw",
                "provenance_detail": "browser viewer: 0700 node-local socket dir (HPC)",
            })
            handle.update({"transport": "unix",
                           # `ssh -L` on the node targets the HOST-side socket (the
                           # bind SOURCE), not the container mountpoint — record that
                           # (Fable-5 M2). `/run/viewer/...` exists only inside the
                           # cage. NOTE: under compose-at-submit this runs on the LOGIN
                           # node, so both node and this path are provisional
                           # until job-start recording lands (H5/H6); watch flags it.
                           #
                           # HPC-parity audit (C1): this read HOSTNAME,
                           # which is NOT in botainer/plugins/hooks.py's
                           # _HOOK_ENV_ALLOWLIST — hooks run with a filtered env, so
                           # it was ALWAYS "". That made the whole HPC viewer path
                           # dead: `gateway` refused every session and `watch`
                           # printed a hostless `ssh -L`. SLURMD_NODENAME *is*
                           # allowlisted (hooks.py:58) and is set on the compute
                           # node, so it is correct whenever the hook runs there
                           # (salloc/srun). Under sbatch compose-at-submit the hook
                           # runs on the LOGIN node where neither is set — the node
                           # is genuinely unknown at that point, so "" is the correct
                           # answer and the connect commands must say so rather than
                           # emit a broken command.
                           "node": os.environ.get("SLURMD_NODENAME", ""),
                           "node_provisional": True})
            if mode == "gateway":
                # Gateway forwards to the RAW RFB socket (x11vnc -unixsock);
                # there is no in-container noVNC socket in this mode.
                handle.update({"rfb_sock": str(sock_dir / "vnc.sock"),
                               "rfb_sock_container": "/run/viewer/vnc.sock"})
            else:
                handle.update({"novnc_sock": str(sock_dir / "novnc.sock"),
                               "novnc_sock_container": "/run/viewer/novnc.sock"})
        else:  # docker — TCP inside the container netns, published to host loopback
            # by a compose-time PortForward (S4): legacy publishes the in-container
            # noVNC (6080); gateway publishes the AUTHENTICATED raw RFB (5900) and
            # serves no web content from the container. The chosen host port lands
            # in spec.port_forwards (record); watch/gateway read it by label.
            env["BOTAINER_VIEWER_MODE"] = "tcp"
            if mode == "gateway":
                handle.update({"transport": "tcp", "container_rfb_port": 5900})
            else:
                env["BOTAINER_VIEWER_NOVNC_PORT"] = "6080"   # in-container noVNC port
                handle.update({"transport": "tcp", "container_novnc_port": 6080})

    # Rewrite the session mcp-servers.json (composed at §6b): headed (viewer) and/or
    # --storage-state (handoff). A missing file means the browser mcp_server isn't
    # enabled — refuse (a viewer/handoff with no browser tool is nonsense), fail-loud.
    mcp_path = session_dir / "mcp-servers.json"
    if not mcp_path.exists():
        _err("mcp-servers.json not found — enable the browser mcp_server. Refusing.")
        return 2
    try:
        text = mcp_path.read_text(encoding="utf-8")
        if viewer:
            text = rewrite_mcp_for_headed(text)
        if storage_host is not None:
            text = rewrite_mcp_add_storage_state(text, _STORAGE_STATE_TARGET)
        mcp_path.write_text(text, encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        _err(f"could not rewrite mcp-servers.json ({exc}). Refusing.")
        return 2

    # Fail loud if handoff was requested but there was no `browser` server to carry
    # the flag (else the live-session file is bound but consumed by nothing, and the
    # user thinks they're logged in) — Fable-5 MEDIUM-1.
    if storage_host is not None:
        try:
            b_args = ((json.loads(text).get("mcpServers") or {}).get("browser")
                      or {}).get("args") or []
        except json.JSONDecodeError:
            b_args = []
        if "--storage-state" not in b_args:
            _err("storage_state is set but the `browser` mcp server is absent — the "
                 "handoff has nothing to load into. Enable the browser mcp server. "
                 "Refusing.")
            return 2

    if viewer and handle is not None:
        rh = record.setdefault("runtime_handle", {})
        rh["browser_viewer"] = handle
        tmp = record_path_p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, record_path_p)
        verb = "gateway" if mode == "gateway" else "watch"
        _err(f"viewer armed ({handle['runtime']}, {mode} mode); run "
             f"`botainer plugin browser {verb}` to open it.")
    if storage_host is not None:
        _err("browser: handed-over session bound RO; the agent's browser starts "
             "logged in. Keep that file secret — it's a live session.")

    _contribution(env, binds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
