"""AUDIT (C1): `--preflight`'s forbidden-bind-source list was mostly
DEAD CODE — 5 of its 7 entries could never match.

The list mixed absolute paths with bare names, but the matcher only did
exact-or-under against the WHOLE source path, so a bare name like `.ssh` could
never match `/home/user/.ssh`. CLAUDE.md calls `--preflight` "the gate" to run
before pushing any security-surface change, so silently non-matching entries are
exactly the enforcement theatre this project keeps finding.
"""
from __future__ import annotations

import pytest

from botainer.core.spec import (
    Bind,
    BindMode,
    EnvSpec,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    Provenance,
    SessionSpec,
)
from botainer.inspect import preflight


def _spec_with_source(source: str) -> SessionSpec:
    return SessionSpec(
        session_id="ses-x", project_uuid="u" * 32, project_root="/tmp/p",
        image="t:0.1", runtime="mock", state_dir="/home/t/.botainer/state/u",
        plugins_enabled=(), env=EnvSpec(values={}),
        mount_plan=MountPlan(binds=(Bind(
            source=source, target="/data", mode=BindMode.RO,
            provenance=Provenance.USER, provenance_detail="test"),)),
        network=NetworkSpec(mode=NetworkMode.NONE),
    )


@pytest.mark.parametrize("source", [
    "/var/run/docker.sock",          # was NOT CAUGHT (no '/' boundary after "docker")
    "/home/user/.ssh",               # was NOT CAUGHT (bare name vs absolute path)
    "/home/user/.aws",               # was NOT CAUGHT
    "/home/user/.kube",              # was NOT CAUGHT
    "/Users/someone/.ssh/id_ed25519",
    "/etc/passwd",                   # was caught before, must stay caught
    "/etc/shadow",
])
def test_forbidden_sources_are_actually_caught(source: str) -> None:
    assert preflight.run(_spec_with_source(source)) != 0, (
        f"{source} must be refused by --preflight")


@pytest.mark.parametrize("source", [
    "/home/user/.kube-backup",       # component match must not be a substring match
    "/home/user/project",
    "/data/datasets",
    "/home/user/sshkeys",            # not a `.ssh` component
])
def test_ordinary_sources_are_not_false_positives(source: str) -> None:
    assert preflight.run(_spec_with_source(source)) == 0, (
        f"{source} must NOT be refused")
