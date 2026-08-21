"""HPC-parity audit (C2): the sbatch cross-node check must be
TRANSPORT-AGNOSTIC.

Check 1 of `_refuse_cross_node_binds` is bind-shaped (UNIX_SOCKET / FIFO), so it
was blind to a TCP rendezvous. `agent-codex-broker` is TCP on every runtime and
contributes NO bind, so `botainer hpc submit` succeeded, the daemon started on the
LOGIN node, OPENAI_BASE_URL=http://127.0.0.1:<login-port> was baked into the sbatch
script, and hours later the job hit ECONNREFUSED on a compute node — after burning
the queue wait and the allocation. submit.py's name gate matches only `*-proxy`.

A loopback address is host-local BY DEFINITION, which is why this is a property
rather than another plugin-name list.
"""
from __future__ import annotations

import pytest

from botainer.core import composition
from botainer.core.refusal import Refused
from botainer.core.spec import EnvSpec, MountPlan, NetworkMode, NetworkSpec, SessionSpec


def _spec(env: dict[str, str]) -> SessionSpec:
    return SessionSpec(
        session_id="ses-x",
        project_uuid="u" * 32,
        project_root="/tmp/proj",
        image="test:0.1",
        runtime="apptainer",
        state_dir="/home/t/.botainer/state/uuuu",
        plugins_enabled=(),
        env=EnvSpec(values=env),
        mount_plan=MountPlan(),
        network=NetworkSpec(mode=NetworkMode.INTERNET),
    )


@pytest.mark.parametrize("value", [
    "http://127.0.0.1:8931/v1",                 # the codex-broker shape
    "http://localhost:8931/v1",
    "http://[::1]:8931/v1",
    "http://host.docker.internal:8931/v1",      # docker-ism leaking onto HPC
])
def test_refuses_loopback_endpoint_on_the_sbatch_path(value: str) -> None:
    with pytest.raises(Refused) as exc:
        composition._refuse_cross_node_binds(_spec({"OPENAI_BASE_URL": value}))
    msg = str(exc.value).lower()
    assert "loopback" in msg
    assert "compute node" in msg          # says WHY, concretely
    assert "salloc" in msg                # says what to do instead


def test_ordinary_env_is_unaffected() -> None:
    composition._refuse_cross_node_binds(_spec({
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "HOME": "/home/user",
        "BOTAINER_AGENT_PERMISSIONS": "bypass",
    }))   # must not raise
