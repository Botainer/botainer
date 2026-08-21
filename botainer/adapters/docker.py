"""Docker adapter — renders SessionSpec into `docker run` argv and execs.

Env parity note (task #131): The audit framing suggested 'Docker leaks
host env'. After investigation: Docker does NOT pass host env into the
container by default. Only env vars explicitly specified via -e
NAME=VALUE or --env-file are forwarded. The launcher subprocess (this
file's subprocess.Popen) does inherit the user's shell env, but the
docker daemon then runs the container with ONLY the explicit env vars.

The cleanenv-vs-no-cleanenv difference is between apptainer's CLI
(which DOES inherit by default unless --cleanenv) and docker's CLI
(which does NOT inherit container-side). Both runtimes end up with
ONLY the spec.env.values + spec.env_files passed in. Parity is actual,
not theatrical. Closed #131 as 'audit framing off; behavior is parity'.

Argv-only. No shell, no string concatenation of user input. All bind/network/
resource flags come from typed SessionSpec fields.

In the dev container (no Docker daemon), `render_argv` works and is
snapshot-tested; `launch` will fail with `RUNTIME_NOT_AVAILABLE`. On host
with Docker, `launch` actually runs the container.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from pathlib import Path

from botainer.adapters.base import RuntimeHandle
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import NetworkMode, SessionSpec
from botainer.mount_plan.render import render_docker_argv


def _selinux_enforcing() -> bool:
    """True iff this host runs SELinux in Enforcing mode.

    Reads /sys/fs/selinux/enforce (the canonical, dependency-free probe:
    '1' = Enforcing, '0' = Permissive; file absent = SELinux not active,
    e.g. macOS / Debian/Ubuntu without SELinux). Used to decide whether the
    docker run needs `--security-opt label=disable` so bind mounts aren't
    blocked by SELinux on RHEL/Fedora/CentOS. Any read error → False (fail
    toward the no-op; a genuinely-enforcing host that we misread will surface
    a clear bind permission-denied that `botainer doctor` explains)."""
    try:
        return Path("/sys/fs/selinux/enforce").read_text().strip() == "1"
    except OSError:
        return False


def _preflight_local_image(image: str) -> None:
    """Refuse early, and CORRECTLY, when a botainer/* image can't be used.

    botainer's agent images are built LOCALLY and never published, so a missing
    one would otherwise surface as an opaque `docker run` pull failure. Tag-only
    check (a rebuild changes the digest; the tag is what "missing" means); only
    botainer/* tags — registry images legitimately pull on run.

    UX audit (M4): `docker image inspect` exits non-zero for ANY
    reason, and this used to assume the only reason was "image absent". With
    Docker Desktop stopped — the most common laptop failure — the user was told
    to run a ~12-minute `botainer image build` that CANNOT succeed, because the
    build needs the same daemon that is down. doctor.py already distinguishes the
    two; the launch path never asked. Extracted from launch() so the distinction
    is directly testable.
    """
    tag = image.split("@", 1)[0]
    if not tag.startswith("botainer/"):
        return
    daemon_down = False
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True, text=True, timeout=10,
        )
        present = probe.returncode == 0
        if not present:
            err = (probe.stderr or "").lower()
            daemon_down = any(s in err for s in (
                "cannot connect to the docker daemon",
                "is the docker daemon running",
                "error during connect",
                "docker daemon is not running",
            ))
    except Exception:
        # Best-effort preflight: if we CAN'T check (docker slow, odd
        # environment), never block the launch — let docker try.
        return
    if daemon_down:
        raise Refused(
            RefusalCategory.RUNTIME_LAUNCH_FAILED,
            "the Docker daemon is not running, so botainer can't check or start "
            "containers.\n"
            "  Start Docker Desktop (or `sudo systemctl start docker` on Linux), "
            "wait for it to report Running, then re-run.\n"
            "  NOTE: do NOT run `botainer image build` to 'fix' this — a build "
            "needs the same daemon and will fail after several minutes. Run "
            "`botainer doctor` if it stays down.",
        )
    if not present:
        plugin = tag.split("/", 1)[1].split(":", 1)[0]
        raise Refused(
            RefusalCategory.RUNTIME_LAUNCH_FAILED,
            f"image {tag!r} is not on this machine. Botainer agent images are "
            f"built LOCALLY — they don't download. Build it: `botainer image "
            f"build {plugin}`. (If it vanished after `docker system prune -a`, "
            f"that `-a` deletes unused images — use `docker builder prune -af` "
            f"next time, which keeps your images.)",
        )


class DockerAdapter:
    name: str = "docker"

    def validate(self, spec: SessionSpec) -> None:
        if spec.runtime != "docker":
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                f"DockerAdapter received spec for runtime={spec.runtime!r}",
            )
        # endpoint-ip-allowlist requires endpoints to be specified.
        if spec.network.mode == NetworkMode.ENDPOINT_IP_ALLOWLIST and not spec.network.endpoints:
            raise Refused(
                RefusalCategory.API_ONLY_REQUIRES_ENDPOINTS,
                "network.mode=endpoint-ip-allowlist requires at least one endpoint",
            )
        # Kernel caps: at v0.1.0 the keep-list must be empty (we drop ALL).
        if spec.kernel_caps.keep:
            raise Refused(
                RefusalCategory.KERNEL_CAP_KEEP_NOT_ALLOWED,
                f"kernel cap keep-list non-empty at v0.1.0: {spec.kernel_caps.keep}",
            )

    def render_argv(
        self, spec: SessionSpec, *, detach: bool = False, interactive: bool = True
    ) -> list[str]:
        self.validate(spec)
        # -it for foreground attach; -d for detached background; neither for a
        # non-interactive capture run (the `botainer selftest` probe, which
        # reads stdout). All keep --rm so the container is auto-deleted on exit.
        if detach:
            argv: list[str] = ["docker", "run", "--rm", "-d"]
        elif interactive:
            argv = ["docker", "run", "--rm", "-it"]
        else:
            argv = ["docker", "run", "--rm"]
        argv += ["--name", f"botainer-{spec.session_id[:12]}"]
        # --init: run tini as PID 1 so ORPHANED PROCESSES GET REAPED.
        #
        # Without it the agent itself is PID 1. Any process whose parent exits
        # while it is still alive is re-parented to PID 1, and a normal
        # application has no reaping loop — so every such orphan becomes a
        # permanent zombie holding a PID slot. Over a long session they
        # accumulate until the container cannot fork at all, and the failures
        # land far from the cause: "Resource temporarily unavailable", thread
        # spawn panics, hooks failing at random. It reads as flakiness; it is
        # exhaustion. Observed in botainer's own dev container: 466
        # zombies, every one with ppid 1, at pids.max=512. Nothing INSIDE can
        # clear them (only PID 1 could, and it will not), so the only recovery
        # is destroying the container — which is the user's long-standing
        # "containers die from time to time".
        #
        # HPC PARITY, inverted for once: the apptainer adapter passes
        # --containall, which contains "PID, IPC, and environment" — the same
        # new-PID-namespace setup. But apptainer STARTS AN INIT SHIM BY DEFAULT
        # under --pid; its `--no-init` flag exists precisely to turn that off.
        # So the HPC path has always reaped correctly and only docker did not.
        # This flag makes docker match apptainer's existing default, so it is a
        # parity RESTORATION rather than a new mechanism — which is also why
        # the fix is one flag and not tini baked into every image.
        #
        # Not a capability grant: tini drops no privileges and adds no access.
        # It changes which process answers for PID 1 inside the same cage.
        argv += ["--init"]
        argv += ["--cap-drop", "ALL"]
        argv += ["--security-opt", "no-new-privileges"]
        # native-Linux (recon T-C): under Enforcing SELinux (RHEL/Fedora/CentOS)
        # a plain docker bind mount is blocked with permission-denied that reads
        # like a botainer bug. Add `--security-opt label=disable` ONLY when
        # SELinux is enforcing on this host (detected via /sys/fs/selinux/enforce)
        # — one portable flag, no recursive relabel latency, doesn't mutate host
        # dir labels. No-op on macOS / non-SELinux Linux (the probe returns
        # False). botainer's isolation is cap-drop + the closed bind plan, not
        # SELinux label confinement, so disabling the label for this container is
        # acceptable on the laptop/dev docker path. `botainer doctor` discloses
        # this. (HPC uses apptainer, not this path.)
        if _selinux_enforcing():
            argv += ["--security-opt", "label=disable"]
        # Task #230: run as host UID:GID so files the container writes
        # are owned by the user, not root. Without this, /packages/pip
        # and any other in-container write ends up root-owned on the
        # host bind mount, requiring sudo to delete.
        import os as _os
        argv += ["--user", f"{_os.getuid()}:{_os.getgid()}"]
        # Ephemeral /tmp + /var/tmp as tmpfs (mode 1777), NOT the container's
        # writable layer. The container runs as the host uid with no home, so
        # tools spill into /tmp (Claude Code's /tmp/claude-<uid> runtime dir, pip
        # build temp); on the writable layer that consumes the Docker VM disk and
        # ENOSPCs when the VM disk is full (which the ~800 MB browser image makes
        # easy to hit). tmpfs is truly ephemeral (cleared on container stop —
        # self-cleaning, never accumulates like a persistent dir) and lives off
        # the VM disk. No explicit size: it self-bounds by the container memory
        # limit (--memory) or VM RAM; a runaway temp write OOM-kills only this
        # container. `exec` because legit tools run helpers from /tmp (pip/npm
        # native builds, chromium). HPC parity: apptainer's --containall already
        # gives a private /tmp, so this is the docker-side equivalent.
        # nosuid,nodev = Docker's tmpfs defaults (an explicit option string
        # REPLACES them, so re-state them); exec is the deliberate exception.
        argv += ["--tmpfs", "/tmp:rw,exec,nosuid,nodev,mode=1777"]
        argv += ["--tmpfs", "/var/tmp:rw,exec,nosuid,nodev,mode=1777"]
        # Network mode
        if spec.network.mode == NetworkMode.NONE and not spec.port_forwards:
            # Pure no-network case.
            argv += ["--network", "none"]
        elif spec.network.mode == NetworkMode.NONE and spec.port_forwards:
            # Port forwards require a network namespace. Docker doesn't allow
            # -p with --network=none. We use a bridge but the iptables-style
            # filtering would have to happen via a host helper (Phase B).
            # For v0.1.0: refuse this combo with a clear remediation. Name the
            # actual forward source(s) so the message is right whether the port
            # came from web-ports OR the browser viewer (both add -p forwards).
            _srcs = sorted({pf.label for pf in spec.port_forwards if pf.label})
            _detail = f" ({', '.join(_srcs)})" if _srcs else ""
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                f"network.mode=none + inbound port forwards{_detail} is not "
                "supported (Docker -p requires a network namespace). Set "
                "network.mode=internet, or remove the forward source "
                "(web-ports plugin `ports:`, or `plugins.browser.viewer: true`).",
            )
        elif spec.network.mode == NetworkMode.INTERNET:
            argv += ["--network", "bridge"]
        # #176: NetworkMode.ENDPOINT_IP_ALLOWLIST is refused at compose
        # time in v0.1. If it ever reaches an adapter here, that's a
        # composition bug; validate() above raises before we get here.
        # Port forwards: -p <host_bind>:<host_port>:<container_port>
        for pf in spec.port_forwards:
            argv += [
                "-p",
                f"{pf.host_bind}:{pf.host_port}:{pf.container_port}",
            ]
        # Resource caps
        if spec.resources.cpu is not None:
            argv += ["--cpus", str(spec.resources.cpu)]
        if spec.resources.memory_mb is not None:
            argv += ["--memory", f"{spec.resources.memory_mb}m"]
        # Env files (host_pre_launch contributions). Applied before -e
        # so individual -e overrides win.
        for env_file in spec.env_files:
            argv += ["--env-file", env_file]
        # Env values (explicit). Win over env-file entries with the same key.
        for k, v in sorted(spec.env.values.items()):
            argv += ["-e", f"{k}={v}"]
        # Step B (#216, internal design note DN-005): module-env PATH-list var
        # prepends. Same shape as the apptainer adapter — pass each value via
        # `-e _BOTAINER_PREPEND_<NAME>=<val>` so the trampoline (spliced
        # below) can PREPEND it onto the container's existing <NAME> at exec
        # time. Docker's -e flag doesn't shell-interpret the value.
        for k, v in spec.module_env_path_prepends:
            argv += ["-e", f"_BOTAINER_PREPEND_{k}={v}"]
        # Mounts
        argv += render_docker_argv(spec.mount_plan)
        # Working directory
        argv += ["--workdir", "/workspace"]
        # Image
        argv += [spec.image]
        # Entrypoint composed from entrypoint_wraps: the outermost wrap
        # becomes docker's --entrypoint; deeper wraps and their args
        # follow the image. Example with one wrap:
        #   <wrap argv>   (e.g. /usr/local/bin/agent-claude-entrypoint claude)
        if spec.entrypoint_wraps:
            argv = self._splice_entrypoint(argv, spec)
        return argv

    def _splice_entrypoint(self, argv: list[str], spec: SessionSpec) -> list[str]:
        # Compose the final exec line: wraps[0] ... wraps[-1].
        # The outermost wrap (wraps[0]) is the docker --entrypoint;
        # deeper wraps become its argv tail.
        composed: list[str] = []
        for wrap in spec.entrypoint_wraps:
            composed += list(wrap)
        if not composed:
            return argv
        # Step B (#216, internal design note DN-005): when module-env path-list
        # prepends are present, override --entrypoint with `bash` and run a
        # trampoline that PREPENDs the values onto the container's PATH /
        # LD_LIBRARY_PATH / … before exec'ing the original entrypoint. The
        # trampoline reads `_BOTAINER_PREPEND_<NAME>` env vars (set via -e
        # above) and is the same script used by the apptainer adapter (single
        # source of truth in adapters/_inner_prepend.py).
        from botainer.adapters._inner_prepend import (
            _inner_prepend_trampoline_script,
        )
        if spec.module_env_path_prepends:
            entrypoint = "bash"
            command_tail = [
                "-c", _inner_prepend_trampoline_script(), "bash", *composed,
            ]
        else:
            entrypoint = composed[0]
            command_tail = composed[1:]
        # Insert --entrypoint flag before the image; docker requires it that way.
        # `argv` ends with [..., "--workdir", "/workspace", spec.image]: the
        # image is appended in render_argv immediately before this call with
        # nothing after it, so it is unconditionally the last token. Splice at
        # that known position rather than argv.index(spec.image), which would
        # mis-locate the image if its string equals an earlier arg (e.g.
        # image == "/workspace" collides with the --workdir value). Found by
        # the AC7 image-positional review; a correctness footgun,
        # not an escalation (the image is already content-validated).
        image_idx = len(argv) - 1
        assert argv[image_idx] == spec.image
        new = argv[:image_idx]
        new += ["--entrypoint", entrypoint]
        new += [spec.image]
        new += command_tail
        return new

    def launch(self, spec: SessionSpec, *, detach: bool = False) -> RuntimeHandle:
        if not shutil.which("docker"):
            raise Refused(
                RefusalCategory.RUNTIME_NOT_AVAILABLE,
                "`docker` not on PATH; cannot launch Docker adapter",
            )
        # Guard: botainer's agent images are built LOCALLY and never published to
        # a registry. If one is missing (classically: a `docker system prune -a`
        # deleted it), `docker run` would try to PULL it and fail with an opaque
        # "pull access denied / repository does not exist". Catch it here with a
        # plain "build it" message. Tag-only check (ignore any @sha256 pin — a
        # rebuild changes the digest but the tag is what "missing" means). Only
        # for botainer/* tags; registry images legitimately pull on run.
        _preflight_local_image(spec.image)
        argv = self.render_argv(spec, detach=detach)
        container_name = f"botainer-{spec.session_id[:12]}"

        # §A19: when the nudge plugin is enabled, wrap the docker
        # invocation in a HOST-side `screen` session so:
        #   1. `botainer nudge "text"` can `screen -X stuff -- "text\n"`
        #      to inject into the container's stdin from any host shell;
        #   2. the user (re)attaches via `screen -r botainer-<sid>`
        #      (this adapter's attach() does that for the foreground path).
        # Refuse if `screen` is not on PATH — silent fallback would leave
        # nudge broken with no signal to the user.
        use_screen = "nudge" in spec.plugins_enabled
        screen_session_id = f"botainer-{spec.session_id}" if use_screen else None
        extras: dict[str, str] = {}
        if screen_session_id:
            extras["screen_session_id"] = screen_session_id
            if not shutil.which("screen"):
                raise Refused(
                    RefusalCategory.RUNTIME_NOT_AVAILABLE,
                    "`screen` not on PATH; the nudge plugin requires a "
                    "host-side screen binary (apt install screen / brew "
                    "install screen). §A19.",
                )
            # screen -dmS: detached session, screen forks and parent exits.
            # In the foreground botainer-start flow, attach() then execs
            # `screen -r <sid>` to put the user on the screen pty.
            argv = ["screen", "-dmS", screen_session_id, *argv]

        if detach and not use_screen:
            # Bare `docker run -d` path: capture container_id from stdout.
            # 180s budget: first-run image pull can be slow on slow networks.
            try:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, check=False, timeout=180
                )
            except subprocess.TimeoutExpired:
                raise Refused(
                    RefusalCategory.RUNTIME_LAUNCH_FAILED,
                    "docker run -d timed out after 180s "
                    "(image pull on a slow network? Try `docker pull <image>` manually first)",
                ) from None
            except OSError as exc:
                raise Refused(
                    RefusalCategory.RUNTIME_LAUNCH_FAILED,
                    f"docker run -d failed: {exc}",
                ) from exc
            if proc.returncode != 0:
                _err = proc.stderr.strip()
                _hint = ""
                if "no space left on device" in _err.lower() or "enospc" in _err.lower():
                    # Plain-language: on macOS/Windows Docker's disk is a hidden
                    # VM disk, separate from the Mac's free space. Tell the user
                    # exactly how to recover instead of leaving a raw ENOSPC.
                    _hint = (
                        "\n\n→ Docker is out of disk. This is Docker's own disk "
                        "(a hidden VM on macOS/Windows), NOT your Mac's free space. "
                        "Reclaim it SAFELY: `docker builder prune -af` (frees build "
                        "cache, keeps your images) — do NOT use `docker system prune "
                        "-a`, whose `-a` deletes your built agent image. Or raise "
                        "Docker Desktop → Settings → Resources → Disk. `botainer "
                        "doctor` shows usage."
                    )
                raise Refused(
                    RefusalCategory.RUNTIME_LAUNCH_FAILED,
                    f"docker run -d exited {proc.returncode}: {_err}{_hint}",
                )
            container_id = proc.stdout.strip() or container_name
            return RuntimeHandle(runtime="docker", id=container_id, pid=None, extras=extras)
        try:
            popen = subprocess.Popen(argv, stdin=None, stdout=None, stderr=None)
        except OSError as exc:
            raise Refused(RefusalCategory.RUNTIME_LAUNCH_FAILED, f"docker run failed: {exc}") from exc
        if use_screen:
            # screen -dm forks fast; wait for the wrapper to detach so the
            # session record reflects "launched" before we return. Inside
            # screen, docker run starts the container under screen's pty.
            try:
                popen.wait(timeout=15)
            except subprocess.TimeoutExpired:
                # screen still hasn't returned after 15s — unusual; let
                # it run. attach() will reattach when its target session
                # exists.
                pass
            # No pid to waitpid on (screen detached); attach() execs
            # `screen -r` against the named session instead.
            return RuntimeHandle(
                runtime="docker", id=container_name, pid=None, extras=extras,
            )
        return RuntimeHandle(runtime="docker", id=container_name, pid=popen.pid, extras=extras)

    def attach(self, handle: RuntimeHandle) -> int:
        """Reattach the user's terminal to the running container.

        Two paths:

        - **Screen wrap (nudge enabled).** Launch put `docker run -it`
          under a host-side `screen -dmS botainer-<sid>` session.
          attach() runs `screen -r <sid>` so the user's tty becomes
          the screen pty, which is the container's stdin/stdout.
          When the container exits, screen exits, we return the
          screen exit code. When the user detaches (Ctrl-A D), the
          container keeps running and we return 0; the user can
          `botainer attach` again later.
        - **Bare docker run -it (nudge NOT enabled).** Launch Popen'd
          docker-run with inherited stdio so the docker CLI is in
          our foreground process group; we just waitpid for it.

        Implementation-review HIGH 2 (historical): previous code tried
        `subprocess.run(["wait", str(handle.pid)])`, but `wait` is a
        shell builtin, not an executable. Removed the dead branch.
        """
        screen_session_id = handle.extras.get("screen_session_id") if handle.extras else None
        if screen_session_id:
            if not shutil.which("screen"):
                # Should never happen — launch() would have refused.
                # Defensive: explain the inconsistency rather than crash.
                raise Refused(
                    RefusalCategory.RUNTIME_NOT_AVAILABLE,
                    "`screen` not on PATH but the session was launched "
                    "with a screen wrap. Install screen and retry "
                    "`botainer attach`, or `docker attach` directly.",
                )
            try:
                proc = subprocess.run(
                    ["screen", "-r", screen_session_id], check=False,
                )
            except OSError:
                return 1
            return proc.returncode
        if handle.pid is None:
            return 0
        import os

        try:
            _, status = os.waitpid(handle.pid, 0)
        except (ChildProcessError, OSError):
            return 0
        if os.WIFSIGNALED(status):
            # Convention: 128 + signum, matching shells.
            return 128 + os.WTERMSIG(status)
        return os.WEXITSTATUS(status)

    def stop(self, handle: RuntimeHandle) -> None:
        if not shutil.which("docker"):
            return
        # Docker default SIGTERM-then-SIGKILL is ~10s; allow 30 for slow images.
        with contextlib.suppress(subprocess.TimeoutExpired):
            subprocess.run(
                ["docker", "stop", "--", handle.id],
                check=False, capture_output=True, timeout=30,
            )

    def inspect(self, handle: RuntimeHandle) -> str:
        if not shutil.which("docker"):
            return ""
        try:
            result = subprocess.run(
                ["docker", "inspect", "--", handle.id],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return ""
        return result.stdout if result.returncode == 0 else ""
