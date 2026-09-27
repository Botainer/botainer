#!/usr/bin/env python3
"""pre_session hook: spawn the credential proxy.

Reads BOTAINER_SESSION_RECORD_PATH, picks a unix-socket location under
the session dir, mints an ephemeral session token, locates the real
API credentials, spawns the proxy script as a detached child, and
emits a contribution JSON describing the resulting binds + env vars
the launcher should inject.

Stdout JSON contribution shape:
  {"version": "plugin-contribution-v1",
   "kind": "pre_session",
   "env": {"ANTHROPIC_API_KEY": "<ephemeral>",
           "ANTHROPIC_BASE_URL": "http+unix://run/anthropic-proxy.sock"},
   "binds": [
     {"source": "<host socket path>",
      "target": "/run/anthropic-proxy.sock",
      "mode": "unix-socket"}
   ],
   "proxy_pid": <int>,
   "audit_log": "<path>"}

Note (corrected, solidity-check): the launcher's `run_pre_session_hooks`
(core/composition.py) DOES consume `env` and `binds` from pre_session hook
output — `env` is merged into the spec subject to the policy `env_var_denylist`
AND a credential-leak refusal (a hook cannot smuggle a credential or a
denylisted var into the agent's environment), and `binds` are merged subject to
the plugin's declared `mount_target_prefixes` envelope (a bind outside it is
refused). So this hook's contribution is NOT inert — it is applied AND gated at
that chokepoint. (The earlier "doesn't yet consume" note was stale and
security-misleading: it implied hook env/binds were ignored.) The `endpoints` /
`proxy_pid` / `audit_log` fields below are additionally recorded in the session
record for `botainer inspect` / stop_proxy.

For now this hook still:
- Spawns the proxy as a detached process so it survives until
  post_session.
- Writes proxy_pid into the session record under
  runtime_handle.proxy.pid so stop_proxy.py can find + kill it.

Exit codes:
  0 — success
  1 — config / credentials problem (refused; session must not start)
  2 — proxy spawn failed
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
    """Read the agent-claude-proxy plugin config from project config.yaml.

    Returns an empty dict if the project has no config or no
    `plugins.agent-claude-proxy:` section.
    """
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    plugins = data.get("plugins") or {}
    return (plugins.get("agent-claude-proxy") or {})


def main() -> int:
    # T0-3 / #59: proxy auth mode is NOT FUNCTIONAL at v0.1.0.
    # The proxy's whole mechanism is to hand the agent an env var named
    # ANTHROPIC_API_KEY (an ephemeral loopback bearer for the local socket).
    # But the launcher's credential-leak guard (core/credential_leak_check)
    # refuses ANY env var named ANTHROPIC_API_KEY in a pre_session
    # contribution — by design, so a hook can't smuggle a real key to the
    # agent. The two are in DIRECT contradiction: a proxy session cannot
    # start on any runtime today (docker / direct / apptainer). Rather than
    # spawn the proxy and die later at composition.py's leak check with a
    # confusing "credential-shaped env var in hook contribution" refusal,
    # fail fast HERE and say why. The real fix (a scoped
    # leak-check exemption for a proxy-minted ephemeral token + OAuth
    # refresh-on-401) is tracked in DN-009.
    #
    # Escape hatches (both still hit the leak check, so neither makes proxy
    # "work" — they only let the spawn path run for development/tests):
    #   BOTAINER_PROXY_EXPERIMENTAL=1 — a developer actively making proxy
    #     functional (they must ALSO patch the leak check to test end-to-end).
    #   BOTAINER_TESTING=1 — the plugin's own security-fix tests exercise the
    #     spawn path (upstream allowlist, creds-override gating, etc.).
    if (
        os.environ.get("BOTAINER_PROXY_EXPERIMENTAL") != "1"
        and os.environ.get("BOTAINER_TESTING") != "1"
    ):
        print(
            "[agent-claude-proxy] refused: proxy auth mode is NOT FUNCTIONAL "
            "at v0.1.0. The agent's ANTHROPIC_API_KEY (an ephemeral proxy "
            "token) is blocked by botainer's credential-leak guard, so a "
            # `--mode` is not an option of `botainer start`; naming it here
            # ended the only road out of proxy at "Error: No such option".
            # This is the message a proxy user actually reaches — the same
            # remedy in the consent summary cannot render, because this hook
            # refuses before the summary is printed.
            "proxy session cannot start. Switch with `botainer auth use "
            "shared` (one host-wide login) or `botainer auth use isolated` "
            "(a login per project). "
            "Tracked in the project's internal design notes.",
            file=sys.stderr,
        )
        return 1
    record_path = os.environ.get("BOTAINER_SESSION_RECORD_PATH")
    if not record_path:
        print("[agent-claude-proxy] BOTAINER_SESSION_RECORD_PATH unset",
              file=sys.stderr)
        return 1
    record_path_p = Path(record_path)
    if not record_path_p.exists():
        print(f"[agent-claude-proxy] record file missing: {record_path_p}",
              file=sys.stderr)
        return 1
    record = json.loads(record_path_p.read_text(encoding="utf-8"))
    if record.get("schema_version") != 1:
        print(
            f"[agent-claude-proxy] unsupported schema_version "
            f"{record.get('schema_version')!r}",
            file=sys.stderr,
        )
        return 1
    session_id = record["session_id"]
    state_dir = Path(record["state_dir"]) if "state_dir" in record else None
    if state_dir is None:
        # THE SECOND BRANCH IS THE ONLY ONE THAT RUNS. This used to be
        # commented "the launcher always sets state_dir; this is defensive",
        # which is backwards: `SessionRecord.to_dict` writes twelve keys and
        # `state_dir` has never been one of them. So the branch above is dead
        # code that reads like the normal path, and the "defensive" fallback is
        # the normal path. Left in place rather than deleted only because a
        # record written by some future version may carry the key; but nothing
        # produces one today, and a reader should not be told otherwise.
        # `tests/unit/test_hook_record_contract.py` pins the real key set.
        spec = record.get("spec", {})
        state_dir = Path(spec.get("state_dir", ""))
    if not state_dir or not state_dir.exists():
        print("[agent-claude-proxy] state_dir missing from record",
              file=sys.stderr)
        return 1

    # Per-project credential file location (per DN-026; agent-claude
    # writes to ${state_dir}/data/agent-claude/profiles/<profile>/.credentials.json).
    # The env override is for tests; requires BOTAINER_TESTING=1 to engage
    # so a stale shell export can't redirect to /etc/passwd.
    creds_override = (
        os.environ.get("BOTAINER_PROXY_CREDS_PATH_OVERRIDE")
        if os.environ.get("BOTAINER_TESTING") == "1"
        else None
    )
    if creds_override:
        creds_path = Path(creds_override)
    else:
        creds_path = (
            state_dir / "data" / "agent-claude" / "profiles" / "default"
            / ".credentials.json"
        )
    if not creds_path.exists():
        print(
            f"[agent-claude-proxy] credentials file not found at {creds_path}. "
            f"Run `botainer plugin agent-claude login` first.",
            file=sys.stderr,
        )
        return 1

    # Session scratch dir (where the socket + audit log live).
    session_scratch = (
        Path(os.environ.get("BOTAINER_SESSION_SCRATCH",
                            str(state_dir / "sessions" / session_id)))
    )
    session_scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    sock_path = session_scratch / "anthropic-proxy.sock"
    audit_log = session_scratch / "proxy-audit.jsonl"

    # Mint an ephemeral token.
    ephemeral = secrets.token_urlsafe(32)

    # Read plugin config from project's config.yaml for proxy tunables.
    # Sharp-edges HIGH 4: previously we did `**os.environ` which let
    # stale shell exports (BOTAINER_PROXY_UPSTREAM=..., etc.) silently
    # redirect upstream + creds path. Now we explicitly construct the
    # env from authoritative sources (plugin config + spec defaults).
    plugin_cfg = _read_plugin_config(Path(record["project_root"]))
    upstream = str(plugin_cfg.get("upstream", "https://api.anthropic.com"))
    max_bytes = str(int(plugin_cfg.get("max_request_size_bytes", 1048576)))
    rate_limit = str(int(plugin_cfg.get("rate_limit_rps", 60)))
    redact = "1" if plugin_cfg.get("redact_request_bodies", False) else "0"

    # Spawn the proxy. The proxy script is alongside this hook. Build
    # the env from scratch (don't inherit potentially-tainted vars from
    # the user's shell), but pass PATH and HOME so /usr/bin/env etc.
    # still works.
    proxy_script = Path(__file__).resolve().parent.parent / "proxy.py"
    safe_inherits = {
        k: v for k, v in os.environ.items()
        if k in {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR"}
    }
    proxy_env = {
        **safe_inherits,
        "BOTAINER_PROXY_SOCKET_PATH": str(sock_path),
        "BOTAINER_PROXY_EPHEMERAL_TOKEN": ephemeral,
        "BOTAINER_PROXY_REAL_CREDS_PATH": str(creds_path),
        "BOTAINER_PROXY_AUDIT_LOG": str(audit_log),
        "BOTAINER_PROXY_UPSTREAM": upstream,
        "BOTAINER_PROXY_MAX_REQUEST_BYTES": max_bytes,
        "BOTAINER_PROXY_RATE_LIMIT_RPS": rate_limit,
        "BOTAINER_PROXY_REDACT_BODIES": redact,
        # #108: the proxy is spawned with start_new_session=True so it
        # survives clean launcher exit + stop_proxy.py. When the launcher
        # dies WITHOUT running post_session (SIGKILL, OOM, reboot), the
        # proxy stays orphaned holding the socket. Pass the launcher PID
        # so the proxy can detect "parent gone" and shut down on its own.
        # Our PARENT here is the run_hook subprocess.run inside the
        # launcher; getppid() gives the launcher's PID.
        "BOTAINER_LAUNCHER_PID": str(os.getppid()),
    }
    # BOTAINER_PROXY_CREDS_PATH_OVERRIDE is intentionally a test-only
    # escape hatch — preserved but gated.
    if os.environ.get("BOTAINER_TESTING") == "1":
        if "BOTAINER_PROXY_CREDS_PATH_OVERRIDE" in os.environ:
            proxy_env["BOTAINER_PROXY_CREDS_PATH_OVERRIDE"] = os.environ[
                "BOTAINER_PROXY_CREDS_PATH_OVERRIDE"
            ]
    # Detach from this hook so the proxy survives.
    try:
        proc = subprocess.Popen(
            [sys.executable, str(proxy_script)],
            env=proxy_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"[agent-claude-proxy] proxy spawn failed: {exc}",
              file=sys.stderr)
        return 2

    # Record the pid so post_session can stop it. We extend the
    # session record with a "proxy" handle.
    rh = record.setdefault("runtime_handle", {})
    rh["proxy"] = {
        "pid": proc.pid,
        "socket_path": str(sock_path),
        "audit_log": str(audit_log),
        "started_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }
    # Atomic rewrite.
    tmp = record_path_p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, record_path_p)

    # Wait briefly (up to 2s) for the socket to appear so subsequent
    # adapter steps don't race against the proxy not-yet-listening.
    import time as _t
    for _ in range(40):
        if sock_path.exists():
            break
        _t.sleep(0.05)

    # Emit the contribution. Today's launcher won't auto-merge this,
    # but recording it in the record gives `botainer inspect` something
    # useful to surface.
    print(json.dumps({
        "version": "plugin-contribution-v1",
        "kind": "pre_session",
        "env": {
            "ANTHROPIC_API_KEY": ephemeral,
            "ANTHROPIC_BASE_URL": "http+unix:///run/anthropic-proxy.sock",
        },
        "binds": [{
            "source": str(sock_path),
            "target": "/run/anthropic-proxy.sock",
            "mode": "unix-socket",
        }],
        "proxy_pid": proc.pid,
        "audit_log": str(audit_log),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
