"""#54: the job mailbox + botainer-job CLI are wired into compose when
job_profiles are declared (and NOT otherwise)."""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import composition, identity
from botainer.core import config as config_module
from botainer.hpc import jobs
from botainer.state import dir as state_dir


def _project(tmp_path: Path, with_profiles: bool) -> Path:
    from tests.conftest import append_image_to_config

    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    append_image_to_config(proj)
    if with_profiles:
        cp = proj / ".botainer" / "config.yaml"
        cp.write_text(
            cp.read_text()
            + "\njob_profiles:\n  quick: {description: devel, partition: devel, "
            "time: '01:00:00', cpus: 1, memory: 4G, gpus: 0, max_concurrent: 2}\n"
        )
    return proj


def _compose(proj: Path):
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    return composition.compose_session(proj, runtime_choice="mock", identity_accept=False)


def test_jobs_wired_when_profiles_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    spec = _compose(_project(tmp_path, with_profiles=True))
    modes = {b.target: b.mode.value for b in spec.mount_plan.binds}
    assert modes.get("/jobs/in") == "rw"
    assert modes.get("/jobs/out") == "ro"
    assert modes.get("/usr/local/bin/botainer-job") == "ro"
    # run/ is host-private — never bound.
    assert not any("/jobs/run" in t for t in modes)
    # profiles.json is host-written so `botainer-job profiles` works in-container.
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    uid = identity.read_project_id(Path(spec.project_root))
    mb = jobs.mailbox_for(paths, uid)
    manifest = mb.out_dir / "profiles.json"
    assert manifest.exists()
    assert "quick" in manifest.read_text()
    # AGENT_HINTS tells the agent it can dispatch (so it stops guessing).
    from botainer.inspect import agent_hints
    hints = agent_hints.render(spec)
    assert "botainer-job" in hints and "Running compute jobs" in hints


def test_jobs_not_wired_without_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    spec = _compose(_project(tmp_path, with_profiles=False))
    targets = {b.target for b in spec.mount_plan.binds}
    assert "/jobs/in" not in targets
    assert "/jobs/out" not in targets
    assert "/usr/local/bin/botainer-job" not in targets
    from botainer.inspect import agent_hints
    assert "Running compute jobs" not in agent_hints.render(spec)
