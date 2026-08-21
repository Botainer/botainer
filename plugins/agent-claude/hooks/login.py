#!/usr/bin/env python3
"""agent-claude (isolated) login: run `claude /login` INSIDE A CONTAINER.

Mirror of agent-claude-shared/hooks/login.py, but writes to the
PER-PROJECT credentials dir under
`<state_root>/state/<uuid>/data/agent-claude/profiles/<profile>/`
instead of the host-wide shared-auth dir.

The two scripts are intentionally kept structurally identical (same
helpers, same argv builders, same message format) — diverging only
on which directory the credential lands in. See internal design note DN-036 for
the planned extraction into a shared helper module.

User direction (a standing user direction): the launcher MUST NEVER
touch the host's credential store (macOS Keychain, ~/.claude/,
~/.config/anthropic/). All four code paths (docker|apptainer
× shared|isolated) must state this promise to the user explicitly.

Supports two runtimes:
  - Docker (laptop default)
  - Apptainer (HPC)

Requirements:
  - One of `docker` / `apptainer` on PATH
  - The agent-claude image is built (`botainer image build agent-claude`
    or `botainer image build agent-claude --runtime apptainer`)
  - You are inside a botainer project (we need BOTAINER_PROJECT_UUID)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

AGENT_IMAGE_DOCKER = "botainer/agent-claude:0.1"

_APPTAINER_ENV_PREFIXES: tuple[str, ...] = (
    "APPTAINERENV_",
    "SINGULARITYENV_",
)

# M3 (sharp-edges M1, deferred): the hook must apply the same
# MY_BOTAINER validation `botainer/state/dir.py` does at the launcher entry
# point. Safe when invoked via `botainer auth login` (parent re-resolves),
# but unsafe if the hook is invoked directly. Mirrors the launcher's
# `_VALID_STATE_DIR_PATTERN` exactly (drift = the validation has a different
# allowlist on the two paths, which is exactly how a sibling-path bypass
# class arises).
_VALID_STATE_DIR_PATTERN = re.compile(r"^[A-Za-z0-9._/\-~]+$")


def _validated_state_root() -> Path:
    """Resolve MY_BOTAINER (or default ~/.botainer) with the same character-
    class + `..` traversal checks the launcher applies. Refuses with a clear
    error rather than silently letting the credential land at an
    attacker-controlled path."""
    val = (
        os.environ.get("BOTAINER_STATE_ROOT")
        or os.environ.get("MY_BOTAINER")
    )
    if not val:
        return Path.home() / ".botainer"
    if not _VALID_STATE_DIR_PATTERN.match(val):
        raise SystemExit(
            f"agent-claude login: MY_BOTAINER contains invalid characters: "
            f"{val!r}. Allowed: [A-Za-z0-9._/~-]"
        )
    expanded = Path(val).expanduser()
    if ".." in expanded.parts:
        raise SystemExit(
            f"agent-claude login: MY_BOTAINER contains '..' (path traversal): "
            f"{val!r}"
        )
    return expanded


def _refuse_symlink_in_ancestry(target: Path, root: Path) -> None:
    """M4 (sharp-edges M4, deferred): walk every ancestor of
    `target` up to (but not above) `root` and refuse if any existing
    component is a symlink. A `Path.mkdir(parents=True)` would follow
    symlinks transparently, so an attacker who controls a parent dir as a
    symlink could redirect the credential file to a path of their choice.
    `root` is the state_root we already validated; we don't walk above it.
    """
    import stat as _stat
    # Walk lexically (not via resolve) from target up to root inclusive. If
    # we resolved on the way up, a symlinked ancestor would silently shift
    # us out of the tree and we'd skip the very check we want to perform.
    components: list[Path] = []
    cur = target
    while True:
        components.append(cur)
        if cur == root:
            break
        parent = cur.parent
        if parent == cur:
            # Reached filesystem root without finding `root` — caller
            # passed a target outside `root`; refuse defensively.
            raise SystemExit(
                f"agent-claude login: target {target} is not under state "
                f"root {root}; refusing to walk above the state tree."
            )
        cur = parent
    # Check root-first so an attacker-controlled state_root symlink is
    # caught before we descend into anything below it.
    for c in reversed(components):
        try:
            st = os.lstat(c)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if _stat.S_ISLNK(st.st_mode):
            raise SystemExit(
                f"agent-claude login: refusing to write — ancestor path "
                f"component {c} is a symlink. A `mkdir -p` here would follow "
                f"the symlink and the credential could land outside "
                f"{root}. Remove the symlink and re-run."
            )


# ─────────────────────── argv builders (pure functions; tested) ───────────────────────


def build_docker_argv(
    image: str,
    creds_dir: Path,
    port_lo: int,
    port_hi: int,
) -> list[str]:
    """See plugins/agent-claude-shared/hooks/login.py for design notes.

    Runs as the HOST user (`--user`) for the same reason the shared variant
    does — without it, on native-Linux Docker the credential lands owned by
    the image's UID 1001 and the host user can't read it (solidity-check
    finding; matches adapters/docker.py).
    """
    return [
        "docker", "run", "--rm", "-it",
        # --entrypoint, for the same reason the codex login needs it: the image
        # sets ENTRYPOINT ["agent-claude-entrypoint"], whose last line is
        # `exec claude "$@"`. Without this, `docker run <image> sh -c "..."`
        # runs `claude sh -c "umask 0077 && exec claude"` — the login command is
        # handed to the AGENT as arguments instead of being executed.
        #
        # The codex login died loudly on this (codex has its own -c flag);
        # claude accepted the junk argv and started anyway, which is why the
        # defect went unnoticed here. Same bug, quieter symptom. `apptainer
        # exec` bypasses %runscript, so only the docker path was affected.
        "--entrypoint", "sh",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-p", f"127.0.0.1:{port_lo}-{port_hi}:{port_lo}-{port_hi}",
        "-v", f"{creds_dir}:/out",
        "-e", "CLAUDE_CONFIG_DIR=/out",
        image,
        # No "sh": --entrypoint already made it the program.
        "-c", "umask 0077 && exec claude",
    ]


def build_apptainer_argv(
    apptainer_bin: str,
    sif_path: Path,
    creds_dir: Path,
) -> list[str]:
    """See plugins/agent-claude-shared/hooks/login.py for design notes.

    M7 (adversarial #3, deferred): `--no-home` is required to make
    the capability-surface "no other mounts" promise hold across apptainer
    versions. Without it, some apptainer builds still bind `$HOME` even under
    `--containall`, leaking ~/.ssh, ~/.aws, ~/.config/* into the OAuth login
    container — exactly the bind-mode surface the login flow exists to avoid.
    """
    return [
        apptainer_bin, "exec",
        "--containall",
        "--cleanenv",
        "--no-home",
        "--bind", f"{creds_dir}:/out",
        str(sif_path),
        "sh", "-c", "umask 0077 && exec claude",
    ]


def build_apptainer_subenv(parent_env: dict[str, str]) -> dict[str, str]:
    """See plugins/agent-claude-shared/hooks/login.py for design notes."""
    out = {
        k: v for k, v in parent_env.items()
        if not any(k.startswith(p) for p in _APPTAINER_ENV_PREFIXES)
    }
    out["APPTAINERENV_CLAUDE_CONFIG_DIR"] = "/out"
    out["SINGULARITYENV_CLAUDE_CONFIG_DIR"] = "/out"
    return out


def _docker_image_present(tag: str) -> bool:
    out = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True, text=True, check=False,
    )
    return out.returncode == 0


def _resolve_apptainer_sif(state_root: Path) -> Path | None:
    """Locate a built .sif. M2 (sharp-edges M2, deferred):
    `apptainer exec` runs the .sif's entrypoint as the calling user, so the
    image processes the OAuth token — a swapped or attacker-owned .sif
    would compromise the credential. Apply three guards on each candidate:
      (a) refuse if it is a symlink (we don't follow into attacker-chosen
          paths; the image MUST be a real file under state_root/images),
      (b) refuse if it's not owned by the current user,
      (c) refuse if it is group- or world-writable.
    Any failed guard skips that candidate and prints a clear refusal so the
    user knows why the image they think is built is being ignored.
    """
    import stat as _stat
    candidates = [
        state_root / "images" / "botainer-agent-claude.sif",
        state_root / "images" / "agent-claude.sif",
    ]
    for c in candidates:
        if not c.exists():
            continue
        try:
            st = os.lstat(c)
        except OSError:
            continue
        if _stat.S_ISLNK(st.st_mode):
            sys.stderr.write(
                f"agent-claude login: refusing .sif at {c}: it is a symlink. "
                f"apptainer would exec the symlink target, which could be an "
                f"attacker-chosen file. Remove the symlink and rebuild.\n"
            )
            continue
        if st.st_uid != os.getuid():
            sys.stderr.write(
                f"agent-claude login: refusing .sif at {c}: owned by uid "
                f"{st.st_uid}, not the current user {os.getuid()}. apptainer "
                f"would still exec it as YOU, processing your OAuth token in "
                f"someone else's image. Rebuild as yourself.\n"
            )
            continue
        if st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
            sys.stderr.write(
                f"agent-claude login: refusing .sif at {c}: group- or world-"
                f"writable (mode {oct(_stat.S_IMODE(st.st_mode))}). Any "
                f"co-tenant could swap the image between rebuild and login. "
                f"`chmod go-w {c}` and retry.\n"
            )
            continue
        return c
    return None


def _looks_like_credential(blob: str) -> bool:
    blob = blob.strip()
    if not blob:
        return False
    if blob.startswith("oat-") or blob.startswith("sk-ant-"):
        return len(blob) >= 32
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    return bool(obj.get("claudeAiOauth") or obj.get("api_key") or obj.get("key"))


def _format_login_help(
    *,
    runtime: str,
    image_or_sif: str,
    creds_dir: Path,
    port_lo: int,
    port_hi: int,
    use_color: bool,
) -> str:
    """Per-project (isolated) variant of the shared message. Same
    structure across runtimes; only the runtime-specific paragraph
    differs."""
    BOLD, YELLOW, CYAN, RED = "1", "33", "36", "31"
    def _c(text: str, *codes: str) -> str:
        if not use_color or not codes:
            return text
        return f"\033[{';'.join(codes)}m{text}\033[0m"

    title = (
        "Starting one-shot Docker container for Anthropic OAuth (PER-PROJECT mode)."
        if runtime == "docker"
        else "Starting one-shot Apptainer container for Anthropic OAuth (PER-PROJECT mode)."
    )
    if runtime == "docker":
        runtime_block = f"  Port range:  127.0.0.1:{port_lo}-{port_hi} (for OAuth callback)\n"
    else:
        runtime_block = (
            "  Network:     apptainer shares host network namespace.\n"
            "               (No -p publish; the callback binds host:localhost directly.)\n"
        )

    keychain_promise = _c(
        "No host credential store is consulted (no macOS Keychain,\n"
        "no `~/.claude/`, no `~/.config/anthropic/`). The credential\n"
        "lands ONLY in the bound directory above.\n"
        "The credential will live in this project's state dir; other\n"
        "projects on this host won't see it.\n",
        BOLD,
    )

    return (
        _c(title + "\n", BOLD, CYAN)
        + f"  Image:       {image_or_sif}\n"
        + f"  Project dir: {creds_dir}\n"
        + runtime_block
        + "\n"
        + keychain_promise
        + "\n"
        + _c("Inside the Claude Code TUI that appears:\n", BOLD, YELLOW)
        + _c("  1. Type ", BOLD, YELLOW) + _c("/login", BOLD)
        + _c(", then complete the OAuth flow in your browser.\n", BOLD, YELLOW)
        + (_c(
            "     If you're on an HPC compute node, the localhost OAuth callback\n"
            "     may not be reachable from your laptop's browser. Two options:\n"
            "       • If Claude offers a device-code / paste-code mode, use that.\n"
            "       • Otherwise, open a new terminal on your LAPTOP and run:\n"
            f"           ssh -L {port_lo}:127.0.0.1:{port_lo} <cluster>\n"
            "         Then open Claude's URL in your laptop browser.\n", CYAN,
        ) if runtime == "apptainer" else "")
        + _c("  2. When you see 'Login successful', ", BOLD, YELLOW)
        + _c("you MUST type /exit", BOLD, RED)
        + _c(" to leave the TUI.\n", BOLD, YELLOW)
        + _c(
            "     If you skip /exit, the container keeps running and botainer\n"
            "     can't finish the flow (no credential verification, no chmod).\n",
            BOLD, RED,
        )
        + "\n"
    )


def main() -> int:
    uid = os.environ.get("BOTAINER_PROJECT_UUID", "")
    # M3: validate MY_BOTAINER characters + `..` traversal at this entry
    # point (matches what botainer/state/dir.py does in the launcher path).
    state_root = _validated_state_root()
    profile = os.environ.get("BOTAINER_PROFILE", "default")
    if not uid:
        sys.stderr.write(
            "agent-claude login: BOTAINER_PROJECT_UUID env not set.\n"
            "  Isolated-mode login must run inside a botainer project; cd to one and retry.\n"
        )
        return 1

    creds_dir = state_root / "state" / uid / "data" / "agent-claude" / "profiles" / profile
    # M4: walk every ancestor and refuse if any is a symlink BEFORE the
    # mkdir, so a parent symlink can't redirect where the credential lands.
    _refuse_symlink_in_ancestry(creds_dir, state_root)
    creds_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(creds_dir, 0o700)
    except OSError:
        pass

    docker_bin = shutil.which("docker")
    apptainer_bin = shutil.which("apptainer") or shutil.which("singularity")

    if not docker_bin and not apptainer_bin:
        sys.stderr.write(
            "agent-claude login: no container runtime on PATH.\n"
            "  Need either `docker` (laptop) or `apptainer` (HPC).\n"
            "  Apptainer usually lives on COMPUTE nodes only, so\n"
            "  `module load apptainer` on a login node typically does NOT\n"
            "  help. Get an allocation first:  salloc -t 60 -c 4\n"
            "  then re-run this there. On a laptop, use docker.\n")
        return 1

    port_lo = int(os.environ.get("BOTAINER_LOGIN_PORT_LO", "54545"))
    port_hi = int(os.environ.get("BOTAINER_LOGIN_PORT_HI", "54549"))
    if port_lo > port_hi:
        port_lo, port_hi = port_hi, port_lo

    # Task #156: refuse concurrent login launches. Two terminals running
    # `botainer plugin agent-claude login` in parallel would both try to
    # bind the same 54545-54549 range; the second would crash with
    # "bind: address already in use". Worse: if the user customized one
    # of them with BOTAINER_LOGIN_PORT_LO, the ranges could partially
    # overlap and silently steal the OAuth callback. Single-instance
    # mutex via flock on a user-scope lock file.
    import fcntl as _fcntl
    locks_dir = state_root / "state" / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / "agent-claude-login.lock"
    try:
        _lock_fp = open(lock_path, "w")
        _fcntl.flock(_lock_fp.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        sys.stderr.write(
            "agent-claude login: another login is already running "
            f"(lock: {lock_path}). Wait for it to finish, or kill it.\n"
        )
        return 1

    use_color = sys.stderr.isatty() and not os.environ.get("NO_COLOR")

    creds = creds_dir / ".credentials.json"
    pre_mtime: int | None = creds.stat().st_mtime_ns if creds.exists() else None

    if docker_bin:
        if not _docker_image_present(AGENT_IMAGE_DOCKER):
            sys.stderr.write(
                f"agent-claude login: image {AGENT_IMAGE_DOCKER!r} is not built.\n"
                f"  Build it first (~8-12 min the first time):\n"
                f"      botainer image build agent-claude\n"
                f"  Then retry the login.\n"
            )
            return 1
        sys.stderr.write(_format_login_help(
            runtime="docker",
            image_or_sif=AGENT_IMAGE_DOCKER,
            creds_dir=creds_dir,
            port_lo=port_lo,
            port_hi=port_hi,
            use_color=use_color,
        ))
        cmd = build_docker_argv(AGENT_IMAGE_DOCKER, creds_dir, port_lo, port_hi)
        sub_env = None
    else:
        sif_path = _resolve_apptainer_sif(state_root)
        if sif_path is None:
            sys.stderr.write(
                f"agent-claude login: no agent-claude .sif found.\n"
                f"  Looked for:\n"
                f"    {state_root}/images/botainer-agent-claude.sif\n"
                f"    {state_root}/images/agent-claude.sif\n"
                f"  Build one with:\n"
                f"      botainer image build agent-claude --runtime apptainer\n"
            )
            return 1
        sys.stderr.write(_format_login_help(
            runtime="apptainer",
            image_or_sif=str(sif_path),
            creds_dir=creds_dir,
            port_lo=port_lo,
            port_hi=port_hi,
            use_color=use_color,
        ))
        cmd = build_apptainer_argv(apptainer_bin, sif_path, creds_dir)
        sub_env = build_apptainer_subenv(dict(os.environ))

    try:
        rc = subprocess.run(cmd, env=sub_env, check=False).returncode
    except KeyboardInterrupt:
        sys.stderr.write(
            "\nagent-claude login: interrupted by user. Aborting.\n"
            "  Any partial credential file from this attempt has not been\n"
            "  validated or mode-protected. Re-run the login.\n"
        )
        return 130

    if creds.exists() and pre_mtime is not None:
        post_mtime = creds.stat().st_mtime_ns
        if post_mtime == pre_mtime:
            sys.stderr.write(
                f"\nagent-claude login: container exited (rc={rc}) but the\n"
                f"credential file at {creds} was not updated. If you saw\n"
                f"'Login successful', the credential may have landed at a\n"
                f"different path inside the container, or you didn't type /exit.\n"
            )
            return 2

    if not creds.exists():
        sys.stderr.write(
            f"\nagent-claude login: container exited (rc={rc}) but no\n"
            f"credential file appeared at {creds}.\n"
            "\n"
            "If you saw 'Login successful' in the TUI, the credential may\n"
            "have landed at a different path inside the container, or you\n"
            "didn't type /exit. Try again, watching for the /exit step.\n"
        )
        return 2

    try:
        os.chmod(creds, 0o600)
    except OSError as exc:
        sys.stderr.write(f"warning: could not chmod 0600 {creds}: {exc}\n")

    try:
        blob = creds.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"warning: could not re-read {creds}: {exc}\n")
        return 0
    if not _looks_like_credential(blob):
        # Task #299: was echoing first 100 bytes of the file to stderr.
        # If the file IS a credential and we just misread its shape,
        # those 100 bytes can contain the real secret. Replace with
        # length + first 4 chars only (enough to identify malformed JSON
        # vs binary garbage without leaking the secret).
        # fix: `blob` is a str (creds.read_text returns str), so
        # the prior bytes-style decode call on the 4-char head raised
        # AttributeError on this very path — turning a clean `return 2` into a
        # traceback. Sanitize the str directly to keep the non-printable
        # safety intent without any decode.
        head_safe = "".join(c if c.isprintable() else "?" for c in blob[:4])
        sys.stderr.write(
            f"\nagent-claude login: a file was written at {creds}\n"
            f"but it doesn't look like a valid Claude credential (too short or\n"
            f"wrong shape). Length: {len(blob)} bytes; starts: {head_safe!r}\n"
            f"To inspect manually (carefully): less {creds}\n"
        )
        return 2

    sys.stderr.write(
        f"\n✓ credential saved to {creds} (mode 0600).\n"
        f"  This project will use it on next `botainer start`.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
