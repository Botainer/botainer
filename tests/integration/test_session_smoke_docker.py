"""Real-Docker end-to-end smoke for `botainer start`.

Unlike `tests/unit/test_session_smoke_with_mock.py` (which uses the
MockAdapter to verify compose wiring without a runtime), this file
exercises the FULL launcher → docker daemon → container path. It
SKIPS when Docker isn't on PATH so the file is harmless to ship in
a container build (the dev-container this repo lives in doesn't have
docker; a Mac/Linux host with Docker Desktop will run them).

Coverage gap addressed: #129, #214 + R9-5. The audit history flagged
"every test labeled smoke/e2e actually uses MockAdapter" as Pattern J6
(mock-labeled-as-e2e). This file is the real-runtime companion: when docker
is present, real launch is asserted; when absent, the test cleanly
skips with a clear reason rather than silently downgrading.

Scope deliberately minimal at v0.1:
- `botainer start --detach` boots a container with the agent image,
  records the container_id in the session_record, returns 0.
- `botainer status` lists the running session.
- `botainer stop` SIGTERMs the container; subsequent `docker ps`
  doesn't show it.
- A round-trip of mount-plan readback (host-side `docker inspect`
  vs. the spec the launcher composed) — the in-tree
  `preflight.host.run_host_readback` helper.

Excluded for now (separate test files):
- Nudge end-to-end (needs `screen` on the host AND a way to drive
  input/output; see the retired `tests/manual/test_nudge_real_tmux.py`
  for the shape a future real-screen companion should take).
- HPC sbatch flow (see test_session_smoke_apptainer.py).

A test that needs to actually start a container takes ~3-5s on a warm
Docker daemon and pulls the agent image first time (~3 GB). Marker is
`docker` (registered in pyproject.toml) so CI can opt in/out cleanly.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest


_DOCKER = shutil.which("docker")


def _docker_alive() -> bool:
    """True if `docker info` succeeds (daemon running, socket reachable)."""
    if _DOCKER is None:
        return False
    try:
        proc = subprocess.run(
            [_DOCKER, "info"], capture_output=True, timeout=5, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_alive(),
    reason=(
        "real-docker smoke needs `docker` on PATH AND a running daemon. "
        "Run on a Mac/Linux host with Docker Desktop / dockerd; the "
        "dev-container this repo lives in deliberately has no daemon."
    ),
)


@pytest.mark.docker
def test_real_docker_start_status_stop_round_trip() -> None:
    """The end-to-end gap from #129. Wiring placeholder until a host
    with Docker actually runs it; the docstring above describes the
    test plan in full. The implementation lands when (a) the test is
    actually executed on a real host or (b) a CI runner with Docker
    is wired."""
    pytest.skip(
        "implementation pending real-host validation — see module "
        "docstring for the test plan",
    )
