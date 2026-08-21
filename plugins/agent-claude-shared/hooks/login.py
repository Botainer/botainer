#!/usr/bin/env python3
"""agent-claude-shared login: run `claude /login` INSIDE A CONTAINER.

The OAuth flow runs inside a one-shot container so it:
  - NEVER touches any host credential store (no macOS Keychain, no
    `~/.claude/`, no `~/.config/anthropic/`).
  - Writes the credential to a host directory we control:
        <state_root>/shared-auth/agent-claude/.credentials.json
  - Wraps `claude` with `sh -c 'umask 0077 && exec claude'` so the
    credential file gets mode 0600 from creation, not 0644-until-the
    -host-chmod (defense against the write→chmod race).

Supports two runtimes:
  - Docker (laptop default): `docker run --rm -it ...`
  - Apptainer (HPC):         `apptainer exec --containall --cleanenv ...`

Runtime selection: prefer Docker if both are on PATH (laptop default
and the more battle-tested OAuth-callback path). Fall back to
Apptainer when Docker isn't present.

Image / .sif must be pre-built:
  - Docker:    `botainer image build agent-claude`
  - Apptainer: `botainer image build agent-claude --runtime apptainer`

User-facing flow:
  1. Run `botainer auth login --shared --agent claude`.
  2. Container starts; Claude Code TUI inside.
  3. Type `/login`. Claude prints an OAuth URL.
     - On a laptop with Docker: open the URL in your local browser;
       the localhost callback reaches Claude through Docker's port
       mapping.
     - On an HPC compute node with Apptainer: open the URL on your
       LAPTOP'S browser. If Claude only offers a localhost callback
       (not device-code paste), the callback won't reach the compute
       node — you'll need an SSH tunnel from your laptop:
           ssh -L 54545:127.0.0.1:54545 <cluster>
       Then click the URL in your laptop browser; the callback
       tunnels through to Claude on the compute node.
  4. When you see "Login successful", type `/exit`.
  5. Container exits; we verify the credential landed in the shared
     dir and `chmod 600` it.
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

# M3 (sharp-edges M1, deferred): mirror of the launcher's
# `_VALID_STATE_DIR_PATTERN` in botainer/state/dir.py. The two paths must
# enforce identical character + traversal rules — drift here is exactly the
# config-vs-CLI asymmetry pattern that's burned us before. Tests pin the
# regex string to the launcher's source of truth.
_VALID_STATE_DIR_PATTERN = re.compile(r"^[A-Za-z0-9._/\-~]+$")


def _validated_state_root() -> Path:
    """Resolve MY_BOTAINER (or default ~/.botainer) with the same character-
    class + `..` traversal checks the launcher applies. Refuses with a clear
    error rather than silently letting the credential land at an attacker-
    controlled path."""
    val = (
        os.environ.get("BOTAINER_STATE_ROOT")
        or os.environ.get("MY_BOTAINER")
    )
    if not val:
        return Path.home() / ".botainer"
    if not _VALID_STATE_DIR_PATTERN.match(val):
        raise SystemExit(
            f"agent-claude-shared login: MY_BOTAINER contains invalid characters: "
            f"{val!r}. Allowed: [A-Za-z0-9._/~-]"
        )
    expanded = Path(val).expanduser()
    if ".." in expanded.parts:
        raise SystemExit(
            f"agent-claude-shared login: MY_BOTAINER contains '..' (path "
            f"traversal): {val!r}"
        )
    return expanded


def _refuse_symlink_in_ancestry(target: Path, root: Path) -> None:
    """M4 (sharp-edges M4, deferred): walk every existing ancestor
    of `target` and refuse if any is a symlink. `Path.mkdir(parents=True)`
    transparently follows symlinks, so an attacker who controls a parent dir
    as a symlink could redirect the credential file to a path of their
    choice. Stops at the already-validated `root` (state_root)."""
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
            raise SystemExit(
                f"agent-claude-shared login: target {target} is not under "
                f"state root {root}; refusing to walk above the state tree."
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
                f"agent-claude-shared login: refusing to write — ancestor "
                f"path component {c} is a symlink. A `mkdir -p` here would "
                f"follow the symlink and the credential could land outside "
                f"{root}. Remove the symlink and re-run."
            )

# Env-var prefixes that, if inherited from the parent shell, would
# be re-injected into the container by apptainer/singularity and
# silently defeat `--cleanenv`. The whole point of `--cleanenv` is
# that env propagation is EXPLICIT, so we strip these from the
# subprocess env even though we ourselves set specific *ENV_* vars
# below.
_APPTAINER_ENV_PREFIXES: tuple[str, ...] = (
    "APPTAINERENV_",
    "SINGULARITYENV_",
)


# ─────────────────────── argv builders (pure functions; tested) ───────────────────────


def build_docker_argv(
    image: str,
    creds_dir: Path,
    port_lo: int,
    port_hi: int,
) -> list[str]:
    """Build the docker-run argv for the login container.

    Single bind: <creds_dir>:/out. The umask wrapper sets file mode
    on creation; we don't rely on the entrypoint to read $UMASK.

    Runs as the HOST user (`--user`), matching the session adapter
    (adapters/docker.py). Without it the container runs as the image's fixed
    UID 1001, so on native-Linux Docker (no userns remap) the credential
    written to the host-owned 0700 `creds_dir` either can't be written or
    lands owned by 1001 — which the shared-mode pre_session ownership check
    (st_uid == getuid) then REFUSES, and the isolated-mode session (also
    `--user`) can't read. Solidity-check finding; the prior apptainer-leaning
    audits missed this Docker-only laptop-path bug. (Mac Docker Desktop maps
    writes to the host user regardless, so this only bit native Linux.)
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
    """Build the apptainer-exec argv for the login container.

    Single bind: <creds_dir>:/out. --containall + --cleanenv enforce
    file/env isolation; the umask wrapper sets file mode on creation.
    No --net options — apptainer shares host network by default,
    which is required for the OAuth callback.

    M7 (adversarial #3, deferred): `--no-home` makes the
    capability-surface "no other mounts" promise hold across apptainer
    versions. Some apptainer builds still bind `$HOME` even under
    `--containall`, leaking ~/.ssh, ~/.aws, ~/.config/* into the OAuth login
    container — exactly the bind surface the login flow exists to avoid.
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
    """Build the subprocess env for apptainer.

    Strips `APPTAINERENV_*` and `SINGULARITYENV_*` from parent_env so
    those don't leak past `--cleanenv` into the container, then adds
    only the two vars we explicitly want inside the container.
    """
    out = {
        k: v for k, v in parent_env.items()
        if not any(k.startswith(p) for p in _APPTAINER_ENV_PREFIXES)
    }
    out["APPTAINERENV_CLAUDE_CONFIG_DIR"] = "/out"
    out["SINGULARITYENV_CLAUDE_CONFIG_DIR"] = "/out"
    return out


# ─────────────────────── helpers (existence / validation) ───────────────────────


def _docker_image_present(tag: str) -> bool:
    out = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True, text=True, check=False,
    )
    return out.returncode == 0


def _resolve_apptainer_sif(state_root: Path) -> Path | None:
    """Locate the agent-claude .sif. Accept both naming conventions
    while the codebase has drift (tracked in DN-036).

    M2 (sharp-edges M2, deferred): `apptainer exec` runs the .sif's
    entrypoint as the calling user, so the image processes the OAuth token —
    a swapped or attacker-owned .sif would compromise the credential. Apply
    three guards on each candidate: (a) refuse symlinks, (b) refuse
    non-owner-owned files, (c) refuse group/world-writable. Failed guards
    skip the candidate and print a clear refusal so the user knows why."""
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
                f"agent-claude-shared login: refusing .sif at {c}: it is a "
                f"symlink. apptainer would exec the symlink target, which "
                f"could be an attacker-chosen file. Remove the symlink and "
                f"rebuild.\n"
            )
            continue
        if st.st_uid != os.getuid():
            sys.stderr.write(
                f"agent-claude-shared login: refusing .sif at {c}: owned by "
                f"uid {st.st_uid}, not the current user {os.getuid()}. "
                f"apptainer would still exec it as YOU, processing your OAuth "
                f"token in someone else's image. Rebuild as yourself.\n"
            )
            continue
        if st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
            sys.stderr.write(
                f"agent-claude-shared login: refusing .sif at {c}: group- or "
                f"world-writable (mode {oct(_stat.S_IMODE(st.st_mode))}). Any "
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
    # Reject obviously truncated tokens. Real OAuth tokens are well
    # over 40 chars in the wild; partial-write garbage often isn't.
    if blob.startswith("oat-") or blob.startswith("sk-ant-"):
        return len(blob) >= 32
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    return bool(obj.get("claudeAiOauth") or obj.get("api_key") or obj.get("key"))


# ─────────────────────── shared user-facing message ───────────────────────


def _format_login_help(
    *,
    runtime: str,            # "docker" or "apptainer"
    image_or_sif: str,
    creds_dir: Path,
    port_lo: int,
    port_hi: int,
    use_color: bool,
) -> str:
    """Render the pre-login warning text.

    Identical structure across runtimes; only the runtime-specific
    paragraph (port mapping vs SSH tunnel) differs. Centralised here
    so the four code paths can't drift independently (per the
    sharp-edges H4 review finding).
    """
    BOLD, YELLOW, CYAN, RED = "1", "33", "36", "31"
    def _c(text: str, *codes: str) -> str:
        if not use_color or not codes:
            return text
        return f"\033[{';'.join(codes)}m{text}\033[0m"

    title = (
        "Starting one-shot Docker container for Anthropic OAuth."
        if runtime == "docker"
        else "Starting one-shot Apptainer container for Anthropic OAuth."
    )
    if runtime == "docker":
        runtime_block = f"  Port range:  127.0.0.1:{port_lo}-{port_hi} (for OAuth callback)\n"
    else:
        runtime_block = (
            "  Network:     apptainer shares host network namespace.\n"
            "               (No -p publish; the callback binds host:localhost directly.)\n"
        )

    # Standing user direction (a standing user direction): the host's
    # credential store must NEVER be touched. State this in every
    # message block.
    keychain_promise = _c(
        "No host credential store is consulted (no macOS Keychain,\n"
        "no `~/.claude/`, no `~/.config/anthropic/`). The credential\n"
        "lands ONLY in the bound directory above.\n",
        BOLD,
    )

    label = "Image:" if runtime == "docker" else "Image:"
    return (
        _c(title + "\n", BOLD, CYAN)
        + f"  {label}       {image_or_sif}\n"
        + f"  Shared dir:  {creds_dir}\n"
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


# ─────────────────────── main ───────────────────────


def main() -> int:
    # M3: validate MY_BOTAINER characters + `..` traversal at this entry
    # point (matches what botainer/state/dir.py does in the launcher path).
    state_root = _validated_state_root()
    shared_dir = state_root / "shared-auth" / "agent-claude"
    # M4: walk every ancestor and refuse if any is a symlink BEFORE the
    # mkdir, so a parent symlink can't redirect where the credential lands.
    _refuse_symlink_in_ancestry(shared_dir, state_root)
    shared_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(shared_dir, 0o700)
    except OSError:
        pass

    docker_bin = shutil.which("docker")
    apptainer_bin = shutil.which("apptainer") or shutil.which("singularity")

    if not docker_bin and not apptainer_bin:
        sys.stderr.write(
            "agent-claude-shared login: no container runtime on PATH.\n"
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

    # Task #156: refuse concurrent login launches (same fix as
    # agent-claude/hooks/login.py). Two parallel `botainer plugin
    # agent-claude-shared login` would both bind 54545-54549; the
    # second would crash, or worse, partially overlap and steal the
    # OAuth callback. Host-wide mutex via flock.
    import fcntl as _fcntl
    locks_dir = state_root / "state" / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / "agent-claude-shared-login.lock"
    try:
        _lock_fp = open(lock_path, "w")
        _fcntl.flock(_lock_fp.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        sys.stderr.write(
            "agent-claude-shared login: another login is already running "
            f"(lock: {lock_path}). Wait for it to finish, or kill it.\n"
        )
        return 1

    use_color = sys.stderr.isatty() and not os.environ.get("NO_COLOR")

    # Stat creds before launch so we can detect "container exited
    # but no fresh write happened" (stale-credential bless).
    creds = shared_dir / ".credentials.json"
    pre_mtime: int | None = creds.stat().st_mtime_ns if creds.exists() else None

    if docker_bin:
        if not _docker_image_present(AGENT_IMAGE_DOCKER):
            sys.stderr.write(
                f"agent-claude-shared login: image {AGENT_IMAGE_DOCKER!r} is not built.\n"
                f"  Build it first (~8-12 min the first time):\n"
                f"      botainer image build agent-claude\n"
                f"  Then retry the login.\n"
            )
            return 1
        sys.stderr.write(_format_login_help(
            runtime="docker",
            image_or_sif=AGENT_IMAGE_DOCKER,
            creds_dir=shared_dir,
            port_lo=port_lo,
            port_hi=port_hi,
            use_color=use_color,
        ))
        cmd = build_docker_argv(AGENT_IMAGE_DOCKER, shared_dir, port_lo, port_hi)
        sub_env = None
    else:
        sif_path = _resolve_apptainer_sif(state_root)
        if sif_path is None:
            sys.stderr.write(
                f"agent-claude-shared login: no agent-claude .sif found.\n"
                f"  Looked for:\n"
                f"    {state_root}/images/botainer-agent-claude.sif\n"
                f"    {state_root}/images/agent-claude.sif\n"
                f"  Build one with:\n"
                f"      botainer image build agent-claude --runtime apptainer\n"
                f"  (Takes ~10-20 min on a compute node.)\n"
            )
            return 1
        sys.stderr.write(_format_login_help(
            runtime="apptainer",
            image_or_sif=str(sif_path),
            creds_dir=shared_dir,
            port_lo=port_lo,
            port_hi=port_hi,
            use_color=use_color,
        ))
        cmd = build_apptainer_argv(apptainer_bin, sif_path, shared_dir)
        sub_env = build_apptainer_subenv(dict(os.environ))

    try:
        rc = subprocess.run(cmd, env=sub_env, check=False).returncode
    except KeyboardInterrupt:
        sys.stderr.write(
            "\nagent-claude-shared login: interrupted by user. Aborting.\n"
            "  Any partial credential file from this attempt has not been\n"
            "  validated or mode-protected. Re-run the login.\n"
        )
        return 130

    # Refuse-if-stale: a credential file that existed before launch
    # and has the same mtime now means no fresh write happened.
    if creds.exists() and pre_mtime is not None:
        post_mtime = creds.stat().st_mtime_ns
        if post_mtime == pre_mtime:
            sys.stderr.write(
                f"\nagent-claude-shared login: container exited (rc={rc}) but\n"
                f"the credential file at {creds} was not updated. The previous\n"
                f"credential (if any) is unchanged. If you saw 'Login successful',\n"
                f"the credential may have landed at a different path inside the\n"
                f"container, or you didn't type /exit.\n"
            )
            return 2

    if not creds.exists():
        sys.stderr.write(
            f"\nagent-claude-shared login: container exited (rc={rc}) but no\n"
            f"credential file appeared at {creds}.\n"
            "\n"
            "If you saw 'Login successful' in the TUI, the credential\n"
            "may have landed at a different path inside the container,\n"
            "or you didn't type /exit. Try again, watching for the /exit step.\n"
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
        # Task #299: the isolated variant was fixed to print
        # length + a sanitized 4-char head instead of the first 100 bytes,
        # but that fix was never mirrored HERE — the shared hook kept echoing
        # up to 100 bytes of the credential file into stderr / terminal
        # scrollback / bug reports. If a real OAuth token lands in a shape
        # `_looks_like_credential` doesn't recognize (format change, partial
        # write — a documented failure mode), that leaks the live secret.
        # Sibling-path drift closed; both variants now use the same safe head.
        head_safe = "".join(c if c.isprintable() else "?" for c in blob[:4])
        sys.stderr.write(
            f"\nagent-claude-shared login: a file was written at {creds}\n"
            f"but it doesn't look like a valid Claude credential (too short or\n"
            f"wrong shape). Length: {len(blob)} bytes; starts: {head_safe!r}\n"
            f"To inspect manually (carefully): less {creds}\n"
        )
        return 2

    sys.stderr.write(
        f"\n✓ credential saved to {creds} (mode 0600).\n"
        f"  All projects on this host using shared mode will pick it up\n"
        f"  on next `botainer start`.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
