"""AC7 validator-parity audit (wf_d09262f2-b33).

CONFIRMED HIGH: project_uuid is interpolated UNQUOTED into the generated
sbatch script (`# project_uuid: ...`, `#SBATCH --job-name=...`,
`#SBATCH --output=.../<uuid>/...`). A tampered, git-shareable
`.botainer/project-id` containing a newline injected `#SBATCH` directives
and shell lines that run as the user on the login/compute node — and the
sbatch path NEVER ran identity._validate_uuid. These pin the chokepoints
that close it: a SessionSpec.project_uuid field_validator (type-level) +
the standalone SubmissionPlan.__post_init__ mirror (HPC path).
"""

from __future__ import annotations

import pytest

from botainer.core.spec import (
    MountPlan,
    NetworkMode,
    NetworkSpec,
    SessionSpec,
    validate_project_uuid,
)

_INJECTION = "aaaa\n#SBATCH --mail-user=attacker@evil\nrm -rf $HOME # "


def _spec(project_uuid: str) -> SessionSpec:
    return SessionSpec(
        session_id="abcdef0123456789",
        project_uuid=project_uuid,
        project_root="/p",
        state_dir="/s",
        runtime="docker",
        image="ubuntu:24.04",
        mount_plan=MountPlan(binds=()),
        network=NetworkSpec(mode=NetworkMode.NONE),
    )


@pytest.mark.parametrize(
    "good",
    [
        "11111111-1111-1111-1111-111111111111",  # canonical
        "u",                                      # short placeholder (tests use these)
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "",                                       # empty (not-yet-set; benign at sink)
    ],
)
def test_legit_project_uuids_accepted(good: str) -> None:
    assert validate_project_uuid(good) == good
    assert _spec(good).project_uuid == good


@pytest.mark.parametrize(
    "bad",
    [
        _INJECTION,            # the exploit: newline → sbatch/shell injection
        "x\nFROM evil",        # newline
        "x\r\ninjected",       # CR
        "x\x00nul",            # NUL
        "x\ttab",              # tab
    ],
)
def test_injection_project_uuids_refused_by_helper(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_project_uuid(bad)


@pytest.mark.parametrize("bad", [_INJECTION, "x\nrm -rf /", "u\x00", "a\tb"])
def test_injection_project_uuid_cannot_construct_sessionspec(bad: str) -> None:
    """Type-level backstop: no SessionSpec can carry a project_uuid with a
    sbatch/shell-injection char, regardless of how it was built."""
    import pydantic

    with pytest.raises((ValueError, pydantic.ValidationError)):
        _spec(bad)
