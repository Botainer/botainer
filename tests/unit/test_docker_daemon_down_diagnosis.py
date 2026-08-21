"""UX audit (M4): a stopped Docker daemon must not be misdiagnosed as
'image not built'.

`docker image inspect` exits non-zero for ANY reason. The launch preflight assumed
the only reason was "image absent" and told the user to run `botainer image build`
— a ~12-minute operation that CANNOT succeed, because the daemon it needs is the
one that is down. This is the single most common laptop failure, and it was the one
place the guidance was actively wrong. doctor.py already detects it correctly.
"""
from __future__ import annotations

import subprocess
import types

import pytest

from botainer.adapters import docker as docker_adapter
from botainer.core.refusal import Refused


def _fake_inspect(stderr: str):
    def _run(argv, **kw):
        if argv[:3] == ["docker", "image", "inspect"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr=stderr)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return _run


@pytest.mark.parametrize("stderr", [
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
    "error during connect: Get \"http://%2F%2F.%2Fpipe%2Fdocker_engine/v1.24/...\": open //./pipe/docker_engine: The system cannot find the file specified.",
    "ERROR: Cannot connect to the Docker daemon. Is the docker daemon running?",
])
def test_daemon_down_says_start_docker_not_build(monkeypatch, stderr: str) -> None:
    monkeypatch.setattr(docker_adapter.subprocess, "run", _fake_inspect(stderr))
    with pytest.raises(Refused) as exc:
        docker_adapter._preflight_local_image("botainer/agent-claude:0.1")
    msg = str(exc.value)
    assert "daemon is not running" in msg
    assert "Docker Desktop" in msg
    assert "do NOT run `botainer image build`" in msg    # the wrong advice, refuted


def test_genuinely_missing_image_still_says_build(monkeypatch) -> None:
    """The original behaviour must survive for the case it was written for."""
    monkeypatch.setattr(docker_adapter.subprocess, "run",
                        _fake_inspect("Error: No such image: botainer/agent-claude:0.1"))
    with pytest.raises(Refused) as exc:
        docker_adapter._preflight_local_image("botainer/agent-claude:0.1")
    msg = str(exc.value)
    assert "not on this machine" in msg
    assert "botainer image build agent-claude" in msg
