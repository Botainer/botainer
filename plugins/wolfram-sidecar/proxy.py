#!/usr/bin/env python3
"""wolfram-sidecar host proxy — unix-socket port of v0.0.x's
docker/wolfram-proxy-host-v2.py.

Three layers of protection, unchanged from v0.0.12:

  Layer 1 (string filter): refuses incoming requests that contain
    known-dangerous Wolfram patterns before wolframscript starts.
  Layer 2 (kernel sandbox): prepends a Wolfram init script that
    Protect[]s Run/RunProcess/URLFetch/etc. and locks the symbols.
  Layer 3 (sandbox-exec on macOS): wolframscript runs inside a
    deny-on-read profile that bars ~/.ssh, ~/.aws, ~/.claude,
    Keychain, browser data, etc., and writes outside /tmp + Wolfram's
    own cache dirs.

Wire protocol (unchanged from v0.0.x — see CLAUDE-MD-v0.0.x notes):
  request JSON:
    {"args": [...], "auth": "<per-launch-token>", "timeout": <int|null>}
  response JSON:
    {"returncode": int, "stdout": str, "stderr": str}

Environment expected (set by hooks/pre_session.py):
  BOTAINER_PROXY_SOCKET_PATH       — unix socket to listen on
  BOTAINER_PROXY_TOKEN_FILE        — file holding the per-launch token
  BOTAINER_PROXY_WOLFRAMSCRIPT     — wolframscript binary path
  BOTAINER_PROXY_TIMEOUT           — global request timeout (s)
  BOTAINER_PROXY_MAX_REQUEST_BYTES — refuse-above-this body size
  BOTAINER_PROXY_REQUIRE_SANDBOX   — "1" = fail-closed without sandbox-exec
  BOTAINER_PROXY_SANDBOX_PROFILE   — path to wolfram-sandbox.sb
  BOTAINER_PROXY_SANDBOX_INIT      — path to wolfram-sandbox-init.wl
  BOTAINER_PROXY_AUDIT_LOG         — append-only audit log path

Signals:
  SIGTERM / SIGINT — clean shutdown; remove socket + token files
                     before exit.

Audit-log lines (JSONL):
  {"ts": ISO8601, "kind": "request_forwarded"|"request_refused"|
                          "proxy_started"|"proxy_stopped",
   "returncode": int, "stderr_bytes": int, "blocked_pattern": ...}
"""
from __future__ import annotations

import hmac
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ────────── config from env ──────────


class ProxyConfig:
    def __init__(self) -> None:
        self.socket_path = Path(os.environ["BOTAINER_PROXY_SOCKET_PATH"])
        self.token_file = Path(os.environ["BOTAINER_PROXY_TOKEN_FILE"])
        self.wolframscript = os.environ.get(
            "BOTAINER_PROXY_WOLFRAMSCRIPT", "wolframscript"
        )
        self.timeout = int(os.environ.get("BOTAINER_PROXY_TIMEOUT", "300"))
        self.max_request_bytes = int(
            os.environ.get("BOTAINER_PROXY_MAX_REQUEST_BYTES", "1048576")
        )
        self.require_sandbox = (
            os.environ.get("BOTAINER_PROXY_REQUIRE_SANDBOX", "1") == "1"
        )
        self.sandbox_profile = Path(
            os.environ.get("BOTAINER_PROXY_SANDBOX_PROFILE", "")
        )
        self.sandbox_init = Path(
            os.environ.get("BOTAINER_PROXY_SANDBOX_INIT", "")
        )
        self.audit_log = Path(os.environ["BOTAINER_PROXY_AUDIT_LOG"])

        # Cache sandbox state.
        self.use_os_sandbox = (
            shutil.which("sandbox-exec") is not None
            and self.sandbox_profile.is_file()
        )
        self.use_kernel_sandbox = self.sandbox_init.is_file()
        if self.use_kernel_sandbox:
            self._sandbox_init_code = self.sandbox_init.read_text(encoding="utf-8")
        else:
            self._sandbox_init_code = ""

    def sandbox_init_code(self) -> str:
        return self._sandbox_init_code


# String filter (unchanged from v0.0.12). Best-effort; the kernel-init
# + sandbox-exec are the actual defenses. The denylist exists to fail
# fast on obvious patterns + as a defense layer that doesn't depend on
# Wolfram's runtime behavior.
BLOCKED_PATTERNS = (
    # Process / shell — bracket form
    "Run[", "RunProcess[", "StartProcess[", "SystemOpen[", "SystemShell[",
    # Process / shell — @-prefix and space-separated forms (bypass attempts)
    "Run @", "Run @ ", "Run @\"", "Run @'",
    "RunProcess @", "StartProcess @",
    "Run [", "RunProcess [", "StartProcess [",
    # Loader / library
    "Install[", "LibraryFunctionLoad[", "LibraryLink",
    # Network
    "URLFetch[", "URLRead[", "URLSubmit[", "URLExecute[",
    'Import["http', 'Import["https',
    'Import ["http', 'Import ["https',
    "SendMail[", "MailReceiverFunction[",
    "CloudDeploy[", "CloudPut[", "CloudPublish[", "CloudSubmit[",
    # File write
    "Export[", "DeleteFile[", "RenameFile[",
    "CopyFile[", "CreateFile[", "OpenWrite[", "OpenAppend[",
    "WriteString[", "BinaryWrite[",
    "Put[", "PutAppend[",
    # File read
    "Get[", "ReadString[", "ReadList[", "ReadByteArray[",
    "FilePrint[", "FileSystemScan[", "FileSystemMap[",
    "Get @", "Get [",
    # Reflection / dynamic eval (bypass vectors)
    "ToExpression[", "Symbol[", "ToHeldExpression[",
    "ToExpression @", "Symbol @",
    "FromCharacterCode[",         # used to reconstruct blocked tokens
    "StringJoin[",                # used to assemble blocked tokens
    "Names[",                     # reflection for system symbols
    # External evaluators
    "JLink", "NETLink",
    "ExternalEvaluate[", "StartExternalSession[",
)


# ────────── audit ──────────


def _audit(cfg: ProxyConfig, **fields: object) -> None:
    """Append-only structured audit. Best-effort — never raises."""
    entry: dict[str, object] = {"ts": datetime.now(timezone.utc).isoformat()}
    entry.update(fields)
    try:
        fd = os.open(
            str(cfg.audit_log),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            mode=0o600,
        )
        try:
            os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


# ────────── request handler ──────────


def _inject_sandbox(cfg: ProxyConfig, args: list[str]) -> list[str]:
    """For -code args, prepend the kernel-init script so user code runs
    after the Protect[] overrides. -file args are refused at the layer
    above; this function never sees them."""
    if not cfg.use_kernel_sandbox:
        return args
    new_args: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-code" and i + 1 < len(args):
            user_code = args[i + 1]
            new_args.extend(["-code", f"{cfg.sandbox_init_code()}\n{user_code}"])
            i += 2
        else:
            new_args.append(args[i])
            i += 1
    return new_args


def handle_client(cfg: ProxyConfig, token: str, conn: socket.socket) -> None:
    try:
        chunks: list[bytes] = []
        total = 0
        oversize = False
        while True:
            data = conn.recv(65536)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            if total > cfg.max_request_bytes:
                oversize = True
                break
        if oversize:
            _audit(cfg, kind="request_refused", reason="body_too_large",
                   total_bytes=total, max=cfg.max_request_bytes)
            _reply(conn, {
                "returncode": 1, "stdout": "",
                "stderr": (
                    f"Request exceeds {cfg.max_request_bytes} bytes "
                    f"(plugins.wolfram-sidecar.max_request_bytes)."
                ),
            })
            return

        try:
            request = json.loads(b"".join(chunks))
        except json.JSONDecodeError as exc:
            _audit(cfg, kind="request_refused", reason="json_decode",
                   error=str(exc))
            _reply(conn, {
                "returncode": 1, "stdout": "",
                "stderr": f"Malformed request: {exc}",
            })
            return

        args = request.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            _audit(cfg, kind="request_refused", reason="args_not_list_of_str")
            _reply(conn, {"returncode": 1, "stdout": "",
                          "stderr": "args must be a list of strings"})
            return

        client_auth = request.get("auth", "")
        if not isinstance(client_auth, str) or not hmac.compare_digest(client_auth, token):
            _audit(cfg, kind="request_refused", reason="auth_failed")
            _reply(conn, {
                "returncode": 1, "stdout": "",
                "stderr": (
                    "Authentication required: client did not send the "
                    "per-launch token. The launcher mounts the token "
                    "file at /run/wolfram-token; the in-container client "
                    "reads it automatically."
                ),
            })
            return

        # Per-request timeout (capped at global).
        req_timeout_raw = request.get("timeout")
        if req_timeout_raw is None:
            req_timeout = cfg.timeout
        else:
            try:
                req_timeout = max(1, min(int(req_timeout_raw), cfg.timeout))
            except (ValueError, TypeError):
                req_timeout = cfg.timeout

        # -file refused at the proxy (v0.0.12 B2: -file is unbounded path
        # input; the Layer-1 string filter runs against original argv and
        # wouldn't see "Get[" / "Import[" — the agent could read
        # /etc/passwd, /var/db/*, etc.). User can write `Get["..."]`
        # explicitly in -code and that goes through the string filter.
        if "-file" in args:
            _audit(cfg, kind="request_refused", reason="dash_file_blocked")
            _reply(conn, {
                "returncode": 1, "stdout": "",
                "stderr": (
                    "Blocked: -file is refused at the proxy layer. Use "
                    "-code with explicit `Get[\"...\"]` if you need to "
                    "load a file (it then goes through the string filter)."
                ),
            })
            return

        # Layer 1: string filter.
        full_input = " ".join(args)
        for pattern in BLOCKED_PATTERNS:
            if pattern in full_input:
                _audit(cfg, kind="request_refused", reason="string_filter",
                       blocked_pattern=pattern)
                _reply(conn, {
                    "returncode": 1, "stdout": "",
                    "stderr": (
                        f"Blocked: '{pattern}' is not allowed in sandbox "
                        f"mode. (Layer-1 string filter; see plugin README.)"
                    ),
                })
                return

        # Layer 2: kernel sandbox injection (for -code).
        args = _inject_sandbox(cfg, args)

        # Layer 3: OS sandbox wrap.
        cmd = [cfg.wolframscript] + args
        if cfg.use_os_sandbox:
            cmd = ["sandbox-exec", "-f", str(cfg.sandbox_profile)] + cmd
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=req_timeout, cwd="/tmp",
            )
            response = {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            _audit(cfg, kind="request_forwarded",
                   returncode=result.returncode,
                   stderr_bytes=len(result.stderr or ""))
        except subprocess.TimeoutExpired:
            response = {
                "returncode": 124, "stdout": "",
                "stderr": f"Timeout: wolframscript exceeded {req_timeout}s",
            }
            _audit(cfg, kind="request_forwarded", returncode=124, timeout=True)
        except FileNotFoundError:
            response = {
                "returncode": 127, "stdout": "",
                "stderr": (
                    f"wolframscript not found at: {cfg.wolframscript}. "
                    f"Configure plugins.wolfram-sidecar.wolframscript_path."
                ),
            }
            _audit(cfg, kind="request_refused", reason="wolframscript_missing")

        _reply(conn, response)
    finally:
        conn.close()


def _reply(conn: socket.socket, body: dict[str, object]) -> None:
    try:
        conn.sendall(json.dumps(body).encode("utf-8"))
    except OSError:
        pass


# ────────── token + socket lifecycle ──────────


def _read_token(cfg: ProxyConfig) -> str:
    """Read the per-launch token file written by the pre_session hook.

    The hook generates the token (32 bytes urlsafe) and writes it mode
    0600 before exec'ing this proxy script. We re-read it on each
    startup so the hook is the only authority on token freshness.
    """
    if not cfg.token_file.exists():
        raise RuntimeError(
            f"token file {cfg.token_file} missing — pre_session hook must "
            f"write it before launching the proxy"
        )
    raw = cfg.token_file.read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError(f"token file {cfg.token_file} is empty")
    return raw


def _preflight_sandbox(cfg: ProxyConfig) -> None:
    if cfg.use_os_sandbox:
        return
    if not cfg.require_sandbox:
        sys.stderr.write(
            "[wolfram-proxy] WARNING: OS sandbox disabled (require_sandbox=false)."
            " Only the string filter + kernel-init Wolfram sandbox are active.\n"
        )
        return
    # Refusal path: fail closed when sandbox-exec is unavailable.
    missing = []
    if shutil.which("sandbox-exec") is None:
        missing.append("`sandbox-exec` not on PATH (macOS-only tool)")
    if not cfg.sandbox_profile.is_file():
        missing.append(f"sandbox profile not found at {cfg.sandbox_profile}")
    sys.stderr.write(
        "[wolfram-proxy] REFUSING TO START: OS-level sandbox is unavailable.\n"
    )
    for m in missing:
        sys.stderr.write(f"  - {m}\n")
    sys.stderr.write(
        "\n"
        "The OS sandbox (macOS sandbox-exec) is the primary defense; without\n"
        "it only the kernel-init Wolfram sandbox is active, which is best-effort.\n"
        "\n"
        "To run without it (e.g. on Linux), set in .botainer/config.yaml:\n"
        "    plugins:\n"
        "      wolfram-sidecar:\n"
        "        require_sandbox: false\n"
    )
    sys.exit(1)


def main() -> int:
    cfg = ProxyConfig()
    _preflight_sandbox(cfg)
    token = _read_token(cfg)

    # Bind unix socket.
    cfg.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cfg.socket_path.exists():
        try:
            cfg.socket_path.unlink()
        except OSError:
            pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(cfg.socket_path))
    except OSError as exc:
        sys.stderr.write(f"[wolfram-proxy] bind failed: {exc}\n")
        return 1
    try:
        os.chmod(cfg.socket_path, 0o600)
    except OSError:
        pass
    server.listen(8)

    def _shutdown(signum: int, _frame: object) -> None:
        sys.stderr.write(f"[wolfram-proxy] received signal {signum}; shutting down\n")
        try:
            server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            server.close()
        except OSError:
            pass
        for p in (cfg.socket_path, cfg.token_file):
            try:
                p.unlink()
            except OSError:
                pass
        _audit(cfg, kind="proxy_stopped", pid=os.getpid())
        sys.exit(0)

    def _reap_children(signum: int, _frame: object) -> None:
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGCHLD, _reap_children)

    _audit(cfg, kind="proxy_started", pid=os.getpid(),
           socket=str(cfg.socket_path),
           sandbox_os=cfg.use_os_sandbox,
           sandbox_kernel=cfg.use_kernel_sandbox)
    sys.stderr.write(
        f"[wolfram-proxy] listening on {cfg.socket_path}; "
        f"sandbox-exec={'ACTIVE' if cfg.use_os_sandbox else 'OFF'} "
        f"kernel-init={'ACTIVE' if cfg.use_kernel_sandbox else 'OFF'} "
        f"pid={os.getpid()}\n"
    )

    try:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            # Fork-per-request so a slow wolframscript run doesn't block
            # other in-flight requests on the same socket.
            pid = os.fork()
            if pid == 0:
                try:
                    server.close()
                except OSError:
                    pass
                handle_client(cfg, token, conn)
                os._exit(0)
            else:
                conn.close()
    finally:
        try:
            server.close()
        except OSError:
            pass
        for p in (cfg.socket_path, cfg.token_file):
            try:
                p.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
