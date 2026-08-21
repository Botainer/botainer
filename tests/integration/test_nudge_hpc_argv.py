"""HPC nudge argv-construction tests.

Post-§A19 (FEATURE-PARTITION-LOCKED.md): nudge delivers via a HOST-side
`screen` session, not via `docker exec` / in-container tmux. This file
pins the post-§A19 argv shape:

  Docker:    screen -S <sid> -X stuff -- "<text>"
  HPC:       srun --overlap --jobid=<X> screen -S <sid> -X stuff -- "<text>"
             (screen lives on the compute node; created in sbatch script)
  Apptainer foreground (no slurm): same as Docker (screen on same host).

A future manual end-to-end test (real cluster + real screen) will
cover actual delivery; until that lands (the pre-§A19 manual test was
retired with the tmux pathway), this test pins the argv construction
so a refactor noisily breaks if the cross-node hop or screen wrapping
changes.

HPC-IMPL #9 in HPC-IMPLEMENTATION-PLAN.md.
"""

from __future__ import annotations

import pytest

from botainer.cli.nudge import _build_delivery_argv
from botainer.state.session_record import (
    ApptainerHandle,
    DockerHandle,
    SessionRecord,
)


def _record(
    *,
    runtime: str,
    apptainer: ApptainerHandle | None = None,
    docker: DockerHandle | None = None,
    screen_session_id: str | None = "botainer-ses1",
) -> SessionRecord:
    return SessionRecord(
        session_id="ses1",
        project_uuid="u1",
        project_root="/proj",
        runtime=runtime,
        image="botainer/agent:0.1",
        host="login.example.edu",
        spec={},
        apptainer=apptainer,
        docker=docker,
        screen_session_id=screen_session_id,
    )


def test_nudge_apptainer_with_slurm_jobid_uses_srun_overlap() -> None:
    """HPC path: when session was launched via sbatch and we have a
    slurm_jobid, nudge must use `srun --overlap --jobid=<id>` to
    reach the compute node from the login node. Then `screen -X stuff`
    against the compute-node screen session (created in sbatch script)."""
    rec = _record(
        runtime="apptainer",
        apptainer=ApptainerHandle(slurm_jobid="12345"),
    )
    argv = _build_delivery_argv(rec, ["hello"])
    assert argv[0] == "srun", f"HPC nudge must use srun; got {argv!r}"
    assert "--overlap" in argv, (
        f"HPC nudge must pass --overlap to share the parent step; got {argv!r}"
    )
    assert "--jobid" in argv, (
        f"HPC nudge must target the agent's jobid; got {argv!r}"
    )
    jobid_idx = argv.index("--jobid")
    assert argv[jobid_idx + 1] == "12345"
    # Post-§A19: must use `screen -X stuff`, NOT `apptainer exec ... tmux`.
    assert "screen" in argv
    assert "stuff" in argv
    assert "apptainer" not in argv, (
        f"HPC nudge must NOT apptainer-exec into the container; the screen "
        f"session lives on the compute node OUTSIDE the apptainer container "
        f"(§A19). got {argv!r}"
    )
    assert "tmux" not in argv, (
        f"HPC nudge must NOT use tmux (§A19: tmux retired in favor of "
        f"screen-outside-container). got {argv!r}"
    )


def test_nudge_apptainer_with_jobid_and_instance_does_not_apptainer_exec() -> None:
    """Even with an apptainer instance recorded, post-§A19 the screen
    session lives OUTSIDE the container on the compute node, so no
    apptainer-exec hop is needed."""
    rec = _record(
        runtime="apptainer",
        apptainer=ApptainerHandle(
            slurm_jobid="12345",
            instance_name="botainer-ses1",
        ),
    )
    argv = _build_delivery_argv(rec, ["hello"])
    assert "apptainer" not in argv, (
        f"§A19: nudge must NOT apptainer-exec; screen is outside the container. "
        f"got {argv!r}"
    )
    assert "instance://botainer-ses1" not in argv


def test_nudge_apptainer_without_jobid_runs_locally() -> None:
    """No slurm_jobid → we're on the same node as the agent; just run
    screen directly (no srun hop)."""
    rec = _record(
        runtime="apptainer",
        apptainer=ApptainerHandle(slurm_jobid=None),
    )
    argv = _build_delivery_argv(rec, ["hello"])
    assert "srun" not in argv, (
        f"local apptainer nudge must NOT use srun; got {argv!r}"
    )
    assert argv[0] == "screen"


def test_nudge_docker_uses_screen_on_host() -> None:
    """Post-§A19 Docker baseline: argv is `screen -S <sid> -X stuff -- ...`
    run DIRECTLY on the host. No `docker exec` — the screen session is
    the docker container's PARENT pty, on the host, created by
    `botainer start` when nudge plugin is enabled."""
    rec = _record(
        runtime="docker",
        docker=DockerHandle(container_id="abc123"),
    )
    argv = _build_delivery_argv(rec, ["hello"])
    assert argv[:5] == ["screen", "-S", "botainer-ses1", "-X", "stuff"], (
        f"docker nudge prefix wrong (post-§A19 should be screen-on-host): {argv!r}"
    )
    assert "docker" not in argv, (
        f"§A19: nudge must NOT docker-exec; screen is outside the container. "
        f"got {argv!r}"
    )


def test_nudge_refuses_when_screen_session_id_missing() -> None:
    """A session started WITHOUT nudge enabled has no host-side screen
    wrap, so screen_session_id is None. Nudge must refuse, not fail open."""
    rec = _record(
        runtime="docker",
        docker=DockerHandle(container_id="abc123"),
        screen_session_id=None,
    )
    from botainer.core.refusal import Refused
    with pytest.raises(Refused) as exc:
        _build_delivery_argv(rec, ["hello"])
    assert "screen_session_id" in str(exc.value).lower() or \
           "nudge" in str(exc.value).lower()
