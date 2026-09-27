"""Standalone broker entry point: ``python3 -m botainer.broker.daemon_main``.

The agent-claude-broker plugin's pre_session hook spawns this detached (the
same pattern as agent-claude-proxy's ``start_proxy.py`` spawning ``proxy.py``)
and binds the resulting unix socket into the container; the container's
Claude Code is pointed at it via ``ANTHROPIC_BASE_URL`` with a
``BROKER-SENTINEL…`` placeholder as its auth token.

The import chain here is stdlib-only (``botainer.broker.*``,
``botainer.core.refusal``, ``botainer.core.broker_sentinel``,
``botainer.state.secure_write``) so the detached daemon needs no third-party
packages and no heavy botainer machinery.

Configuration (argv wins over env):

===============================  =====================================
argv                              env
===============================  =====================================
``--socket PATH``                 ``BOTAINER_BROKER_SOCKET``
``--credential-file PATH``        ``BOTAINER_BROKER_CREDENTIAL_FILE``
``--auth-mode shared|isolated``   ``BOTAINER_BROKER_AUTH_MODE``
(state root for --auth-mode)      ``BOTAINER_STATE_ROOT``
(uuid for isolated mode)          ``BOTAINER_PROJECT_UUID``
``--profile NAME``                ``BOTAINER_PROFILE``
``--upstream URL``                ``BOTAINER_BROKER_UPSTREAM``
``--token-endpoint URL``          ``BOTAINER_BROKER_TOKEN_ENDPOINT``
``--client-id ID``                ``BOTAINER_BROKER_CLIENT_ID``
``--allow-insecure-upstream``     ``BOTAINER_BROKER_ALLOW_INSECURE=1``
===============================  =====================================

Either ``--credential-file`` (explicit path) or ``--auth-mode`` (+ the
``BOTAINER_STATE_ROOT`` / ``BOTAINER_PROJECT_UUID`` subprocess-convention env
vars — see ``botainer.state.dir.subprocess_state_env``) must locate the
credential. ``--token-endpoint`` / ``--client-id`` are optional: without
them the broker serves the stored access token and fails CLOSED at expiry
(no guessed OAuth constants are baked in anywhere).

``--allow-insecure-upstream`` (http upstream) is test-only and additionally
gated on ``BOTAINER_TESTING=1`` so a stale shell export can't downgrade a
real session to plaintext.

Exit codes: 0 clean shutdown, 1 configuration/credential refusal,
2 socket bind failure.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

from botainer.broker.credential_source import (
    BotainerCredentialBroker,
    resolve_credential_path,
)
from botainer.broker.daemon import serve_tcp, serve_unix
from botainer.broker.openai_credential import (
    OpenAICredentialBroker,
    resolve_codex_credential_path,
)
from botainer.broker.openai_oauth import CodexOAuthCredentialBroker
from botainer.core.refusal import Refused

# Per-provider PINNED upstream + path allowlist. The client NEVER chooses these
# (SSRF defense); --upstream is an override for tests only. `openai-chatgpt` is
# the Codex ChatGPT-SUBSCRIPTION path — the container's codex (API-key-shaped,
# pointed at OPENAI_BASE_URL=<broker>/backend-api/codex) sends
# /backend-api/codex/responses, which the broker forwards to the ChatGPT backend
# with the real OAuth Bearer + chatgpt-account-id injected.
DEFAULT_UPSTREAM = "https://api.anthropic.com"
DEFAULT_UPSTREAM_OPENAI = "https://api.openai.com"
DEFAULT_UPSTREAM_OPENAI_CHATGPT = "https://chatgpt.com"
_PROVIDER_UPSTREAM = {
    "anthropic": DEFAULT_UPSTREAM,
    "openai": DEFAULT_UPSTREAM_OPENAI,
    "openai-chatgpt": DEFAULT_UPSTREAM_OPENAI_CHATGPT,
}
_PROVIDER_ALLOWED_PREFIXES = {
    "anthropic": ("/v1/",),
    "openai": ("/v1/",),
    "openai-chatgpt": ("/backend-api/codex/",),
}

# Emitted on stdout once the socket is bound, so the spawning hook can wait
# for readiness instead of polling the socket path.
READY_MARKER = "BOTAINER-BROKER-READY"


def _build_parser() -> argparse.ArgumentParser:
    env = os.environ
    p = argparse.ArgumentParser(
        prog="python3 -m botainer.broker.daemon_main",
        description="botainer host-side credential broker daemon",
    )
    p.add_argument("--socket", default=env.get("BOTAINER_BROKER_SOCKET"))
    # TCP transport (Docker Desktop, where a host unix socket can't be bound
    # across the VM). When --tcp-port is set, the daemon listens on TCP and
    # requires every request to present --required-client-token (the sentinel).
    p.add_argument("--tcp-host", default=env.get("BOTAINER_BROKER_TCP_HOST", "127.0.0.1"))
    p.add_argument(
        "--tcp-port", type=int,
        default=int(env["BOTAINER_BROKER_TCP_PORT"])
        if env.get("BOTAINER_BROKER_TCP_PORT", "").isdigit() else None,
    )
    p.add_argument(
        "--required-client-token",
        default=env.get("BOTAINER_BROKER_REQUIRED_TOKEN") or None,
    )
    p.add_argument(
        "--credential-file", default=env.get("BOTAINER_BROKER_CREDENTIAL_FILE")
    )
    p.add_argument(
        "--auth-mode",
        choices=("shared", "isolated"),
        default=env.get("BOTAINER_BROKER_AUTH_MODE") or None,
    )
    p.add_argument("--profile", default=env.get("BOTAINER_PROFILE", "default"))
    p.add_argument(
        "--provider",
        choices=("anthropic", "openai", "openai-chatgpt"),
        default=env.get("BOTAINER_BROKER_PROVIDER", "anthropic"),
        help="which credential store + pinned upstream to broker "
             "(anthropic=Claude, openai=Codex API key, "
             "openai-chatgpt=Codex ChatGPT subscription)",
    )
    # Upstream is resolved per-provider AFTER parsing unless explicitly given
    # (or set via env). Default None so provider selects the pinned host.
    p.add_argument(
        "--upstream", default=env.get("BOTAINER_BROKER_UPSTREAM") or None
    )
    p.add_argument(
        "--token-endpoint", default=env.get("BOTAINER_BROKER_TOKEN_ENDPOINT") or None
    )
    p.add_argument(
        "--client-id", default=env.get("BOTAINER_BROKER_CLIENT_ID") or None
    )
    p.add_argument(
        "--allow-insecure-upstream",
        action="store_true",
        default=env.get("BOTAINER_BROKER_ALLOW_INSECURE") == "1",
    )
    return p


def _start_launcher_watchdog(launcher_pid: int, server, *, interval: float = 30.0) -> None:
    """Shut the broker down if the launcher process disappears (mirrors the
    proxy's #108 fix; the start_broker hook passes BOTAINER_LAUNCHER_PID for
    exactly this).

    The daemon is spawned detached (``start_new_session=True``) so it survives a
    clean launcher exit + ``stop_broker.py`` SIGTERM. The disaster case is the
    launcher dying WITHOUT running post_session — SIGKILL, OOM, panic, reboot —
    leaving an orphan that still holds the real access token in memory and
    serves a live socket that injects it. This daemon thread polls the launcher
    PID and, when it is gone, calls ``server.shutdown()`` (same clean path as
    SIGTERM → the ``finally`` in main() unlinks the socket).

    ``os.kill(pid, 0)`` is cross-platform (Linux/macOS/BSD). PID-reuse is a
    theoretical race on this backstop; ``stop_broker.py`` is the primary reap.
    """

    def _watch() -> None:
        while True:
            time.sleep(interval)
            try:
                os.kill(launcher_pid, 0)
            except ProcessLookupError:
                sys.stderr.write(
                    f"broker: launcher pid {launcher_pid} is gone; shutting "
                    f"down (orphan guard)\n"
                )
                server.shutdown()
                return
            except PermissionError:
                # Process exists but we can't signal it — still alive.
                continue

    t = threading.Thread(target=_watch, daemon=True, name="broker-launcher-watchdog")
    t.start()


def _resolve_credential(args: argparse.Namespace) -> Path:
    if args.credential_file:
        return Path(args.credential_file)
    if not args.auth_mode:  # main() checks this first; defensive
        raise SystemExit("broker: need --credential-file or --auth-mode")
    state_root = os.environ.get("BOTAINER_STATE_ROOT", "")
    if not state_root:
        raise SystemExit(
            "broker: --auth-mode requires BOTAINER_STATE_ROOT in the "
            "environment (subprocess convention; see state/dir.py)"
        )
    resolver = (resolve_codex_credential_path
                if args.provider.startswith("openai") else resolve_credential_path)
    return resolver(
        state_root=Path(state_root),
        mode=args.auth_mode,
        project_uuid=os.environ.get("BOTAINER_PROJECT_UUID") or None,
        profile=args.profile,
    )


def _build_keystore(args: argparse.Namespace, credential_path: Path):
    """Provider-specific keystore for daemon.serve_*.
      * openai         → Codex API key (no OAuth refresh);
      * openai-chatgpt → Codex ChatGPT subscription (OAuth: refresh + rotate +
        write-back host-side, inject chatgpt-account-id — pinned endpoint/client);
      * anthropic      → Claude (may refresh host-side).
    """
    if args.provider == "openai-chatgpt":
        return CodexOAuthCredentialBroker(credential_path)
    if args.provider == "openai":
        return OpenAICredentialBroker(credential_path)
    return BotainerCredentialBroker(
        credential_path,
        token_endpoint=args.token_endpoint,
        client_id=args.client_id,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    tcp = args.tcp_port is not None
    if tcp:
        if not args.required_client_token:
            print("broker: --tcp-port requires --required-client-token "
                  "(the per-session sentinel gates the loopback port)",
                  file=sys.stderr)
            return 1
    elif not args.socket:
        print("broker: --socket (or BOTAINER_BROKER_SOCKET) is required "
              "(or --tcp-port for the Docker transport)", file=sys.stderr)
        return 1
    if not args.credential_file and not args.auth_mode:
        print(
            "broker: need --credential-file or --auth-mode to locate the "
            "botainer credential",
            file=sys.stderr,
        )
        return 1
    if args.allow_insecure_upstream and os.environ.get("BOTAINER_TESTING") != "1":
        print(
            "broker: --allow-insecure-upstream is test-only and requires "
            "BOTAINER_TESTING=1; refusing plaintext upstream",
            file=sys.stderr,
        )
        return 1

    # THE UPSTREAM IS THE DESTINATION OF THE REAL CREDENTIAL, so the pin is the
    # whole point. `--upstream` has been documented as "an override for tests
    # only" since it was written (see _PROVIDER_UPSTREAM above) and nothing
    # enforced it — the sibling flag two lines up IS gated, this one was not.
    # A comment claiming a check nobody performs is the shape this repo keeps
    # finding; #216 makes the sentence true.
    #
    # NARROWING, NOT BLOCKING: the value the hooks pass is the pinned one (both
    # broker hooks compute it from their own trusted constants and hand it over
    # explicitly), so restating the pin is always allowed. Only a DIFFERENT
    # destination needs the testing gate — which is exactly the case the
    # docstring calls test-only.
    pinned = _PROVIDER_UPSTREAM[args.provider]
    if not args.upstream:
        args.upstream = pinned
    elif (args.upstream.rstrip("/") != pinned.rstrip("/")
            and os.environ.get("BOTAINER_TESTING") != "1"):
        print(
            f"broker: --upstream/BOTAINER_BROKER_UPSTREAM may not redirect the "
            f"{args.provider} credential to {args.upstream!r}; it is pinned to "
            f"{pinned!r}. Overriding it is test-only and requires "
            f"BOTAINER_TESTING=1.",
            file=sys.stderr,
        )
        return 1

    try:
        credential_path = _resolve_credential(args)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Refused as exc:
        print(f"broker: {exc}", file=sys.stderr)
        return 1

    keystore = _build_keystore(args, credential_path)

    # Fail-closed startup probe: refuse to come up (and to let a session
    # start against us) if the credential cannot produce an Authorization
    # value RIGHT NOW. Same call the first request would make.
    try:
        keystore.outbound_authorization()
        # Also validate the extra injected headers up front (codex-subscription's
        # chatgpt-account-id is resolved here) so a bundle that would 502 on every
        # request refuses to START instead (audit LOW).
        _extra = getattr(keystore, "outbound_headers", None)
        if callable(_extra):
            _extra()
    except Refused as exc:
        print(f"broker: refusing to start: {exc}", file=sys.stderr)
        return 1

    # Per-provider path allowlist (the ChatGPT backend uses /backend-api/codex/,
    # not /v1/). PINNED like the upstream — the client can't widen it.
    allowed_prefixes = _PROVIDER_ALLOWED_PREFIXES[args.provider]

    debug_log = os.environ.get("BOTAINER_BROKER_DEBUG_LOG") or None
    try:
        if tcp:
            server = serve_tcp(
                args.tcp_host,
                args.tcp_port,
                keystore,
                args.upstream,
                required_client_token=args.required_client_token,
                allowed_prefixes=allowed_prefixes,
                allow_insecure_upstream=args.allow_insecure_upstream,
                debug_log=debug_log,
            )
        else:
            server = serve_unix(
                args.socket,
                keystore,
                args.upstream,
                allowed_prefixes=allowed_prefixes,
                allow_insecure_upstream=args.allow_insecure_upstream,
                debug_log=debug_log,
            )
    except OSError as exc:
        where = f"{args.tcp_host}:{args.tcp_port}" if tcp else args.socket
        print(f"broker: could not bind {where}: {exc}", file=sys.stderr)
        return 2

    def _terminate(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    # Orphan guard: if the launcher dies without running stop_broker.py, shut
    # down rather than linger holding the real token on a live socket.
    launcher_pid_raw = os.environ.get("BOTAINER_LAUNCHER_PID", "")
    if launcher_pid_raw.isdigit():
        _start_launcher_watchdog(int(launcher_pid_raw), server)

    ready_where = (f"tcp={args.tcp_host}:{args.tcp_port}" if tcp
                   else f"socket={args.socket}")
    print(f"{READY_MARKER} {ready_where}", flush=True)
    try:
        server.serve_forever()
    except SystemExit:
        pass
    finally:
        server.server_close()
        if not tcp:
            try:
                os.unlink(args.socket)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
