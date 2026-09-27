"""State-root version stamping (#202).

The branch that matters most here — a root written by a build that does not
exist yet — is the one you cannot stage by running the product, so it is tested
against the record directly.
"""
from __future__ import annotations

import json

import pytest

from botainer.cli.doctor import root_version_findings
from botainer.state import root_version
from botainer.state.root_version import LAYOUT_VERSION, RootVersion


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "botainer-root"
    (r / "state").mkdir(parents=True)
    return r


def test_first_record_writes_and_claims_authorship(root):
    res = root_version.record(root, "0.1.0a5", created_now=True)

    assert res.wrote is True
    assert res.previous is None
    got = root_version.read(root)
    assert got.layout_version == LAYOUT_VERSION
    # An empty root is one we are creating, so authorship is genuinely known.
    assert got.created_by == "0.1.0a5"
    assert got.last_used_by == "0.1.0a5"


def test_second_run_of_the_same_build_does_not_write(root):
    root_version.record(root, "0.1.0a5", created_now=True)
    before = root_version.path_for(root).stat().st_mtime_ns

    res = root_version.record(root, "0.1.0a5", created_now=True)

    # Write-on-change is load-bearing: ensure_user_state_dir runs on nearly
    # every command, and a measured production cluster has a hard inode cap on $HOME
    # that has already killed sessions mid-write.
    assert res.wrote is False
    assert res.write_failed is False
    assert root_version.path_for(root).stat().st_mtime_ns == before


def test_upgrade_updates_last_used_but_preserves_created_by(root):
    root_version.record(root, "0.1.0a5", created_now=True)

    res = root_version.record(root, "0.1.0a6", created_now=True)

    assert res.wrote is True
    got = root_version.read(root)
    assert got.created_by == "0.1.0a5", "authorship is history, not the latest run"
    assert got.last_used_by == "0.1.0a6"


def test_a_root_we_did_not_create_does_not_get_fabricated_authorship(root):
    # A root that predates this feature. Claiming we created it would write a
    # provenance record that is simply false, and later code would trust it.
    root_version.record(root, "0.1.0a5", created_now=False)

    got = root_version.read(root)
    assert got.origin_is_known is False
    assert got.created_by != "0.1.0a5"
    assert got.last_used_by == "0.1.0a5", "we did use it, and that much is true"


def test_created_now_is_required_so_it_cannot_be_silently_guessed(root):
    # The first version INFERRED authorship from whether state/ held anything,
    # and got it wrong whenever a pre-#202 build made the root but never ran a
    # session. The fix is only durable if omitting the fact is impossible.
    import inspect

    param = inspect.signature(root_version.record).parameters["created_now"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, (
        "a default would let a future caller omit it and get a guess back"
    )
    with pytest.raises(TypeError):
        root_version.record(root, "0.1.0a5")


def test_authorship_does_not_depend_on_what_state_holds(root):
    # Structure over enumeration: record() no longer looks at the directory, so
    # an unanticipated entry (state/locks/ is a real one, made by the codex
    # login hook) cannot change the answer.
    (root / "state" / "locks").mkdir()
    (root / "state" / "by-name").mkdir()
    (root / "state" / "8a1f0c22-dead-4beef-0000-000000000001").mkdir()

    root_version.record(root, "0.1.0a5", created_now=True)

    assert root_version.read(root).created_by == "0.1.0a5"


def test_older_build_refuses_to_lower_a_newer_root(root):
    # Stamping this back down would erase the only evidence that a newer build
    # has been here. A filter, not a property — see the deletion test below for
    # the bypass, which is pinned deliberately.
    root_version.path_for(root).write_text(json.dumps({
        "layout_version": LAYOUT_VERSION + 7,
        "created_by": "0.9.0",
        "last_used_by": "0.9.0",
    }))

    res = root_version.record(root, "0.1.0a5", created_now=True)

    assert res.root_is_newer is True
    assert res.wrote is False
    still = root_version.read(root)
    assert still.layout_version == LAYOUT_VERSION + 7
    assert still.last_used_by == "0.9.0"


@pytest.mark.parametrize("body", [
    "not json at all",
    '["a", "list"]',
    '{"layout_version": "one"}',
    '{"layout_version": true}',      # bool is an int subclass; must not pass
    '{"created_by": "0.1.0"}',       # no layout_version
])
def test_unusable_records_read_as_absent_not_as_an_error(root, body):
    root_version.path_for(root).write_text(body)

    assert root_version.read(root) is None
    # ...and a corrupt file must be no more dangerous than a missing one.
    assert root_version.record(root, "0.1.0a5", created_now=True).wrote is True
    assert root_version.read(root).layout_version == LAYOUT_VERSION


def test_record_never_raises_when_the_write_fails(root, monkeypatch):
    def boom(*a, **k):
        raise OSError(122, "Disk quota exceeded")

    monkeypatch.setattr(root_version, "write_secure", boom)

    res = root_version.record(root, "0.1.0a5", created_now=True)

    assert res.write_failed is True
    assert res.wrote is False


def test_the_note_says_what_deleting_actually_costs(root):
    root_version.record(root, "0.1.0a5", created_now=True)

    note = json.loads(root_version.path_for(root).read_text())["_"]
    assert "Do not edit" in note
    # It must NOT call deletion plainly "safe". That is the comfortable half of
    # the truth: your projects are fine, but the newer-build record is gone and
    # with it the only thing this file exists to detect.
    assert "cannot tell" in note and "newer version" in note
    # ...and the recoverable half must be true: deleting really does re-record.
    root_version.path_for(root).unlink()
    assert root_version.record(root, "0.1.0a5", created_now=True).wrote is True


def test_deleting_the_file_really_does_defeat_the_skew_detection(root):
    # Pinning the documented WEAKNESS, not just the strength. If someone later
    # makes absence non-benign, this test fails and they have to decide on
    # purpose rather than by accident.
    root_version.path_for(root).write_text(json.dumps({
        "layout_version": LAYOUT_VERSION + 3,
        "created_by": "0.9.0", "last_used_by": "0.9.0",
    }))
    assert root_version.record(root, "0.1.0a5", created_now=False).root_is_newer

    root_version.path_for(root).unlink()
    res = root_version.record(root, "0.1.0a5", created_now=False)

    assert res.root_is_newer is False, "the evidence is gone; this is a filter"
    assert root_version.read(root).layout_version == LAYOUT_VERSION


# ---------------------------------------------------------------- doctor -----

def test_doctor_warns_when_the_root_is_newer_than_the_build():
    newer = RootVersion(LAYOUT_VERSION + 1, "0.9.0", "0.9.0")

    (f,) = root_version_findings(newer, LAYOUT_VERSION, "0.1.0a5")

    assert f.severity == "warn"
    assert "0.9.0" in f.detail and "0.1.0a5" in f.detail
    assert f.remediation, "a warning the user cannot act on is noise"


def test_doctor_is_quiet_when_the_root_matches():
    (f,) = root_version_findings(
        RootVersion(LAYOUT_VERSION, "0.1.0a5", "0.1.0a5"), LAYOUT_VERSION, "0.1.0a5")

    assert f.severity == "ok"


def test_doctor_reports_an_older_layout_as_information_not_alarm():
    (f,) = root_version_findings(
        RootVersion(LAYOUT_VERSION - 1, "0.0.9", "0.0.9"), LAYOUT_VERSION, "0.1.0a5")

    assert f.severity == "info", "reading an older root is the supported direction"


def test_doctor_says_origin_is_unrecorded_rather_than_inventing_one():
    unknown = RootVersion(LAYOUT_VERSION, "unknown (root predates version recording)",
                          "0.1.0a5")

    (f,) = root_version_findings(unknown, LAYOUT_VERSION, "0.1.0a5")

    assert "not recorded" in f.detail
    assert f.severity == "ok", "an unknown origin is not a defect in the install"


def test_doctor_handles_a_root_with_no_record_at_all():
    (f,) = root_version_findings(None, LAYOUT_VERSION, "0.1.0a5")

    assert f.severity == "info"
    assert f.is_actionable() is False


# ------------------------------------------------- the session-record cliff --

def _session(root, project, sid, version):
    d = root / "state" / project / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.json").write_text(json.dumps({"schema_version": version,
                                             "session_id": sid}))


def test_census_counts_across_every_project(root):
    from botainer.state import session_record as sr

    _session(root, "proj-a", "aaaa1111", sr.SCHEMA_VERSION)
    _session(root, "proj-a", "aaaa2222", sr.SCHEMA_VERSION)
    _session(root, "proj-b", "bbbb3333", sr.SCHEMA_VERSION + 4)

    census = sr.schema_version_census(root)

    assert census[sr.SCHEMA_VERSION] == 2
    assert census[sr.SCHEMA_VERSION + 4] == 1


def test_census_ignores_launcher_internal_dirs(root):
    from botainer.state import session_record as sr

    _session(root, "proj-a", "aaaa1111", sr.SCHEMA_VERSION)
    (root / "state" / "proj-a" / "sessions" / "_submit-scripts").mkdir()

    # Same exclusion list_sessions uses; counting these would inflate the
    # denominator with things that were never session records.
    assert sum(sr.schema_version_census(root).values()) == 1


def test_census_records_unreadable_separately_from_a_known_version(root):
    from botainer.state import session_record as sr

    d = root / "state" / "p" / "sessions" / "cccc4444"
    d.mkdir(parents=True)
    (d / "spec.json").write_text("{ truncated")

    census = sr.schema_version_census(root)

    assert census[None] == 1, "'cannot tell' is not the same fact as 'version 7'"


def test_census_survives_a_root_with_no_state_dir_at_all(tmp_path):
    from botainer.state import session_record as sr

    assert sr.schema_version_census(tmp_path / "nope") == {}


def test_unreadable_count_matches_what_from_dict_actually_refuses(root):
    from botainer.state import session_record as sr

    v = sr.SCHEMA_VERSION
    census = {v: 3, v - 1: 2, v + 1: 1, None: 4}

    # Pin the count against the REAL parser rather than restating its rule, and
    # do it with a record that has every field a real one has — a thinner dict
    # fails on a missing key and would let this test "pass" for the wrong reason.
    real = sr.SessionRecord(
        session_id="abcd1234", project_uuid="u", project_root="/w",
        runtime="docker", image="img", host="h", spec={},
    ).to_dict()

    for version, expected_ok in ((v, True), (v - 1, True), (v + 1, False)):
        try:
            sr.SessionRecord.from_dict({**real, "schema_version": version})
            accepted = True
        except ValueError:
            accepted = False
        assert accepted is expected_ok, f"schema {version}"

    assert sr.unreadable_by_this_build(census) == 5   # the v+1 and the 4 unknown


def test_doctor_is_silent_when_there_are_no_session_records():
    from botainer.cli.doctor import session_schema_findings

    assert session_schema_findings({}, 1) == []


def test_doctor_warns_with_a_count_when_records_fall_off_the_cliff():
    from botainer.cli.doctor import session_schema_findings

    (f,) = session_schema_findings({1: 2, 5: 3}, 1)

    assert f.severity == "warn"
    assert "3 of 5" in f.detail
    assert "botainer list" in f.remediation, "say WHERE the loss shows up"
    assert f.is_actionable() is False, "nothing is broken right now"


def test_doctor_does_not_name_a_schema_version_that_never_existed():
    from botainer.cli.doctor import session_schema_findings

    # At schema 1 there is no "0" to have shipped, so the accepted range must
    # not be printed as a span. Observed on the first real run.
    (f,) = session_schema_findings({1: 1, 9: 1}, 1)
    assert "reads 1)" in f.detail
    assert "0 and 1" not in f.detail

    # ...but once a second schema exists, naming both is the useful thing.
    (g,) = session_schema_findings({2: 1, 9: 1}, 2)
    assert "1 and 2" in g.detail


# ------------------------------------- what review caught before it shipped --

def test_an_aborted_launch_is_not_counted_as_an_unreadable_record(root):
    """THE ONE THAT WOULD HAVE SHIPPED.

    composition.py creates the session directory long before it writes the
    record, so every refusal and every Ctrl-C leaves a dir with no spec.json.
    Nothing removes them. Counting those as "unreadable" made `doctor` warn —
    and `doctor --strict` exit 1 — on a completely healthy install, while
    blaming a downgrade that never happened.
    """
    from botainer.cli.doctor import session_schema_findings
    from botainer.state import session_record as sr

    _session(root, "proj-a", "aaaa1111", sr.SCHEMA_VERSION)
    (root / "state" / "proj-a" / "sessions" / "bbbb2222").mkdir()   # aborted

    census = sr.schema_version_census(root)

    assert census == {sr.SCHEMA_VERSION: 1}, "the aborted dir is not a record"
    assert sr.unreadable_by_this_build(census) == 0
    (f,) = session_schema_findings(census, sr.SCHEMA_VERSION)
    assert f.severity == "ok", "a healthy install must not be warned at"


def test_census_and_list_sessions_agree_on_what_counts(root):
    # They used to encode the rule separately, which is how they came to
    # disagree. One helper now, and this pins that they still agree.
    from botainer.state import session_record as sr

    sessions = root / "state" / "proj-a" / "sessions"
    _session(root, "proj-a", "aaaa1111", sr.SCHEMA_VERSION)
    (sessions / "_submit-scripts").mkdir()
    (sessions / ".hidden").mkdir()
    (sessions / "bbbb2222").mkdir()                                 # aborted
    (sessions / "notadir").write_text("x")

    # The candidate helper filters entries; filesystem enumeration has no order.
    assert sorted(p.name for p in sr.candidate_session_dirs(sessions)) == [
        "aaaa1111", "bbbb2222"]
    # list_sessions returns only the one with a record; the census counts the
    # same one. Neither counts the launcher-internal or hidden dirs.
    assert sum(sr.schema_version_census(root).values()) == 1


def test_a_damaged_record_IS_still_counted(root):
    # The fix must not overshoot: a spec.json that exists and will not parse is
    # a real record in trouble, and staying quiet about it would be the
    # opposite error.
    from botainer.state import session_record as sr

    d = root / "state" / "p" / "sessions" / "cccc4444"
    d.mkdir(parents=True)
    (d / "spec.json").write_text("{ truncated")

    assert sr.schema_version_census(root) == {None: 1}


def test_an_absurdly_large_record_is_counted_but_not_parsed(root):
    from botainer.state import session_record as sr

    d = root / "state" / "p" / "sessions" / "dddd5555"
    d.mkdir(parents=True)
    (d / "spec.json").write_text("x" * (sr._CENSUS_MAX_RECORD_BYTES + 1))

    # Counted as unknown rather than read: this runs on every doctor and setup,
    # over a directory nothing GCs, on NFS.
    assert sr.schema_version_census(root) == {None: 1}


def test_census_wants_the_ROOT_and_is_named_so(root):
    # Passing <root>/state or <root>/state/<uuid> — both called "state_dir"
    # elsewhere in this package — used to return {} silently, which doctor
    # renders as nothing at all.
    import inspect

    from botainer.state import session_record as sr

    assert list(inspect.signature(sr.schema_version_census).parameters) == ["root"]


def test_findings_derive_the_count_rather_than_trusting_a_caller():
    import inspect

    from botainer.cli import doctor

    params = list(inspect.signature(doctor.session_schema_findings).parameters)
    assert params == ["census", "schema_version"], (
        "a caller-supplied count could disagree with the census and print a "
        "green tick while records were being dropped"
    )


def test_remedy_does_not_tell_you_to_downgrade_for_old_records():
    from botainer.cli.doctor import session_schema_findings

    # Records TOO OLD for this build. "Upgrade to the version that wrote them"
    # would be an instruction to downgrade, which then strands the new ones.
    (old,) = session_schema_findings({5: 2, 1: 1}, 5)
    assert "no conversion" in old.remediation
    assert "Upgrading botainer" not in old.remediation

    # Records too NEW: upgrading really is the fix.
    (new,) = session_schema_findings({1: 1, 9: 1}, 1)
    assert "Upgrading botainer" in new.remediation


def test_read_survives_input_that_is_not_an_OSError_or_ValueError(root):
    """json.loads on deeply nested input raises RecursionError, not ValueError.

    The original excepts missed it, so a corrupt root.json tracebacked out of
    `doctor` — the command you run BECAUSE the state root is in a bad way.

    THE DEPTH IS CHOSEN, NOT GUESSED. The first version of this test used
    200_000 brackets, which is larger than _MAX_RECORD_BYTES, so the size guard
    returned None before json.loads was ever called — the test passed while
    exercising nothing, and narrowing the except back to (OSError, ValueError)
    left it green. Measured on this interpreter: 40_000 gives JSONDecodeError,
    60_000 gives RecursionError. This sits above that and below the size guard,
    so it reaches the parser and needs the broad except to survive.
    """
    depth = 60_000
    assert depth < root_version._MAX_RECORD_BYTES, "must reach json.loads"
    with pytest.raises(RecursionError):
        json.loads("[" * depth)          # pin WHY this input is interesting

    root_version.path_for(root).write_text("[" * depth)

    assert root_version.read(root) is None
    assert root_version.record(root, "0.1.0a5", created_now=False).wrote is True


def test_read_does_not_load_an_enormous_file(root, monkeypatch):
    root_version.path_for(root).write_text(
        "x" * (root_version._MAX_RECORD_BYTES + 1))

    def refuse(*a, **k):
        raise AssertionError("read_text called on an oversized record")

    monkeypatch.setattr(type(root_version.path_for(root)), "read_text", refuse)
    assert root_version.read(root) is None


@pytest.mark.parametrize("layout_repr", ["0", "-1", "999999999"],
                         ids=["zero", "negative", "far-future"])
def test_absurd_layout_versions_are_refused(root, layout_repr):
    # An unbounded value would be accepted, refuse every future write forever,
    # and be interpolated whole into a terminal Finding. Passed as text so no
    # enormous int is ever constructed here.
    root_version.path_for(root).write_text(
        '{"layout_version": %s, "created_by": "x", "last_used_by": "x"}'
        % layout_repr)

    assert root_version.read(root) is None


def test_a_thousands_of_digits_layout_is_refused_by_python_not_by_us(root):
    # ATTRIBUTION MATTERS. A 5000-digit integer never reaches our bound at all:
    # CPython caps int-from-string at 4300 digits and json.loads raises
    # ValueError first. Keeping this as a separate test — rather than a
    # parametrise case on the one above — stops it from looking like evidence
    # for a check it does not exercise.
    root_version.path_for(root).write_text(
        '{"layout_version": %s, "created_by": "x", "last_used_by": "x"}'
        % ("1" + "0" * 5000))

    with pytest.raises(ValueError):
        json.loads("1" + "0" * 5000)     # the mechanism actually responsible

    assert root_version.read(root) is None


def test_control_characters_never_reach_a_finding(root):
    # These two fields go straight into click.secho, which filters nothing.
    root_version.path_for(root).write_text(json.dumps({
        "layout_version": LAYOUT_VERSION,
        "created_by": "0.1.0\x1b[31mRED\x07",
        "last_used_by": "\x00\x01\x02",
    }))

    got = root_version.read(root)

    assert "\x1b" not in got.created_by and "\x07" not in got.created_by
    (f,) = root_version_findings(got, LAYOUT_VERSION, "0.1.0a5")
    assert "\x1b" not in f.detail and "\x00" not in f.detail


def test_a_long_label_is_bounded(root):
    root_version.path_for(root).write_text(json.dumps({
        "layout_version": LAYOUT_VERSION,
        "created_by": "v" * 5000, "last_used_by": "0.1.0"}))

    assert len(root_version.read(root).created_by) <= root_version._MAX_LABEL_CHARS


# --------------------------------------------------- through the REAL caller --

def test_ensure_user_state_dir_actually_writes_the_record(tmp_path, monkeypatch):
    # Everything above drives record() directly. This drives the real caller,
    # which is the only thing that proves the feature is wired at all.
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)

    got = root_version.read(paths.root)
    assert got is not None
    assert got.layout_version == LAYOUT_VERSION
    assert got.origin_is_known, "we just created it, so authorship is a fact"


def test_inspecting_a_root_does_not_write_to_it(tmp_path, monkeypatch):
    # A stated property currently guaranteed by one indentation level, which a
    # refactor moves for free. Pin it.
    from botainer.state import dir as state_dir

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("MY_BOTAINER", str(root))

    state_dir.ensure_user_state_dir(create_if_missing=False)

    assert not root_version.path_for(root).exists()


def test_a_root_created_by_an_earlier_build_is_not_claimed(tmp_path, monkeypatch):
    """The sequence the directory-sniffing got wrong.

    A pre-#202 build makes the root and logs in, but never runs a session — so
    `state/` is empty. The old inference read that as "brand new" and stamped
    the CURRENT version as `created_by`, permanently.
    """
    from botainer.state import dir as state_dir

    root = tmp_path / "root"
    (root / "state").mkdir(parents=True)          # exists, but no projects
    (root / "shared-auth").mkdir()
    monkeypatch.setenv("MY_BOTAINER", str(root))

    state_dir.ensure_user_state_dir(create_if_missing=True)

    assert root_version.read(root).origin_is_known is False
