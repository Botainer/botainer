"""The original client result must survive a fast child exit or failed wait."""
import os
import sys

from botainer.adapters.base import RuntimeHandle
from botainer.adapters.docker import DockerAdapter
from botainer.core.spec import SessionSpec


def test_original_process_keeps_fast_nonzero_result_after_wait(tmp_path, monkeypatch):
    from botainer.adapters import docker
    spec = SessionSpec(session_id="abcdef0123456789", project_uuid="fixture-project",
                       project_root=str(tmp_path), state_dir=str(tmp_path / "state"),
                       runtime="docker", image="fixture:inert")
    argv = [sys.executable, "-I", "-B", "-c", "raise SystemExit(7)"]
    monkeypatch.setattr(docker.shutil, "which", lambda _: "/synthetic/docker")
    monkeypatch.setattr(DockerAdapter, "render_argv", lambda *a, **k: argv)
    adapter = DockerAdapter()
    handle = adapter.launch(spec)
    assert handle.process is not None
    assert handle.process.wait(timeout=5) == 7
    assert adapter.attach(handle) == 7


def test_missing_wait_result_is_failure_not_success(monkeypatch):
    def reaped(*args):
        raise ChildProcessError("fixture already reaped")
    monkeypatch.setattr(os, "waitpid", reaped)
    handle = RuntimeHandle(runtime="docker", id="a" * 64, pid=123456)
    assert DockerAdapter().attach(handle) != 0
