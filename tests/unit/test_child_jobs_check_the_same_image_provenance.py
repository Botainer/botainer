"""A dispatched job runs a container too, so it hashes its image too.

WHAT WAS OBSERVED, and it is why this file exists rather than a comment. The
session path learned to compare a `.sif` against the sha256 recorded when
botainer built it. `botainer hpc submit` refused a replaced image with exit 2 and
`doctor --strict` reported an error — and in the same state root,
`botainer hpc dispatcher once` composed a caged child sbatch containing that exact
replaced image, printed nothing, and recorded nothing. The only reason it was not
submitted is that `sbatch` is absent from the box it ran on, which is a fact about
that box and not a check.

The cause was structural: `cli/hpc.py::_resolve_child_image` is a SECOND resolver,
serving the dispatcher and the warm-pool worker, and it never went near
`composition._resolve_session_image`. The fix routes it through the same
`_ENFORCE_SIF_PROVENANCE` policy, keyed the same way — which is what the
single-exit refactor claimed to have achieved while there were still two exits.

WHAT THIS PINS, and both halves matter:

  * the plugin's own `.sif`, replaced out-of-band → the child job is REFUSED,
    exactly as a session is. Silence here is worse than for a session: nobody is
    watching a dispatched job's launch.
  * a top-level `image:` → DISCLOSED and allowed, exactly as a session is,
    because that is the policy the maintainer is deciding and a child job must
    not answer it differently from its parent.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from botainer.core import composition
from botainer.core.refusal import Refused
from botainer.plugins import provenance as prov
from botainer.state import dir as state_dir


class _Cfg:
    """The two fields `_resolve_child_image` reads."""
    def __init__(self, agent="claude", image=None):
        self.agent = agent
        self.image = image


@pytest.fixture
def state(monkeypatch, tmp_path):
    """A scratch state root, with the plugin dir inside it.

    HERMETIC ON PURPOSE, and it was not. `_resolve_child_image` finds the plugin
    dir through `list_installed()`, which in an EDITABLE install returns the
    working CLONE — observed by a refuting review: `agent-claude ->
    <repo>/plugins/agent-claude`, even with MY_BOTAINER pointed at a tmp dir. So
    two of the four candidate paths pointed into the repository rather than into
    the fixture.

    Harmless today (no .sif lives in the repo, and `images/` is searched first),
    which is exactly why it is worth pinning: a test whose candidate set depends
    on the developer's checkout can pass for the wrong reason, and the next
    person to drop a stray file under plugins/ would never connect the two.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)

    plugin_dir = paths.plugins_dir / "agent-claude"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    import types

    from botainer.plugins import lifecycle as _lifecycle
    monkeypatch.setattr(
        _lifecycle, "list_installed",
        lambda: [types.SimpleNamespace(name="agent-claude", plugin_dir=plugin_dir)])
    return paths


def test_the_fixture_ITSELF_does_not_reach_outside_the_tmp_state_root(state):
    """The fixture's own claim, checked — otherwise it is a comment.

    If `list_installed` stops being patched, or starts being consulted somewhere
    this fixture does not cover, the candidate set silently grows to include the
    developer's checkout again.
    """
    from botainer.plugins.lifecycle import list_installed

    for inst in list_installed():
        assert str(inst.plugin_dir).startswith(str(state.root)), (
            f"plugin dir {inst.plugin_dir} is outside the fixture's state root "
            f"{state.root}; the resolver's candidates are not hermetic")


def _sif_with_marker(paths, content=b"GENUINE" * 100) -> Path:
    sif = paths.apptainer_sif_path("agent-claude")
    sif.parent.mkdir(parents=True, exist_ok=True)
    sif.write_bytes(content)
    prov.append_lock(paths.installed_lock_path, prov.ProvenanceEntry(
        name="agent-claude", version="0.1.0", source="image-built-locally",
        tree_sha="sha256:0",
        image_digest=prov.apptainer_marker(sif, hashlib.sha256(content).hexdigest()),
        installed_at="t", tier="first-party"))
    return sif


def test_a_child_job_REFUSES_the_plugin_sif_that_was_replaced(state):
    """THE BYPASS. The dispatcher composed a caged job around it, silently."""
    from botainer.cli import hpc as hpcmod

    sif = _sif_with_marker(state)
    assert hpcmod._resolve_child_image(_Cfg(), state) == str(sif), (
        "control: the genuine image resolves and says nothing")

    sif.write_bytes(b"TAMPERED" * 100)
    with pytest.raises(Refused) as exc:
        hpcmod._resolve_child_image(_Cfg(), state)
    msg = str(exc.value)
    assert "does NOT match" in msg, msg
    assert "image forget" in msg, f"no way out was offered:\n{msg}"


def test_a_child_job_DISCLOSES_a_top_level_image_and_still_runs(
        state, capsys: pytest.CaptureFixture[str]):
    """The other half: the child must not answer the open policy question
    differently from its parent session. Same source, same outcome."""
    from botainer.cli import hpc as hpcmod

    sif = _sif_with_marker(state)
    sif.write_bytes(b"TAMPERED" * 100)

    got = hpcmod._resolve_child_image(_Cfg(image=str(sif)), state)
    assert got == str(sif), "a config-supplied image must still launch"
    err = capsys.readouterr().err
    assert "does NOT match" in err, f"the child path went silent again:\n{err}"
    assert "NOT verified" in err, err


def test_the_child_resolver_reads_the_SAME_policy_as_the_session_resolver(state):
    """Not a second copy of the rule — the same dict, or this drifts again.

    Two resolvers agreeing today by coincidence is what produced the bypass. A
    future edit that gives the child path its own policy table passes both tests
    above and fails this one.
    """
    import inspect as _inspect
    from botainer.cli import hpc as hpcmod

    src = _inspect.getsource(hpcmod._resolve_child_image)
    assert "_ENFORCE_SIF_PROVENANCE" in src, (
        "the child resolver no longer consults the shared policy dict")
    assert composition._ENFORCE_SIF_PROVENANCE["plugin-sif"] is True
    assert composition._ENFORCE_SIF_PROVENANCE["config-image"] is False, (
        "if the maintainer flipped this, the tests above encode the OLD answer "
        "and must be rewritten together with the doctor sentence")


# ──────────────────────────────────────────────────────────────────────────────
# THE OTHER TWO WAYS THE CHILD RESOLVER DIFFERED FROM THE SESSION ONE.
#
# Same function, same review, filed separately because each is its own change.
# Both measured, and the first one is user-visible in the worst way: the SESSION
# launches and every dispatched JOB dies.
# ──────────────────────────────────────────────────────────────────────────────


def _legacy_named_sif(paths) -> Path:
    """`images/<plugin>.sif` — what `botainer hpc build` once wrote (DN-036)."""
    sif = paths.images_dir / "agent-claude.sif"
    sif.parent.mkdir(parents=True, exist_ok=True)
    sif.write_bytes(b"SIF" * 100)
    return sif


def test_the_legacy_sif_name_that_SESSIONS_accept_is_accepted_for_JOBS_too(state):
    """THE DEFECT, and the asymmetry is the whole finding.

    Measured on one state root holding only `images/agent-claude.sif`:

        session resolver   → .../images/agent-claude.sif
        child resolver     → ClickException: child-job image not found

    So on an install built by the older `hpc build`, `botainer start` works and
    every dispatched job fails naming a path the user never chose. The session
    side walks four candidate spellings; this walked one.
    """
    from botainer.cli import hpc as hpcmod
    from botainer.core import composition

    legacy = _legacy_named_sif(state)
    assert not state.apptainer_sif_path("agent-claude").exists(), (
        "precondition: the canonical name must be ABSENT, or this proves nothing")

    session_answer = composition._resolve_apptainer_sif_path("agent-claude")
    assert session_answer == legacy, (
        f"the session resolver changed and no longer finds the legacy name "
        f"({session_answer}); this test's premise is gone")

    assert hpcmod._resolve_child_image(_Cfg(), state) == str(legacy), (
        "a dispatched job still cannot find the image its own session runs")


def test_the_refusal_NAMES_both_spellings_it_looked_for(state):
    """A refusal that names one path sends the reader looking for one file.

    The original said "child-job image not found: <canonical>", which is a true
    statement that hides the question the user needs answered — whether the file
    they DO have counts.
    """
    from botainer.cli import hpc as hpcmod

    with pytest.raises(Exception) as exc:        # click.ClickException
        hpcmod._resolve_child_image(_Cfg(), state)
    msg = str(exc.value)
    assert "botainer-agent-claude.sif" in msg, msg
    assert "agent-claude.sif" in msg and "legacy" in msg, (
        f"the refusal names only one spelling:\n{msg}")
    assert "botainer hpc build" in msg, msg


def test_a_flag_like_image_in_config_is_refused_HERE(state):
    """`image: --privileged` must not reach an sbatch script.

    Every other resolution path applied `validate_image_reference`; this one did
    not. `botainer/hpc/jobs.py` catches it further down, so this was
    defence-in-depth rather than a live hole — and depending on one distant
    caller is precisely the arrangement that produced the missing provenance
    check this file's first tests are about.
    """
    from botainer.cli import hpc as hpcmod

    _sif_with_marker(state)
    with pytest.raises(Exception) as exc:
        hpcmod._resolve_child_image(_Cfg(image="--privileged"), state)
    msg = str(exc.value)
    assert "starts with '-'" in msg, msg
    assert "image:" in msg, f"the message does not say WHERE the value came from:\n{msg}"


@pytest.mark.parametrize("bad", ["with space", "two\nlines", "tab\there"])
def test_whitespace_in_a_config_image_is_refused_too(state, bad):
    """The other half of the same validator: argv tokenisation, not just flags."""
    from botainer.cli import hpc as hpcmod

    _sif_with_marker(state)
    with pytest.raises(Exception) as exc:
        hpcmod._resolve_child_image(_Cfg(image=bad), state)
    assert "forbidden character" in str(exc.value), str(exc.value)


def test_an_ordinary_absolute_sif_path_in_config_still_WORKS(state):
    """THE CONTROL. A validator that refuses everything passes every test above.

    `image:` pointing at an absolute .sif is the documented HPC configuration; if
    this fails, the fix broke the feature it was hardening.
    """
    from botainer.cli import hpc as hpcmod

    sif = _sif_with_marker(state)          # genuine, marker matches
    assert hpcmod._resolve_child_image(_Cfg(image=str(sif)), state) == str(sif)


# ──────────────────────────────────────────────────────────────────────────────
# AN IMAGE FAULT MUST NOT FREEZE THE REST OF THE CYCLE.
#
# Image resolution happens once per dispatcher cycle, BEFORE the inbox is read.
# A refuting review measured what that costs when it fails: with an unresolvable
# `image:`, the cycle raised ahead of `poll_running`, the `*.cancel` sweep and the
# pool-status publish — so a FINISHED job still read `queued` to the agent for
# ever, and a cancel was silently dropped, both for a reason unrelated to either.
# An auto-started dispatcher sends stdout AND stderr to /dev/null.
#
# The previous behaviour, before the provenance work, was a per-request
# `{"state": "refused", "reason": "internal error handling request"}` — vague but
# VISIBLE. So the fix is not "raise later", it is: refuse the requests that need
# an image, with the real reason, and run everything that does not.
# ──────────────────────────────────────────────────────────────────────────────


def test_an_unresolvable_image_REFUSES_the_requests_and_says_why(state, tmp_path):
    """The refusal reaches the agent's own out/ directory, not just stderr."""
    from botainer.hpc import dispatcher as disp
    from botainer.hpc import jobs as _jobs

    mb = _jobs.ensure_mailbox(state, "11111111-2222-3333-4444-555555555555")
    (mb.in_dir / "abcdef0123456789.json").write_text('{"argv": ["true"]}')

    results = disp.refuse_all_pending(mb, "cannot resolve the job image: nope")

    assert [r.state for r in results] == ["refused"], results
    rec = json.loads((mb.out_dir / "abcdef0123456789.status.json").read_text())
    assert rec["state"] == "refused"
    assert "cannot resolve the job image" in rec["reason"], rec
    assert "nope" in rec["reason"], (
        f"the real cause was dropped, which is the vague "
        f"'internal error handling request' this replaces: {rec}")


def test_it_does_NOT_touch_a_request_already_answered(state):
    """Idempotence, and it is not cosmetic.

    The dispatcher runs every 15 s. Re-refusing a request that already has a
    terminal status would overwrite a real outcome — a job that RAN — with
    "cannot resolve the image".
    """
    from botainer.hpc import dispatcher as disp
    from botainer.hpc import jobs as _jobs

    mb = _jobs.ensure_mailbox(state, "11111111-2222-3333-4444-555555555556")
    (mb.in_dir / "abcdef0123456789.json").write_text('{"argv": ["true"]}')
    (mb.out_dir / "abcdef0123456789.status.json").write_text(
        '{"id": "abcdef0123456789", "state": "completed", "exit_code": 0}')

    assert disp.refuse_all_pending(mb, "whatever") == []
    rec = json.loads((mb.out_dir / "abcdef0123456789.status.json").read_text())
    assert rec["state"] == "completed", (
        f"a finished job was overwritten with an image error: {rec}")


def test_it_ignores_a_pool_control_request_and_a_junk_filename(state):
    """Same selection rules as the real submit path, or it answers for requests
    that are not jobs — and `pool_control` ids are not 16-hex, so a status
    written for one lands under a different name and confuses `pool status`."""
    from botainer.hpc import dispatcher as disp
    from botainer.hpc import jobs as _jobs

    mb = _jobs.ensure_mailbox(state, "11111111-2222-3333-4444-555555555557")
    (mb.in_dir / "abcdef0123456789.json").write_text(
        '{"kind": "pool_control", "op": "start"}')
    (mb.in_dir / "not-a-job-id.json").write_text('{"argv": ["true"]}')
    (mb.in_dir / ".hidden.json").write_text('{"argv": ["true"]}')
    (mb.in_dir / "notjson.txt").write_text("x")

    assert disp.refuse_all_pending(mb, "reason") == []
    assert list(mb.out_dir.glob("*.status.json")) == []


def test_an_image_that_is_not_a_usable_PATH_falls_through_like_a_session(state):
    """THE REFUTER'S CASE 1, and my first fix did not cover it.

    Composition validates the charset AND THEN requires, under apptainer, an
    absolute path that is a file or a directory — anything else drops `image:`
    and falls through. Porting only the charset half gave one config three
    answers, measured on the shipped example's own line with the `USER`
    placeholder unsubstituted:

        inspect                  the plugin .sif
        hpc submit --dry-run     refused [config-missing]
        hpc dispatcher once      a caged sbatch around the nonexistent path

    A docker-style tag was worse: a RELATIVE operand, which apptainer resolves
    against the job's CWD — the agent-writable project root.

    Mutation-checked: removing the fall-through leaves the other tests in this
    file green, which is why this one exists.
    """
    from botainer.cli import hpc as hpcmod

    sif = _sif_with_marker(state)
    for unusable in ("/home/USER/.botainer/images/botainer-agent-claude.sif",
                     "botainer/agent-claude:0.1",
                     "relative/path.sif",
                     str(state.images_dir / "does-not-exist.sif")):
        got = hpcmod._resolve_child_image(_Cfg(image=unusable), state)
        assert got == str(sif), (
            f"image: {unusable!r} reached a dispatched job; it must fall "
            f"through to the plugin .sif exactly as `start` does. Got {got!r}")


def test_the_sif_is_hashed_ONCE_per_process_not_once_per_dispatcher_cycle(
        state, monkeypatch):
    """The dispatcher runs every 15 s and this used to re-hash the whole image.

    Measured by a refuting review: +0.9 s per cycle for a 512 MiB .sif at
    1151 MiB/s page-cached, so ~4 s of CPU and 4 GiB of reads every 15 s for a
    real 3-5 GiB agent image — on a LOGIN NODE. And if one cycle exceeds the
    dispatcher claim's staleness window, the claim expires while the dispatcher
    is alive and a second one can start, which is the double-submit the claim
    exists to prevent. `doctor` gates the identical hash behind `--strict`.

    Counting calls rather than timing them: a timing assertion on a shared box is
    a flake, and the call count is the property.
    """
    from botainer.cli import hpc as hpcmod
    from botainer.core import composition
    from botainer.plugins import provenance as prov_mod

    sif = _sif_with_marker(state)
    composition._SIF_VERIFIED.clear()

    calls: list[str] = []
    real = prov_mod.sha256_file
    monkeypatch.setattr(prov_mod, "sha256_file",
                        lambda p: (calls.append(str(p)), real(p))[1])

    for _ in range(5):                      # five dispatcher cycles
        assert hpcmod._resolve_child_image(_Cfg(), state) == str(sif)
    assert len(calls) == 1, (
        f"the .sif was hashed {len(calls)} times across five cycles; on a real "
        f"image that is {len(calls)} × ~4 s of login-node CPU")

    # A REPLACED file must still be caught: the cache key carries mtime + size.
    sif.write_bytes(b"TAMPERED" * 200)
    with pytest.raises(Refused):
        hpcmod._resolve_child_image(_Cfg(), state)
    assert len(calls) == 2, (
        "the cache answered for a file that had changed — mtime/size must be "
        "part of the key")
