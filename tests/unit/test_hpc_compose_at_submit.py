"""Compose-at-submit restructure (DN-004).

`composition.compose_agent_exec_for_hpc(project_root, image_override)` composes
a full apptainer session ON THE LOGIN NODE and returns the `apptainer exec …`
argv the sbatch generator bakes into the script. The core theorem this file
pins:

    the sbatch argv == ApptainerAdapter().render_argv(spec)     (parity)
    and NO element of it is the bare `botainer` CLI              (never-botainer)

i.e. the compute-node container runs ONLY the agent entrypoint (which IS in
the .sif) — never `botainer start --in-container` on a binary that isn't
there (the FATAL this restructure fixes). No Slurm / no apptainer needed:
this is a render-only test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from botainer.adapters.apptainer import ApptainerAdapter
from botainer.core import composition, identity
from botainer.core import config as config_module
from botainer.plugins import builtin as plugin_builtin
from botainer.state import dir as state_dir

_FAKE_OAUTH = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-test-token-not-real",
        "refreshToken": "sk-ant-ort01-test-refresh-not-real",
        "expiresAt": 9999999999999,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }
}
_FAKE_CLAUDE_JSON = {
    "oauthAccount": {"emailAddress": "test@example.com", "subscriptionType": "max"}
}


@pytest.fixture
def installed_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()
    return state


def _make_shared_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    cfg_path = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    enabled = [
        p for p in data.get("plugins_enabled", []) if not p.startswith("agent-claude")
    ]
    enabled.insert(0, "agent-claude-shared")
    data["plugins_enabled"] = enabled
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
    return proj


def _seed_shared_credential(state_root: Path) -> None:
    shared_dir = state_root / "shared-auth" / "agent-claude"
    shared_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(shared_dir, 0o700)
    creds = shared_dir / ".credentials.json"
    creds.write_text(json.dumps(_FAKE_OAUTH))
    os.chmod(creds, 0o600)  # the shared hook refuses a non-0600 credential
    cj = shared_dir / ".claude.json"
    cj.write_text(json.dumps(_FAKE_CLAUDE_JSON))
    os.chmod(cj, 0o600)


def _fake_sif(tmp_path: Path) -> str:
    sif = tmp_path / "botainer-agent-claude.sif"
    sif.write_bytes(b"")  # image_override only requires an existing file
    return str(sif)


def _compose_hpc(tmp_path: Path, state_root: Path):
    _seed_shared_credential(state_root)
    proj = _make_shared_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    sif = _fake_sif(tmp_path)
    spec, argv = composition.compose_agent_exec_for_hpc(proj, image_override=sif)
    return spec, argv, sif


# ───────────────────────── the two theorems ─────────────────────────


def test_hpc_argv_equals_adapter_render(installed_state: Path, tmp_path: Path) -> None:
    """PARITY: the argv baked into sbatch is EXACTLY the direct apptainer
    adapter's render of the same spec. A single source for the §4 cage."""
    spec, argv, _sif = _compose_hpc(tmp_path, installed_state)
    assert argv == ApptainerAdapter().render_argv(spec)


def test_hpc_argv_never_runs_botainer(installed_state: Path, tmp_path: Path) -> None:
    """NEVER-BOTAINER: the compute-node container execs the agent, not the
    botainer CLI. No `botainer`, no `--in-container` anywhere in the argv."""
    _spec, argv, sif = _compose_hpc(tmp_path, installed_state)
    assert argv[:2] == ["apptainer", "exec"]
    assert "botainer" not in argv, argv
    assert "--in-container" not in argv, argv
    # The element after the image begins the in-container exec line — it must
    # be the agent entrypoint (or the module trampoline `bash`), never botainer.
    img_idx = argv.index(sif)
    after = argv[img_idx + 1:]
    assert after, "no entrypoint after the image"
    assert after[0] != "botainer"
    # basename of the eventual exec target is an agent entrypoint, not botainer
    joined = " ".join(after)
    assert "botainer" not in joined, after


def test_hpc_argv_has_section4_hardening(installed_state: Path, tmp_path: Path) -> None:
    """§4 SOLID: the compute-node cage keeps caps=0 + no-new-privs +
    cleanenv + containall — now single-sourced from the adapter."""
    _spec, argv, _sif = _compose_hpc(tmp_path, installed_state)
    for flag in ("--containall", "--cleanenv", "--no-privs"):
        assert flag in argv, f"{flag} missing from HPC cage argv"
    # --drop-caps all is a two-token pair
    assert "--drop-caps" in argv
    assert argv[argv.index("--drop-caps") + 1] == "all"


def test_hpc_argv_binds_credentials_and_workspace(
    installed_state: Path, tmp_path: Path
) -> None:
    """The pre_session credential wiring + the project workspace both route
    onto the OUTER argv (they are spec binds the adapter renders) — the thing
    the old minimal-bind path could not do for plugin contributions."""
    _spec, argv, _sif = _compose_hpc(tmp_path, installed_state)
    joined = " ".join(argv)
    assert "/workspace" in joined
    # shared-auth credential bind contributed by agent-claude-shared pre_session
    assert "/shared-auth/agent-claude" in joined
    assert "/home/agent/.claude" in joined


def test_hpc_argv_has_no_state_umbrella_bind(
    installed_state: Path, tmp_path: Path
) -> None:
    """NARROWING (closes the Codex Priority-A HIGH): the old path bound the
    whole `state/<uuid>` subtree RW into the container, exposing every other
    session's credentials. The adapter binds only the narrow /packages,
    /scratch, session files + profile dir. No bind source is exactly the
    per-project state root, and no BOTAINER_STATE_ROOT env leaks in."""
    spec, argv, _sif = _compose_hpc(tmp_path, installed_state)
    # Assert no --bind source equals the state/<uuid> dir (the old umbrella).
    state_root = installed_state
    per_project = state_root / "state" / spec.project_uuid
    for tok in argv:
        if tok.startswith("--bind="):
            src = tok[len("--bind="):].split(":", 1)[0]
            assert Path(src) != per_project, (
                f"umbrella state bind present: {tok}"
            )
    # The inner-launcher-only env vars must not be on the argv.
    assert "BOTAINER_STATE_ROOT" not in " ".join(argv)
    assert "MY_BOTAINER" not in " ".join(argv)


def _spec_with_bind(tmp_path: Path, *, source: str, mode):
    from botainer.core.spec import (
        AgentRendering, Bind, BindMode, MountPlan, NetworkMode, NetworkSpec,
        Provenance, SessionSpec,
    )
    extra = Bind(
        source=source, target="/x/thing", mode=mode, provenance=Provenance.PLUGIN,
        provenance_detail="test", agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_EXTRA_BIND",
    )
    # ws source is a fixed shared-style path (NOT under tmp_path, which pytest
    # roots at /tmp — a node-local prefix that would otherwise always warn).
    ws = Bind(
        source="/scratch/gpfs/ws", target="/workspace", mode=BindMode.RW,
        provenance=Provenance.CORE, provenance_detail="ws",
        agent_rendering=AgentRendering.SHOWN, self_test="SELFTEST_WORKSPACE_RW",
    )
    return SessionSpec(
        session_id="s", project_uuid="u", project_root="/scratch/gpfs/ws",
        state_dir=str(tmp_path / "state"), runtime="apptainer",
        image="/x.sif", mount_plan=MountPlan(binds=(ws, extra)),
        network=NetworkSpec(mode=NetworkMode.INTERNET),
    )


def test_refuse_cross_node_binds_socket_refused_file_warns(tmp_path, capsys) -> None:
    """review MEDIUM-2 + the socket contract: a UNIX_SOCKET bind is REFUSED
    (unreachable cross-node rendezvous); a regular bind whose SOURCE is on a
    node-local filesystem (/tmp) is WARNED (best-effort, not refused — a site may
    share /tmp, and legit /apps binds must not be false-refused)."""
    from botainer.core import composition
    from botainer.core.refusal import Refused
    from botainer.core.spec import BindMode

    with pytest.raises(Refused):
        composition._refuse_cross_node_binds(
            _spec_with_bind(tmp_path, source=str(tmp_path / "s.sock"),
                            mode=BindMode.UNIX_SOCKET)
        )
    # node-local file source → warning, no raise.
    composition._refuse_cross_node_binds(
        _spec_with_bind(tmp_path, source="/tmp/thing", mode=BindMode.RO)
    )
    assert "node-local filesystem" in capsys.readouterr().err
    # a shared-FS source (e.g. a GPFS/home path, NOT under a node-local prefix)
    # → no warning. (NB: pytest's tmp_path is itself under /tmp, so use a fixed
    # absolute shared-style path here.)
    composition._refuse_cross_node_binds(
        _spec_with_bind(tmp_path, source="/scratch/gpfs/proj/thing",
                        mode=BindMode.RO)
    )
    assert "node-local filesystem" not in capsys.readouterr().err
