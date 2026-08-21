#!/usr/bin/env python3
"""pre_session hook: spawn the credential broker.

The functional replacement for agent-claude-proxy's start_proxy.py. The
CRUCIAL difference: the proxy handed the container an ephemeral
ANTHROPIC_API_KEY, which botainer's credential-leak guard refuses on principle,
so a proxy session never started (T0-3/#59). The broker instead hands the
container a provably-FAKE SENTINEL (core.broker_sentinel.make_sentinel) — which
the leak guard explicitly ALLOWS because it carries no secret — and holds the
real credential host-side in the broker daemon.

What this hook does:
1. Resolve botainer's OWN credential file (shared-auth by default, or the
   per-project profile) — the file `botainer auth login` wrote. NOT the host's
   native ~/.claude / macOS Keychain (unreadable anyway).
2. Mint a per-session sentinel (tenant + nonce).
3. Spawn `python3 -m botainer.broker.daemon_main` as a detached child, on a
   per-session unix socket under the session scratch dir. The daemon reads the
   real credential host-side, injects the real Bearer on the outbound leg, and
   optionally refreshes the OAuth token host-side (writing rotated tokens back
   to botainer's own store — never into the container).
4. Emit the pre_session contribution: bind the socket in + provision
   ANTHROPIC_BASE_URL + the sentinel ANTHROPIC_AUTH_TOKEN.
5. Record the daemon pid in the session record so stop_broker.py can kill it.

Stdout JSON contribution shape:
  {"version": "plugin-contribution-v1",
   "kind": "pre_session",
   "env": {"ANTHROPIC_AUTH_TOKEN": "<sentinel>",
           "ANTHROPIC_BASE_URL": "http+unix:///run/anthropic-broker.sock"},
   "binds": [{"source": "<host socket path>",
              "target": "/run/anthropic-broker.sock",
              "mode": "unix-socket"}],
   "broker_pid": <int>}

Exit codes:
  0 — success
  1 — config / credentials problem (refused; session must not start)
  2 — broker spawn failed
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def _read_plugin_config(project_root: Path) -> dict[str, Any]:
    """Read the agent-claude-broker plugin config from project config.yaml.

    Returns an empty dict if the project has no config or no
    `plugins.agent-claude-broker:` section.
    """
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    plugins = data.get("plugins") or {}
    return (plugins.get("agent-claude-broker") or {})


# A unix-domain socket path must fit in sockaddr_un.sun_path — 108 bytes on
# Linux, 104 on macOS. Use the smaller as the ceiling and leave margin.
_SUN_PATH_MAX = 100


def _resolve_socket_path(state_root: Path, session_id: str) -> Path:
    """Return a bindable unix-socket path that (a) fits sun_path (~104 bytes)
    AND (b) lives where the container runtime can actually bind it.

    Two hard constraints collide:
      * sun_path: a unix socket path has a ~104-byte limit a normal file does
        not. The natural per-session scratch path
        (``<state_root>/state/<uuid>/sessions/<sid>/…``) blows past it — two
        36-char UUIDs alone nearly exhaust the budget.
      * Docker Desktop (macOS/Windows) runs containers in a VM and only bind-
        mounts host paths it SHARES into that VM (``/Users`` etc.). A socket in
        ``$TMPDIR`` (macOS ``/var/folders/…``) is NOT shared → the bind fails
        with "source path does not exist". The state dir IS shared (the session
        binds live there), so a socket there mounts fine.

    Resolution:
      1. PRIMARY: ``<state_root>/run/brk-<sid>.sock`` — skips the deep
         ``state/<uuid>/sessions/<sid>/`` nesting so it fits sun_path, and sits
         on the already-shared state filesystem (works on Docker Desktop, native
         Linux, and apptainer alike). Used whenever state_root is short enough.
      2. FALLBACK (long state_root, e.g. an HPC ``$SCRATCH``): a short per-
         session runtime dir under ``$XDG_RUNTIME_DIR`` → ``$TMPDIR`` → ``/tmp``
         named ``botainer-brk-*`` (stop_broker removes it). Those platforms
         (native Linux / apptainer) have no VM file-share boundary, so ``/tmp``
         is bindable directly.

    Either way the socket is 0600 and bound at ``/run/anthropic-broker.sock``.
    """
    sid = "".join(c for c in session_id if c.isalnum())[:16] or "session"
    run_dir = state_root / "run"
    primary = run_dir / f"brk-{sid}.sock"
    if len(str(primary).encode("utf-8")) <= _SUN_PATH_MAX:
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return primary
    base = (os.environ.get("XDG_RUNTIME_DIR")
            or os.environ.get("TMPDIR")
            or "/tmp")
    # exist_ok=False (L2): the dir name is freshly random (token_hex), so a
    # pre-existing dir of that name is a collision/hijack — fail hard rather than
    # silently reuse an attacker-owned dir (matters on a shared /tmp when
    # XDG_RUNTIME_DIR isn't available). mode 0o700 is host-private.
    short_dir = Path(base) / f"botainer-brk-{secrets.token_hex(8)}"
    short_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    candidate = short_dir / "b.sock"
    if len(str(candidate).encode("utf-8")) > _SUN_PATH_MAX:
        raise SystemExit(
            f"[agent-claude-broker] cannot place a unix socket within the "
            f"{_SUN_PATH_MAX}-byte limit; even {candidate} is too long. Set "
            f"XDG_RUNTIME_DIR or TMPDIR to a short path."
        )
    return candidate


def _free_tcp_port(host: str) -> int:
    """Allocate an ephemeral loopback port for the TCP transport. Binds :0, reads
    the assigned port, and closes — the daemon rebinds it. A brief TOCTOU window
    exists (another process could grab it first); the daemon-exit check below
    turns that into a clear failure rather than a silent hang."""
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _tcp_accepting(host: str, port: int) -> bool:
    """True once the broker is accepting connections on (host, port)."""
    import socket as _socket
    try:
        with _socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


_DEFAULT_UPSTREAM = "https://api.anthropic.com"
# Claude Code's OAuth refresh endpoint + PUBLIC client_id — verified against
# three independent open-source implementations (CLIProxyAPI, achetronic/
# claude-oauth-proxy, meridian) + Anthropic auth docs. Baked as TRUSTED CONSTANTS
# (never from untrusted config): the durable REFRESH token is sent here, so a
# hostile .botainer/config.yaml must not be able to redirect it. We use
# api.anthropic.com (not platform.claude.com) because it is NOT behind the
# Cloudflare managed-challenge WAF that blocks non-browser clients like a daemon.
# The token endpoint wants a JSON body {grant_type,refresh_token,client_id}; the
# response rotates the refresh token (written back atomically — credential_source).
_OAUTH_TOKEN_ENDPOINT = "https://api.anthropic.com/v1/oauth/token"
_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _trusted_destinations(*, testing: bool, env) -> tuple[str, str, str]:
    """Return ``(upstream, token_endpoint, client_id)`` from TRUSTED sources ONLY.

    These are the destinations the broker sends the REAL credential to — the
    upstream gets the access token; the token_endpoint gets the REFRESH token
    (the durable secret). ``.botainer/config.yaml`` is UNTRUSTED (git-shareable,
    agent-adjacent), so it must NEVER decide where a credential is sent: a
    hostile config setting ``token_endpoint: https://evil`` would exfiltrate the
    refresh token. Hence these are PINNED here, not read from plugin config.

    Refresh is ENABLED in production via the baked constants above, so an expired
    access token is refreshed host-side (using the refresh token, which never
    enters the container) instead of failing the session daily. In TESTING mode
    the endpoint/client_id come from the (empty-by-default) override env so unit
    tests stay hermetic (no real network / no consuming a real rotating token);
    a test sets the override to a mock endpoint to exercise refresh.
    """
    if not testing:
        return _DEFAULT_UPSTREAM, _OAUTH_TOKEN_ENDPOINT, _OAUTH_CLIENT_ID
    return (
        env.get("BOTAINER_BROKER_UPSTREAM_OVERRIDE", _DEFAULT_UPSTREAM),
        env.get("BOTAINER_BROKER_TOKEN_ENDPOINT_OVERRIDE", ""),
        env.get("BOTAINER_BROKER_CLIENT_ID_OVERRIDE", ""),
    )


def _resolve_state_root(state_dir: Path) -> Path:
    """state_root from BOTAINER_STATE_ROOT, else derive from state_dir.

    The launcher lays out per-project state as
    <state_root>/state/<uuid>, so state_root = state_dir.parent.parent.
    Prefer the explicit env the launcher sets (matches agent-claude-shared's
    pre_session), fall back to derivation.
    """
    env_root = os.environ.get("BOTAINER_STATE_ROOT")
    if env_root:
        return Path(env_root)
    # state_dir = <root>/state/<uuid> → parents[1] is <root>
    return state_dir.parent.parent


def _resolve_credential_path(
    state_root: Path, profile: str, scope: str, project_uuid: str
) -> Path:
    """Locate botainer's OWN login credential file.

    Delegates to botainer.broker.credential_source.resolve_credential_path so
    the hook and the daemon share ONE source of truth for the path (which in
    turn mirrors the agent-claude / agent-claude-shared pre_session binds). A
    test-only override is gated behind BOTAINER_TESTING=1 so a stale shell
    export can't redirect the read.
    """
    override = (
        os.environ.get("BOTAINER_BROKER_CREDS_PATH_OVERRIDE")
        if os.environ.get("BOTAINER_TESTING") == "1"
        else None
    )
    if override:
        return Path(override)
    from botainer.broker.credential_source import resolve_credential_path
    return resolve_credential_path(
        state_root=state_root,
        mode=scope,
        project_uuid=project_uuid or None,
        profile=profile,
    )


# The ONLY host env vars the broker daemon inherits. Everything else is dropped.
#
# TEST-QUALITY AUDIT (B1): the sibling scrub in the (retired) proxy
# hook was mutation-verified to be DELETABLE with its test still green —
# replacing it with `dict(os.environ)` leaked AWS_SECRET_ACCESS_KEY and
# GITHUB_TOKEN into a long-lived daemon while the suite stayed green, because the
# test only asserted `returncode == 0`. This broker runs the SAME pattern and
# holds the real OAuth credential, so the allowlist is hoisted to a module
# constant and the scrub to a named function: both are now directly assertable
# (tests/unit/test_broker_env_scrub.py) instead of being an inline dict
# comprehension no test can reach.
#
# Add a key here only after confirming it carries no credential surface.
INHERITABLE_ENV_KEYS = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR",
    # The daemon is spawned as `sys.executable -m botainer.broker.daemon_main`,
    # so it must be able to import botainer from the same interpreter/venv.
    "PYTHONPATH", "VIRTUAL_ENV",
})


def safe_inherited_env(environ) -> dict:
    """Host env reduced to INHERITABLE_ENV_KEYS (credential-free by construction).

    Takes `environ` as a parameter rather than reading os.environ directly so a
    test can hand it a hostile environment and assert what survives.
    """
    return {k: v for k, v in environ.items() if k in INHERITABLE_ENV_KEYS}


def main() -> int:
    record_path = os.environ.get("BOTAINER_SESSION_RECORD_PATH")
    if not record_path:
        print("[agent-claude-broker] BOTAINER_SESSION_RECORD_PATH unset",
              file=sys.stderr)
        return 1
    record_path_p = Path(record_path)
    if not record_path_p.exists():
        print(f"[agent-claude-broker] record file missing: {record_path_p}",
              file=sys.stderr)
        return 1
    record = json.loads(record_path_p.read_text(encoding="utf-8"))
    if record.get("schema_version") != 1:
        print(
            f"[agent-claude-broker] unsupported schema_version "
            f"{record.get('schema_version')!r}",
            file=sys.stderr,
        )
        return 1
    session_id = record["session_id"]
    state_dir = Path(record["state_dir"]) if "state_dir" in record else None
    if state_dir is None:
        spec = record.get("spec", {})
        state_dir = Path(spec.get("state_dir", ""))
    if not state_dir or not state_dir.exists():
        print("[agent-claude-broker] state_dir missing from record",
              file=sys.stderr)
        return 1

    project_root = Path(record["project_root"])
    plugin_cfg = _read_plugin_config(project_root)
    scope = str(plugin_cfg.get("credential_scope", "shared"))
    if scope not in {"shared", "isolated"}:
        print(f"[agent-claude-broker] invalid credential_scope {scope!r} "
              f"(want 'shared' or 'isolated')", file=sys.stderr)
        return 1
    profile = os.environ.get("BOTAINER_PROFILE", "default")
    state_root = _resolve_state_root(state_dir)
    project_uuid = (
        record.get("project_uuid")
        or os.environ.get("BOTAINER_PROJECT_UUID")
        or state_dir.name
    )
    creds_path = _resolve_credential_path(state_root, profile, scope, project_uuid)
    if not creds_path.exists():
        hint = (
            "botainer auth login --shared --agent claude"
            if scope == "shared"
            else "botainer plugin agent-claude login"
        )
        print(
            f"[agent-claude-broker] credential file not found at {creds_path}. "
            f"Run `{hint}` first (broker mode reads botainer's own login store, "
            f"not your host credential).",
            file=sys.stderr,
        )
        return 1

    # ── Mint the per-session sentinel (used by BOTH transports) ──
    # tenant = short project-scoped id; nonce random per session so a leaked
    # sentinel from a prior session is instantly distinguishable. The sentinel
    # carries NO secret — it's a placeholder the container holds instead of a
    # real token; on TCP it also doubles as the loopback access token.
    tenant_id = (record.get("project_uuid")
                 or os.environ.get("BOTAINER_PROJECT_UUID")
                 or session_id)[:16]
    tenant_id = "".join(c for c in tenant_id if c.isalnum() or c in "_-") or "session"
    nonce = secrets.token_hex(16)  # 128-bit: the sentinel doubles as the TCP token
    # Import the sentinel maker from the INSTALLED botainer package (the trust
    # anchor). We deliberately do NOT add project_root to sys.path: the project
    # dir is agent-writable and may contain a `botainer/` tree (this is how the
    # repo itself is laid out), which would shadow the real module. A single
    # source of truth for the sentinel format (core.broker_sentinel) also avoids
    # a drifting literal here.
    try:
        from botainer.core.broker_sentinel import make_sentinel
    except ImportError as exc:
        print(f"[agent-claude-broker] cannot import botainer.core "
              f"(broken install?): {exc}", file=sys.stderr)
        return 1
    sentinel = make_sentinel(tenant_id, nonce)

    upstream, token_endpoint, client_id = _trusted_destinations(
        testing=os.environ.get("BOTAINER_TESTING") == "1", env=os.environ
    )

    # ── Choose the transport by runtime ──
    # Docker Desktop (macOS/Windows) cannot bind-mount a host unix socket across
    # its VM, so on docker the broker listens on loopback TCP and the container
    # reaches it via host.docker.internal, GATED by the sentinel as an access
    # token (a loopback port has no uid boundary). On apptainer (HPC) + native
    # Linux a unix socket works natively and is preferred: its 0600 mode is the
    # boundary and nothing binds a port.
    runtime = str(record.get("runtime") or "docker")
    transport_env: dict[str, str] = {}
    contribution_binds: list[dict] = []
    contribution_env_extra: dict[str, str] = {}
    is_unix = runtime == "apptainer"
    sock_path: Path | None = None
    tcp_host, tcp_port = "127.0.0.1", None
    if is_unix:
        sock_dir_override = (
            os.environ.get("BOTAINER_BROKER_SOCKET_DIR")
            if os.environ.get("BOTAINER_TESTING") == "1" else None
        )
        if sock_dir_override:
            sock_dir = Path(sock_dir_override)
            sock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            sock_path = sock_dir / "b.sock"
        else:
            try:
                sock_path = _resolve_socket_path(state_root, session_id)
            except SystemExit as exc:
                print(str(exc), file=sys.stderr)
                return 1
        transport_env["BOTAINER_BROKER_SOCKET"] = str(sock_path)
        # Claude Code speaks HTTP-over-unix-socket via the ANTHROPIC_UNIX_SOCKET
        # env var (a PLAIN PATH) + an http:// base URL — NOT via an
        # `http+unix://` base URL, which is silently DEAD (verified empirically:
        # zero bytes reach the socket, the CLI hangs). Requires Claude Code
        # >= 2.1.118 (the shipped Bun binary's `fetch({unix})`). The base URL
        # must be http:// (https:// would make it attempt TLS-over-UDS).
        # CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 because telemetry/update
        # checks do NOT ride the socket and would otherwise error in a caged,
        # egress-restricted container.
        base_url = "http://localhost"
        contribution_env_extra["ANTHROPIC_UNIX_SOCKET"] = "/run/anthropic-broker.sock"
        contribution_env_extra["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        contribution_binds = [{
            "source": str(sock_path),
            "target": "/run/anthropic-broker.sock",
            "mode": "unix-socket",
        }]
    else:
        try:
            tcp_port = _free_tcp_port(tcp_host)
        except OSError as exc:
            print(f"[agent-claude-broker] could not allocate a loopback TCP "
                  f"port for the broker: {exc}", file=sys.stderr)
            return 2
        transport_env["BOTAINER_BROKER_TCP_HOST"] = tcp_host
        transport_env["BOTAINER_BROKER_TCP_PORT"] = str(tcp_port)
        transport_env["BOTAINER_BROKER_REQUIRED_TOKEN"] = sentinel
        # The container reaches the host broker via host.docker.internal (Docker
        # Desktop provides it automatically; native-Linux Docker needs
        # --add-host host.docker.internal:host-gateway — tracked follow-up).
        base_url = f"http://host.docker.internal:{tcp_port}"

    # State persistence (transport-independent): broker mode is mutually
    # exclusive with the mount plugins, which used to provide BOTH auth AND the
    # per-project state bind (history, sessions, .claude.json). Broker only
    # replaced auth, so without this Claude Code writes state to an ephemeral
    # in-container dir → it "forgets" across sessions. Bind a DEDICATED
    # broker-state dir (never holds a credential — the broker injects that
    # host-side) at /home/agent/.claude, and point CLAUDE_CONFIG_DIR at it.
    broker_state_dir = state_dir / "data" / "agent-claude" / "broker-state" / profile
    broker_state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(broker_state_dir, 0o700)
    except OSError:
        pass
    contribution_binds.append({
        "source": str(broker_state_dir),
        "target": "/home/agent/.claude",
        "mode": "rw",
        "provenance_detail": (
            "agent-claude-broker: per-project Claude Code STATE (history, "
            "sessions, .claude.json). Holds NO credential — broker mode injects "
            "the credential host-side; this dir only persists session state."
        ),
    })

    # Always-on host-side debug log (never in the container): records upstream
    # status / content-type / a short body head so a failing session is
    # diagnosable. Model output, not secrets; request headers (which carry the
    # injected credential) are never logged.
    session_dir = state_dir / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    debug_log = session_dir / "broker-debug.log"

    # Build the daemon env from scratch (don't inherit tainted shell vars); pass
    # through only the minimal runtime vars + the transport + credential vars.
    # We pass the explicit credential file (not --auth-mode) so the pre-spawn
    # existence check and the daemon read the SAME path. The sentinel is
    # provisioned into the CONTAINER (contribution below) — plus, for TCP, to
    # the daemon as the required access token.
    safe_inherits = safe_inherited_env(os.environ)
    # Canonical state env (BOTAINER_STATE_ROOT/STATE_DIR/PROJECT_UUID) via the
    # shared helper — not a locally-built dict. The daemon reads --credential-file
    # explicitly, so these are belt-and-suspenders (they also let the daemon fall
    # back to --auth-mode resolution), but building them the canonical way keeps
    # the subprocess-state-env convention intact.
    from botainer.state.dir import subprocess_state_env
    broker_env = {
        **safe_inherits,
        **subprocess_state_env(project_uuid=project_uuid),
        **transport_env,
        "BOTAINER_BROKER_CREDENTIAL_FILE": str(creds_path),
        "BOTAINER_BROKER_UPSTREAM": upstream,
        "BOTAINER_BROKER_TOKEN_ENDPOINT": token_endpoint,
        "BOTAINER_BROKER_CLIENT_ID": client_id,
        "BOTAINER_BROKER_DEBUG_LOG": str(debug_log),
        # Parent-death detection (mirrors the proxy #108 fix): if the launcher
        # dies without running post_session, the daemon shuts itself down.
        "BOTAINER_LAUNCHER_PID": str(os.getppid()),
    }

    # Capture the daemon's stderr to a host-side 0600 file so that when it fails
    # closed at startup (the common case: expired token, no refresh configured)
    # we can surface the daemon's OWN specific reason instead of a generic guess.
    daemon_err = session_dir / "broker-daemon.err"
    _err_fd = None
    try:
        _err_fd = os.open(str(daemon_err),
                          os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                          0o600)
    except OSError:
        _err_fd = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "botainer.broker.daemon_main"],
            env=broker_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=(_err_fd if _err_fd is not None else subprocess.DEVNULL),
            start_new_session=True,
        )
    except OSError as exc:
        print(f"[agent-claude-broker] broker spawn failed: {exc}",
              file=sys.stderr)
        return 2
    finally:
        if _err_fd is not None:
            os.close(_err_fd)  # Popen dup'd it for the child

    rh = record.setdefault("runtime_handle", {})
    rh["broker"] = {
        "pid": proc.pid,
        "transport": "unix" if is_unix else "tcp",
        "socket_path": str(sock_path) if is_unix else None,
        "tcp_host": None if is_unix else tcp_host,
        "tcp_port": None if is_unix else tcp_port,
        "credential_scope": scope,
        "started_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }
    tmp = record_path_p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, record_path_p)

    # Wait for the listener so the adapter doesn't race it, and fail fast with a
    # clear message if the daemon exits during startup (fail-closed credential).
    def _ready() -> bool:
        return sock_path.exists() if is_unix else _tcp_accepting(tcp_host, tcp_port)

    def _daemon_died() -> int:
        # Surface the daemon's OWN reason (last stderr line) — the common case is
        # an expired access token with no host-side refresh configured. Keep the
        # message short: the launcher truncates hook stderr to 300 chars.
        reason = ""
        try:
            txt = daemon_err.read_text(encoding="utf-8", errors="replace")
            nonempty = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            if nonempty:
                import re as _re
                reason = nonempty[-1]
                reason = reason.removeprefix("broker: ")
                reason = reason.removeprefix("refusing to start: ")
                reason = _re.sub(r"^\[[a-z0-9-]+\]\s*", "", reason)  # drop [category]
        except OSError:
            pass
        reason = reason or ("credential can't produce an access token "
                            "(likely an expired token)")
        # The launcher truncates hook stderr to 300 chars — keep the whole line
        # under that so the actionable hint at the end survives.
        if len(reason) > 170:
            reason = reason[:167] + "..."
        print(
            f"[agent-claude-broker] broker failed: {reason} "
            f"→ run `botainer auth login --shared --agent claude`.",
            file=sys.stderr,
        )
        return 2

    import time as _t
    for _ in range(60):
        if _ready():
            break
        if proc.poll() is not None:
            return _daemon_died()
        _t.sleep(0.05)
    # L4: _ready() can go true because ANOTHER local process won the ephemeral
    # TCP port between _free_tcp_port() and the daemon's bind (the daemon then
    # exits on the bind failure). Confirm OUR daemon is the one listening before
    # handing the container the base_url. (Host is trusted, so this is
    # defense-in-depth against integrity/DoS, not credential leak.)
    if proc.poll() is not None:
        return _daemon_died()

    print(json.dumps({
        "version": "plugin-contribution-v1",
        "kind": "pre_session",
        "env": {
            "ANTHROPIC_AUTH_TOKEN": sentinel,
            "ANTHROPIC_BASE_URL": base_url,
            # Persist Claude Code state to the bound broker-state dir so sessions
            # are remembered (broker mode dropped the mount plugins' state bind).
            "CLAUDE_CONFIG_DIR": "/home/agent/.claude",
            # Transport-specific (unix socket): ANTHROPIC_UNIX_SOCKET +
            # CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC.
            **contribution_env_extra,
        },
        "binds": contribution_binds,
        "broker_pid": proc.pid,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
