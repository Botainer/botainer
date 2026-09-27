"""Snapshot tests for adapter argv rendering."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from botainer.adapters.apptainer import ApptainerAdapter
from botainer.adapters.docker import DockerAdapter
from botainer.adapters.mock import MockAdapter
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import (
    Bind,
    BindMode,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    Provenance,
    ResourceSpec,
    SessionSpec,
)


def _spec(runtime: str = "docker") -> SessionSpec:
    plan = MountPlan(
        binds=(
            Bind(
                source="/host/proj",
                target="/workspace",
                mode=BindMode.RW,
                provenance=Provenance.CORE,
            ),
            Bind(
                source="/host/anchor",
                target="/workspace/.botainer",
                mode=BindMode.NULL_BIND,
                provenance=Provenance.CORE,
            ),
            Bind(
                source="/host/aas.txt",
                target="/workspace/.botainer/AGENT_ACCESS.txt",
                mode=BindMode.RO,
                provenance=Provenance.CORE,
                nested_under="/workspace/.botainer",
            ),
        )
    )
    return SessionSpec(
        session_id="abcdef0123456789",
        project_uuid="11111111-1111-1111-1111-111111111111",
        project_root="/host/proj",
        state_dir="/state/dir",
        runtime=runtime,
        image="botainer/agent-claude:0.1@sha256:" + "a" * 64,
        mount_plan=plan,
        network=NetworkSpec(mode=NetworkMode.NONE),
    )


def test_docker_argv_baseline() -> None:
    da = DockerAdapter()
    argv = da.render_argv(_spec("docker"))
    assert argv[0:4] == ["docker", "run", "--rm", "-it"]
    assert "--cap-drop" in argv
    assert "ALL" in argv
    assert "--security-opt" in argv
    assert "no-new-privileges" in argv
    assert "--network" in argv
    assert "none" in argv
    assert "--mount" in argv
    # Image is the last token.
    assert argv[-1].startswith("botainer/agent-claude:0.1@sha256:")


def test_docker_argv_maps_user_uid_gid() -> None:
    """Parity-audit tripwire (#230): the docker cage must run as the caller's
    uid:gid (so bind-mounted writes are user-owned, not root — and the agent
    can't act as root in the container). Only the login-container --user was
    pinned before; this pins the session cage."""
    import os
    argv = DockerAdapter().render_argv(_spec("docker"))
    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"


def test_docker_argv_mounts_ephemeral_tmpfs_tmp() -> None:
    """/tmp + /var/tmp are tmpfs (ephemeral, off the VM writable layer), so the
    container running as a homeless uid doesn't spill runtime temp
    (Claude Code's /tmp/claude-<uid>) onto a possibly-full Docker VM disk.
    Parity: apptainer gets a private /tmp from --containall."""
    argv = DockerAdapter().render_argv(_spec("docker"))
    tmpfs_vals = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert any(v.startswith("/tmp:") and "mode=1777" in v for v in tmpfs_vals)
    assert any(v.startswith("/var/tmp:") for v in tmpfs_vals)


def test_docker_launch_refuses_missing_botainer_image_with_build_hint(monkeypatch) -> None:
    """A `docker system prune -a` deletes botainer's locally-built agent image;
    `docker run` would then fail with an opaque 'pull access denied'. The adapter
    must catch a missing botainer/* image BEFORE run and say how to rebuild it."""
    from botainer.adapters import docker as dmod
    from botainer.core.refusal import Refused

    monkeypatch.setattr(dmod.shutil, "which", lambda _x: "/usr/bin/docker")

    class _Missing:
        returncode = 1
        stdout = ""
        stderr = "No such image"

    monkeypatch.setattr(dmod.subprocess, "run", lambda *a, **k: _Missing())
    with pytest.raises(Refused) as ei:
        dmod.DockerAdapter().launch(_spec("docker"))
    msg = str(ei.value)
    assert "botainer image build agent-claude" in msg
    assert "builder prune" in msg  # points at the SAFE reclaim, not system prune -a


def test_docker_endpoint_ip_allowlist_requires_endpoints() -> None:
    spec = _spec("docker")
    bad = spec.model_copy(update={"network": NetworkSpec(mode=NetworkMode.ENDPOINT_IP_ALLOWLIST)})
    with pytest.raises(Refused) as exc:
        DockerAdapter().render_argv(bad)
    assert exc.value.category == RefusalCategory.API_ONLY_REQUIRES_ENDPOINTS


def test_apptainer_argv_baseline() -> None:
    aa = ApptainerAdapter()
    # Task #88 + #162: apptainer adapter now refuses network.mode=none
    # because it can't enforce. Use INTERNET (host network, the only
    # mode apptainer actually supports without unshare -n).
    spec = _spec("apptainer").model_copy(
        update={"network": NetworkSpec(mode=NetworkMode.INTERNET)}
    )
    argv = aa.render_argv(spec)
    assert argv[0:4] == ["apptainer", "exec", "--containall", "--cleanenv"]
    assert "--bind" in argv
    assert argv[-1].startswith("botainer/agent-claude:0.1@sha256:")


def test_apptainer_refuses_network_none() -> None:
    # Task #88: apptainer can't enforce network=none, refuses with
    # RUNTIME_CANNOT_ENFORCE rather than silently inheriting host net.
    with pytest.raises(Refused) as exc:
        ApptainerAdapter().render_argv(_spec("apptainer"))
    assert exc.value.category == RefusalCategory.RUNTIME_CANNOT_ENFORCE


def test_apptainer_refuses_endpoint_ip_allowlist() -> None:
    spec = _spec("apptainer").model_copy(
        update={
            "network": NetworkSpec(mode=NetworkMode.ENDPOINT_IP_ALLOWLIST, endpoints=("anthropic",))
        }
    )
    with pytest.raises(Refused) as exc:
        ApptainerAdapter().render_argv(spec)
    assert exc.value.category == RefusalCategory.RUNTIME_CANNOT_ENFORCE


def test_mock_adapter_records_launches() -> None:
    ma = MockAdapter()
    spec = _spec("mock")
    h = ma.launch(spec)
    assert h.runtime == "mock"
    assert len(ma.launched) == 1
    assert ma.attach(h) == 0


# ────────── §A19 screen wrap on launch ──────────


def _patch_binaries(monkeypatch, *, screen: bool, docker: bool, apptainer: bool) -> None:
    """Make shutil.which() answer per the flags so launch() takes the
    intended branches without requiring real binaries on PATH."""
    avail = {}
    if screen:
        avail["screen"] = "/usr/bin/screen"
    if docker:
        avail["docker"] = "/usr/bin/docker"
    if apptainer:
        avail["apptainer"] = "/usr/bin/apptainer"
    monkeypatch.setattr(
        "shutil.which", lambda b: avail.get(b),
    )


def _stub_image_present(monkeypatch) -> None:
    """Make the `docker image inspect` preflight answer "present".

    THE cause of the CI failure. `DockerAdapter.launch` calls
    `_preflight_local_image(spec.image)` BEFORE the screen check, and that
    probe runs the REAL `docker` binary through `subprocess.run` — a route
    `shutil.which` stubbing does not touch. So on any machine that HAS Docker
    (every GitHub runner, every laptop with Docker Desktop) the launch refuses
    at `docker.py:105` with "image ... is not on this machine" and never
    reaches the branch under test.

    REPRODUCED locally, contrary to the earlier note: put any `docker` on PATH
    that exits non-zero and this test fails on the category. `screen` presence
    is irrelevant — `shutil.which` IS stubbed. And RUNTIME_LAUNCH_FAILED has
    SIX raise sites in `botainer/adapters/docker.py`, two of them (93, 105)
    inside the preflight that runs first.

    So this test's green in the dev container was a property of the container:
    Docker is ABSENT here, `subprocess.run` raised FileNotFoundError, and the
    probe's deliberately broad `except Exception: return` swallowed it. The
    three sibling launch tests escape the same probe BY ACCIDENT — they patch
    `subprocess.Popen` for an unrelated reason, `subprocess.run` is built on
    `Popen`, and the TypeError lands in that same `except`.

    Narrow deliberately: only the docker-inspect argv is intercepted, so
    anything else a test shells out to still runs for real.
    """
    real_run = subprocess.run

    def _fake_run(argv, *a, **kw):
        if list(argv)[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _fake_run)


class _FakePopen:
    """Stand-in for subprocess.Popen — records argv, fakes a fast exit."""
    last_argv: list[str] | None = None

    def __init__(self, argv, **_kw):
        _FakePopen.last_argv = list(argv)
        self.pid = 4242

    def wait(self, timeout=None):
        return 0


def test_docker_launch_wraps_with_screen_when_nudge_enabled(monkeypatch) -> None:
    _patch_binaries(monkeypatch, screen=True, docker=True, apptainer=False)
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    spec = _spec("docker").model_copy(update={"plugins_enabled": ("nudge",)})
    handle = DockerAdapter().launch(spec)
    assert _FakePopen.last_argv is not None
    assert _FakePopen.last_argv[0] == "screen"
    assert _FakePopen.last_argv[1] == "-dmS"
    assert _FakePopen.last_argv[2] == f"botainer-{spec.session_id}"
    assert "docker" in _FakePopen.last_argv
    assert handle.extras["screen_session_id"] == f"botainer-{spec.session_id}"
    # No pid for screen-wrapped path — attach() reattaches via `screen -r`,
    # not waitpid.
    assert handle.pid is None


def test_docker_launch_does_not_wrap_when_nudge_disabled(monkeypatch) -> None:
    _patch_binaries(monkeypatch, screen=True, docker=True, apptainer=False)
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    spec = _spec("docker")  # plugins_enabled empty
    handle = DockerAdapter().launch(spec)
    assert _FakePopen.last_argv is not None
    assert _FakePopen.last_argv[0] == "docker"
    assert "screen_session_id" not in handle.extras


def test_docker_launch_refuses_if_screen_missing_with_nudge(monkeypatch) -> None:
    _patch_binaries(monkeypatch, screen=False, docker=True, apptainer=False)
    _stub_image_present(monkeypatch)
    spec = _spec("docker").model_copy(update={"plugins_enabled": ("nudge",)})

    # SELF-DIAGNOSING, deliberately — and the diagnostics are what settled it.
    # This passed in the dev container and FAILED on a GitHub runner with
    # RUNTIME_LAUNCH_FAILED. The captured state below REFUTED both of the first
    # two theories in one run: which(screen) was None, so the monkeypatch DID
    # take effect, and plugins_enabled was ('nudge',), so model_copy DID apply.
    # The real cause is the image preflight that runs before the screen check —
    # see _stub_image_present. Keep the capture: it is what makes the next
    # surprise cost one run instead of a round trip.
    observed = {
        "which(screen)": shutil.which("screen"),
        "which(docker)": shutil.which("docker"),
        "plugins_enabled": spec.plugins_enabled,
    }

    with pytest.raises(Refused) as exc:
        DockerAdapter().launch(spec)
    assert exc.value.category == RefusalCategory.RUNTIME_NOT_AVAILABLE, (
        f"expected the screen pre-check to refuse, got {exc.value.category}.\n"
        f"  state at call time: {observed}\n"
        f"  refusal message: {exc.value}\n"
        "  'image ... is not on this machine' -> _preflight_local_image ran the\n"
        "     REAL docker before the screen check; _stub_image_present is not\n"
        "     covering the argv it now uses.\n"
        "  which(screen) truthy -> the shutil.which monkeypatch did not apply;\n"
        "  plugins_enabled without 'nudge' -> model_copy(update=...) did not apply."
    )
    assert "screen" in str(exc.value)


def test_apptainer_launch_wraps_with_screen_when_nudge_enabled(monkeypatch) -> None:
    _patch_binaries(monkeypatch, screen=True, docker=False, apptainer=True)
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    spec = _spec("apptainer").model_copy(
        update={
            "plugins_enabled": ("nudge",),
            "image": "/path/to/img.sif",
            "network": NetworkSpec(mode=NetworkMode.INTERNET),
        }
    )
    handle = ApptainerAdapter().launch(spec)
    assert _FakePopen.last_argv is not None
    assert _FakePopen.last_argv[0] == "screen"
    assert _FakePopen.last_argv[1] == "-dmS"
    assert _FakePopen.last_argv[2] == f"botainer-{spec.session_id}"
    assert "apptainer" in _FakePopen.last_argv
    assert handle.extras["screen_session_id"] == f"botainer-{spec.session_id}"


def test_apptainer_launch_refuses_if_screen_missing_with_nudge(monkeypatch) -> None:
    _patch_binaries(monkeypatch, screen=False, docker=False, apptainer=True)
    spec = _spec("apptainer").model_copy(
        update={
            "plugins_enabled": ("nudge",),
            "image": "/path/to/img.sif",
            "network": NetworkSpec(mode=NetworkMode.INTERNET),
        }
    )
    with pytest.raises(Refused) as exc:
        ApptainerAdapter().launch(spec)
    assert exc.value.category == RefusalCategory.RUNTIME_NOT_AVAILABLE


# ────────── §A19 screen attach (reattach via `screen -r`) ──────────


class _FakeRun:
    last_argv: list[str] | None = None
    returncode: int = 0

    def __init__(self, argv, **_kw):
        _FakeRun.last_argv = list(argv)
        self.returncode = _FakeRun.returncode


def test_docker_attach_uses_screen_r_when_screen_session_recorded(monkeypatch) -> None:
    from botainer.adapters.base import RuntimeHandle
    _patch_binaries(monkeypatch, screen=True, docker=True, apptainer=False)
    monkeypatch.setattr("subprocess.run", _FakeRun)
    handle = RuntimeHandle(
        runtime="docker", id="botainer-abc", pid=None,
        extras={"screen_session_id": "botainer-abcdef0123456789"},
    )
    rc = DockerAdapter().attach(handle)
    assert _FakeRun.last_argv == ["screen", "-r", "botainer-abcdef0123456789"]
    assert rc == 0


def test_apptainer_attach_uses_screen_r_when_screen_session_recorded(monkeypatch) -> None:
    from botainer.adapters.base import RuntimeHandle
    _patch_binaries(monkeypatch, screen=True, docker=False, apptainer=True)
    monkeypatch.setattr("subprocess.run", _FakeRun)
    handle = RuntimeHandle(
        runtime="apptainer", id="42", pid=None,
        extras={"screen_session_id": "botainer-abcdef0123456789"},
    )
    rc = ApptainerAdapter().attach(handle)
    assert _FakeRun.last_argv == ["screen", "-r", "botainer-abcdef0123456789"]
    assert rc == 0


def test_docker_argv_selinux_label_disable_when_enforcing(monkeypatch) -> None:
    """recon T-C: on a SELinux-Enforcing host the docker run gets
    `--security-opt label=disable` so bind mounts aren't blocked. Gated on
    the /sys/fs/selinux/enforce probe — no-op on mac / non-SELinux Linux."""
    import botainer.adapters.docker as dmod
    monkeypatch.setattr(dmod, "_selinux_enforcing", lambda: True)
    argv = dmod.DockerAdapter().render_argv(_spec("docker"))
    # The pair must appear together.
    assert "label=disable" in argv
    i = argv.index("label=disable")
    assert argv[i - 1] == "--security-opt"


def test_docker_argv_no_selinux_flag_when_not_enforcing(monkeypatch) -> None:
    """No SELinux → no label=disable (the common mac / Debian case)."""
    import botainer.adapters.docker as dmod
    monkeypatch.setattr(dmod, "_selinux_enforcing", lambda: False)
    argv = dmod.DockerAdapter().render_argv(_spec("docker"))
    assert "label=disable" not in argv


def test_selinux_enforcing_probe_reads_sys_file(monkeypatch, tmp_path) -> None:
    """The probe reads /sys/fs/selinux/enforce; absent file → False."""
    import botainer.adapters.docker as dmod
    from pathlib import Path as _P
    # Absent file (patch Path to point at a nonexistent path) → False.
    real_read = _P.read_text
    def fake_read(self, *a, **k):
        if str(self) == "/sys/fs/selinux/enforce":
            raise OSError("no selinux")
        return real_read(self, *a, **k)
    monkeypatch.setattr(_P, "read_text", fake_read)
    assert dmod._selinux_enforcing() is False


def test_apptainer_sets_home_via_home_flag_not_env(tmp_path) -> None:
    """Apptainer REFUSES `--env HOME=` — it must be `--home src:dest`.

    Apptainer reports:
        WARNING: Overriding HOME environment variable with APPTAINERENV_HOME
                 is not permitted

    Composition sets HOME=/home/user so npm/npx, pip and ~/.claude land in the
    persistent per-project home bind. The adapter passed it as `--env HOME=…`,
    which apptainer silently drops. Net effect on HPC: the home dir was mounted
    at /home/user and NOTHING pointed at it — under --containall the agent's
    $HOME was an empty tmpfs and every cache/credential write was ephemeral.
    Docker was always right, so it never showed up on the laptop.

    The old test shape ("--env HOME=... is in argv") passed throughout, because
    presence in argv is not effect. This asserts the mechanism apptainer
    actually honours, and that the bind is not applied twice.
    """
    from botainer.adapters.apptainer import ApptainerAdapter

    base = _spec("apptainer")
    home_target = "/home/user"
    # The shape composition produces: a rw bind at the HOME target, plus
    # HOME in spec.env.values.
    spec = base.model_copy(update={
        # apptainer refuses network.mode=none (it can't enforce it)
        "network": NetworkSpec(mode=NetworkMode.INTERNET),
        "mount_plan": base.mount_plan.model_copy(update={
            "binds": base.mount_plan.binds + (
                Bind(source="/state/home", target=home_target,
                     mode=BindMode.RW, provenance=Provenance.CORE),
            )
        }),
        "env": base.env.model_copy(update={
            "values": {**base.env.values, "HOME": home_target}
        }),
    })

    argv = ApptainerAdapter().render_argv(spec)

    assert "--home" in argv, (
        "apptainer ignores `--env HOME=`; the home must be set with --home")
    home_val = argv[argv.index("--home") + 1]
    assert home_val.endswith(f":{home_target}"), home_val

    # the rejected channel must be gone (it only reproduced the warning)
    assert not any(a == f"HOME={home_target}" for a in argv), (
        "`--env HOME=` is refused by apptainer and must not be emitted")

    # ...and the home path must be mounted exactly once
    mounted = [a for a in argv if a.endswith(f":{home_target}")]
    assert len(mounted) == 1, f"home bound {len(mounted)}x: {mounted}"


# --------------------------------------------------------------------------
# PID-1 must reap orphan processes to prevent zombie accumulation and
# eventual exhaustion of the container process limit.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {},                                        # foreground -it
    {"detach": True},                          # background -d
    {"interactive": False},                    # capture run (selftest probe)
])
def test_docker_always_runs_an_init_that_reaps_orphans(kwargs) -> None:
    """Without --init the AGENT is PID 1 and never reaps re-parented orphans.

    They accumulate as zombies holding PID slots until the container cannot
    fork; the resulting errors ("Resource temporarily unavailable", thread
    spawn panics) land far from the cause and read as flakiness. Nothing
    inside the container can recover — only PID 1 could reap, and it will not
    — so the container has to be destroyed.

    All three launch modes need it: a long-lived detached session is if
    anything MORE exposed than an interactive one.
    """
    argv = DockerAdapter().render_argv(_spec("docker"), **kwargs)
    assert "--init" in argv, f"no init process for {kwargs or 'foreground'}: {argv}"
    # must precede the image/command, i.e. be a `docker run` flag
    assert argv.index("--init") < len(argv) - 1


def test_apptainer_never_disables_its_default_init_shim() -> None:
    """A regression guard on a default we silently depend on.

    apptainer passes --containall, which contains "PID, IPC, and environment"
    — the same new-PID-namespace setup that breaks docker. It has been fine
    only because apptainer STARTS AN INIT SHIM BY DEFAULT under --pid, and
    `--no-init` is the documented way to turn that off. Nothing in our code
    references that behaviour, so a future author could add --no-init (to
    "reduce overhead", say) and reintroduce the docker bug on the HPC side
    with no visible connection. This test is the connection.
    """
    spec = _spec("apptainer").model_copy(
        update={"network": NetworkSpec(mode=NetworkMode.INTERNET)}
    )
    argv = ApptainerAdapter().render_argv(spec)
    # the flag whose default we are relying on
    assert "--containall" in argv, "no PID namespace, so this guard is moot"
    assert "--no-init" not in argv, (
        "--no-init disables the shim that reaps orphans in apptainer's PID "
        "namespace; see the docker --init comment for what that costs")


# ── GPU exposure (#177) ────────────────────────────────────────────────────
#
# Slurm ALLOCATING a device and the container being able to SEE it are two
# separate things, and only the first was happening for a session. hpc/jobs.py
# and hpc/pool.py already rendered `--nv` for dispatched jobs and warm-pool
# tasks — the session path, one layer up, did not. So `hpc submit --gpus 1`
# consumed a GPU off a contended partition and the workload failed at import
# time, far from the flag that caused it.
#
# These tests are the connection between the two layers. They render argv
# only, which is pure and needs no daemon; whether the device is actually
# usable inside the container is a cluster observation and is NOT claimed here.

def _gpu_spec(runtime: str, gpus: int) -> SessionSpec:
    update: dict = {"resources": ResourceSpec(gpus=gpus)}
    if runtime == "apptainer":
        # The baseline spec asks for network.mode=none, which the apptainer
        # adapter REFUSES outright (it shares the host netns and will not
        # pretend otherwise — audit T4). Nothing to do with GPUs; without
        # this the test fails on the refusal and never reaches the flag.
        update["network"] = NetworkSpec(mode=NetworkMode.INTERNET)
    return _spec(runtime).model_copy(update=update)


def test_apptainer_renders_nv_when_gpus_requested() -> None:
    """`--containall` is precisely why this is required, not optional: it
    hides the host's NVIDIA driver libraries and device nodes, so a job
    holding a `--gres=gpu:N` allocation sees nothing without `--nv`."""
    argv = ApptainerAdapter().render_argv(_gpu_spec("apptainer", 1))
    assert "--nv" in argv, (
        "apptainer session with gpus>0 must render --nv, or the Slurm "
        "allocation is paid for and unusable (#177)"
    )


def test_apptainer_omits_nv_when_no_gpu_requested() -> None:
    """The default must not quietly widen what the container reaches."""
    assert "--nv" not in ApptainerAdapter().render_argv(_gpu_spec("apptainer", 0))


def test_docker_renders_gpus_when_requested() -> None:
    argv = DockerAdapter().render_argv(_gpu_spec("docker", 2))
    assert "--gpus" in argv
    assert argv[argv.index("--gpus") + 1] == "2"


def test_docker_omits_gpus_when_none_requested() -> None:
    assert "--gpus" not in DockerAdapter().render_argv(_spec("docker"))
    assert "--gpus" not in DockerAdapter().render_argv(_gpu_spec("docker", 0))


def test_both_runtimes_expose_gpus_or_neither_does() -> None:
    """HPC parity as a test rather than a promise.

    The defect this pins is not "apptainer lacked a flag" — it is that ONE
    runtime grew GPU support and the other did not, which is the shape that
    produced the docker-only `bot1 image build` the project calls its
    canonical bad example. Asserting the two together means a future change
    cannot quietly regress to a single-runtime feature.
    """
    docker = DockerAdapter().render_argv(_gpu_spec("docker", 1))
    apptainer = ApptainerAdapter().render_argv(_gpu_spec("apptainer", 1))
    assert ("--gpus" in docker) and ("--nv" in apptainer), (
        f"one runtime exposes the GPU and the other does not: "
        f"docker={'--gpus' in docker} apptainer={'--nv' in apptainer}"
    )


# ── a GPU request that cannot be honoured must say so at COMPOSE ───────────
#
# Rendering the flag (above) fixes the case where a GPU exists. These cover the
# two where the request cannot work, because #177 was not "the flag was
# missing" so much as "the failure surfaced far from its cause".

def test_a_gpu_request_is_refused_on_macos(monkeypatch) -> None:
    """Docker Desktop has no NVIDIA passthrough and Metal is not exposed to
    Linux containers, so `--gpus` cannot work on a Mac under any config.
    Passing it through makes the user decode dockerd's error instead."""
    from botainer.core.composition import _refuse_or_warn_on_gpu_request

    monkeypatch.setattr("sys.platform", "darwin")
    with pytest.raises(Refused) as exc:
        _refuse_or_warn_on_gpu_request(1, "docker")
    msg = str(exc.value)
    assert "macOS" in msg, "must name the platform in words, not 'darwin'"
    # A refusal that does not say what to do instead is half a refusal.
    assert "hpc submit" in msg and "resources.gpus: 0" in msg


def test_a_gpu_request_on_linux_warns_but_proceeds(monkeypatch, capsys) -> None:
    """Whether the NVIDIA Container Toolkit is installed cannot be known at
    compose time without calling the daemon. Refusing would break every
    correctly-configured Linux GPU host; silence would reproduce #177."""
    from botainer.core.composition import _refuse_or_warn_on_gpu_request

    monkeypatch.setattr("sys.platform", "linux")
    _refuse_or_warn_on_gpu_request(2, "docker")          # must NOT raise
    err = capsys.readouterr().err
    assert "NVIDIA Container Toolkit" in err
    assert "--gpus" in err, "name the flag dockerd will name"


def test_no_gpu_request_is_silent_on_every_platform(monkeypatch, capsys) -> None:
    """Pinned so the fix above cannot become a warning on every session — the
    scenery problem this project has been bitten by."""
    from botainer.core.composition import _refuse_or_warn_on_gpu_request

    for plat in ("darwin", "linux"):
        monkeypatch.setattr("sys.platform", plat)
        _refuse_or_warn_on_gpu_request(0, "docker")
        assert capsys.readouterr().err == ""


def test_apptainer_gpu_requests_are_never_refused(monkeypatch) -> None:
    """The cluster case is the one expected to work, and `--nv` degrades to
    'no device visible' rather than failing — so a platform guard there would
    only ever be wrong."""
    from botainer.core.composition import _refuse_or_warn_on_gpu_request

    monkeypatch.setattr("sys.platform", "darwin")
    _refuse_or_warn_on_gpu_request(4, "apptainer")       # must NOT raise
