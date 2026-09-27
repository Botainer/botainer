"""Regression test: apptainer image resolution returns an absolute
path to a .sif file, not a docker tag.

Passing a Docker tag as the Apptainer image makes the runtime treat it
as a path relative to the current directory and fail container creation.
Resolution must select the installed .sif file instead.

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
        "image. Apptainer interprets a Docker tag as a relative path under CWD."
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




# ──────────────────────────────────────────────────────────────────────────────
# THE THIRD BRANCH — and why it DISCLOSES rather than refuses.
#
# `_resolve_session_image` had three ways to produce an apptainer image path and
# verification was written into two of them, one at a time, months apart. The
# third — a top-level `image:` in the project config — is the one
# `GETTING_STARTED-HPC.md:488` documents and `examples/hpc-slurm.yaml:18` ships,
# so the unverified branch was the DOCUMENTED branch.
#
# Measured before this change, on a real editable install with a .sif replaced
# out-of-band: `hpc submit --dry-run` refused `[image-invalid]` (it routes
# through image_override), while `start --preflight` — this project's stated
# pre-push gate, "exit 0 is the gate" — reported CLEAN and `start` proceeded.
#
# WHY NOT JUST REFUSE HERE TOO. Adding a refusal to the documented launch route
# breaks anyone whose `image:` points at a .sif botainer did not build, and that
# is a product decision with a real cost, recorded as an open ask to the
# maintainer (refuse / warn / disclose-only). What does NOT need anyone's
# permission is ending the SILENCE. So: mismatch on this branch is stated, in the
# same words the refusal would use, and the launch proceeds.
#
# The structure is the durable part. Every return from the resolver carries its
# SOURCE, one dict says what each source means, and an unregistered source fails
# closed — so the maintainer's answer is one word, and a fourth branch is covered
# the day it lands.
# ──────────────────────────────────────────────────────────────────────────────


def _genuine_sif_with_marker(installed_state: Path, content: bytes = b"genuine-sif-bytes"):
    import hashlib
    images = installed_state / "images"
    images.mkdir(parents=True, exist_ok=True)
    sif = images / "botainer-agent-claude.sif"
    sif.write_bytes(content)
    _record_apptainer_marker(
        installed_state, "agent-claude", sif, hashlib.sha256(content).hexdigest())
    return sif


def _point_config_image_at(proj: Path, target: Path) -> None:
    import yaml
    cfg_path = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["image"] = str(target)          # the documented HPC config, verbatim
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_a_top_level_image_that_was_swapped_is_STATED_not_swallowed(
    installed_state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE DEFECT: this said nothing at all. Not "it should have refused".

    The path in the guide IS `$MY_BOTAINER/images/botainer-agent-claude.sif`,
    which is the file the marker was recorded against — so a baseline exists and
    botainer KNEW the file had changed. It resolved it and ran it without a word.
    """
    from botainer.core import config as cfgmod

    proj = _make_project(tmp_path, "agent-claude", "claude")
    sif = _genuine_sif_with_marker(installed_state)
    _point_config_image_at(proj, sif)
    cfg = cfgmod.load_config(proj)
    assert cfg.image == str(sif), "precondition: the cfg.image branch is taken"

    # Control: the genuine image says nothing. A warning on every launch is the
    # scenery this project has a rule against.
    assert composition._resolve_session_image(cfg, runtime="apptainer") == str(sif)
    assert "WARNING" not in capsys.readouterr().err

    sif.write_bytes(b"TAMPERED-sif-bytes")
    assert composition._resolve_session_image(cfg, runtime="apptainer") == str(sif), (
        "this branch must still LAUNCH — whether it should refuse is the "
        "maintainer's open decision, and this test must not quietly settle it")
    err = capsys.readouterr().err
    assert "does NOT match" in err, f"the swap was silent again:\n{err}"
    assert "image forget" in err, f"no way out was offered:\n{err}"
    assert "NOT verified" in err, (
        f"it must not read as a passed check:\n{err}")


def test_the_other_two_branches_still_REFUSE(
    installed_state: Path, tmp_path: Path
) -> None:
    """The disclosure must not have leaked into the branches that refuse today.

    A single-exit refactor makes exactly this mistake easy: one policy applied
    everywhere. `hpc submit`'s chokepoint and the plugin's own .sif refused
    before and must still refuse, or this change is a silent relaxation of two
    shipped guarantees.
    """
    from botainer.core import config as cfgmod

    proj = _make_project(tmp_path, "agent-claude", "claude")
    sif = _genuine_sif_with_marker(installed_state)
    cfg = cfgmod.load_config(proj)
    sif.write_bytes(b"TAMPERED-sif-bytes")

    with pytest.raises(Refused) as exc:                      # override branch
        composition._resolve_session_image(
            cfg, runtime="apptainer", image_override=str(sif))
    assert "Refusing" in str(exc.value)

    with pytest.raises(Refused) as exc:                      # plugin-sif branch
        composition.compose_session(
            proj, runtime_choice="apptainer", identity_accept=False)
    assert "Refusing" in str(exc.value)


def test_the_disclosure_reaches_a_REAL_compose_not_just_the_resolver(
    installed_state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through the real caller, because `--preflight` is what reported CLEAN.

    `start --preflight` composes a session and exits 0. It still exits 0 — and
    now it cannot do so in silence. Asserting on the resolver alone would not
    have told me whether `compose_session` reaches this branch at all, and
    "verify through the real caller" is a rule this repo learned expensively.
    """
    proj = _make_project(tmp_path, "agent-claude", "claude")
    sif = _genuine_sif_with_marker(installed_state)
    _point_config_image_at(proj, sif)
    sif.write_bytes(b"TAMPERED-sif-bytes")

    spec = composition.compose_session(
        proj, runtime_choice="apptainer", identity_accept=False)
    assert spec.image == str(sif), "compose must still succeed on this branch"
    assert "does NOT match" in capsys.readouterr().err


def test_a_source_NOBODY_HAS_WRITTEN_YET_fails_closed(
    installed_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE STRUCTURAL GUARD, and the reason this is a refactor not a third call.

    A future branch returns a path and a source string nobody registered. The
    policy dict has no entry, so it gets the STRICT answer — a new resolution
    path cannot be silently exempt, and its author does not have to know this
    check exists.

    This test is what dies if verification moves back inside the branches: the
    three behavioural tests above would all still pass.
    """
    from botainer.core import config as cfgmod

    proj = _make_project(tmp_path, "agent-claude", "claude")
    sif = _genuine_sif_with_marker(installed_state)
    cfg = cfgmod.load_config(proj)

    elsewhere = tmp_path / "somewhere-else" / "hand-built.sif"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    elsewhere.write_bytes(b"a-different-sif-entirely")
    monkeypatch.setattr(
        composition, "_resolve_session_image_unverified",
        lambda *a, **k: (str(elsewhere), "a-branch-invented-next-year"))

    with pytest.raises(Refused) as exc:
        composition._resolve_session_image(cfg, runtime="apptainer")
    assert "sha256" in str(exc.value).lower(), str(exc.value)


def test_every_source_the_resolver_can_return_is_a_source_the_policy_KNOWS(
    installed_state: Path, tmp_path: Path
) -> None:
    """Fail-closed is the backstop, not the plan.

    An unregistered source refuses, which is safe but arrives as a surprise
    refusal for a user. So the sources the resolver actually returns are
    enumerated from the source text and checked against the dict: a new branch
    that forgets to declare its policy fails HERE, at commit time, instead of on
    someone's cluster.
    """
    import inspect as _inspect
    import re
    src = _inspect.getsource(composition)
    body = src.split("def _resolve_session_image_unverified(", 1)[1]
    body = body.split("\ndef ", 1)[0]
    returned = set(re.findall(r'return [^\n]*, "([a-z0-9-]+)"', body))
    assert len(returned) >= 5, (
        f"only found {returned} — the extraction stopped matching the code, so "
        f"this test is no longer checking what it claims")

    apptainer_sources = {"override", "config-image", "plugin-sif"}
    assert apptainer_sources <= returned, (
        f"a source this test names is gone from the resolver: "
        f"{sorted(apptainer_sources - returned)}")
    undeclared = apptainer_sources - set(composition._ENFORCE_SIF_PROVENANCE)
    assert not undeclared, (
        f"these sources can produce an apptainer .sif and have no entry in "
        f"_ENFORCE_SIF_PROVENANCE: {sorted(undeclared)}. They will fail closed, "
        f"which is safe and is not the intent — declare them.")


def test_the_chokepoint_leaves_DOCKER_alone(
    installed_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE CONTROL for the refactor, and it is a capability fact, not a detail.

    Docker has NO image-identity check (docs/CAPABILITY-SURFACE.md §4bh says
    so). A wrapper that hashed everything would start refusing docker tags —
    which cannot be hashed at all — so "always verify" must mean "always verify
    APPTAINER". Without this, the change could pass every test above and break
    every docker user.
    """
    from botainer.core import config as cfgmod

    proj = _make_project(tmp_path, "agent-claude", "claude")
    _genuine_sif_with_marker(installed_state)
    cfg = cfgmod.load_config(proj)

    monkeypatch.setattr(
        composition, "_resolve_session_image_unverified",
        lambda *a, **k: ("test-runtime/agent:0.1", "plugin-lock-tag"))
    assert composition._resolve_session_image(
        cfg, runtime="docker") == "test-runtime/agent:0.1"


def test_an_apptainer_SANDBOX_DIR_is_named_as_uncheckable_on_both_policies(
    installed_state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory has no sha256, so the honest answer says THAT.

    Before the single exit this depended on which branch you arrived through:
    `image_override` hashed it and refused with `[Errno 21] Is a directory`
    dressed up as "the .sif is unreadable"; `cfg.image` proceeded unverified.
    Neither told the user what was actually wrong.

    Only when a marker EXISTS. A sandbox on an install that never recorded one
    keeps working, unverified — the same fail-open-on-no-marker semantics the
    rest of this file pins.
    """
    from botainer.core import config as cfgmod

    proj = _make_project(tmp_path, "agent-claude", "claude")
    sandbox = tmp_path / "sandbox-dir"
    sandbox.mkdir()
    _point_config_image_at(proj, sandbox)
    cfg = cfgmod.load_config(proj)

    # No marker yet -> unverifiable, and nothing claims otherwise.
    assert composition._resolve_session_image(cfg, runtime="apptainer") == str(sandbox)
    assert "SANDBOX" not in capsys.readouterr().err

    _genuine_sif_with_marker(installed_state)
    assert composition._resolve_session_image(cfg, runtime="apptainer") == str(sandbox)
    err = capsys.readouterr().err
    assert "SANDBOX DIRECTORY" in err, err
    assert "image forget" in err, err

    # The same fact, on a branch whose policy is to refuse.
    with pytest.raises(Refused) as exc:
        composition._resolve_session_image(
            cfg, runtime="apptainer", image_override=str(sandbox))
    assert "SANDBOX DIRECTORY" in str(exc.value), str(exc.value)
