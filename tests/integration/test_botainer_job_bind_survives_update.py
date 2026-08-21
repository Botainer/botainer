"""Updating botainer must not break a session that is already running.

User,, mid-session on Grace: "codex also seems to say that
botainer-jobs isn't there, something about a failed mount. oh, maybe the bind
died when the version updated..."

Exactly right, and the mechanism is worth writing down because it will recur
anywhere a single FILE is bind-mounted:

    Bind(source=<plugins>/hpc-launcher/agent_helper/botainer-job,
         target=/usr/local/bin/botainer-job)

A file bind pins an INODE at mount time. Updating botainer — `git pull`, `rsync`
without `--inplace`, or any editor that writes a temp file and renames it over
the target — REPLACES that file with a new inode. The running container's mount
still refers to the old, now-unlinked one, so `botainer-job` disappears from a
session that was working a minute earlier and nothing explains why.

Same shape as EXTERNAL-FACTS EF-2 (`rename()` replaces a symlink rather than
following it): replace-in-place destroys identity-pinned references. The lesson
generalises to "do not pin an identity you do not control the lifetime of".

THE FIX IS STRUCTURAL, not a warning: compose copies the helper into the
session's own scratch dir and binds THAT. The session owns the file it runs, so
the plugin tree can be updated, moved or deleted mid-session without reaching
into it. Per CLAUDE.md, prefer making the bad state impossible over detecting
it — there is nothing here to detect any more.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from botainer.core.composition import compose_session

_REPO = Path(__file__).resolve().parents[2]


def _project(tmp_path: Path) -> Path:
    """A project with jobs enabled (job_profiles is TOP-LEVEL, not under plugins:)."""
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(str(uuid.uuid4()))
    (proj / ".botainer" / "config.yaml").write_text(
        "version: config-v1\n"
        "agent: claude\n"
        "runtime: docker\n"
        "image: ubuntu:24.04\n"
        "network:\n  mode: internet\n"
        "job_profiles:\n"
        "  quick:\n"
        "    description: \"test profile\"\n"
        "    partition: day\n"
        "    time: \"01:00:00\"\n"
        "    cpus: 1\n"
    )
    return proj


def _botjob_bind(spec):
    return next((b for b in spec.mount_plan.binds
                 if b.target == "/usr/local/bin/botainer-job"), None)


@pytest.fixture()
def composed(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "bot"))
    monkeypatch.setenv("BOTAINER_STATE_ROOT", str(tmp_path / "bot"))
    proj = _project(tmp_path)
    spec = compose_session(proj, runtime_choice="docker", identity_accept=True)
    bind = _botjob_bind(spec)
    if bind is None:
        pytest.skip("hpc-launcher not installed in this environment")
    return spec, bind


def test_the_helper_is_bound_from_the_SESSION_not_the_plugin_tree(composed) -> None:
    """THE regression guard.

    Binding the plugin file directly is what let a `git pull` yank the tool out
    of a live session.
    """
    _spec, bind = composed
    source = Path(bind.source)
    assert "plugins" not in source.parts, (
        f"botainer-job is bound straight from the plugin tree ({source}); a "
        f"version update replaces that file and the running session's mount "
        f"goes dangling. Bind a per-session copy instead.")
    assert "sessions" in source.parts, (
        f"expected a per-session copy, got {source}")


def test_the_copy_exists_and_is_executable(composed) -> None:
    """A bind whose source is missing or non-executable fails at launch, which
    would just trade one broken session for another."""
    _spec, bind = composed
    source = Path(bind.source)
    assert source.is_file(), f"bind source does not exist: {source}"
    assert os.access(source, os.X_OK), f"copied helper is not executable: {source}"
    assert source.stat().st_size > 0


def test_replacing_the_plugin_file_does_NOT_touch_the_running_session(
        composed) -> None:
    """The property the whole change exists for.

    Simulates the update: replace the plugin's copy the way git/rsync do —
    unlink and write a NEW inode — and confirm the composed session still points
    at intact content. With the old design the session's source WAS that file,
    so this is precisely what went dangling under the user mid-session.
    """
    from botainer.plugins.lifecycle import list_installed

    hpc = next((i for i in list_installed() if i.name == "hpc-launcher"), None)
    if hpc is None:
        pytest.skip("hpc-launcher not installed")
    plugin_file = Path(hpc.plugin_dir) / "agent_helper" / "botainer-job"
    original = plugin_file.read_bytes()
    original_inode = plugin_file.stat().st_ino

    _spec, bind = composed
    session_copy = Path(bind.source)
    before = session_copy.read_bytes()

    try:
        # Exactly what an update does: new file, new inode, renamed over the old.
        tmp = plugin_file.with_suffix(".new")
        tmp.write_bytes(b"#!/bin/sh\necho REPLACED\n")
        tmp.replace(plugin_file)
        assert plugin_file.stat().st_ino != original_inode, (
            "the simulated update reused the inode; this test would prove "
            "nothing")

        assert session_copy.is_file(), (
            "the running session's helper vanished when the plugin file was "
            "replaced — this is the reported bug")
        assert session_copy.read_bytes() == before, (
            "the running session's helper changed under it")
    finally:
        plugin_file.write_bytes(original)
        plugin_file.chmod(0o755)


def test_two_sessions_each_pin_their_own_copy(tmp_path, monkeypatch) -> None:
    """Sessions started either side of an update must not share one file.

    Before this change both bound the same path, so after an update two live
    sessions could disagree about what `botainer-job` does depending on whether
    the inode survived. Now each is pinned to the version it launched with.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "bot"))
    monkeypatch.setenv("BOTAINER_STATE_ROOT", str(tmp_path / "bot"))
    proj = _project(tmp_path)

    a = _botjob_bind(compose_session(proj, runtime_choice="docker",
                                     identity_accept=True))
    b = _botjob_bind(compose_session(proj, runtime_choice="docker",
                                     identity_accept=True))
    if a is None or b is None:
        pytest.skip("hpc-launcher not installed")
    assert a.source != b.source, (
        "both sessions bound the same file; an update would hit both at once")
    assert Path(a.source).read_bytes() == Path(b.source).read_bytes()
