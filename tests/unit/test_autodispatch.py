"""HPC job-dispatcher auto-start (botainer/hpc/autodispatch.py).

This is the wiring that makes agent-submitted jobs actually RUN (instead of
sitting `pending` while the user is told to start a dispatcher). The spawn is
mocked — these assert the DECISION logic + that the launcher-PID watchdog is
wired, without launching a real dispatcher.
"""
from __future__ import annotations

from botainer.core.spec import (
    Bind,
    BindMode,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    Provenance,
    SessionSpec,
)
from botainer.hpc import autodispatch


def _spec(*, with_jobs: bool, runtime: str = "apptainer") -> SessionSpec:
    binds = [
        Bind(source="/h/p", target="/workspace", mode=BindMode.RW,
             provenance=Provenance.CORE),
    ]
    if with_jobs:
        binds.append(Bind(
            source="/h/botainer-job", target="/usr/local/bin/botainer-job",
            mode=BindMode.RO, provenance=Provenance.USER))
    return SessionSpec(
        session_id="abc123def456",
        project_uuid="11111111-1111-1111-1111-111111111111",
        project_root="/h/p",
        state_dir="/state",
        runtime=runtime,
        image="botainer/agent-claude:0.1@sha256:" + "a" * 64,
        mount_plan=MountPlan(binds=tuple(binds)),
        network=NetworkSpec(mode=NetworkMode.NONE),
        plugins_enabled=("agent-claude", "hpc-launcher"),
    )


def test_jobs_enabled_keys_on_the_botainer_job_bind() -> None:
    assert autodispatch.jobs_enabled(_spec(with_jobs=True)) is True
    assert autodispatch.jobs_enabled(_spec(with_jobs=False)) is False


def test_maybe_start_none_when_jobs_disabled(monkeypatch) -> None:
    monkeypatch.setattr(autodispatch.shutil, "which", lambda _n: "/usr/bin/sbatch")
    assert autodispatch.maybe_start(_spec(with_jobs=False)) is None


def test_maybe_start_none_without_sbatch(monkeypatch) -> None:
    # Jobs enabled but no scheduler here (laptop) → skip, requests pend.
    monkeypatch.setattr(autodispatch.shutil, "which", lambda _n: None)
    assert autodispatch.maybe_start(_spec(with_jobs=True)) is None


def test_maybe_start_spawns_dispatcher_with_watchdog(monkeypatch) -> None:
    monkeypatch.setattr(autodispatch.shutil, "which", lambda _n: "/usr/bin/sbatch")
    captured: dict = {}

    class _Proc:
        pid = 4242

    def _popen(argv, env=None, **_kw):
        captured["argv"] = argv
        captured["env"] = env
        return _Proc()

    monkeypatch.setattr(autodispatch.subprocess, "Popen", _popen)
    pid = autodispatch.maybe_start(_spec(with_jobs=True), interval=7)
    assert pid == 4242
    argv = captured["argv"]
    assert argv[1:5] == ["-m", "botainer.cli.main", "hpc", "dispatcher"]
    assert "start" in argv and "--interval" in argv and "7" in argv
    assert "--project" in argv and "/h/p" in argv
    # The watchdog: the dispatcher self-exits if this launcher dies.
    assert captured["env"]["BOTAINER_LAUNCHER_PID"]


def test_maybe_start_is_guarded_against_spawn_failure(monkeypatch) -> None:
    monkeypatch.setattr(autodispatch.shutil, "which", lambda _n: "/usr/bin/sbatch")

    def _boom(*_a, **_kw):
        raise OSError("no fork for you")

    monkeypatch.setattr(autodispatch.subprocess, "Popen", _boom)
    # Must NOT raise — the session has to launch regardless.
    assert autodispatch.maybe_start(_spec(with_jobs=True)) is None


def test_stop_handles_none_and_dead_pid(monkeypatch) -> None:
    autodispatch.stop(None)  # no-op, no raise

    def _kill(_pid, _sig):
        raise ProcessLookupError()

    monkeypatch.setattr(autodispatch.os, "kill", _kill)
    autodispatch.stop(12345)  # swallowed, no raise
