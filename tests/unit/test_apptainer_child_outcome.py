"""Foreground Apptainer client results through real launch/attach callers.

Only the runtime argv and binary-presence boundary are replaced. Children are
inert Python processes, never Apptainer workloads. Retaining a test-side Popen
reference makes the same tests reach their result assertions before and after
the correction, without depending on the new handle field to capture a child.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

import pytest

from botainer.adapters import apptainer
from botainer.adapters.apptainer import ApptainerAdapter
from botainer.adapters.base import RuntimeHandle
from botainer.core.spec import SessionSpec


@pytest.fixture
def launch_inert(tmp_path, monkeypatch, record_property):
    children = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(apptainer.shutil, "which",
                        lambda name: "/synthetic/apptainer" if name == "apptainer" else None)
    monkeypatch.setenv("APPTAINERENV_TEST_SENTINEL", "must-not-reach-child")
    monkeypatch.setenv("SINGULARITYENV_TEST_SENTINEL", "must-not-reach-child")

    def launch(code):
        argv = [sys.executable, "-I", "-B", "-c", code]

        def retain(command, **kwargs):
            assert command == argv
            assert set(kwargs) == {"env"}
            assert not any(key.startswith(("APPTAINERENV_", "SINGULARITYENV_"))
                           for key in kwargs["env"])
            child = real_popen(command, **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(apptainer.subprocess, "Popen", retain)
        adapter = ApptainerAdapter()
        monkeypatch.setattr(adapter, "render_argv", lambda _spec: list(argv))
        spec = SessionSpec(
            session_id="abcdef0123456789",
            project_uuid="11111111-1111-4111-8111-111111111111",
            project_root=str(tmp_path), state_dir=str(tmp_path / "state"),
            runtime="apptainer", image="fixture:inert",
        )
        handle = adapter.launch(spec)
        assert len(children) == 1
        return adapter, handle, children[0]

    yield launch

    cleanup = []
    for child in children:
        action = "already-reaped"
        if child.poll() is None:
            child.terminate()
            action = "terminated"
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
                action = "killed"
        cleanup.append({"action": action, "returncode": child.returncode,
                        "reaped": child.poll() is not None})
    record_property("owned_children", json.dumps(cleanup, sort_keys=True))
    assert all(row["reaped"] for row in cleanup)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_native_launch_preserves_cached_child_result(launch_inert, record_property, exit_code):
    adapter, handle, child = launch_inert(f"raise SystemExit({exit_code})")
    actual_child = child.wait(timeout=5)
    assert actual_child == exit_code
    observed = adapter.attach(handle)
    record_property("observed_child_exit", actual_child)
    record_property("observed_attach_result", observed)
    assert observed == exit_code
    assert adapter.attach(handle) == exit_code


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal exit convention")
def test_native_launch_preserves_cached_signal_result(launch_inert, record_property):
    adapter, handle, child = launch_inert(
        "import os,signal; os.kill(os.getpid(),signal.SIGTERM)")
    actual_child = child.wait(timeout=5)
    assert actual_child == -signal.SIGTERM
    observed = adapter.attach(handle)
    record_property("observed_child_exit", actual_child)
    record_property("observed_attach_result", observed)
    assert observed == 128 + signal.SIGTERM
    assert adapter.attach(handle) == 128 + signal.SIGTERM


@pytest.mark.parametrize("error", [ChildProcessError, OSError])
def test_pid_only_unavailable_wait_is_failure(monkeypatch, error):
    def unavailable(pid, options):
        assert pid == 2147483647 and options == 0
        raise error("synthetic unavailable child result")

    monkeypatch.setattr(os, "waitpid", unavailable)
    handle = RuntimeHandle(runtime="apptainer", id="fixture", pid=2147483647)
    assert ApptainerAdapter().attach(handle) == 1


@pytest.mark.parametrize("error", [ChildProcessError, OSError])
def test_retained_process_wait_error_is_failure(error):
    class UnavailableProcess:
        def wait(self):
            raise error("synthetic unavailable process result")

    handle = RuntimeHandle(runtime="apptainer", id="fixture", process=UnavailableProcess())
    assert ApptainerAdapter().attach(handle) == 1


def test_screen_viewer_result_takes_precedence_over_process_wait(monkeypatch):
    class DoNotWait:
        def wait(self):
            pytest.fail("screen attach must not wait for an ordinary foreground child")

    monkeypatch.setattr(apptainer.shutil, "which", lambda name: "/synthetic/screen")
    calls = []

    def viewer(argv, *, check):
        calls.append(argv)
        assert check is False
        return subprocess.CompletedProcess(argv, 23)

    monkeypatch.setattr(apptainer.subprocess, "run", viewer)
    handle = RuntimeHandle(runtime="apptainer", id="fixture", process=DoNotWait(),
                           extras={"screen_session_id": "botainer-fixture"})
    assert ApptainerAdapter().attach(handle) == 23
    assert calls == [["screen", "-r", "botainer-fixture"]]
