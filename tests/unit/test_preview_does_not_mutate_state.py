"""A preview command must not delete host state. (#227)

`compose_session` is the shared front end for `start` AND for `inspect`,
`dry-run`, `access` and `selftest`. It resets the null-bind masking dir — an
`rmtree` of everything under `data/<uuid>/null-bind-anchor/` — and did so for
every caller. So `botainer inspect`, whose whole promise is to show what WOULD
happen without doing it, recursively deleted host state as a side effect.

The reset itself is right for a launch: the anchor is an empty masking dir bound
over `/workspace/.botainer/`, its invariant is "empty", and a prior crash leaves
mask-writes behind that would otherwise be visible to the agent. What was wrong
is that it ran for callers that launch nothing.

The default is False so that FORGETTING is safe: a new caller that does not
think about it gets the non-mutating behaviour, and a launcher that forgets
fails loudly on the existing refusal rather than silently deleting.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from botainer.core.composition import compose_session


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "bot"))
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    uid = str(uuid.uuid4())
    (proj / ".botainer" / "project-id").write_text(uid)
    (proj / ".botainer" / "config.yaml").write_text(
        "version: config-v1\nagent: claude\nruntime: docker\n"
        "image: ubuntu:24.04\nnetwork:\n  mode: internet\n"
        "plugins_enabled: [agent-claude, git]\n")
    return proj, tmp_path / "bot" / "state" / uid / "data" / "null-bind-anchor"


def _plant(anchor: Path) -> Path:
    anchor.mkdir(parents=True, exist_ok=True)
    victim = anchor / "leftover"
    victim.mkdir(exist_ok=True)
    (victim / "someone's-file.txt").write_text("do not delete me")
    return victim


def test_a_preview_compose_deletes_nothing(project) -> None:
    proj, anchor = project
    victim = _plant(anchor)
    compose_session(proj, runtime_choice="docker", identity_accept=True)
    assert victim.exists(), (
        "compose_session deleted host state with reset_null_anchor unset — "
        "this is the path `inspect`, `dry-run` and `access` take")


def test_a_LAUNCH_compose_still_resets_the_masking_dir(project) -> None:
    """The reset must survive the fix; it is a real requirement of launching.

    Without this, the fix would be satisfied by deleting the behaviour, and a
    prior crash's mask-writes would become visible to the agent.
    """
    proj, anchor = project
    victim = _plant(anchor)
    compose_session(proj, runtime_choice="docker", identity_accept=True,
                    reset_null_anchor=True)
    assert not victim.exists(), (
        "reset_null_anchor=True did not clear the masking dir; a launch would "
        "expose a prior session's writes to the agent")


def test_the_unsafe_direction_requires_an_explicit_argument() -> None:
    """Structural: the default must be the non-destructive one.

    Asserted on the signature rather than behaviour, because the point is that
    a caller who never thinks about this gets safety by omission.
    """
    import inspect as _inspect
    default = _inspect.signature(compose_session).parameters["reset_null_anchor"].default
    assert default is False, (
        f"reset_null_anchor defaults to {default!r}; a caller that forgets it "
        f"would delete host state")
