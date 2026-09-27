"""Both commands that build a .sif must record what they built.

THE DEFECT. Two commands produce the same artefact:

    botainer image build --runtime apptainer    recorded an
                                                `apptainer:sha256:<hex>:<path>`
                                                marker in installed.lock
    botainer hpc build                          printed "✓ built …" and stopped

`composition._verify_apptainer_sif_provenance` exists to refuse a .sif whose
sha256 no longer matches what was built. Its first branch is "no marker →
nothing to verify → proceed". So for every image built by `hpc build` it was
not weak — it was INERT, taking the fail-open branch every time.

WHO THAT ACTUALLY AFFECTED — stated precisely, because the first version of
this docstring overstated it and a refuting review said so. BOTH commands are
documented cluster routes: `tools/pkg/install-hpc.sh` executes `image build
--runtime apptainer`, while the getting-started guide's §4A and the build-
failure advice in `cli/hpc.py` both say `botainer hpc build`. So the gap was
not "the cluster had no check"; it was "which of two documented commands you
followed decided whether you had one" — silently, with no way to tell.

WHY THESE TESTS DRIVE THE CLI COMMAND rather than the recorder. A test calling
`record_apptainer_sif` directly would have passed on the broken code too — the
recorder was never the broken part; the missing CALL was. Verifying through the
real caller is the rule this project adopted after a component driven directly
gave three wrong answers in a week.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from botainer.core import composition
from botainer.core.refusal import Refused
from botainer.plugins import provenance as prov

#: Just over `hpc.py`'s 1 MiB floor. `hpc build` refuses a smaller file as
#: "apptainer exited 0 but produced nothing", so a tiny fixture would never
#: reach the recording step this file is about — it would test the size guard
#: instead, and pass for the wrong reason.
_PLAUSIBLE_SIF = b"PRETEND SIF PAYLOAD" * 60_000


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A state root with one installed plugin, as a real install has."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    from botainer.state import dir as state_dir
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    lock = paths.installed_lock_path
    lock.parent.mkdir(parents=True, exist_ok=True)
    prov.append_lock(lock, prov.ProvenanceEntry(
        name="agent-claude", version="1.2.3", source="file://bundled",
        tree_sha="sha256:aaaa", image_digest=None,
        installed_at=prov.now_iso(), tier="first-party"))
    return paths


def _run_hpc_build(monkeypatch, state, payload=_PLAUSIBLE_SIF):
    """Drive the REAL `botainer hpc build`, faking only apptainer itself."""
    from botainer.cli import hpc as hpc_mod

    plugin_dir = state.root / "plugins" / "agent-claude"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "agent-claude.def").write_text("Bootstrap: docker\n")
    fake = types.SimpleNamespace(name="agent-claude", plugin_dir=plugin_dir)

    from botainer.plugins import lifecycle as lifecycle_module
    monkeypatch.setattr(lifecycle_module, "list_installed", lambda: [fake])
    monkeypatch.setattr(hpc_mod.shutil, "which", lambda n: "/usr/bin/apptainer")
    monkeypatch.setattr(hpc_mod.profile_module, "active_profile", lambda: None)

    sif_path = state.apptainer_sif_path("agent-claude")

    def fake_build(cmd, env=None, cwd=None):
        # What a successful `apptainer build` leaves behind: the .sif.
        Path(cmd[-2]).parent.mkdir(parents=True, exist_ok=True)
        Path(cmd[-2]).write_bytes(payload)
        return 0

    monkeypatch.setattr(hpc_mod.subprocess, "call", fake_build)
    hpc_mod.build.callback(plugin_name="agent-claude", force=True)
    return sif_path


def test_hpc_build_records_a_marker_the_verifier_can_use(state, monkeypatch):
    """THE FIX. Before it, this entry's image_digest stayed None forever."""
    sif_path = _run_hpc_build(monkeypatch, state)

    entry = [e for e in prov.read_lock(state.installed_lock_path)
             if e.name == "agent-claude"][0]

    assert entry.image_digest, (
        "`hpc build` completed and recorded no image digest — the .sif "
        "provenance verifier has no baseline and stays inert on every cluster")
    assert prov.parse_apptainer_marker(entry.image_digest) == \
        prov.sha256_file(sif_path), (
        f"the recorded marker does not carry the .sif's actual sha256: "
        f"{entry.image_digest!r}")


def test_a_sif_swapped_after_hpc_build_is_REFUSED(state, monkeypatch):
    """The property the marker exists FOR, end to end through both halves.

    This is the test that would have failed before the fix: no marker meant
    `_verify_apptainer_sif_provenance` returned quietly and the swapped image
    was accepted.
    """
    sif_path = _run_hpc_build(monkeypatch, state)
    composition._verify_apptainer_sif_provenance("agent-claude", sif_path)

    sif_path.write_bytes(b"REPLACED OUT OF BAND" * 60_000)

    with pytest.raises(Refused) as exc:
        composition._verify_apptainer_sif_provenance("agent-claude", sif_path)
    assert "does NOT match" in str(exc.value), str(exc.value)


def test_hpc_build_preserves_the_rest_of_the_install_entry(state, monkeypatch):
    """Recording a digest must AMEND the entry, not flatten it.

    The recorder does a read-modify-WRITE of the whole lock. A version that
    rebuilt entries from defaults would silently drop the install's version,
    source and tier — which `plugin verify` and the tier ceiling both read.
    """
    _run_hpc_build(monkeypatch, state)

    entry = [e for e in prov.read_lock(state.installed_lock_path)
             if e.name == "agent-claude"][0]

    # Assert the REWRITE happened first. Without this line the test passes on
    # code that never records anything — the untouched entry trivially still
    # has its original fields. Caught by mutating the fix away and watching
    # this test stay green.
    assert entry.image_digest, "no rewrite happened; the rest is vacuous"
    assert (entry.version, entry.source, entry.tier) == \
        ("1.2.3", "file://bundled", "first-party"), entry


def test_the_lock_stays_world_readable_and_not_writable(state, monkeypatch):
    """The rewrite goes through `write_secure` (tasks #264/#265).

    A hand-rolled `write_text` would pass every other test here while dropping
    the atomic rename and the create-time mode — which is exactly why the
    recorder was MOVED rather than reimplemented for the second caller.

    THE CHMOD BELOW IS LOAD-BEARING. `append_lock` happens to create the file
    0o644 under a normal umask, so asserting 0o644 after the build passed even
    with the recording mutated away — a vacuous test of a real property.
    Starting from 0o600 means only an actual `write_secure` rewrite can produce
    the expected mode.
    """
    state.installed_lock_path.chmod(0o600)

    _run_hpc_build(monkeypatch, state)

    mode = state.installed_lock_path.stat().st_mode & 0o777
    assert mode == 0o644, (
        f"installed.lock mode is {oct(mode)}, expected 0o644 — the lock was "
        f"not rewritten through write_secure")


def test_no_marker_still_means_fail_open(state):
    """The documented behaviour, pinned so a future change is deliberate.

    The lock is user-co-writable, so it is not a tamper-proof root of trust;
    the check raises the bar against accidental swaps, not against someone who
    already has write access to the state dir. If that ever becomes fail-CLOSED
    it must be a decision, not a side effect.
    """
    sif = state.root / "images" / "unrecorded.sif"
    sif.parent.mkdir(parents=True, exist_ok=True)
    sif.write_bytes(_PLAUSIBLE_SIF)

    composition._verify_apptainer_sif_provenance("agent-claude", sif)


def _run_image_build_apptainer(monkeypatch, state, payload=_PLAUSIBLE_SIF):
    """Drive the REAL `botainer image build --runtime apptainer` build step."""
    from botainer.cli import image as image_mod

    plugin_dir = state.root / "plugins" / "agent-claude"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "agent-claude.def").write_text("Bootstrap: docker\n")
    fake = types.SimpleNamespace(name="agent-claude", plugin_dir=plugin_dir)

    monkeypatch.setattr(image_mod.shutil, "which",
                        lambda n: "/usr/bin/apptainer")

    def fake_build(cmd, env=None, cwd=None):
        Path(cmd[-2]).parent.mkdir(parents=True, exist_ok=True)
        Path(cmd[-2]).write_bytes(payload)
        return 0

    monkeypatch.setattr(image_mod.subprocess, "call", fake_build)
    image_mod._build_one_apptainer(fake, no_cache=True)
    return state.apptainer_sif_path("agent-claude")


def test_image_build_ALSO_records_a_usable_marker(state, monkeypatch):
    """The OTHER builder, driven for real.

    THIS TEST EXISTS BECAUSE THE FILE WITHOUT IT WAS MIS-TITLED. A refuting
    review mutated `image build`'s recording call away and all seven tests
    here stayed green: a file called "both builders" was exercising one of
    them, and `_build_one_apptainer` had no coverage at all — before or after
    the change — despite being what the HPC installer script actually runs.
    """
    sif_path = _run_image_build_apptainer(monkeypatch, state)

    entry = [e for e in prov.read_lock(state.installed_lock_path)
             if e.name == "agent-claude"][0]

    assert entry.image_digest, (
        "`image build --runtime apptainer` recorded no image digest")
    assert prov.parse_apptainer_marker(entry.image_digest) == \
        prov.sha256_file(sif_path), entry.image_digest


def test_both_builders_record_the_SAME_marker_for_the_same_sif(
        state, monkeypatch):
    """Anti-drift, by observing what each command WRITES.

    The previous version of this test asserted
    `hpc_mod.prov_module.record_apptainer_sif is
     image_mod.prov_module.record_apptainer_sif`
    which is `f is f` — both names bind the same cached module object, so it
    passed no matter what either call site did, and the refuting review
    demonstrated exactly that. Comparing the RECORDED VALUES is the property:
    it fails if either builder stops recording, records a different key, or
    formats the marker differently.
    """
    from_hpc = _run_hpc_build(monkeypatch, state)
    hpc_marker = [e for e in prov.read_lock(state.installed_lock_path)
                  if e.name == "agent-claude"][0].image_digest

    _run_image_build_apptainer(monkeypatch, state)
    image_marker = [e for e in prov.read_lock(state.installed_lock_path)
                    if e.name == "agent-claude"][0].image_digest

    assert hpc_marker == image_marker, (
        f"the two builders recorded different markers for the same .sif at "
        f"{from_hpc}:\n  hpc build:   {hpc_marker!r}\n"
        f"  image build: {image_marker!r}")


def test_image_py_no_longer_carries_its_own_copy(state):
    """The recorder was MOVED, so the old private names must be gone.

    A re-duplication would pass every behavioural test here on the day it was
    written and drift afterwards — which is the whole history of this defect.
    """
    from botainer.cli import image as image_mod

    for gone in ("_sha256_file", "_record_image_digest",
                 "_record_image_digest_locked"):
        assert not hasattr(image_mod, gone), (
            f"cli/image.py still defines its own {gone} — a second copy of the "
            f"recorder is back, and the two builders can drift again")


def test_the_marker_format_has_one_owner(state):
    """Builder and parser are inverses, so neither can be changed alone.

    `composition.py` used to slice `marker[len("apptainer:sha256:"):]` against
    its own string literal while `image.py` built the marker with a separate
    f-string. Nothing connected them.
    """
    marker = prov.apptainer_marker(Path("/x/y/botainer-agent-claude.sif"), "beef" * 16)

    assert prov.parse_apptainer_marker(marker) == "beef" * 16
    assert prov.parse_apptainer_marker("sha256:deadbeef") is None, (
        "a bare docker image id was read as an apptainer marker")
    assert prov.parse_apptainer_marker(None) is None


def test_a_MALFORMED_marker_still_refuses(state, monkeypatch):
    """Selection must not silently skip a marker it cannot parse.

    A refuting review caught this as a side effect of the move: routing the
    ENTRY SELECTION through `parse_apptainer_marker` meant a malformed marker
    (`apptainer:sha256::/path`, empty hex) parsed to a falsy value, was skipped
    as "not a marker", and the verifier proceeded — where the previous
    `startswith(...)` selection kept it and failed the comparison.

    Turning a refusal into a silent proceed is the worst direction for a fix to
    move, so selection and parsing are now separate questions.
    """
    sif_path = _run_hpc_build(monkeypatch, state)

    lock = state.installed_lock_path
    lock.write_text(lock.read_text().replace(
        prov.parse_apptainer_marker(
            [e for e in prov.read_lock(lock)
             if e.name == "agent-claude"][0].image_digest), ""))

    with pytest.raises(Refused):
        composition._verify_apptainer_sif_provenance("agent-claude", sif_path)


def test_forget_lets_a_DELIBERATE_replacement_through(state, monkeypatch):
    """The remedy the refusal names must exist and work.

    THE WORKFLOW THIS PROTECTS, from the HPC guide: build the .sif on a
    workstation and copy it to a cluster whose login nodes cannot run
    `apptainer build`. Recording a marker made that copied file refuse — and
    the refusal's advice was "remove the stale installed.lock entry", which had
    no command behind it. A remedy the product does not provide is the defect.
    """
    sif_path = _run_hpc_build(monkeypatch, state)
    sif_path.write_bytes(b"BUILT ELSEWHERE AND COPIED IN" * 50_000)

    with pytest.raises(Refused) as exc:
        composition._verify_apptainer_sif_provenance("agent-claude", sif_path)
    assert "botainer image forget agent-claude" in str(exc.value), (
        f"the refusal does not name the command that resolves it: {exc.value}")

    assert prov.forget_image_digest("agent-claude") is True

    composition._verify_apptainer_sif_provenance("agent-claude", sif_path)


def test_forget_says_so_when_there_is_nothing_to_forget(state):
    """And does NOT invent a lock entry while saying it.

    `record_image_digest`'s not-found branch deliberately ADDS a minimal entry;
    forgetting must not, or "clear what I never recorded" would create the very
    row it was asked to remove.
    """
    before = prov.read_lock(state.installed_lock_path)

    assert prov.forget_image_digest("agent-never-installed") is False

    after = prov.read_lock(state.installed_lock_path)
    assert [e.name for e in after] == [e.name for e in before], after


def test_every_writer_stamps_installed_at_in_ONE_shape(tmp_path, monkeypatch):
    """Two formats in one lock file, and neither sorts with the other.

    `append_lock` and both installers stamp via `now_iso()` —
    `2026-09-12T13:31:50Z`. ONE branch of `record_image_digest_locked` (the
    "plugin was not recorded" fallback) called `datetime.now(...).isoformat()`
    instead — `2026-09-12T13:31:50.645956+00:00`. Both are valid ISO-8601 and
    nothing compared them, which is why it survived; but '.' (0x2E) sorts below
    'Z' (0x5A), so a lexical sort puts a microsecond-bearing stamp BEFORE a
    whole-second one from the same moment.

    Driven through the real writer on a lock that does NOT already contain the
    plugin, because that is the only branch that had the second format.
    """
    import json
    import re

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    from botainer.plugins import provenance as prov_mod
    from botainer.state import dir as state_dir

    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    lock = paths.installed_lock_path
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")                      # no entry for this plugin

    prov_mod.record_image_digest("agent-nobody-recorded-me", "sha256:abc")

    rows = [json.loads(ln) for ln in lock.read_text().splitlines() if ln.strip()]
    stamps = [r["installed_at"] for r in rows if r["name"].startswith("agent-")]
    assert stamps, f"the fallback wrote no entry: {rows}"
    for s in stamps:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", s), (
            f"installed_at {s!r} is not the one shape `now_iso()` produces; two "
            f"formats in one lock file do not sort together")


def test_now_iso_is_the_only_timestamp_FORMATTER_in_the_module():
    """The structural half: one formatter, so there cannot be a second shape.

    The behavioural test above covers the branch that WAS wrong. This covers the
    branch nobody has written yet — a new `datetime.now(...)` in this module is
    how the first divergence happened, and the fix is only durable if adding a
    second one fails.
    """
    import inspect as _inspect

    from botainer.plugins import provenance as prov_mod

    src = _inspect.getsource(prov_mod)
    calls = src.count("datetime.datetime.now(")
    assert calls == 1, (
        f"{calls} calls to datetime.datetime.now() in provenance.py; exactly one "
        f"is allowed and it belongs inside now_iso(). A second call site means a "
        f"second timestamp shape in the same lock file.")
