"""Regression test: apptainer image resolution returns an absolute
path to a .sif file, not a docker tag.

real-host Grace bug: `botainer start` with `runtime=
apptainer` produced `spec.image = 'botainer/agent-claude:0.1'` (a
docker tag) instead of a .sif path. apptainer interpreted that as a
path relative to CWD and tried to read
`<project_root>/botainer/agent-claude:0.1` → "container creation
failed". User-facing symptom: "looking for the actual software or
container under the project folder."

This test pins: when runtime=apptainer, `_resolve_session_image` (and
therefore `compose_session(..., runtime_choice='apptainer')`) MUST
return an absolute path to an existing file, NEVER a docker tag.

Same shape as the umbrella-bind regression test
(test_capability_surface_matches_inventory.py): the principle "for
apptainer, the image is a .sif path" lives in CLAUDE.md "HPC parity"
prose; without a test, drift between the docker and apptainer code
paths recurs silently.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import composition, identity
from botainer.core import config as config_module
from botainer.core.refusal import Refused
from botainer.plugins import builtin as plugin_builtin
from botainer.state import dir as state_dir
from tests.conftest import append_image_to_config


@pytest.fixture
def installed_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()
    return state


def _make_project(tmp_path: Path, plugin_name: str, agent_short: str) -> Path:
    proj = tmp_path / f"proj_{plugin_name}"
    proj.mkdir()
    config_module.write_initial_config(proj, agent=agent_short, force=False)
    append_image_to_config(proj)
    cfg_path = proj / ".botainer" / "config.yaml"
    import yaml
    data = yaml.safe_load(cfg_path.read_text())
    enabled = [
        p for p in data.get("plugins_enabled", [])
        if not p.startswith("agent-")
    ]
    enabled.insert(0, plugin_name)
    data["plugins_enabled"] = enabled
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
    identity.init_project(proj, agent=agent_short, force=True, non_interactive=True)
    return proj


def test_apptainer_returns_absolute_sif_path(
    installed_state: Path, tmp_path: Path
) -> None:
    """The primary regression check: apptainer compose returns the .sif
    path, not the docker tag."""
    proj = _make_project(tmp_path, "agent-claude", "claude")
    # Place a placeholder .sif under <state>/images/.
    images = installed_state / "images"
    images.mkdir(parents=True, exist_ok=True)
    sif = images / "botainer-agent-claude.sif"
    sif.touch()

    spec = composition.compose_session(
        proj, runtime_choice="apptainer", identity_accept=False
    )

    # The image MUST be the absolute .sif path. The bug
    # would have produced a docker tag here.
    assert spec.image == str(sif), (
        f"Expected spec.image to be the absolute .sif path {sif!r}, "
        f"got {spec.image!r}. If this is a docker tag like "
        f"'botainer/agent-claude:0.1', apptainer would interpret it "
        f"as a path relative to CWD and fail."
    )
    # Belt-and-suspenders: it must look like a path, not a tag.
    assert spec.image.startswith("/"), (
        f"spec.image for apptainer must be absolute; got {spec.image!r}"
    )
    assert spec.image.endswith(".sif"), (
        f"spec.image for apptainer must be a .sif file; got {spec.image!r}"
    )


def test_apptainer_accepts_unprefixed_sif_name(
    installed_state: Path, tmp_path: Path
) -> None:
    """The resolver accepts both naming conventions while the codebase
    has drift (internal design note DN-036). Mirrors doctor.py's candidate list."""
    proj = _make_project(tmp_path, "agent-claude", "claude")
    images = installed_state / "images"
    images.mkdir(parents=True, exist_ok=True)
    # Unprefixed name (the older convention from `botainer hpc build`).
    sif = images / "agent-claude.sif"
    sif.touch()

    spec = composition.compose_session(
        proj, runtime_choice="apptainer", identity_accept=False
    )
    assert spec.image == str(sif)


def test_apptainer_refuses_when_no_sif(
    installed_state: Path, tmp_path: Path
) -> None:
    """If no .sif exists, refuse with a clear error pointing at the
    fix command — don't silently fall through to a docker tag."""
    proj = _make_project(tmp_path, "agent-claude", "claude")
    # No .sif created.

    with pytest.raises(Refused) as exc:
        composition.compose_session(
            proj, runtime_choice="apptainer", identity_accept=False
        )
    msg = str(exc.value)
    assert "agent-claude" in msg
    assert "--runtime apptainer" in msg, (
        "Refusal must point at the fix command for users who hit this."
    )


def test_apptainer_honors_cfg_image_when_it_is_a_real_sif_path(
    installed_state: Path, tmp_path: Path
) -> None:
    """If the user sets `image:` to an absolute path to an existing
    .sif file, honor it (don't fall through to the lookup)."""
    proj = _make_project(tmp_path, "agent-claude", "claude")
    # User-supplied SIF in a non-standard location.
    user_sif = tmp_path / "my-custom" / "claude.sif"
    user_sif.parent.mkdir(parents=True, exist_ok=True)
    user_sif.touch()

    cfg_path = proj / ".botainer" / "config.yaml"
    import yaml
    data = yaml.safe_load(cfg_path.read_text())
    data["image"] = str(user_sif)
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))

    spec = composition.compose_session(
        proj, runtime_choice="apptainer", identity_accept=False
    )
    assert spec.image == str(user_sif)


def test_apptainer_ignores_cfg_image_when_it_is_a_docker_tag(
    installed_state: Path, tmp_path: Path
) -> None:
    """If cfg.image is a docker tag (the default after `append_image`),
    apptainer compose must NOT pass it through — it must fall through
    to the .sif lookup, OR refuse if no .sif exists."""
    proj = _make_project(tmp_path, "agent-claude", "claude")
    # Place a .sif so we can verify fallthrough.
    images = installed_state / "images"
    images.mkdir(parents=True, exist_ok=True)
    sif = images / "botainer-agent-claude.sif"
    sif.touch()

    # cfg.image is the docker tag from append_image_to_config
    # (test-runtime/agent:0.1@sha256:aaa...).
    spec = composition.compose_session(
        proj, runtime_choice="apptainer", identity_accept=False
    )
    # Critical: docker tag must NOT have been passed through.
    assert spec.image == str(sif), (
        "Apptainer compose must NOT pass a docker tag through as the "
        "image. The 2026-05-19 Grace bug: apptainer interpreted the "
        "docker tag as a relative path under CWD."
    )


def test_docker_still_returns_docker_tag(
    installed_state: Path, tmp_path: Path
) -> None:
    """Sanity: the runtime-aware change must NOT break the docker
    code path. Docker compose still gets the docker tag."""
    proj = _make_project(tmp_path, "agent-claude", "claude")
    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=False
    )
    # Whatever cfg.image was set to (the docker-shaped TEST_IMAGE_REF
    # from append_image_to_config), docker/mock compose returns it.
    assert spec.image.startswith("test-runtime/")


def _record_apptainer_marker(state: Path, plugin: str, sif: Path, sha_hex: str) -> None:
    """Append an installed.lock entry carrying the apptainer:sha256 marker."""
    from botainer.plugins import provenance as prov
    from botainer.state import dir as _sd
    lock = _sd.ensure_user_state_dir(create_if_missing=True).installed_lock_path
    prov.append_lock(lock, prov.ProvenanceEntry(
        name=plugin, version="0.1.0", source="image-built-locally",
        tree_sha="sha256:0", image_digest=f"apptainer:sha256:{sha_hex}:{sif}",
        installed_at="t", tier="first-party",
    ))


def test_apptainer_sif_provenance_match_succeeds(
    installed_state: Path, tmp_path: Path
) -> None:
    """AUDIT (MEDIUM): when the .sif's sha256 matches the marker
    recorded at build time, compose proceeds."""
    import hashlib
    proj = _make_project(tmp_path, "agent-claude", "claude")
    images = installed_state / "images"
    images.mkdir(parents=True, exist_ok=True)
    sif = images / "botainer-agent-claude.sif"
    sif.write_bytes(b"genuine-sif-bytes")
    _record_apptainer_marker(
        installed_state, "agent-claude", sif,
        hashlib.sha256(b"genuine-sif-bytes").hexdigest(),
    )
    spec = composition.compose_session(proj, runtime_choice="apptainer", identity_accept=False)
    assert spec.image == str(sif)


def test_apptainer_sif_provenance_mismatch_refuses(
    installed_state: Path, tmp_path: Path
) -> None:
    """The core fix: a .sif REPLACED out-of-band (sha256 != recorded marker)
    is refused before exec — apptainer parity with docker digest pinning."""
    proj = _make_project(tmp_path, "agent-claude", "claude")
    images = installed_state / "images"
    images.mkdir(parents=True, exist_ok=True)
    sif = images / "botainer-agent-claude.sif"
    sif.write_bytes(b"TAMPERED-sif-bytes")
    _record_apptainer_marker(installed_state, "agent-claude", sif, "0" * 64)  # wrong hash
    with pytest.raises(Refused) as exc:
        composition.compose_session(proj, runtime_choice="apptainer", identity_accept=False)
    assert "match" in str(exc.value).lower() and "recorded" in str(exc.value).lower()


def test_image_override_path_also_verifies_sif_provenance(
    installed_state: Path, tmp_path: Path
) -> None:
    """SUPPLY-CHAIN audit (H2): the image_override branch returned
    WITHOUT verifying the .sif hash — and plugins/hpc-launcher/host_helper/
    submit.py ALWAYS takes that branch. So `botainer hpc submit`, the product's
    PRIMARY path, never hashed the image it was about to exec, despite the
    hpc-launcher having just parsed that path out of the
    `apptainer:sha256:<hex>:<path>` marker and discarded the hex half.
    """
    import hashlib

    from botainer.core import config as cfgmod

    proj = _make_project(tmp_path, "agent-claude", "claude")
    images = installed_state / "images"
    images.mkdir(parents=True, exist_ok=True)
    sif = images / "botainer-agent-claude.sif"
    sif.write_bytes(b"genuine-sif-bytes")
    _record_apptainer_marker(
        installed_state, "agent-claude", sif,
        hashlib.sha256(b"genuine-sif-bytes").hexdigest(),
    )
    cfg = cfgmod.load_config(proj)

    # matching content -> the override path proceeds
    assert composition._resolve_session_image(
        cfg, runtime="apptainer", image_override=str(sif)) == str(sif)

    # swapped out-of-band -> must refuse on THIS branch too, not just the
    # plugin-lookup branch
    sif.write_bytes(b"TAMPERED-sif-bytes")
    with pytest.raises(composition.Refused) as exc:
        composition._resolve_session_image(
            cfg, runtime="apptainer", image_override=str(sif))
    assert "sha256" in str(exc.value).lower()
