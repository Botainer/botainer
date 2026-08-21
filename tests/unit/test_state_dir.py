"""Tests for state-dir layout and MY_BOTAINER handling."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from botainer.state import dir as state_dir


def test_my_botainer_env_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom-state"
    monkeypatch.setenv("MY_BOTAINER", str(custom))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    assert paths.root == custom
    assert custom.exists()


def test_state_dir_mode_is_700(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    mode = stat.S_IMODE(paths.root.stat().st_mode)
    assert mode == 0o700


def test_my_botainer_validation_rejects_bad_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_BOTAINER", "/tmp/bad name with space")
    with pytest.raises(ValueError):
        state_dir.ensure_user_state_dir(create_if_missing=True)


def test_for_project_returns_expected_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    uid = "11111111-1111-1111-1111-111111111111"
    proj = paths.for_project(uid)
    assert proj.base == paths.state_dir / uid
    assert proj.meta_path == paths.state_dir / uid / "meta.json"
    assert proj.protected_hashes_path == paths.state_dir / uid / "protected.hashes"


def test_write_default_policy_creates_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    wrote = state_dir.write_default_policy(paths.root, allow_tiers=["first-party"], force=False)
    assert wrote
    assert paths.policy_path.exists()
    # Idempotent without force.
    wrote2 = state_dir.write_default_policy(paths.root, allow_tiers=["first-party"], force=False)
    assert not wrote2


@pytest.mark.parametrize("runtime", ["docker", "apptainer"])
def test_fresh_install_does_not_refuse_first_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime: str
) -> None:
    """Re-audit round 3 (#2, CRITICAL onboarding): the documented zero-to-running
    path (setup → init → start) must NOT hard-refuse on the very first launch.
    The bug: write_default_policy wrote a user-policy network CEILING of `none`
    while write_initial_config writes a project network.mode of `internet`, so
    intersect()'s effective ceiling is `none` and compose refuses `internet >
    none`. Exercise setup + init together (the combo no test covered) on BOTH
    runtimes and assert the project mode is within the effective ceiling.
    """
    from botainer.core import config as config_module
    from botainer.core import policy as policy_module

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    # setup: write the default USER policy.
    state_dir.write_default_policy(paths.root, allow_tiers=["first-party"], force=True)
    # init: write the project config the way `botainer init` does.
    project_root = tmp_path / "proj"
    config_module.write_initial_config(
        project_root, agent="claude", force=True, runtime=runtime
    )
    project_mode = config_module.load_config(project_root).network.mode

    user_policy = policy_module.load_user_policy()
    # No site policy on a fresh laptop/cluster → effective ceiling is user policy.
    effective = policy_module.intersect(policy_module.SitePolicy(), user_policy)
    ceiling = effective.network.default_mode

    rank = {"none": 0, "endpoint-ip-allowlist": 1, "internet": 2}
    assert rank[project_mode] <= rank[ceiling], (
        f"[{runtime}] fresh-install break: init wrote project "
        f"network.mode={project_mode!r} which EXCEEDS the default user-policy "
        f"ceiling network.default_mode={ceiling!r}; first `botainer start` "
        f"would hard-refuse."
    )


def test_path_history_dedup_and_truncation() -> None:
    """Per v0.0.13 schema upgrade: path_history is a list of records
    `{path, first_seen, last_seen, host}`. Re-adding an existing (host, path)
    pair bumps `last_seen` but doesn't duplicate the entry."""
    meta: dict[str, object] = {}
    for i in range(20):
        meta = state_dir.append_path_history(meta, f"/p/{i}")
    hist = meta["path_history"]
    assert isinstance(hist, list)
    assert len(hist) == 8  # _PATH_HISTORY_KEEP
    # Records are dicts now; extract path of first record.
    first_record = hist[0]
    assert isinstance(first_record, dict)
    first_path = first_record["path"]
    # Re-adding the same (host, path) tuple shouldn't grow the list.
    meta = state_dir.append_path_history(meta, first_path)
    hist2 = meta["path_history"]
    assert len(hist2) == 8
    # The re-added entry's last_seen got bumped to "now", so it's the most recent now.
    assert hist2[-1]["path"] == first_path
