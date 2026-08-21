"""Apptainer adapter.

Same contract as Docker. Renders SessionSpec into `apptainer exec --bind ...`
argv.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess

from botainer.adapters.base import RuntimeHandle
from botainer.adapters._inner_prepend import _inner_prepend_trampoline_script
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import NetworkMode, SessionSpec
from botainer.mount_plan.render import render_apptainer_argv


class ApptainerAdapter:
    name: str = "apptainer"

    def validate(self, spec: SessionSpec) -> None:
        if spec.runtime != "apptainer":
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                f"ApptainerAdapter received spec for runtime={spec.runtime!r}",
            )
        if spec.port_forwards:
            _srcs = sorted({pf.label for pf in spec.port_forwards if pf.label})
            _detail = f": {', '.join(_srcs)}" if _srcs else ""
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                "Apptainer does not support container port forwarding "
                f"({len(spec.port_forwards)} requested{_detail}). These come "
                "from the web-ports plugin — disable it to compose this spec, "
                "and SSH-tunnel laptop -> login node -> compute node to reach a "
                "web app. NOTE: the browser viewer does NOT use a port forward on "
                "HPC (compose adds its noVNC port on Docker only); on a cluster it "
                "is reached over a node-local unix socket + `ssh -L`.",
            )
        if spec.kernel_caps.keep:
            raise Refused(
                RefusalCategory.KERNEL_CAP_KEEP_NOT_ALLOWED,
                f"kernel cap keep-list non-empty at v0.1.0: {spec.kernel_caps.keep}",
            )
        # Task #97: HPC PARITY — apptainer adapter silently dropped
        # resources.cpu / memory_mb. Docker honors them via --cpus /
        # --memory; apptainer has no equivalent at exec time (cgroup limits
        # are enforced by SLURM at job submission, not by apptainer). If a
        # user sets resources but launches via raw apptainer (not via the
        # hpc-launcher plugin's sbatch wrapper), the limits silently
        # vanish. Refuse so the gap is loud.
        if spec.resources.cpu is not None or spec.resources.memory_mb is not None:
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                "Apptainer adapter at v0.1.0 cannot enforce resources.cpu / "
                "resources.memory_mb (no exec-time cgroup support). For HPC, "
                "use the hpc-launcher plugin which sets SLURM --cpus / --mem "
                "at sbatch time. Clear resources to launch direct.",
            )
        # Apptainer doesn't directly expose `--network none`; user must configure
        # cluster-side network namespaces. We refuse explicit modes the runtime
        # can't enforce.
        if spec.network.mode == NetworkMode.ENDPOINT_IP_ALLOWLIST:
            raise Refused(
                RefusalCategory.RUNTIME_CANNOT_ENFORCE,
                "Apptainer adapter at v0.1.0 cannot enforce endpoint-ip-allowlist; "
                "use Docker on laptop or configure network policy at cluster level",
            )
        # Task #88 + #162: apptainer adapter MUST refuse network modes it
        # cannot enforce. Previously NetworkMode.NONE silently leaked through
        # (apptainer exec inherits host network namespace), defeating
        # policy.refuse_network on HPC. Until we wire `unshare -n` (req
        # rootless slurm/HPC setup) or document a cluster-side namespace
        # config, refuse explicit NONE so the lie is at least surfaced.
        if spec.network.mode == NetworkMode.NONE:
            raise Refused(
                RefusalCategory.RUNTIME_CANNOT_ENFORCE,
                "Apptainer adapter at v0.1.0 does not enforce network.mode=none "
                "(no namespace isolation). Either: (a) use Docker on laptop, "
                "(b) configure cluster-side network namespaces, or (c) accept "
                "the host network and set network.mode=internet explicitly.",
            )

    def render_argv(
        self, spec: SessionSpec, *, detach: bool = False, interactive: bool = True
    ) -> list[str]:
        # `interactive` accepted for Adapter-protocol parity; apptainer exec is
        # already non-interactive (no -it equivalent needed), so it's ignored.
        del interactive
        if detach:
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                "Apptainer runtime doesn't support --detach at v0.1.0 (HPC "
                "users should submit via sbatch through the hpc-launcher plugin).",
            )
        self.validate(spec)
        # Task #292: --no-privs sets PR_SET_NO_NEW_PRIVS=1 so setuid
        # binaries inside the container can't gain privileges via
        # exec(). Without this, a malicious image / package install
        # leaves a setuid escape vector open on the host.
        # Apptainer ≥1.0.1 supports this; earlier versions ignore the
        # flag (harmless).
        #
        # Task #91: actively drop ALL kernel capabilities. Previously the
        # validate() check refused non-empty keep-list but never asked
        # apptainer to drop the caps the user inherits by default
        # (CAP_NET_BIND_SERVICE etc. in some configs). --drop-caps=all
        # makes the empty-keep promise concrete instead of theatrical.
        # On apptainer in user-namespace mode this is mostly redundant
        # with the userns reduction, but in setuid mode (--privileged
        # builds, root daemon) it's the actual cap-drop the security
        # surface promises.
        argv: list[str] = [
            "apptainer", "exec",
            "--containall", "--cleanenv", "--no-privs",
            "--drop-caps", "all",
        ]
        # No --writable; project workspace bind below provides rw.
        #
        # HOME (HPC-parity bug, reported from a real Grace run):
        # composition sets HOME=/home/user so npm/npx, pip and Claude Code's
        # ~/.claude land in the PERSISTENT per-project home bind. That used to
        # be passed as `--env HOME=...` below — and apptainer REFUSES it:
        #     WARNING: Overriding HOME environment variable with
        #              APPTAINERENV_HOME is not permitted
        # HOME is protected; the flag was silently dropped. So on HPC the home
        # dir was mounted at /home/user and then nothing pointed at it: under
        # --containall the agent's $HOME was an EMPTY tmpfs, and every cache,
        # credential and npm/pip write went somewhere ephemeral. Docker was
        # always correct, so this never showed up on the laptop.
        #
        # `--home <src>:<dest>` is apptainer's own mechanism and does BOTH jobs
        # — bind and set HOME — so it is used instead of the rejected --env,
        # and the same bind is dropped from the --bind list to avoid mounting
        # the path twice. Structural rather than a workaround: HOME is set by
        # the runtime that owns it, not asserted through a channel that runtime
        # ignores.
        home_target = spec.env.values.get("HOME")
        home_bind = next(
            (b for b in spec.mount_plan.binds if b.target == home_target), None
        ) if home_target else None
        if home_bind is not None:
            argv += ["--home", f"{home_bind.source}:{home_bind.target}"]
            remaining = spec.mount_plan.model_copy(update={
                "binds": tuple(b for b in spec.mount_plan.binds if b is not home_bind)
            })
            argv += render_apptainer_argv(remaining)
        else:
            argv += render_apptainer_argv(spec.mount_plan)
        # Env files (host_pre_launch contributions). Apptainer accepts
        # --env-file (Apptainer 1.0+). Same semantics as Docker: later
        # files override earlier on identical keys.
        for env_file in spec.env_files:
            argv += ["--env-file", env_file]
        # Env values (explicit). HOME is excluded when --home carried it:
        # apptainer refuses `--env HOME=` and warns, so emitting it would only
        # reproduce the warning the user reported.
        for k, v in sorted(spec.env.values.items()):
            if k == "HOME" and home_bind is not None:
                continue
            argv += ["--env", f"{k}={v}"]
        # Step B (#216, internal design note DN-005): module-env PATH-list var
        # prepends. Pass each value via `--env _BOTAINER_PREPEND_<NAME>=<val>`
        # so the trampoline (appended below) can PREPEND it onto the
        # container's existing <NAME> at exec time. Apptainer's --env flag
        # does NOT shell-interpret the value, so even shell-metacharacter-
        # bearing module values arrive as literal strings.
        for k, v in spec.module_env_path_prepends:
            argv += ["--env", f"_BOTAINER_PREPEND_{k}={v}"]
        # Workdir
        argv += ["--pwd", "/workspace"]
        # Image
        argv += [spec.image]
        # Entrypoint composed from entrypoint_wraps (outermost-first).
        # apptainer doesn't have docker's separate --entrypoint flag —
        # the argv after the image IS the in-container exec line.
        wraps_argv: list[str] = []
        for wrap in spec.entrypoint_wraps:
            wraps_argv += list(wrap)
        if spec.module_env_path_prepends and wraps_argv:
            # Step B trampoline: PREPEND each _BOTAINER_PREPEND_<NAME> onto
            # the container's current <NAME>, unset the helper vars, then
            # exec the original wraps. Fully-quoted bash parameter expansions
            # mean the values are treated as literal strings (no word-split,
            # no glob, no command substitution) — safe even if a module value
            # contains shell metacharacters.
            # Trampoline requires non-empty wraps (the `exec "$@"` line);
            # without wraps the image's %runscript runs and the prepends
            # quietly don't apply (agent plugins always contribute wraps).
            trampoline = _inner_prepend_trampoline_script()
            argv += ["bash", "-c", trampoline, "bash"] + wraps_argv
        else:
            argv += wraps_argv
        return argv

    def launch(self, spec: SessionSpec, *, detach: bool = False) -> RuntimeHandle:
        if detach:
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                "Apptainer runtime doesn't support --detach at v0.1.0",
            )
        # Collect Slurm metadata in the adapter (not composition).
        # Architecture review #10: keeps runtime-env-reading where it belongs.
        import os as _os
        extras: dict[str, str] = {}
        for env_key, extras_key in (
            ("SLURM_JOB_ID", "slurm_jobid"),
            ("SLURM_JOBID", "slurm_jobid"),
            ("SLURM_STEP_ID", "slurm_step_id"),
            ("SLURMD_NODENAME", "node"),
        ):
            v = _os.environ.get(env_key)
            if v and extras_key not in extras:
                extras[extras_key] = v
        if not (shutil.which("apptainer") or shutil.which("singularity")):
            raise Refused(
                RefusalCategory.RUNTIME_NOT_AVAILABLE,
                "neither `apptainer` nor `singularity` on PATH",
            )
        argv = self.render_argv(spec)

        # §A19: host-side screen wrap when nudge is enabled. Symmetric
        # with DockerAdapter — see docker.py for the rationale. For the
        # sbatch path (`hpc-launcher submit`), the wrap is emitted
        # INSIDE the sbatch script (see render_sbatch_script in
        # plugins/hpc-launcher/host_helper/_common.py) because the
        # screen must live on the COMPUTE NODE; this adapter only
        # handles the foreground `apptainer exec` path (local box or
        # inside an existing salloc/srun).
        use_screen = "nudge" in spec.plugins_enabled
        if use_screen:
            screen_session_id = f"botainer-{spec.session_id}"
            if not shutil.which("screen"):
                raise Refused(
                    RefusalCategory.RUNTIME_NOT_AVAILABLE,
                    "`screen` not on PATH; the nudge plugin requires a "
                    "host-side screen binary (apt install screen / brew "
                    "install screen / module load screen on HPC). §A19.",
                )
            argv = ["screen", "-dmS", screen_session_id, *argv]
            extras["screen_session_id"] = screen_session_id

        try:
            # Scrub APPTAINERENV_*/SINGULARITYENV_* from the launch env: apptainer
            # treats those host vars as an EXPLICIT env-injection mechanism that
            # `--cleanenv` does NOT strip (that's its documented purpose), so an
            # untrusted repo's direnv `.envrc` (`export APPTAINERENV_LD_PRELOAD=…`)
            # inherited by `botainer start` would smuggle env past the cage
            # (sharp-edges MED). All INTENDED env is delivered via
            # --env/--env-file in `argv`, so dropping these breaks nothing.
            import os as _os
            _env = {k: v for k, v in _os.environ.items()
                    if not k.startswith(("APPTAINERENV_", "SINGULARITYENV_"))}
            proc = subprocess.Popen(argv, env=_env)
        except OSError as exc:
            raise Refused(RefusalCategory.RUNTIME_LAUNCH_FAILED, f"apptainer exec failed: {exc}") from exc
        if use_screen:
            # screen -dm forks fast; wait the wrapper out so the session
            # record reflects "launched" before we return.
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            return RuntimeHandle(
                runtime="apptainer", id=str(proc.pid), pid=None, extras=extras,
            )
        return RuntimeHandle(
            runtime="apptainer",
            id=str(proc.pid),
            pid=proc.pid,
            extras=extras,
        )

    def attach(self, handle: RuntimeHandle) -> int:
        # §A19 screen wrap path: reattach via `screen -r <sid>`.
        screen_session_id = handle.extras.get("screen_session_id") if handle.extras else None
        if screen_session_id:
            if not shutil.which("screen"):
                raise Refused(
                    RefusalCategory.RUNTIME_NOT_AVAILABLE,
                    "`screen` not on PATH but the session was launched "
                    "with a screen wrap. Install screen and retry "
                    "`botainer attach`.",
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
        # Task #196: previously we returned os.WEXITSTATUS(status) blindly,
        # which yields 0 when the child was killed by a signal (e.g. SIGTERM
        # from a SLURM scancel). That misreported a cancelled job as
        # successful. Mapping: 128 + signum on signal-kill, exit
        # status on normal exit. Matches bash/sh convention.
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        return 1

    def stop(self, handle: RuntimeHandle) -> None:
        if handle.pid is None:
            return
        import os
        import signal

        with contextlib.suppress(OSError):
            os.kill(handle.pid, signal.SIGTERM)

    def inspect(self, handle: RuntimeHandle) -> str:
        # Apptainer has no `inspect <container>` for running containers.
        # Readback via /proc/<pid>/mounts is the host-side mechanism.
        if handle.pid is None:
            return ""
        try:
            with open(f"/proc/{handle.pid}/mounts", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""
