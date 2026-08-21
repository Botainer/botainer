#!/usr/bin/env python3
"""pre_session hook: spawn the credential broker for Codex (OpenAI).

The OpenAI analog of agent-claude-broker's start_broker.py. It hands the
container a provably-FAKE SENTINEL (core.broker_sentinel.make_sentinel) as
`OPENAI_API_KEY` and holds the real key host-side in the broker daemon (provider
mode `openai` → pinned api.openai.com, API-key credential source).

Two things differ from the Claude broker:
1. TRANSPORT IS TCP-ONLY. The Codex CLI has no unix-socket support (there is no
   OpenAI analog of ANTHROPIC_UNIX_SOCKET), so even under apptainer the broker
   listens on loopback TCP. The container reaches it via 127.0.0.1 (apptainer
   shares the host net namespace) or host.docker.internal (Docker Desktop). A
   loopback port has no uid boundary, so every request MUST carry the per-session
   sentinel as its access token (required_client_token) — the daemon enforces it.
2. NO OAUTH REFRESH. This is API-key mode: the key doesn't expire, so no
   token_endpoint/client_id is passed. ChatGPT-subscription OAuth is the
   documented follow-up (a host-rewrite proxy), not this hook.

Stdout JSON contribution shape:
  {"version": "plugin-contribution-v1",
   "kind": "pre_session",
   "env": {"OPENAI_API_KEY": "<sentinel>",
           "OPENAI_BASE_URL": "http://<host-reachable>:<port>/v1",
           "CODEX_HOME": "/home/agent/.codex"},
   "binds": [{"source": "<broker-state dir>", "target": "/home/agent/.codex",
              "mode": "rw"}],
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

_DEFAULT_UPSTREAM = "https://api.openai.com"
_CHATGPT_UPSTREAM = "https://chatgpt.com"

# The container-side OPENAI_BASE_URL suffix per mode. In API-key mode codex hits
# <base>/responses → the broker forwards to api.openai.com/v1/responses. In
# subscription mode codex hits <base>/responses → the broker forwards to
# chatgpt.com/backend-api/codex/responses (host + auth rewrite; body is identical,
# per the codex source). The path the broker sees IS the base suffix + /responses,
# so the suffix also selects the daemon's pinned path-allowlist.
_MODE_BASEPATH = {"api-key": "/v1", "subscription": "/backend-api/codex"}
_MODE_PROVIDER = {"api-key": "openai", "subscription": "openai-chatgpt"}
_MODE_UPSTREAM = {"api-key": _DEFAULT_UPSTREAM, "subscription": _CHATGPT_UPSTREAM}


def _detect_mode(creds_path: Path) -> str:
    """Which codex auth the login store holds: 'subscription' (ChatGPT OAuth —
    auth.json with a `tokens` object) or 'api-key' (an `api_key` file, or
    auth.json carrying OPENAI_API_KEY). A ChatGPT login can't be served as an API
    key and vice-versa, so the mode is derived from the file, not config."""
    try:
        raw = creds_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "api-key"
    try:
        doc = json.loads(raw)
    except ValueError:
        return "api-key"  # a plain api_key file
    if isinstance(doc, dict) and isinstance(doc.get("tokens"), dict):
        return "subscription"
    return "api-key"


def _read_plugin_config(project_root: Path) -> dict[str, Any]:
    """Read the agent-codex-broker plugin config from project config.yaml."""
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    plugins = data.get("plugins") or {}
    return (plugins.get("agent-codex-broker") or {})


def _trusted_upstream(mode: str, *, testing: bool, env) -> str:
    """The PINNED destination the REAL credential is sent to (api.openai.com for
    API-key mode, chatgpt.com for subscription). `.botainer/config.yaml` is
    untrusted, so this is NEVER a config key: a hostile config `upstream:
    https://evil` would exfiltrate the token. In TESTING mode a mock upstream may
    be injected via the (empty-by-default) override env so unit tests stay
    hermetic; production always pins the real host."""
    default = _MODE_UPSTREAM[mode]
    if not testing:
        return default
    return env.get("BOTAINER_BROKER_UPSTREAM_OVERRIDE", default)


def _resolve_state_root(state_dir: Path) -> Path:
    env_root = os.environ.get("BOTAINER_STATE_ROOT")
    if env_root:
        return Path(env_root)
    return state_dir.parent.parent  # <root>/state/<uuid> → <root>


def _resolve_credential_path(
    state_root: Path, profile: str, scope: str, project_uuid: str
) -> Path:
    """Locate botainer's OWN codex login credential file (shared auth.json or the
    per-project api_key). Delegates to
    broker.openai_credential.resolve_codex_credential_path (single source of
    truth, mirrors the codex pre_session binds). A test-only override is gated
    behind BOTAINER_TESTING=1."""
    override = (
        os.environ.get("BOTAINER_BROKER_CREDS_PATH_OVERRIDE")
        if os.environ.get("BOTAINER_TESTING") == "1"
        else None
    )
    if override:
        return Path(override)
    from botainer.broker.openai_credential import resolve_codex_credential_path
    return resolve_codex_credential_path(
        state_root=state_root,
        mode=scope,
        project_uuid=project_uuid or None,
        profile=profile,
    )


def _free_tcp_port(host: str) -> int:
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _tcp_accepting(host: str, port: int) -> bool:
    import socket as _socket
    try:
        with _socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def main() -> int:
    record_path = os.environ.get("BOTAINER_SESSION_RECORD_PATH")
    if not record_path:
        print("[agent-codex-broker] BOTAINER_SESSION_RECORD_PATH unset",
              file=sys.stderr)
        return 1
    record_path_p = Path(record_path)
    if not record_path_p.exists():
        print(f"[agent-codex-broker] record file missing: {record_path_p}",
              file=sys.stderr)
        return 1
    record = json.loads(record_path_p.read_text(encoding="utf-8"))
    if record.get("schema_version") != 1:
        print(f"[agent-codex-broker] unsupported schema_version "
              f"{record.get('schema_version')!r}", file=sys.stderr)
        return 1
    session_id = record["session_id"]
    state_dir = Path(record["state_dir"]) if "state_dir" in record else None
    if state_dir is None:
        spec = record.get("spec", {})
        state_dir = Path(spec.get("state_dir", ""))
    if not state_dir or not state_dir.exists():
        print("[agent-codex-broker] state_dir missing from record", file=sys.stderr)
        return 1

    project_root = Path(record["project_root"])
    plugin_cfg = _read_plugin_config(project_root)
    scope = str(plugin_cfg.get("credential_scope", "shared"))
    if scope not in {"shared", "isolated"}:
        print(f"[agent-codex-broker] invalid credential_scope {scope!r} "
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
        hint = ("botainer auth login --shared --agent codex" if scope == "shared"
                else "botainer plugin agent-codex login")
        print(f"[agent-codex-broker] credential file not found at {creds_path}. "
              f"Run `{hint}` first (broker mode reads botainer's own codex login "
              f"store, not your host credential).", file=sys.stderr)
        return 1

    # ── Mint the per-session sentinel (used as the container's fake OPENAI_API_KEY
    # AND — because TCP has no uid boundary — as the daemon's required access
    # token). Carries NO secret. ──
    tenant_id = (record.get("project_uuid")
                 or os.environ.get("BOTAINER_PROJECT_UUID")
                 or session_id)[:16]
    tenant_id = "".join(c for c in tenant_id if c.isalnum() or c in "_-") or "session"
    nonce = secrets.token_hex(16)  # 128-bit: the sentinel doubles as the TCP token
    try:
        from botainer.core.broker_sentinel import make_sentinel
    except ImportError as exc:
        print(f"[agent-codex-broker] cannot import botainer.core "
              f"(broken install?): {exc}", file=sys.stderr)
        return 1
    sentinel = make_sentinel(tenant_id, nonce)

    # Which codex auth the store holds decides the whole downstream shape:
    # API-key (→ api.openai.com, /v1) vs ChatGPT subscription (→ chatgpt.com
    # backend, /backend-api/codex, OAuth refresh + chatgpt-account-id).
    mode = _detect_mode(creds_path)
    provider = _MODE_PROVIDER[mode]
    upstream = _trusted_upstream(
        mode, testing=os.environ.get("BOTAINER_TESTING") == "1", env=os.environ)

    # ── TCP transport (always; codex has no unix socket). base_url host differs
    # by runtime: apptainer shares the host netns so 127.0.0.1 is reachable;
    # Docker Desktop reaches the host via host.docker.internal. Codex appends
    # `/responses` to OPENAI_BASE_URL, so the base SUFFIX (/v1 or /backend-api/
    # codex) makes the caged request path match the broker's pinned allowlist and
    # forward to the right upstream path. ──
    runtime = str(record.get("runtime") or "docker")
    tcp_host = "127.0.0.1"
    try:
        tcp_port = _free_tcp_port(tcp_host)
    except OSError as exc:
        print(f"[agent-codex-broker] could not allocate a loopback TCP port: "
              f"{exc}", file=sys.stderr)
        return 2
    reachable_host = "127.0.0.1" if runtime == "apptainer" else "host.docker.internal"
    base_url = f"http://{reachable_host}:{tcp_port}{_MODE_BASEPATH[mode]}"

    # State persistence: broker mode is exclusive with the codex mount plugins,
    # which provided the /home/agent/.codex state bind (history, sessions, the
    # AGENTS.md the entrypoint writes). Bind a DEDICATED broker-state dir (never
    # holds a credential — the broker injects the key host-side) so codex
    # remembers state and the entrypoint can write AGENTS.md.
    broker_state_dir = state_dir / "data" / "agent-codex" / "broker-state" / profile
    broker_state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(broker_state_dir, 0o700)
    except OSError:
        pass
    contribution_binds = [{
        "source": str(broker_state_dir),
        "target": "/home/agent/.codex",
        "mode": "rw",
        "provenance_detail": (
            "agent-codex-broker: per-project Codex STATE (history, sessions, the "
            "generated AGENTS.md). Holds NO credential — broker mode injects the "
            "key host-side; this dir only persists session state."
        ),
    }]

    session_dir = state_dir / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    debug_log = session_dir / "broker-debug.log"

    safe_inherits = {
        k: v for k, v in os.environ.items()
        if k in {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR",
                 "PYTHONPATH", "VIRTUAL_ENV"}
    }
    from botainer.state.dir import subprocess_state_env
    broker_env = {
        **safe_inherits,
        **subprocess_state_env(project_uuid=project_uuid),
        "BOTAINER_BROKER_PROVIDER": provider,
        "BOTAINER_BROKER_TCP_HOST": tcp_host,
        "BOTAINER_BROKER_TCP_PORT": str(tcp_port),
        "BOTAINER_BROKER_REQUIRED_TOKEN": sentinel,
        "BOTAINER_BROKER_CREDENTIAL_FILE": str(creds_path),
        "BOTAINER_BROKER_UPSTREAM": upstream,
        "BOTAINER_BROKER_DEBUG_LOG": str(debug_log),
        "BOTAINER_LAUNCHER_PID": str(os.getppid()),
    }

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
        print(f"[agent-codex-broker] broker spawn failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if _err_fd is not None:
            os.close(_err_fd)

    rh = record.setdefault("runtime_handle", {})
    rh["broker"] = {
        "pid": proc.pid,
        "transport": "tcp",
        "socket_path": None,
        "tcp_host": tcp_host,
        "tcp_port": tcp_port,
        "credential_scope": scope,
        "provider": provider,
        "started_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }
    tmp = record_path_p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, record_path_p)

    def _daemon_died() -> int:
        reason = ""
        try:
            txt = daemon_err.read_text(encoding="utf-8", errors="replace")
            nonempty = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            if nonempty:
                import re as _re
                reason = nonempty[-1]
                reason = reason.removeprefix("broker: ")
                reason = reason.removeprefix("refusing to start: ")
                reason = _re.sub(r"^\[[a-z0-9-]+\]\s*", "", reason)
        except OSError:
            pass
        reason = reason or ("credential can't produce an api key "
                            "(re-run codex login)")
        if len(reason) > 170:
            reason = reason[:167] + "..."
        print(f"[agent-codex-broker] broker failed: {reason} "
              f"→ run `botainer auth login --shared --agent codex`.",
              file=sys.stderr)
        return 2

    import time as _t
    for _ in range(60):
        if _tcp_accepting(tcp_host, tcp_port):
            break
        if proc.poll() is not None:
            return _daemon_died()
        _t.sleep(0.05)
    if proc.poll() is not None:
        return _daemon_died()

    print(json.dumps({
        "version": "plugin-contribution-v1",
        "kind": "pre_session",
        "env": {
            "OPENAI_API_KEY": sentinel,
            "OPENAI_BASE_URL": base_url,
            "CODEX_HOME": "/home/agent/.codex",
        },
        "binds": contribution_binds,
        "broker_pid": proc.pid,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
