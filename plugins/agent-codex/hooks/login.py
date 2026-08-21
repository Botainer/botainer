#!/usr/bin/env python3
"""agent-codex login (ISOLATED mode): OAuth or an API key, per project.

ISOLATED mode keeps credentials PER PROJECT — nothing is shared across
projects, which is the whole point of choosing it over shared mode.

WHAT CHANGED (#62). This hook used to offer ONLY an API-key paste,
under a docstring asserting "OpenAI doesn't have an OAuth flow like Anthropic".
That is false — codex has had one, and agent-codex-shared has been using it all
day. The consequence was a cost cliff attached to the MORE SECURE option: a user
who wanted per-project isolation was forced onto per-token API billing, while
shared and broker modes could use the ChatGPT/Codex subscription. Isolation and
billing are unrelated choices and should not have been welded together.

OAuth now runs the same way agent-codex-shared runs it — `codex login` INSIDE
the agent-codex container, writing to a bind — with one difference: the bind is
this project's own credential dir rather than the host-wide shared one. That is
the only thing isolated mode needs to change.

Layout, either method:
    <state_root>/state/<uuid>/data/agent-codex/profiles/<profile>/
        auth.json   OAuth tokens, written by codex itself (mode 0600)
        api_key     raw API key, one line, read by entrypoint_wrap.sh

The API key stays a RAW file, not JSON: the image's entrypoint does
`export OPENAI_API_KEY="$(cat "$OPENAI_API_KEY_FILE")"`, so wrapping it in JSON
would export a JSON blob as the key. agent-codex-shared writes JSON because
nothing cats its file. Two formats, two readers, deliberately.
"""
from __future__ import annotations

import fcntl
import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

AGENT_IMAGE_DOCKER = "botainer/agent-codex:0.1"
_SIF_NAMES = ("botainer-agent-codex.sif", "agent-codex.sif")

# M3 / M4 (2026-06-17 — meta-pattern parity with the agent-claude login hooks):
# mirror of the launcher's `_VALID_STATE_DIR_PATTERN` in botainer/state/dir.py.
_VALID_STATE_DIR_PATTERN = re.compile(r"^[A-Za-z0-9._/\-~]+$")


def _validated_state_root() -> Path:
    """Resolve MY_BOTAINER / BOTAINER_STATE_ROOT with the same character-class
    and `..` traversal checks the launcher applies. Honors the same env-var
    precedence as the launcher (BOTAINER_STATE_ROOT wins over MY_BOTAINER)."""
    val = (
        os.environ.get("BOTAINER_STATE_ROOT")
        or os.environ.get("MY_BOTAINER")
    )
    if not val:
        return Path.home() / ".botainer"
    if not _VALID_STATE_DIR_PATTERN.match(val):
        raise SystemExit(
            f"agent-codex login: MY_BOTAINER contains invalid characters: "
            f"{val!r}. Allowed: [A-Za-z0-9._/~-]"
        )
    expanded = Path(val).expanduser()
    if ".." in expanded.parts:
        raise SystemExit(
            f"agent-codex login: MY_BOTAINER contains '..' (path traversal): "
            f"{val!r}"
        )
    return expanded


def _refuse_symlink_in_ancestry(target: Path, root: Path) -> None:
    """Walk lexically (not via resolve) from target up to root inclusive and
    refuse if any existing ancestor is a symlink. A `mkdir -p` would follow
    symlinks transparently, letting an attacker who controls a parent dir
    redirect where the credential file lands."""
    import stat as _stat
    cur = target
    components: list[Path] = []
    while True:
        components.append(cur)
        if cur == root:
            break
        parent = cur.parent
        if parent == cur:
            raise SystemExit(
                f"agent-codex login: target {target} is not under state "
                f"root {root}; refusing to walk above the state tree."
            )
        cur = parent
    for c in reversed(components):
        try:
            st = os.lstat(c)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if _stat.S_ISLNK(st.st_mode):
            raise SystemExit(
                f"agent-codex login: refusing to write — ancestor path "
                f"component {c} is a symlink. A `mkdir -p` here would follow "
                f"the symlink and the credential could land outside {root}."
            )


def _sif_path(state_root: Path) -> Path | None:
    for name in _SIF_NAMES:
        p = state_root / "images" / name
        if p.exists():
            return p
    return None


def _docker_image_present(tag: str) -> bool:
    try:
        r = subprocess.run(["docker", "image", "inspect", tag],
                           capture_output=True, check=False)
        return r.returncode == 0
    except (OSError, ValueError):
        return False


def _login_cmd(device_auth: bool) -> str:
    """The in-container command. `--device-auth` is the remote-friendly flow."""
    return ("umask 0077 && exec codex login --device-auth" if device_auth
            else "umask 0077 && exec codex login")


def build_docker_argv(image: str, creds_dir: Path,
                      port_lo: int, port_hi: int,
                      device_auth: bool = False) -> list[str]:
    """`docker run` argv for the codex login container.

    Identical to agent-codex-shared's builder except for what `creds_dir`
    points at — per-project here, host-wide there. A drift test pins the two
    against each other, because duplicated launch logic that silently diverges
    is how one auth mode ends up hardened and the other not.
    """
    argv = [
        "docker", "run", "--rm", "-it", "--init",
        # --entrypoint IS THE POINT. The image sets
        # ENTRYPOINT ["agent-codex-entrypoint"], whose last line is
        # `exec codex "$@"` — so `docker run <image> sh -c "..."` does NOT run
        # sh, it runs `codex sh -c "umask 0077 && exec codex login"`. codex has
        # its own -c flag (config override) and died on it:
        #     Error parsing -c overrides: Invalid override (missing '='):
        #     umask 0077 && exec codex login --device-auth
        # Found by the user's first real login,. `apptainer exec`
        # bypasses %runscript, so the HPC path was correct and only docker was
        # broken — the same inverted parity as the missing --init (#108).
        "--entrypoint", "sh",
        "--user", f"{os.getuid()}:{os.getgid()}",
    ]
    if not device_auth:
        argv += ["-p", f"127.0.0.1:{port_lo}-{port_hi}:{port_lo}-{port_hi}"]
    argv += [
        "-v", f"{creds_dir}:/out",
        "-e", "CODEX_HOME=/out",
        "-e", "OPENAI_CONFIG_DIR=/out",
        image,
        # No "sh" here: --entrypoint already made sh the program, so this is
        # sh's own argv. Passing it again would make it sh's $0.
        "-c", _login_cmd(device_auth),
    ]
    return argv


def build_apptainer_argv(apptainer_bin: str, sif_path: Path,
                         creds_dir: Path,
                         device_auth: bool = False) -> list[str]:
    """`apptainer exec` argv.

    `--no-home` is load-bearing, not tidiness: some apptainer builds bind $HOME
    even under --containall, which would put ~/.ssh and ~/.aws inside the very
    login container that exists to avoid touching them.
    """
    return [
        apptainer_bin, "exec",
        "--containall", "--cleanenv", "--no-home",
        "--bind", f"{creds_dir}:/out",
        "--env", "CODEX_HOME=/out",
        "--env", "OPENAI_CONFIG_DIR=/out",
        str(sif_path),
        "sh", "-c", _login_cmd(device_auth),
    ]


def _choose_oauth_flow() -> bool:
    """Return True for --device-auth. DEVICE CODE IS THE DEFAULT, always.

    One option works EVERYWHERE (device code: no port, no tunnel, fine over
    ssh); the other works locally only. When one branch is universally correct,
    detecting which branch you are in is cleverness that can only be wrong — an
    earlier version sniffed SSH_CONNECTION/DISPLAY and the user retired it:
    "you really think I'd default into running ssh command line?"
    """
    forced = (os.environ.get("BOTAINER_CODEX_OAUTH_FLOW") or "").strip().lower()
    if forced in ("device", "device-auth", "deviceauth"):
        return True
    if forced in ("browser", "callback", "local"):
        return False
    if forced:
        sys.stderr.write(
            f"refused: BOTAINER_CODEX_OAUTH_FLOW={forced!r} is not "
            f"'device' or 'browser'.\n")
        raise SystemExit(2)

    sys.stderr.write(
        "\nWhich OAuth flow?\n"
        "\n"
        "  1) Device code  [default] — codex prints a short code and a URL.\n"
        "     Open the URL on ANY device (laptop, phone) and enter the code.\n"
        "     No browser needed here, no open port, works fine over ssh.\n"
        "\n"
        "  2) Browser callback — opens a URL that redirects to a port on THIS\n"
        "     machine. Only works if a browser here can reach it. From a\n"
        "     remote host you would first need:\n"
        "         ssh -L 54545:127.0.0.1:54545 <this-host>\n"
        "\n")
    if not sys.stdin.isatty():
        return True
    choice = input("Choose 1 or 2 [1]: ").strip() or "1"
    if choice == "1":
        return True
    if choice == "2":
        return False
    sys.stderr.write("refused: answer 1 or 2.\n")
    raise SystemExit(2)


def _choose_method(oauth_available: bool) -> str:
    """Return "oauth" or "api-key" — ASKED, never inferred.

    The two paths BILL DIFFERENT ACCOUNTS: OAuth uses the ChatGPT/Codex
    subscription, an API key is charged per token to the API account. Refusing
    when nobody is there to ask is the safe default, because the silent path is
    the one that spends money.

    Non-interactive callers set BOTAINER_CODEX_LOGIN_METHOD=oauth|api-key.
    """
    forced = (os.environ.get("BOTAINER_CODEX_LOGIN_METHOD") or "").strip().lower()
    if forced in ("oauth", "api-key", "apikey", "key"):
        return "oauth" if forced == "oauth" else "api-key"
    if forced:
        sys.stderr.write(
            f"refused: BOTAINER_CODEX_LOGIN_METHOD={forced!r} is not "
            f"'oauth' or 'api-key'.\n")
        raise SystemExit(2)

    sys.stderr.write(
        "\nHow do you want to authenticate Codex? These bill DIFFERENT accounts.\n"
        "\n"
        "  1) OAuth  — your ChatGPT/Codex SUBSCRIPTION.\n"
        "     Runs `codex login` inside a container.\n"
        f"     Container runtime + image available: "
        f"{'YES' if oauth_available else 'NO'}\n"
        "\n"
        "  2) API key — your OpenAI API ACCOUNT, charged PER TOKEN.\n"
        "     Separate from any subscription you already pay for.\n"
        "\n")
    if not oauth_available:
        sys.stderr.write(
            "  Option 1 is unavailable: no container runtime on PATH, or the\n"
            "  agent-codex image is not built. Fix with:\n"
            "      botainer image build agent-codex\n"
            "  (on a cluster, apptainer is usually on COMPUTE nodes only,\n"
            "   so `module load apptainer` on the login node typically does\n"
            "   not help — get an allocation with `salloc` and re-run there).\n\n")
    if not sys.stdin.isatty():
        sys.stderr.write(
            "refused: no TTY to ask, and no BOTAINER_CODEX_LOGIN_METHOD set.\n"
            "  Not choosing a billing method on your behalf. Set\n"
            "  BOTAINER_CODEX_LOGIN_METHOD=oauth or =api-key and re-run.\n")
        raise SystemExit(2)

    choice = input("Choose 1 (OAuth) or 2 (API key), or Ctrl-C to cancel: ").strip()
    if choice == "1":
        if not oauth_available:
            sys.stderr.write(
                "\nrefused: OAuth needs the agent-codex container.\n"
                "  Build it with `botainer image build agent-codex` and re-run.\n")
            raise SystemExit(2)
        return "oauth"
    if choice == "2":
        return "api-key"
    sys.stderr.write("refused: answer 1 or 2.\n")
    raise SystemExit(2)


def _run_api_key_paste(creds_file: Path) -> int:
    """Store a RAW key. entrypoint_wrap.sh cats this file into OPENAI_API_KEY,
    so it must not be JSON-wrapped (that would export the JSON as the key)."""
    sys.stderr.write(
        "\nPaste an OpenAI API key. This bills your API ACCOUNT per token.\n"
        "Get one at platform.openai.com → API keys. Ctrl-C to cancel.\n\n")
    key = (getpass.getpass("OpenAI API key (no echo): ").strip()
           if sys.stdin.isatty() else sys.stdin.read().strip())
    if not key:
        sys.stderr.write("refused: empty key\n")
        return 2
    if not key.startswith("sk-"):
        sys.stderr.write("warning: key doesn't look like sk-*; proceeding\n")
    fd = os.open(str(creds_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key.encode("utf-8") + b"\n")
    finally:
        os.close(fd)
    sys.stderr.write(f"\nwrote {creds_file} (mode 0600)\n")
    return 0


def main() -> int:
    uid = os.environ.get("BOTAINER_PROJECT_UUID", "")
    # M3: validate MY_BOTAINER at this entry point (matches launcher).
    state_root = _validated_state_root()
    profile = os.environ.get("BOTAINER_PROFILE", "default")
    if not uid:
        sys.stderr.write("login: no BOTAINER_PROJECT_UUID in env\n")
        return 1

    creds_dir = state_root / "state" / uid / "data" / "agent-codex" / "profiles" / profile
    # M4: refuse if any ancestor is a symlink before mkdir would follow it.
    _refuse_symlink_in_ancestry(creds_dir, state_root)
    creds_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(creds_dir, 0o700)
    except OSError:
        pass

    docker_bin = shutil.which("docker")
    apptainer_bin = shutil.which("apptainer") or shutil.which("singularity")
    sif = _sif_path(state_root)
    oauth_available = bool(
        (docker_bin and _docker_image_present(AGENT_IMAGE_DOCKER))
        or (apptainer_bin and sif))

    if _choose_method(oauth_available) == "api-key":
        return _run_api_key_paste(creds_dir / "api_key")

    device_auth = _choose_oauth_flow()
    auth_file = creds_dir / "auth.json"

    port_lo = int(os.environ.get("BOTAINER_LOGIN_PORT_LO", "54545"))
    port_hi = int(os.environ.get("BOTAINER_LOGIN_PORT_HI", "54549"))
    if port_lo > port_hi:
        port_lo, port_hi = port_hi, port_lo

    # The lock exists to stop two logins fighting over the same CALLBACK PORTS.
    # The device-code flow opens none, so serialising it would be a restriction
    # with nothing behind it. Lock only what actually contends.
    lock_fp = None
    if not device_auth:
        locks_dir = state_root / "state" / "locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = locks_dir / "agent-codex-login.lock"
        try:
            lock_fp = open(lock_path, "w")
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.stderr.write(
                f"refused: another browser-flow codex login is running "
                f"(lock: {lock_path}). Wait, or use the device-code flow, "
                f"which needs no port.\n")
            return 1

    pre_mtime = auth_file.stat().st_mtime_ns if auth_file.exists() else None

    if docker_bin and _docker_image_present(AGENT_IMAGE_DOCKER):
        cmd = build_docker_argv(AGENT_IMAGE_DOCKER, creds_dir,
                                port_lo, port_hi, device_auth)
    else:
        cmd = build_apptainer_argv(apptainer_bin, sif, creds_dir, device_auth)

    sys.stderr.write(
        f"\nRunning `codex login{' --device-auth' if device_auth else ''}` "
        f"INSIDE the agent-codex container.\n"
        f"  credential target: {creds_dir}\n"
        f"  THIS PROJECT ONLY — no other project can see it.\n"
        f"  your host ~/.codex is NOT visible to it.\n")
    if device_auth:
        sys.stderr.write(
            "  Codex will print a URL and a short code — open the URL on any\n"
            "  device and enter the code. No port or tunnel is needed.\n\n")
    else:
        sys.stderr.write(
            f"  Callback listens on 127.0.0.1:{port_lo}-{port_hi} of THIS host.\n"
            f"  If your browser is elsewhere: ssh -L {port_lo}:127.0.0.1:{port_lo} "
            f"<this-host>\n\n")
    rc = subprocess.run(cmd, check=False).returncode
    if rc != 0:
        sys.stderr.write(f"\ncodex login exited {rc}\n")
        return rc

    # FAIL-CLOSED. If the CLI ignored CODEX_HOME it wrote inside the container,
    # which is now gone — so there is no credential and we say so, rather than
    # reporting success and leaving the next session to discover it.
    post_mtime = auth_file.stat().st_mtime_ns if auth_file.exists() else None
    if post_mtime is None or post_mtime == pre_mtime:
        sys.stderr.write(
            f"\nrefused: login finished but no fresh credential appeared at\n"
            f"  {auth_file}\n"
            f"  The codex CLI probably did not honour CODEX_HOME /\n"
            f"  OPENAI_CONFIG_DIR and wrote inside the container, which has\n"
            f"  been discarded. Nothing was written to your host ~/.codex —\n"
            f"  that is the point of running this in a container. Please report\n"
            f"  which env var the installed codex version honours.\n")
        return 1
    try:
        os.chmod(auth_file, 0o600)
    except OSError:
        pass
    sys.stderr.write(f"\nwrote {auth_file} (mode 0600)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
