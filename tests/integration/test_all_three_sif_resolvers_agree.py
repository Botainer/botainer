"""Three surfaces resolve a `.sif`. They must answer the same question alike.

WHAT WAS MEASURED, by a refuting review, on ONE real install — four states, and no
two resolvers agreed:

    on disk                          start          hpc submit      child job
    only images/<plugin>.sif         that file      REFUSED         that file
    only at the RECORDED path        "not built"    that file       REFUSED
    plugins.hpc-launcher override    (n/a)          that file       (n/a)
    nothing                          refused        refused         refused

The middle row is the one this file was written for: `hpc submit` honoured the
recorded path, `start` said "no .sif built" about a file botainer itself had
recorded, and a dispatched job refused and told the user to rebuild a multi-GiB
image.

A PREMISE I FIRST WROTE HERE WAS FALSE, and a refuting review measured it: I said
that state came from "building to /scratch, which this project's storage guidance
encourages". It does not. Neither builder has an `--output`, so no botainer command
can record a .sif outside `<state_root>/images/`, and `docs/STORAGE.md` §5 says
`.sif` files "stay wherever the state root is". The reachable ways to have a
recorded path elsewhere are a hand-edited lock and a MOVED OR COPIED state root.

THE ORDERING FOLLOWS FROM THAT SECOND ONE. The recorded path is consulted LAST,
because a copied state root (`cp -a`, or the documented build-elsewhere-and-scp)
carries a lock naming the ORIGINAL root's file — and with the recorded path first,
a session under the new root silently execs the old root's image while ignoring the
one the user just placed, with `doctor --strict` flipping from ✗ to ✓. Consulting
it last fixes the original defect and shadows nothing.

WHY THIS IS A TEST AND NOT A SHARED FUNCTION. `plugins/hpc-launcher/host_helper/`
is deliberately standalone — zero `botainer` imports, because it runs on a login
node from a rendered script. So the code cannot be shared; only the PRECEDENCE can,
and a parity test is how this repo already holds such a pair together (the
job-output dir has the same arrangement).

THE ONE DIVERGENCE THAT IS DELIBERATE: the launcher also honours
`plugins.hpc-launcher.apptainer_image`, a per-project key for the HPC launcher
specifically. The session path has no equivalent and should not — its equivalent is
the top-level `image:`, which both already honour. The last test pins that this is
the ONLY divergence, so a new one cannot appear quietly.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

from botainer.plugins import provenance as prov
from botainer.state import dir as state_dir

REPO = Path(__file__).resolve().parents[2]
_HELPER = REPO / "plugins/hpc-launcher/host_helper/_common.py"


def _load_helper():
    """Import the standalone helper by path — it is not an installed module.

    REGISTERED IN sys.modules BEFORE exec, and that is not boilerplate: the
    helper defines dataclasses, and `dataclasses` resolves string annotations by
    looking the class's module up in sys.modules. Omit this and the module loads
    to an AttributeError on None.__dict__ — which is how the first version of
    this file failed.
    """
    name = "hpc_launcher_common_resolverparity"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HELPER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def install(monkeypatch, tmp_path):
    """A state root and a project, with nothing built yet."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    paths.images_dir.mkdir(parents=True, exist_ok=True)
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text("agent: claude\n")
    return paths, proj


def _record(paths, sif: Path) -> None:
    """Record the marker `botainer image build` would have written."""
    prov.append_lock(paths.installed_lock_path, prov.ProvenanceEntry(
        name="agent-claude", version="0.1.0", source="image-built-locally",
        tree_sha="sha256:0",
        image_digest=prov.apptainer_marker(
            sif, hashlib.sha256(sif.read_bytes()).hexdigest()),
        installed_at="t", tier="first-party"))


def _launcher_answer(paths, proj) -> str:
    helper = _load_helper()
    return helper._resolve_apptainer_image({}, proj, paths.root, "claude")


def _library_answer(paths) -> str | None:
    found = paths.find_apptainer_sif("agent-claude")
    return str(found) if found is not None else None


def test_a_sif_ONLY_at_the_recorded_path_is_found_by_both(install):
    """THE DEFECT. `hpc submit` found it; nothing else did.

    This is the state you reach by building to /scratch — the recorded path is
    botainer's own record of where it wrote.
    """
    paths, proj = install
    elsewhere = paths.root.parent / "scratch-ish" / "botainer-agent-claude.sif"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    elsewhere.write_bytes(b"BUILT-ELSEWHERE" * 100)
    _record(paths, elsewhere)
    assert not (paths.images_dir / "botainer-agent-claude.sif").exists(), (
        "precondition: the conventional name must be absent")

    assert _library_answer(paths) == str(elsewhere), (
        "the session/child resolver still cannot see the path botainer recorded")
    assert _launcher_answer(paths, proj) == str(elsewhere), (
        "the launcher has honoured this since the H2 fix; if it stopped, the two "
        "have diverged in the other direction")


def test_the_legacy_unprefixed_name_is_found_by_both(install):
    """The other half of DN-036, from the opposite direction.

    `hpc submit` used to REFUSE this one ("not an existing absolute .sif") while
    `start` accepted it. Nothing is recorded here, so both fall to the filename
    candidates.
    """
    paths, proj = install
    legacy = paths.images_dir / "agent-claude.sif"
    legacy.write_bytes(b"LEGACY" * 100)

    assert _library_answer(paths) == str(legacy)
    # THIS USED TO ASSERT THE DIVERGENCE. The launcher had only the PREFIXED name
    # as a candidate and returned it whether or not it existed, so a state root
    # holding just the legacy spelling gave a submitted job a path that was not
    # there — it died at exec on the compute node while `start` ran fine. The
    # launcher now carries the same two filename candidates, so the assertion is
    # agreement rather than a documented gap.
    assert _launcher_answer(paths, proj) == str(legacy), (
        "the launcher still cannot see the spelling `start` runs")


def test_the_conventional_name_is_preferred_over_the_legacy_one(install):
    """Both spellings present: the canonical one wins, on both sides."""
    paths, proj = install
    (paths.images_dir / "agent-claude.sif").write_bytes(b"OLD" * 100)
    canonical = paths.images_dir / "botainer-agent-claude.sif"
    canonical.write_bytes(b"NEW" * 100)

    assert _library_answer(paths) == str(canonical)
    assert _launcher_answer(paths, proj) == str(canonical)


def test_a_RECORDED_path_that_no_longer_EXISTS_does_not_shadow_a_real_image(
        install):
    """The record is a hint, not an oracle.

    If the recorded file was deleted and a conventional one is present, the
    resolver must return the file that exists. Preferring a recorded path
    unconditionally would turn a rebuilt-then-moved install into "no image".
    """
    paths, proj = install
    gone = paths.root.parent / "gone" / "botainer-agent-claude.sif"
    gone.parent.mkdir(parents=True, exist_ok=True)
    gone.write_bytes(b"X" * 10)
    _record(paths, gone)
    gone.unlink()
    canonical = paths.images_dir / "botainer-agent-claude.sif"
    canonical.write_bytes(b"REAL" * 100)

    assert _library_answer(paths) == str(canonical), (
        "a dead recorded path shadowed a .sif that is actually there")


def test_a_BROKEN_lock_file_does_not_break_resolution(install):
    """This runs on every launch. A corrupt lock must not stop a session.

    Three shapes, because each failed differently in the copies this replaced:
    unparseable JSON, a marker with no path half, and a lock that is a directory.
    """
    paths, proj = install
    canonical = paths.images_dir / "botainer-agent-claude.sif"
    canonical.write_bytes(b"REAL" * 100)

    paths.installed_lock_path.write_text("{not json at all\n")
    assert _library_answer(paths) == str(canonical)

    paths.installed_lock_path.write_text(
        '{"name": "agent-claude", "version": "0", "source": "s", '
        '"tree_sha": "t", "image_digest": "apptainer:sha256:abc", '
        '"installed_at": "t", "tier": "first-party"}\n')
    assert _library_answer(paths) == str(canonical)

    paths.installed_lock_path.unlink()
    paths.installed_lock_path.mkdir()
    assert _library_answer(paths) == str(canonical)


def test_nothing_built_means_None_on_both_sides(install):
    """The control. A resolver that always returns something passes every test
    above, and 'not built' has to stay reachable — it is what sends a new user
    to `botainer image build`."""
    paths, proj = install

    assert _library_answer(paths) is None
    # The launcher returns a PATH even when nothing exists (its docstring says
    # so: apptainer rejects it at exec time with a clear error). That is its
    # documented shape, not a disagreement about what is on disk.
    assert not Path(_launcher_answer(paths, proj)).exists()


def test_the_launchers_OWN_sources_are_exercised_and_the_library_lacks_them(install):
    """The deliberate divergence, driven rather than counted.

    THE TEST THIS REPLACES COUNTED `return ` OCCURRENCES IN THE FUNCTION, and a
    refuting review broke it both ways in a minute: adding a comment containing
    the word "return" failed it with zero behaviour change, and adding two new
    launcher-only image sources that shared an existing `return` passed it — the
    exact thing its docstring claimed to prevent. It also never touched
    `find_apptainer_sif`, and every other test in this file passed `{}` as the
    plugin cfg, so the launcher's first two cases were exercised nowhere.

    So drive them. `plugins.hpc-launcher.apptainer_image` wins for the launcher and
    means nothing to the library (that is correct — it is a key for the HPC
    launcher, and the session equivalent is the top-level `image:`, which
    `composition` honours). A top-level `image:` wins for the launcher too, ahead
    of any filename.
    """
    import yaml

    paths, proj = install
    helper = _load_helper()
    conv = paths.images_dir / "botainer-agent-claude.sif"
    conv.write_bytes(b"CONVENTIONAL" * 100)

    # Case 1: the launcher's own plugin key.
    plugin_choice = paths.root.parent / "chosen-by-plugin-cfg.sif"
    plugin_choice.write_bytes(b"X" * 10)
    assert helper._resolve_apptainer_image(
        {"apptainer_image": str(plugin_choice)}, proj, paths.root,
        "claude") == str(plugin_choice)
    assert _library_answer(paths) == str(conv), (
        "the library honoured a key that belongs to the HPC launcher")

    # Case 2: the top-level `image:`, which BOTH honour — the launcher here, and
    # `composition._resolve_session_image` on the session side.
    top_level = paths.root.parent / "top-level-image.sif"
    top_level.write_bytes(b"Y" * 10)
    cfg_path = proj / ".botainer" / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"agent": "claude", "image": str(top_level)}))
    assert helper._resolve_apptainer_image(
        {}, proj, paths.root, "claude") == str(top_level)


def test_a_FIFO_at_a_candidate_path_does_not_HANG_the_resolver(install):
    """A hang is not an exception, and this one had no output at all.

    Measured by a refuting review: a FIFO at the resolved path plus a recorded
    marker made `inspect --runtime apptainer` sit at a 20 s timeout and
    `hpc dispatcher once` at 15 s, both with ZERO bytes printed — because the
    later sha256 read blocks with no writer. Pre-existing (HEAD hangs the same way
    with the FIFO at the conventional name), and this file is where it gets
    closed: `exists()` says yes to a FIFO, `is_file() or is_dir()` does not, and
    the `cfg.image` branches have required file-or-dir for exactly this reason for
    months.

    A hang in the dispatcher is worse than a crash: its output goes to /dev/null,
    so the symptom is a cycle that never completes and never says why.
    """
    import os

    paths, proj = install
    fifo = paths.images_dir / "botainer-agent-claude.sif"
    os.mkfifo(fifo)
    real = paths.images_dir / "agent-claude.sif"
    real.write_bytes(b"A-REAL-SIF" * 100)

    # Must not block, and must not select the FIFO.
    assert _library_answer(paths) == str(real), (
        "the FIFO was selected; the next read of it blocks for ever")


def test_a_socket_or_device_is_not_selected_either(install):
    """The same rule, for the other non-regular files.

    A unix socket is the shape the browser/viewer plugins leave around, and
    /dev/zero is the one the `cfg.image` comment names. Neither is an image.
    """
    import socket

    paths, proj = install
    sock_path = paths.images_dir / "botainer-agent-claude.sif"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        real = paths.images_dir / "agent-claude.sif"
        real.write_bytes(b"A-REAL-SIF" * 100)
        assert _library_answer(paths) == str(real), (
            "a unix socket was selected as a container image")
    finally:
        srv.close()


def test_the_ACTIVE_root_is_never_shadowed_by_a_recorded_path(install):
    """THE REGRESSION THAT DECIDED THE ORDERING, and it needs no hand-editing.

    Copy a state root, or follow the documented "build elsewhere and scp the .sif
    into $MY_BOTAINER/images/": the copy's lock still names the ORIGINAL root's
    file. Measured with the recorded path consulted FIRST — a session silently ran
    the other root's image, `inspect` showed it, and `doctor --strict` went from
    ✗ "does NOT match the digest recorded at build time" to ✓ "sha256 matches".

    THE LAUNCHER WAS LEFT DIVERGENT HERE and no longer is. It preferred the record
    before any filename, so on a copied root a SUBMITTED job ran the other root's
    image while the session ran the new one — the same regression, on the path
    where it is hardest to notice, because nobody watches a compute node's exec
    line. Changing it was deferred once as "a separate decision with its own blast
    radius"; the blast radius turned out to be this test plus two assertions.
    """
    paths, proj = install
    other_root = paths.root.parent / "other-root" / "images"
    other_root.mkdir(parents=True, exist_ok=True)
    theirs = other_root / "botainer-agent-claude.sif"
    theirs.write_bytes(b"THE-OTHER-ROOTS-IMAGE" * 100)
    _record(paths, theirs)                       # what `cp -a` leaves behind
    mine = paths.images_dir / "botainer-agent-claude.sif"
    mine.write_bytes(b"THE-ONE-I-JUST-PLACED" * 100)

    assert _library_answer(paths) == str(mine), (
        "a .sif from ANOTHER state root shadowed the active one — the measured "
        "regression this ordering exists to prevent")
    assert _launcher_answer(paths, proj) == str(mine), (
        "a submitted job would exec ANOTHER state root's image while the session "
        "runs the one the user placed")


def test_the_recorded_path_is_the_LAST_resort_not_the_first(install):
    """The ordering as its own property, both halves.

    With nothing at any conventional name the recorded path is still found, so the
    original defect stays fixed; with a conventional name present it is not
    consulted. Without the second half, "last" is indistinguishable from "present".
    """
    paths, proj = install
    elsewhere = paths.root.parent / "elsewhere" / "botainer-agent-claude.sif"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    elsewhere.write_bytes(b"RECORDED" * 100)
    _record(paths, elsewhere)

    assert _library_answer(paths) == str(elsewhere), "still the fallback"
    assert _launcher_answer(paths, proj) == str(elsewhere), "launcher: same"

    conv = paths.images_dir / "botainer-agent-claude.sif"
    conv.write_bytes(b"CONVENTIONAL" * 100)
    assert _library_answer(paths) == str(conv), (
        "the recorded path is being consulted before the conventional name")
    assert _launcher_answer(paths, proj) == str(conv), (
        "the launcher still consults the record before the conventional name")


def test_a_recorded_path_that_is_GONE_does_not_beat_a_real_image_on_EITHER_side(
        install):
    """Divergence (a), and it is the one with teeth.

    The launcher returned the recorded path with its existence unchecked. Delete
    that file (rebuild elsewhere, prune a scratch dir, move a root) and leave a
    real image behind: `start` ran the real one and a submitted job execed a path
    that no longer existed. The record is a hint, not an oracle.

    THE REAL IMAGE IS AT THE LEGACY NAME HERE, DELIBERATELY. My first version put
    it at the conventional name, and a mutation that ignored existence entirely
    still passed it — because the conventional name is the first candidate, so
    ORDER alone gave the right answer and the existence check was never the thing
    under test. Putting the survivor second makes skipping a dead candidate the
    only way to reach it.
    """
    paths, proj = install
    gone = paths.root.parent / "gone" / "botainer-agent-claude.sif"
    gone.parent.mkdir(parents=True, exist_ok=True)
    gone.write_bytes(b"X" * 10)
    _record(paths, gone)
    gone.unlink()
    real = paths.images_dir / "agent-claude.sif"
    real.write_bytes(b"REAL" * 100)

    assert _library_answer(paths) == str(real)
    assert _launcher_answer(paths, proj) == str(real), (
        "a submitted job would exec a recorded path that is not there, while the "
        "session runs the image that is")

    # And with nothing on disk at all, the answer names the file the user is meant
    # to build — not the dead path out of the lock, which tells them nothing they
    # can act on.
    real.unlink()
    assert _library_answer(paths) is None
    assert _launcher_answer(paths, proj) == str(
        paths.images_dir / "botainer-agent-claude.sif"), (
        "the failure names a recorded path that no longer exists")


def test_a_FIRST_lock_entry_with_no_path_does_not_end_the_search(install):
    """Divergence (b). Two entries for one plugin, the first unusable.

    The library skipped to the second; the launcher returned on the first name
    match and fell through to a filename, so the two resolved different files from
    the same lock. Reachable by hand-editing or by any future writer that appends
    rather than replaces — and "appends rather than replaces" is what
    `append_lock` does.
    """
    paths, proj = install
    real = paths.root.parent / "recorded" / "botainer-agent-claude.sif"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"RECORDED" * 100)
    paths.installed_lock_path.write_text(
        # first: a marker with an EMPTY path half
        '{"name": "agent-claude", "version": "0", "source": "s", "tree_sha": "t",'
        ' "image_digest": "apptainer:sha256:abc:", "installed_at": "t",'
        ' "tier": "first-party"}\n'
        # second: the usable one
        '{"name": "agent-claude", "version": "0", "source": "s", "tree_sha": "t",'
        f' "image_digest": "apptainer:sha256:abc:{real}", "installed_at": "t",'
        ' "tier": "first-party"}\n')

    assert _library_answer(paths) == str(real)
    assert _launcher_answer(paths, proj) == str(real), (
        "the launcher stopped at the first entry and never saw the usable one")


def test_a_NON_SHA256_marker_is_ignored_by_BOTH(install):
    """Divergence (c). `apptainer:md5:<hex>:<path>` — honoured by the launcher,
    ignored by the library.

    The algorithm half is not decoration: it says which digest the provenance
    check compares, so a marker naming a different one is not a marker either side
    understands. The fix is the stricter reading on both, which means the file is
    found by its conventional name or not at all.
    """
    paths, proj = install
    md5ish = paths.root.parent / "md5-recorded" / "botainer-agent-claude.sif"
    md5ish.parent.mkdir(parents=True, exist_ok=True)
    md5ish.write_bytes(b"MD5" * 100)
    paths.installed_lock_path.write_text(
        '{"name": "agent-claude", "version": "0", "source": "s", "tree_sha": "t",'
        f' "image_digest": "apptainer:md5:abc:{md5ish}", "installed_at": "t",'
        ' "tier": "first-party"}\n')

    assert _library_answer(paths) is None, (
        "the library started honouring a digest algorithm it cannot verify")
    assert not Path(_launcher_answer(paths, proj)).exists(), (
        "the launcher still honours a marker the library ignores, so the two "
        "resolve different images from the same lock")


def test_the_ONE_REMAINING_divergence_is_the_plugin_DIRECTORY(install):
    """Pin what still differs, so the docstring claiming near-parity stays honest.

    The library tries the installed plugin directory as well (`start` can locate it
    via `list_installed()`); the standalone helper cannot, without importing
    botainer, which is the thing it must not do. So a `.sif` sitting ONLY in the
    plugin dir is found by `start` and not by a submit. Hand-placement only — no
    builder writes there — but a divergence asserted in a comment and nowhere else
    is how the previous four survived. If this test starts failing because the
    launcher found it, the comment in `_resolve_apptainer_image` needs updating,
    not this file.
    """
    paths, proj = install
    helper = _load_helper()
    plugin_dir = paths.root / "plugins" / "agent-claude"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    in_plugin_dir = plugin_dir / "botainer-agent-claude.sif"
    in_plugin_dir.write_bytes(b"IN-THE-PLUGIN-DIR" * 100)

    assert paths.find_apptainer_sif("agent-claude", plugin_dir) == in_plugin_dir
    assert _launcher_answer(paths, proj) == str(
        paths.images_dir / "botainer-agent-claude.sif"), (
        "the launcher found the plugin dir — good, but the comment saying it "
        "cannot is now false")


def test_a_FIFO_is_not_SELECTED_by_the_launcher(install):
    """SELECTION, not hanging — and the first version of this docstring said the
    wrong one, which the refuting review caught: `stat()` on a FIFO does not
    block, only `open()` does. The library's hang came from the sha256 read that
    FOLLOWS selection; the launcher never reads the file, it bakes the path into an
    sbatch script.

    So the property here is narrower and still worth holding: a FIFO is not a
    container image, and handing one to `apptainer exec` on a compute node fails
    in a place with no one watching. `exists()` says yes to it; `is_file() or
    is_dir()` does not.
    """
    import os

    paths, proj = install
    os.mkfifo(paths.images_dir / "botainer-agent-claude.sif")
    real = paths.images_dir / "agent-claude.sif"
    real.write_bytes(b"A-REAL-SIF" * 100)

    assert _launcher_answer(paths, proj) == str(real), (
        "the launcher selected the FIFO; the next read of it blocks for ever")


def test_the_SESSION_resolver_sees_the_recorded_path_too(install):
    """Through `composition`, because that is the surface that said "not built".

    `_resolve_apptainer_sif_path` carried its own copy of the candidate walk. It
    now delegates, and this fails if someone re-inlines one — which is how the
    copy came to exist in the first place.
    """
    from botainer.core import composition

    paths, proj = install
    elsewhere = paths.root.parent / "scratchish" / "botainer-agent-claude.sif"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    elsewhere.write_bytes(b"RECORDED-ONLY" * 100)
    _record(paths, elsewhere)

    assert composition._resolve_apptainer_sif_path("agent-claude") == elsewhere, (
        "`start` still cannot see an image botainer itself recorded")


def test_the_PREFIXED_agent_name_resolves_the_same_on_both_sides(install):
    """`agent: agent-claude` is a thing users write, and `init` can write it.

    MEASURED DIVERGENCE, by a refuting review: every botainer-side reader
    tolerates the prefixed form (`_agent_plugin_name` and
    `_agent_variant_for_mode` both check `startswith("agent-")`), and the
    standalone launcher helper appends the prefix unconditionally. So
    `hpc submit` resolved `botainer-agent-agent-claude.sif` and refused naming a
    file the user never typed, while `start` launched the real image. Config
    validation ACCEPTED the input, so nothing anywhere said the word "agent-".

    Both sides now strip ONE prefix and keep the short form, so the two resolve
    the same file. `botainer init --agent agent-claude` writes the short form
    too, which is the third place this had to be fixed for the state to stop
    being reachable.
    """
    import yaml

    from botainer.core import config as cfgmod

    paths, proj = install
    sif = paths.images_dir / "botainer-agent-claude.sif"
    sif.write_bytes(b"REAL" * 100)
    (proj / ".botainer" / "config.yaml").write_text(
        yaml.safe_dump({"agent": "agent-claude"}))

    # The botainer side normalises at parse …
    assert cfgmod.load_config(proj).agent == "claude"
    # … and the standalone helper, which never builds a ProjectConfig, agrees.
    helper = _load_helper()
    assert helper._load_agent_name(proj) == "claude"
    assert helper._resolve_apptainer_image({}, proj, paths.root, "claude") == str(sif)


def test_a_DOUBLED_prefix_is_refused_by_name_on_both_sides(install):
    """Two prefixes is not a spelling confusion, and silence would be the bug.

    Stripping repeatedly would accept `agent-agent-agent-claude` and resolve a
    real image for it — the same over-tolerance that produced the divergence
    above. Both sides refuse and both name the short form in the message.
    """
    import pytest as _pytest
    import yaml

    from botainer.core import config as cfgmod

    paths, proj = install
    (proj / ".botainer" / "config.yaml").write_text(
        yaml.safe_dump({"agent": "agent-agent-claude"}))

    with _pytest.raises(Exception) as exc:
        cfgmod.load_config(proj)
    assert "agent: claude" in str(exc.value) or "SHORT name" in str(exc.value), exc.value

    helper = _load_helper()
    with _pytest.raises(ValueError) as verr:
        helper._load_agent_name(proj)
    assert "short agent name" in str(verr.value), verr.value


def test_init_WRITES_the_short_form_even_when_given_the_prefixed_one(tmp_path):
    """The third place, and the one that made the state reachable at all.

    `botainer init --agent agent-claude` used to write `agent: agent-claude`
    verbatim, so botainer produced the config that botainer's two resolvers then
    disagreed about.
    """
    import yaml

    from botainer.core import config as cfgmod

    proj = tmp_path / "p"
    proj.mkdir()
    cfgmod.write_initial_config(proj, agent="agent-claude", force=True)
    data = yaml.safe_load((proj / ".botainer" / "config.yaml").read_text())

    assert data["agent"] == "claude", (
        f"init wrote {data['agent']!r}; botainer adds the prefix itself, and "
        f"writing it back is what let the two resolvers diverge")
    assert any(p.startswith("agent-claude") for p in data.get("plugins_enabled", [])), (
        f"the plugin name must still be the PREFIXED one: {data.get('plugins_enabled')}")
