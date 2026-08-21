"""AC7 image-injection fix (hole-hunt, wf_c7506f3f-6c4).

The adversarial hunter confirmed a CRITICAL bug: `image: "--privileged"`
in config.yaml was accepted unvalidated and rendered into `docker run`
with NO `--` separator before the image positional, so docker parsed it
as a FLAG — yielding a privileged container that defeats --cap-drop ALL
+ --security-opt no-new-privileges. SessionSpec.image was a bare str, so
`SessionSpec(image="--privileged")` was ACCEPTED.

These pin the fix: a validator at the SessionSpec type level (every
construction path) + the standalone helper.
"""

from __future__ import annotations

import pytest

from botainer.core.spec import (
    MountPlan,
    NetworkMode,
    NetworkSpec,
    SessionSpec,
    validate_image_reference,
)


def _spec(image: str) -> SessionSpec:
    return SessionSpec(
        session_id="abcdef0123456789",
        project_uuid="u",
        project_root="/p",
        state_dir="/s",
        runtime="docker",
        image=image,
        mount_plan=MountPlan(binds=()),
        network=NetworkSpec(mode=NetworkMode.NONE),
    )


@pytest.mark.parametrize(
    "good",
    [
        "ubuntu:24.04",
        "botainer/agent-claude:0.1@sha256:" + "a" * 64,
        "test-runtime/agent:0.1",
        "/home/user/.botainer/images/botainer-agent-claude.sif",  # absolute .sif
        "registry.example.com:5000/team/img:tag",
    ],
)
def test_legit_images_accepted(good: str) -> None:
    assert validate_image_reference(good) == good
    assert _spec(good).image == good  # SessionSpec construction succeeds


@pytest.mark.parametrize(
    "bad",
    [
        "--privileged",                 # the exploit
        "-v=/etc:/host:ro",
        "--security-opt=seccomp=unconfined",
        "--entrypoint=/bin/sh",
        "ubuntu:24.04 --privileged",    # whitespace → second token is a flag
        "img\nFROM evil",               # newline
        "img\twith\ttab",
        "img\x00nul",
        "",                             # empty
    ],
)
def test_hostile_images_refused_by_helper(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_image_reference(bad)


@pytest.mark.parametrize("bad", ["--privileged", "-v=/etc:/host", "x y", ""])
def test_hostile_image_cannot_construct_sessionspec(bad: str) -> None:
    """The type-level backstop: no SessionSpec can EVER hold a
    flag/shell-hostile image, so no adapter render path can emit one —
    regardless of how the spec was built (compose, plugin, test, fuzz).
    This is the exact construction the hole-hunter exploited."""
    import pydantic

    with pytest.raises((ValueError, pydantic.ValidationError)):
        _spec(bad)


def test_docker_adapter_never_sees_leading_dash_image() -> None:
    """Belt-and-suspenders: even the adapter render is unreachable with a
    hostile image because construction fails first. Confirms the defense
    sits BEFORE render_argv (where the missing `--` separator lives)."""
    import pydantic

    with pytest.raises((ValueError, pydantic.ValidationError)):
        # Would have rendered `docker run ... --privileged` (flag injection).
        _spec("--privileged")
