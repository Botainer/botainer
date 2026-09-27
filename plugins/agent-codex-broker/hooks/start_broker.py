#!/usr/bin/env python3
"""pre_session hook: spawn the credential broker for Codex (OpenAI).

The OpenAI analog of agent-claude-broker's start_broker.py. It hands the
container a provably-FAKE SENTINEL (core.broker_sentinel.make_sentinel) as
`OPENAI_API_KEY`, writes a `config.toml` pointing codex at the broker, and holds
the real credential host-side in the broker daemon.

Three things differ from the Claude broker:
1. TRANSPORT IS TCP-ONLY. The Codex CLI has no unix-socket support (there is no
   OpenAI analog of ANTHROPIC_UNIX_SOCKET), so even under apptainer the broker
   listens on loopback TCP. The container reaches it via 127.0.0.1 (apptainer
   shares the host net namespace) or host.docker.internal (Docker Desktop). A
   loopback port has no uid boundary, so every request MUST carry the per-session
   sentinel as its access token (required_client_token) — the daemon enforces it.
2. ROUTING IS A FILE, NOT AN ENV VAR. Claude Code follows
   `ANTHROPIC_BASE_URL`; codex does not follow `OPENAI_BASE_URL` for its model
   calls, on either auth path. It has to be told via a `model_providers` entry
   in `$CODEX_HOME/config.toml`. `_provider_config_toml` has the measurement
   and the table of what was tried.
3. BOTH AUTH PATHS LOOK IDENTICAL INSIDE. The container always sees one plain
   API-key provider. Whether the real credential is an OpenAI API key or a
   ChatGPT subscription — and so whether the request is forwarded to
   api.openai.com or chatgpt.com with an OAuth Bearer and a chatgpt-account-id
   — is decided host-side from the login store (`_detect_mode`). No `auth.json`
   is written into the container, so nothing in the cage tries to refresh a
   token it cannot refresh.

Stdout JSON contribution shape:
  {"version": "plugin-contribution-v1",
   "kind": "pre_session",
   "env": {"OPENAI_API_KEY": "<sentinel>",
           "OPENAI_BASE_URL": "http://<host-reachable>:<port>/v1",
           "CODEX_HOME": "/home/agent/.codex"},
   "binds": [{"source": "<broker-state dir>", "target": "/home/agent/.codex",
              "mode": "rw"}],
   "broker_pid": <int>}

Files written into the broker-state dir (bound at /home/agent/.codex):
  config.toml — the model_providers entry that routes codex to the broker.
                Rewritten every session.

Exit codes:
  0 — success
  1 — config / credentials problem (refused; session must not start)
  2 — broker spawn failed
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_UPSTREAM = "https://api.openai.com"
_CHATGPT_UPSTREAM = "https://chatgpt.com"

# The provider base_url suffix per mode. In API-key mode codex hits
# <base>/responses → the broker forwards to api.openai.com/v1/responses. In
# subscription mode codex hits <base>/responses → the broker forwards to
# chatgpt.com/backend-api/codex/responses (host + auth rewrite; the body is a
# standard Responses request either way — OBSERVED for the request codex sends a
# custom provider, ASSUMED for what chatgpt.com will accept, which is the one leg
# of this path no test here can exercise without a real subscription).
# The path the broker sees IS the base suffix + /responses, so the suffix also
# selects the daemon's pinned path-allowlist.
_MODE_BASEPATH = {"api-key": "/v1", "subscription": "/backend-api/codex"}
_MODE_PROVIDER = {"api-key": "openai", "subscription": "openai-chatgpt"}
_MODE_UPSTREAM = {"api-key": _DEFAULT_UPSTREAM, "subscription": _CHATGPT_UPSTREAM}


_CREDENTIAL_FILENAMES = frozenset({
    ".credentials.json",
    "api_key",
    "auth.json",
})


def _credential_names_under(root):
    """Credential-NAMED files anywhere under `root`, recursively.

    A DELIBERATE THIRD COPY of `botainer.core.history_carry.
    credential_files_under`. A hook runs as a subprocess with no guarantee that
    `botainer` is importable, so it cannot share the real one — which is exactly
    how copies drift, and drift in this scan is the defect the whole
    broker-state thread is about. `tests/unit/test_broker_state_scans_agree.py`
    drives this function, its codex twin and the library original against one
    fixture set and fails if any of the three disagrees. The copy is allowed;
    diverging silently is not.

    Recursive because the profile directory is bound WHOLE — nesting changes
    nothing about what the container can read. `.pre-shared` included because
    this project's own docs say those backups ARE credentials.
    """
    wanted = set(_CREDENTIAL_FILENAMES) | {
        f"{n}.pre-shared" for n in _CREDENTIAL_FILENAMES}
    found = []
    stack = [root]
    seen = set()
    while stack:
        d = stack.pop()
        try:
            real = d.resolve()
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir():
                    stack.append(e)
                elif e.name in wanted:
                    found.append(e)
            except OSError:
                continue
    return sorted(found)


def _provider_config_toml(base_url: str) -> str:
    """The config.toml that points codex at the broker. THIS is the routing.

    MEASURED, NOT INFERRED (2026-09-04). codex 0.153.2 was run against a
    logging HTTP server with every delivery mechanism this plugin had tried:

      | how codex was told                     | where the request WENT      |
      |----------------------------------------|-----------------------------|
      | `OPENAI_BASE_URL` + ChatGPT auth.json  | chatgpt.com/backend-api/... |
      | `OPENAI_BASE_URL` + api-key            | wss://api.openai.com/v1/... |
      | `chatgpt_base_url` in config.toml      | plugins/apps only, NOT the  |
      |                                        | model endpoint              |
      | **this: a custom `model_provider`**    | **the broker**              |

    So `OPENAI_BASE_URL` never routed the model call at all, on either auth
    path, and the broker was never in the request path — which is why the
    container's sentinel reached OpenAI and came back as

        401 "Could not parse your authentication token. Please try signing in
             again."

    Three earlier fixes (a JWT-shaped sentinel, a freshly-stamped
    `last_refresh`, a stub `auth.json` mirroring the real one) were all aimed
    at that 401 on the theory that codex was rejecting our credential. It was
    not; OpenAI was. None of them could have worked.

    WHY EACH KEY IS HERE:
      * `wire_api = "responses"` — both upstreams speak the Responses API
        (`api.openai.com/v1/responses`, `chatgpt.com/backend-api/codex/
        responses`); the broker forwards the body unchanged.
      * `env_key = "OPENAI_API_KEY"` — codex sends that env var as
        `Authorization: Bearer …`. It holds the SENTINEL, which is also the
        daemon's required token, so the same value both authenticates to the
        broker and proves the container holds no secret.
      * `requires_openai_auth = false` — this is what stops codex demanding a
        ChatGPT login for a provider that does not need one. With it, no
        `auth.json` is needed and none is written.
      * `supports_websockets = false` — codex prefers a WebSocket transport
        that ignores the configured base URL (that is the second row of the
        table above). A custom provider defaults to HTTP today, so this is
        belt-and-braces: it makes the HTTP transport a DECLARED property
        rather than a default that a future codex release could flip.

    THE AGENT CAN EDIT THIS FILE — it is in a rw bind — and that is harmless
    by construction, not by permission: repointing the provider elsewhere
    sends the SENTINEL, which carries no secret, and the daemon still refuses
    anything that does not present it. The file is rewritten every session, so
    an edit does not persist into the next one.

    `base_url` is built from a two-element literal host set and an int port,
    so no untrusted string reaches this TOML.
    """
    return (
        "# WRITTEN BY botainer (agent-codex-broker). Rewritten every session;\n"
        "# edits here do not survive a restart.\n"
        'model_provider = "botainer-broker"\n'
        "\n"
        "[model_providers.botainer-broker]\n"
        'name = "botainer credential broker"\n'
        f'base_url = "{base_url}"\n'
        'wire_api = "responses"\n'
        'env_key = "OPENAI_API_KEY"\n'
        "requires_openai_auth = false\n"
        "supports_websockets = false\n"
    )


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


# NO _tcp_accepting HERE, DELIBERATELY. Readiness used to be "connect to the
# port; any answer means ready" — which a process that squatted the ephemeral
# port between _free_tcp_port()'s close and the daemon's bind satisfies on the
# first try. The daemon then dies with EADDRINUSE and the container spends the
# session talking to the squatter, reported as a successful start. Readiness is
# the daemon's own BOTAINER-BROKER-READY marker; see botainer/broker/readiness.py.


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
    # NO LOCAL SANITISER. make_sentinel owns the charset, so this cannot drift
    # from what is_sentinel accepts — which is exactly what the isalnum() filter
    # that used to be here had done (#216).
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
    #
    # THE SENTINEL IS THE SAME PLAIN STRING IN BOTH MODES. A JWT-shaped variant
    # was minted here for subscription mode on 2026-09-04, to satisfy a codex
    # expiry check that turned out not to be the problem (see
    # `_provider_config_toml`). It also made the value unrecognisable to the
    # credential-leak guard, which then had to be taught to unwrap it — a
    # security check widened to fit a plumbing change. Both are gone.
    mode = _detect_mode(creds_path)
    provider = _MODE_PROVIDER[mode]
    upstream = _trusted_upstream(
        mode, testing=os.environ.get("BOTAINER_TESTING") == "1", env=os.environ)

    # ── TCP transport (always; codex has no unix socket). base_url host differs
    # by runtime: apptainer shares the host netns so 127.0.0.1 is reachable;
    # Docker Desktop reaches the host via host.docker.internal. Codex appends
    # `/responses` to the provider base_url, so the base SUFFIX (/v1 or
    # /backend-api/codex) makes the caged request path match the broker's pinned
    # allowlist and forward to the right upstream path. ──
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

    # SIBLING DRIFT, closed. `ba8703d` added this warning to the CLAUDE broker
    # and stopped there, so for three weeks the codex broker bound a polluted
    # state dir in total silence — while the comment a hundred lines below in
    # THIS file already said "broker_state_dir is bound rw at /home/agent/.codex,
    # so a caged agent can `mkdir /home/agent/.codex/auth.json`". The hazard was
    # known here and unguarded here.
    #
    # A WARNING, NOT A REFUSAL, for the same reason as the claude side:
    # pollution can arrive by routes the user did not choose (an older
    # shared-mode layout, a restored backup), refusing would strand them
    # mid-launch, and a refusal must name a remedy that exists. None does.
    _leaked = ([str(f.relative_to(broker_state_dir))
                for f in _credential_names_under(broker_state_dir)]
               if broker_state_dir.is_dir() else [])
    if _leaked:
        print(
            "[agent-codex-broker] WARNING: broker-state holds "
            + ", ".join(_leaked)
            + " — this directory is bound into the container and is supposed "
            "to hold NO credential. In broker mode the container should only "
            "ever see a sentinel. `botainer auth doctor --agent agent-codex` "
            "reads these files and says which are live. Inspect it: "
            f"{broker_state_dir}",
            file=sys.stderr,
        )

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
    # THE FILENAME CARRIES THE PLUGIN, so an unattributable broker error cannot
    # exist. Both brokers used to write `broker-debug.log` and
    # `broker-daemon.err` into the SAME per-session directory, with nothing in
    # either name or content saying which one. Both also do OAuth refresh, so
    # even the error text does not distinguish them.
    #
    # Include the plugin name to avoid filename collisions and identify which
    # broker produced a diagnostic, even when both report OAuth errors.
    debug_log = session_dir / "agent-codex-broker-debug.log"

    # PYTHONPATH is retained as compatibility context, but the isolated child
    # ignores it; the selected interpreter must contain installed Botainer.
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

    daemon_err = session_dir / "agent-codex-broker-daemon.err"
    _err_fd = None
    try:
        _err_fd = os.open(str(daemon_err),
                          os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                          0o600)
    except OSError:
        _err_fd = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-m", "botainer.broker.daemon_main"],
            env=broker_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,   # the READY marker is read from here
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

    # READINESS IS THE DAEMON'S OWN WORD, not "a port answers". Both hooks
    # used to connect to the port and treat any answer as success — so a
    # process that grabbed the ephemeral port between _free_tcp_port()'s close
    # and the daemon's bind satisfied the check on iteration 0, the daemon then
    # died with EADDRINUSE, and the container talked to the squatter for the
    # whole session. See botainer/broker/readiness.py for what this does and
    # does NOT fix.
    from botainer.broker.readiness import wait_for_ready
    _ok, _why = wait_for_ready(proc, f"tcp={tcp_host}:{tcp_port}", timeout=30.0)
    if not _ok:
        if proc.poll() is not None:
            return _daemon_died()
        # Alive but never reported ready: do not leave it holding the real
        # credential on a live socket while we walk away.
        try:
            proc.terminate()
        except OSError:
            pass
        print("[agent-codex-broker] broker did not become ready: %s" % _why,
              file=sys.stderr)
        return 2

    # ── POINT CODEX AT THE BROKER, the one way that actually routes ─────────
    #
    # THE DEFECT THIS CLOSES (#121): the plugin worked on every layer except
    # the one that matters. The daemon started, held the real credential, and
    # was never contacted — because `OPENAI_BASE_URL` does not route codex's
    # model calls. See `_provider_config_toml` for the measurement.
    #
    # SAME FILE FOR BOTH MODES. api-key and subscription differ only in where
    # the request is FORWARDED (api.openai.com vs chatgpt.com — chosen
    # host-side, from the credential, by `_MODE_UPSTREAM`) and in the base
    # path. From inside the container both look like one plain provider with
    # one key. That is the point of a broker: the container should not know,
    # and now does not.
    # WRITE THROUGH write_secure, NEVER write_text + chmod INTO THIS DIRECTORY.
    #
    # broker_state_dir is bound rw into the container AND outlives the session
    # (the deletion below says so in as many words). This hook runs UNCAGED as
    # the host user, and the container runs as that same uid. So a
    # `.with_suffix(".toml.tmp")` temp — a PREDICTABLE name — is a symlink
    # target a prior compromised session can plant:
    #
    #     session N   (in the cage):  ln -s ~/.ssh/config config.toml.tmp
    #     session N+1 (on the host):  write_text() follows it, chmod follows it
    #
    # That is create/truncate/overwrite of any file the launching user can
    # write — a write OUT of the cage, the same class as the 2026-05 shared-
    # /jobs symlink-clobber. This plugin's own sibling files already say so:
    # agent-codex/entrypoint_wrap.sh warns "do NOT fall back to a fixed path"
    # about this exact directory, and agent-codex-shared/hooks/pre_session.py
    # uses O_EXCL|O_NOFOLLOW for the same reason. Found by review 2026-09-04,
    # after the first version of this code did it anyway.
    #
    # write_secure() is the repo's answer: mkstemp (unpredictable, O_EXCL) with
    # the mode set at create, then an atomic os.replace. Using it means the
    # hazard is absent by construction rather than guarded by a comment.
    from botainer.state.secure_write import write_secure
    _cfg_out = broker_state_dir / "config.toml"
    try:
        write_secure(_cfg_out, _provider_config_toml(base_url), mode=0o600)
    except OSError as _exc:
        print(f"[agent-codex-broker] could not write the container's codex "
              f"config at {_cfg_out}: {_exc}. codex will ignore the broker and "
              f"try to reach OpenAI directly, which will fail to authenticate.",
              file=sys.stderr)
        return 2

    # REMOVE A STALE auth.json, because this directory OUTLIVES the session.
    # botainer up to 2026-09-04 wrote a stub `auth.json` here to make codex
    # believe it was logged in. It never worked, and left behind it would be
    # actively harmful now: codex would find a ChatGPT login, enter
    # subscription mode, and go back to talking to chatgpt.com directly with a
    # sentinel — reproducing the exact 401 this change fixes, but only for
    # users who ran the older version. A one-line deletion beats a release note
    # nobody reads.
    #
    # Deleting is safe: this is the broker's own state dir, and the file was
    # never anything but a botainer-written stub. The REAL credential lives in
    # the login store and is not touched.
    # THE AGENT CAN CHOOSE THE SHAPE OF THIS PATH, so `unlink()` alone is not
    # enough (#216). broker_state_dir is bound rw at /home/agent/.codex, so a
    # caged agent can `mkdir /home/agent/.codex/auth.json`; unlink then raises
    # IsADirectoryError, this hook returns 2, and broker mode for that project
    # is bricked until a human works out what happened — while the message
    # tells them to delete a "file" that is not one. A DoS the container can
    # inflict on its own future sessions, and a lie in the error.
    #
    # lstat first, and branch on what is actually there. Nothing legitimate is
    # ever a directory at this path: botainer only ever wrote a small JSON stub,
    # and the REAL credential lives in the login store, not here.
    _stale_auth = broker_state_dir / "auth.json"
    try:
        _st = os.lstat(_stale_auth)
    except FileNotFoundError:
        _st = None
    except OSError as _exc:
        print(f"[agent-codex-broker] cannot inspect {_stale_auth}: {_exc}. "
              f"codex may enter ChatGPT mode and bypass the broker.",
              file=sys.stderr)
        return 2
    if _st is not None:
        try:
            if stat.S_ISDIR(_st.st_mode):
                # NOT reached by lstat-following anything: S_ISDIR on an lstat
                # is false for a symlink-to-a-directory, so a symlink still
                # takes the unlink() branch and the link itself is removed
                # without touching its target.
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise OSError("rmtree is not symlink-safe on this platform")
                shutil.rmtree(_stale_auth)
            else:
                _stale_auth.unlink()
        except OSError as _exc:
            _what = "directory" if stat.S_ISDIR(_st.st_mode) else "file"
            print(f"[agent-codex-broker] could not remove the stale {_what} "
                  f"{_stale_auth}: {_exc}. codex may enter ChatGPT mode and "
                  f"bypass the broker; remove that {_what} and start again.",
                  file=sys.stderr)
            return 2

    # OPENAI_BASE_URL STAYS, and it is no longer what routes codex — say so
    # rather than leave a reader to assume it. It is kept because it is still
    # TRUE and still load-bearing, twice over:
    #
    #  1. IT IS THE VECTOR THE CROSS-NODE GUARD SEES. Check 3 of
    #     `composition._refuse_cross_node_binds` scans spec env values for a
    #     loopback rendezvous, because this plugin is TCP-only and contributes
    #     no bind for the bind-shaped checks to catch. That guard exists so
    #     `hpc submit` refuses instead of baking a login-node 127.0.0.1 into an
    #     sbatch script that runs on a compute node — a failure that costs the
    #     queue wait and the allocation before it shows up. Move the endpoint
    #     into config.toml ALONE and the guard goes blind: the routing would
    #     live in a file it does not read. Pinned by
    #     test_broker_endpoint_stays_visible_to_the_cross_node_guard.
    #  2. Other OpenAI-SDK clients in the container (a python `openai` script
    #     the agent writes) DO honour it, and get the broker — with the same
    #     sentinel and the same host-side credential swap.
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
