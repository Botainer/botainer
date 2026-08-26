"""#54 P1: caged child-job compose + never-bare-job chokepoint (INV-2)."""
from __future__ import annotations

import os

import pytest

from botainer.core.refusal import Refused
from botainer.core.spec import Bind, BindMode, Provenance
from botainer.hpc import jobs

IMG = "botainer-child.sif"


def _bind(source: str, target: str, mode: BindMode = BindMode.RO) -> Bind:
    return Bind(source=source, target=target, mode=mode, provenance=Provenance.CORE)


def test_child_argv_is_section4_caged() -> None:
    argv = jobs.compose_child_job_argv(IMG, ("python", "train.py", "--epochs", "50"))
    assert argv[:2] == ["apptainer", "exec"]
    for flag in ("--containall", "--cleanenv", "--no-privs"):
        assert flag in argv
    i = argv.index("--drop-caps")
    assert argv[i + 1] == "all"
    # image, then the workload as ARGV operands (not shell).
    assert argv.index(IMG) < len(argv) - 1
    assert argv[argv.index(IMG) + 1:] == ["python", "train.py", "--epochs", "50"]


def test_gpu_job_gets_nv_flag() -> None:
    """A GPU job (gpus>0) must add `--nv` so the workload can see the GPU — the
    cage's --containall hides /dev/nvidia* otherwise (audit). Placed
    after the cage flags, before the image; the cage is otherwise unchanged."""
    argv = jobs.compose_child_job_argv(IMG, ("python", "train.py"), gpus=1)
    assert "--nv" in argv
    assert argv.index("--nv") < argv.index(IMG)          # before the image
    for flag in ("--containall", "--cleanenv", "--no-privs"):
        assert flag in argv                              # cage intact


def test_cpu_job_has_no_nv_flag() -> None:
    """--nv is GPU-only — never added to CPU jobs."""
    assert "--nv" not in jobs.compose_child_job_argv(IMG, ("python", "x.py"), gpus=0)
    assert "--nv" not in jobs.compose_child_job_argv(IMG, ("python", "x.py"))


def test_child_cage_matches_adapter_section4_flags() -> None:
    """Drift guard: the child cage MUST be the same §4 flags the ApptainerAdapter
    renders for the agent session — one definition of the cage."""
    import inspect

    from botainer.adapters import apptainer

    src = inspect.getsource(apptainer.ApptainerAdapter.render_argv)
    for flag in jobs._CHILD_CAGE_FLAGS:
        assert flag in src, f"child cage flag {flag!r} not in ApptainerAdapter"


def test_child_argv_never_shell() -> None:
    """A workload token full of shell metacharacters stays a single argv operand
    — never interpolated into a shell string (INV-2 / the data-flow rule)."""
    evil = "; rm -rf / #"
    argv = jobs.compose_child_job_argv(IMG, ("bash", "-lc", evil))
    assert evil in argv  # present verbatim as ONE element
    assert argv[-1] == evil


@pytest.mark.parametrize("cmd", [
    (),                       # empty
    ("apptainer", "exec"),    # container runtime entrypoint
    ("docker", "run"),        # container runtime
    ("podman", "run"),
    ("botainer", "start"),    # agent launcher
    ("claude",),              # agent CLI
    ("codex",),
])
def test_child_refuses_forbidden_or_empty_entrypoint(cmd) -> None:
    with pytest.raises(Refused):
        jobs.compose_child_job_argv(IMG, cmd)


def test_child_refuses_control_char_in_command() -> None:
    with pytest.raises(Refused):
        jobs.compose_child_job_argv(IMG, ("bash", "-c", "x\x00y"))


@pytest.mark.parametrize("target", [
    "/home", "/home/agent/.ssh", "/home/agent/.claude", "/home/agent/.codex",
    "/root", "/home/agent/.config/gcloud",
])
def test_child_refuses_credential_or_home_bind_target(target) -> None:
    """INV-2: a child job gets NO credentials/home/ssh."""
    with pytest.raises(Refused):
        jobs.compose_child_job_argv(IMG, ("bash",), (_bind("/host/x", target),))


@pytest.mark.parametrize("source", ["/", "/etc/shadow", None])
def test_child_refuses_denied_bind_source(source, monkeypatch, tmp_path) -> None:
    """The (ancestor-aware) source denylist applies to child binds too —
    source=/ can't smuggle /etc/shadow, etc.

    The home case is the CALLER'S OWN home, computed at run time. It used to be
    the literal "/home/user", which is this dev container's HOME — so the test
    passed here and failed on any machine with a different one, while appearing
    to assert something much broader.

    Note what this does NOT assert, because the code does not do it: ANOTHER
    user's home is not denied. `/home/alice/.ssh` is an allowed bind source on a
    shared login node — the denylist is built from expanduser("~") alone. That
    gap is tracked as #168; do not widen this test to paper over it,
    and do not narrow it either."""
    if source is None:
        # A home that is NOT already in the hard denylist. Under HOME=/root
        # (normal for container CI and for `sudo pytest`) the literal home
        # case is caught by DENYLISTED_SOURCES instead, so the
        # home-ancestor rule this test names would never execute and the
        # test would pass for the wrong reason.
        monkeypatch.setenv("HOME", str(tmp_path))
        source = str(tmp_path)

    with pytest.raises(Refused):
        jobs.compose_child_job_argv(IMG, ("bash",), (_bind(source, "/data"),))


@pytest.mark.parametrize("target", ["/etc", "/etc/ld.so.preload", "/proc",
                                    "/sys", "/dev", "/var/run/docker.sock"])
def test_child_refuses_denied_system_bind_target(target) -> None:
    """Defense-in-depth (sharp-edges F2): a child bind can't mount OVER the
    container's system dirs (/etc, /proc, /sys, /dev, docker sockets) — the
    shared target denylist, not just the creds/home one."""
    with pytest.raises(Refused):
        jobs.compose_child_job_argv(IMG, ("bash",), (_bind("/opt/x", target),))


def test_child_rw_workspace_without_botainer_mask_refused() -> None:
    # Structural invariant (sharp-edges): a RW /workspace bind with NO
    # /workspace/.botainer mask must be REFUSED — the class of the config-exposure
    # bug. This is what makes the mask structural, not "remember to add it".
    with pytest.raises(Refused):
        jobs.compose_child_job_argv(
            IMG, ("python", "run.py"),
            (_bind("/gpfs/data/proj", "/workspace", BindMode.RW),))


def test_child_allows_safe_binds_with_ro() -> None:
    argv = jobs.compose_child_job_argv(
        IMG, ("python", "run.py"),
        (_bind("/gpfs/data/proj", "/workspace", BindMode.RW),
         _bind("/anchor", "/workspace/.botainer", BindMode.NULL_BIND),  # required mask
         _bind("/apps", "/apps", BindMode.RO)),
    )
    assert "--bind" in argv
    joined = " ".join(argv)
    assert "/gpfs/data/proj:/workspace" in joined  # rw → no :ro
    assert "/apps:/apps:ro" in joined              # ro


def test_chokepoint_rejects_uncaged_argv() -> None:
    """assert_caged_child_job is the frozen-plan backstop: a hand-built argv
    missing the cage (or not apptainer-exec, or bare) is refused."""
    with pytest.raises(Refused):
        jobs.assert_caged_child_job(["bash", "train.sh"], IMG)  # bare, uncaged
    with pytest.raises(Refused):
        # apptainer exec but missing --drop-caps all
        jobs.assert_caged_child_job(
            ["apptainer", "exec", "--containall", "--cleanenv", "--no-privs", IMG, "python"],
            IMG,
        )
    with pytest.raises(Refused):
        # caged but the workload ENTRYPOINT is a runtime
        jobs.assert_caged_child_job(
            ["apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
             "--drop-caps", "all", IMG, "docker", "run", "x"],
            IMG,
        )


def test_chokepoint_allows_runtime_name_as_a_later_arg() -> None:
    """A forbidden name as an ARGUMENT (not the entrypoint) is fine — e.g. a
    grep pattern or filename `docker`. The cage bounds the workload regardless."""
    jobs.assert_caged_child_job(
        ["apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
         "--drop-caps", "all", IMG, "grep", "docker", "notes.txt"],
        IMG,
    )
