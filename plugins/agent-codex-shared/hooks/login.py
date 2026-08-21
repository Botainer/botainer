#!/usr/bin/env python3
"""agent-codex-shared login: run `codex login` INSIDE A CONTAINER.

REWRITTEN. The previous version ran `subprocess.run(["codex",
"login"])` ON THE HOST, inheriting the full host environment and HOME, and
merely SET `CODEX_HOME` hoping the CLI would honour it. Its own comments
admitted that was unverified, and it carried a detector that fired AFTER the
fact:

    "⚠ DETECTED: codex login modified ~/.codex despite CODEX_HOME=... The L1
     'no host credential store' promise was violated for this login."

So the plugin knew it could write the user's host credential store and reported
it afterwards instead of preventing it. That is detect-don't-prevent, which
CLAUDE.md's "structure over rules" section rejects — and it is a promise the
sibling agent-claude-shared already keeps by construction.

TWO PROBLEMS, ONE FIX. Running the login in the agent-codex container solves
both at once:

  1. THE HOST BINARY IS NOT NEEDED. The user hit this on a cluster login node:
     `codex` is not installed there, so the old code fell through to an API-key
     prompt. The container ships codex, so OAuth works anywhere a runtime does
     — which is exactly why `claude /login` already works on the cluster.

  2. L1 HOLDS BY CONSTRUCTION. Inside `--containall` / `--rm` there IS no host
     `~/.codex` to fall back to. The question "does the CLI honour CODEX_HOME?"
     stops mattering: if it ignores the variable it writes to a path that dies
     with the container, and we detect the missing credential and say so. The
     failure mode moves from "silently wrote to your host store" to "wrote
     nothing, here is why" — fail-closed instead of fail-quiet.

Bind surface is one directory: <shared_dir>:/out. `--no-home` is set on
apptainer for the same reason agent-claude-shared sets it — some builds still
bind $HOME under `--containall`, which would leak ~/.ssh, ~/.aws and ~/.config
into the very login container that exists to avoid them.
"""
from __future__ import annotations

import fcntl
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

AGENT_IMAGE_DOCKER = "botainer/agent-codex:0.1"
_SIF_NAMES = ("botainer-agent-codex.sif", "agent-codex.sif")


def _state_root() -> Path:
    return Path(
        os.environ.get("BOTAINER_STATE_ROOT")
        or os.environ.get("MY_BOTAINER")
        or str(Path.home() / ".botainer")
    )


def _refuse_symlink_in_ancestry(target: Path, root: Path) -> None:
    """A symlinked ancestor could redirect where the credential lands."""
    p = target
    while True:
        if p.is_symlink():
            sys.stderr.write(
                f"refused: {p} is a symlink; a redirected ancestor could send "
                f"the credential somewhere else.\n")
            raise SystemExit(2)
        if p == root or p == p.parent:
            return
        p = p.parent


def _sif_path(state_root: Path) -> Path | None:
    for name in _SIF_NAMES:
        cand = state_root / "images" / name
        if cand.exists():
            return cand
    return None


def _docker_image_present(tag: str) -> bool:
    try:
        out = subprocess.run(["docker", "images", "-q", tag],
                             capture_output=True, text=True, check=False)
        return bool(out.stdout.strip())
    except OSError:
        return False


def _login_cmd(device_auth: bool) -> str:
    """The in-container command. `--device-auth` is the remote-friendly flow."""
    return ("umask 0077 && exec codex login --device-auth" if device_auth
            else "umask 0077 && exec codex login")


def build_docker_argv(image: str, creds_dir: Path,
                      port_lo: int, port_hi: int,
                      device_auth: bool = False) -> list[str]:
    """`docker run` argv for the codex login container.

    Mirrors agent-claude-shared: one bind, run as the HOST user so the
    credential is owned by whoever will read it, and umask 0077 so the file is
    0600 from creation rather than 0644-until-chmod.

    PORTS ARE PUBLISHED ONLY FOR THE BROWSER FLOW. The device-code flow needs
    no callback listener at all, so publishing a port range for it would widen
    the surface for nothing. Fewer holes when fewer holes are needed.
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
    """`apptainer exec` argv. `--no-home` is deliberate — see module docstring."""
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

    WHY THIS EXISTS (user,, logging in on Grace): "It says: codex
    login --device-auth which I can't run. We need to have at least the two
    options for doing this. Maybe even its default doesn't make sense because
    we can't reach its local port with the browser."

    Exactly right. `codex login` defaults to a browser + localhost-callback
    flow. On a remote host that fails twice over: there is no browser, and the
    callback port is unreachable from the laptop where the browser lives.
    agent-claude-shared's answer to the same problem is an ssh tunnel
    (`ssh -L 54545:127.0.0.1:54545 <cluster>`), documented only in a docstring
    the user never sees.

    WHY NO HEURISTIC. My first version sniffed SSH_CONNECTION / DISPLAY to pick
    a default. The user's reply retired it: "you really think I'd default into
    running ssh command line?" No — and the deeper point is that a heuristic
    was never needed, because one option WORKS EVERYWHERE:

        device code      -> works local AND remote, no port, no tunnel
        browser callback -> works local only; remote needs an ssh tunnel

    When one branch is universally correct, detecting which branch you are in
    is cleverness that can only be wrong. Default to the one that always works
    and let anyone who prefers the browser say so. Structure over rules, applied
    to a default rather than to a guard.
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

    The old code decided from `shutil.which("codex")` alone: binary present ->
    OAuth, absent -> paste a key. The user was never asked, and the two paths
    BILL DIFFERENT ACCOUNTS — OAuth uses the ChatGPT/Codex subscription, an API
    key is charged per token to the API account.

    The user's description is the sharper bug report: "it didn't decide for me -
    first it failed, then went to something else. It surprised me." A step
    FAILED and the failure was silently converted into a DIFFERENT ACTION with a
    different cost. Asking is the fix; refusing when nobody is there to ask is
    the safe default, because the silent path was the one that spends money.

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
        "     Runs `codex login` inside a container; opens a browser.\n"
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
        os.write(fd, json.dumps({"api_key": key}).encode("utf-8") + b"\n")
    finally:
        os.close(fd)
    sys.stderr.write(f"\nwrote {creds_file} (mode 0600)\n")
    return 0


def main() -> int:
    state_root = _state_root()
    shared_dir = state_root / "shared-auth" / "agent-codex"
    _refuse_symlink_in_ancestry(shared_dir.parent, state_root)
    shared_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(shared_dir, 0o700)
    except OSError:
        pass
    creds_file = shared_dir / "auth.json"

    docker_bin = shutil.which("docker")
    apptainer_bin = shutil.which("apptainer") or shutil.which("singularity")
    sif = _sif_path(state_root)
    oauth_available = bool(
        (docker_bin and _docker_image_present(AGENT_IMAGE_DOCKER))
        or (apptainer_bin and sif))

    if _choose_method(oauth_available) == "api-key":
        return _run_api_key_paste(creds_file)

    device_auth = _choose_oauth_flow()

    port_lo = int(os.environ.get("BOTAINER_LOGIN_PORT_LO", "54545"))
    port_hi = int(os.environ.get("BOTAINER_LOGIN_PORT_HI", "54549"))
    if port_lo > port_hi:
        port_lo, port_hi = port_hi, port_lo

    # The lock exists to stop two logins fighting over the same CALLBACK PORTS.
    # The device-code flow opens none, so serialising it would be a restriction
    # with nothing behind it — two people can device-auth at once perfectly
    # well. Lock only what actually contends.
    lock_fp = None
    if not device_auth:
        locks_dir = state_root / "state" / "locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = locks_dir / "agent-codex-shared-login.lock"
        try:
            lock_fp = open(lock_path, "w")
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.stderr.write(
                f"refused: another browser-flow codex login is running "
                f"(lock: {lock_path}). Wait, or use the device-code flow, "
                f"which needs no port.\n")
            return 1

    pre_mtime = creds_file.stat().st_mtime_ns if creds_file.exists() else None

    if docker_bin and _docker_image_present(AGENT_IMAGE_DOCKER):
        cmd = build_docker_argv(AGENT_IMAGE_DOCKER, shared_dir,
                                port_lo, port_hi, device_auth)
    else:
        cmd = build_apptainer_argv(apptainer_bin, sif, shared_dir, device_auth)

    sys.stderr.write(
        f"\nRunning `codex login{' --device-auth' if device_auth else ''}` "
        f"INSIDE the agent-codex container.\n"
        f"  credential target: {shared_dir}\n"
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
    # which is now gone — so there is no credential and we say so, instead of
    # the old behaviour of quietly landing one in the host store.
    post_mtime = creds_file.stat().st_mtime_ns if creds_file.exists() else None
    if post_mtime is None or post_mtime == pre_mtime:
        sys.stderr.write(
            f"\nrefused: login finished but no fresh credential appeared at\n"
            f"  {creds_file}\n"
            f"  The codex CLI probably did not honour CODEX_HOME /\n"
            f"  OPENAI_CONFIG_DIR and wrote inside the container, which has\n"
            f"  been discarded. Nothing was written to your host ~/.codex —\n"
            f"  that is the point of running this in a container. Please report\n"
            f"  which env var the installed codex version honours.\n")
        return 1
    try:
        os.chmod(creds_file, 0o600)
    except OSError:
        pass
    sys.stderr.write(f"\nwrote {creds_file} (mode 0600)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
