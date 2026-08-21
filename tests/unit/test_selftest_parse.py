"""Parse tests for the self-test probe output — tolerant of hostile/noisy
stdout (apptainer banners, interleaved lines), strict on a missing frame."""

from __future__ import annotations

import pytest

from botainer.preflight.checks import PreflightCheck
from botainer.preflight.parse import (
    ProbeFrameError,
    extract_block,
    parse_selftest_output,
    salvage_results,
)

GOOD = """image banner noise
BOTAINER-SELFTEST-V1-BEGIN
{"check":"workspace-rw","result":"pass","detail":""}
{"check":"caps-dropped","result":"fail","detail":"CapEff nonzero"}
BOTAINER-SELFTEST-V1-END
BOTAINER-PROC-MOUNTS-BEGIN
proc /proc proc rw,nosuid 0 0
/dev/sda1 /workspace ext4 rw 0 0
BOTAINER-PROC-MOUNTS-END
trailing noise
"""


def test_parses_checks_and_mounts() -> None:
    checks, mounts = parse_selftest_output(GOOD)
    by = {c.check: c for c in checks}
    assert by[PreflightCheck.WORKSPACE_RW].result == "pass"
    assert by[PreflightCheck.CAPS_DROPPED].result == "fail"
    assert "/workspace" in mounts


def test_missing_frame_raises() -> None:
    with pytest.raises(ProbeFrameError):
        parse_selftest_output("no sentinels here at all\njust noise\n")


def test_empty_frame_raises() -> None:
    with pytest.raises(ProbeFrameError):
        parse_selftest_output("BOTAINER-SELFTEST-V1-BEGIN\nBOTAINER-SELFTEST-V1-END\n")


def test_garbage_and_unknown_lines_skipped_not_fatal() -> None:
    txt = """BOTAINER-SELFTEST-V1-BEGIN
not json at all
{"check":"workspace-rw","result":"pass","detail":""}
{"check":"totally-unknown-check","result":"pass"}
{"check":"caps-dropped","result":"bogus-result"}
{"nope":"missing keys"}
BOTAINER-SELFTEST-V1-END
"""
    checks, mounts = parse_selftest_output(txt)
    # Only the one valid, known-check, valid-result line survives.
    assert len(checks) == 1
    assert checks[0].check == PreflightCheck.WORKSPACE_RW
    assert mounts is None  # no mounts frame


def test_mounts_frame_optional() -> None:
    txt = 'BOTAINER-SELFTEST-V1-BEGIN\n{"check":"caps-dropped","result":"pass","detail":""}\nBOTAINER-SELFTEST-V1-END\n'
    checks, mounts = parse_selftest_output(txt)
    assert checks and mounts is None


def test_salvage_recovers_fails_from_unterminated_frame() -> None:
    # A timed-out probe emits BEGIN + some lines but no END. Strict parse fails;
    # salvage recovers the check lines it did emit.
    partial = (
        "banner\nBOTAINER-SELFTEST-V1-BEGIN\n"
        '{"check":"caps-dropped","result":"pass","detail":""}\n'
        '{"check":"negative-etc-shadow","result":"fail","detail":"readable"}\n'
        "(killed here, no END)\n"
    )
    with pytest.raises(ProbeFrameError):
        parse_selftest_output(partial)  # strict rejects it
    salv = salvage_results(partial)
    by = {c.check: c for c in salv}
    assert by[PreflightCheck.NEGATIVE_ETC_SHADOW].result == "fail"
    assert by[PreflightCheck.CAPS_DROPPED].result == "pass"


def test_salvage_no_begin_sentinel_returns_empty() -> None:
    assert salvage_results("total garbage\nno sentinels\n") == []


def test_salvage_stops_at_end_sentinel() -> None:
    txt = (
        "BOTAINER-SELFTEST-V1-BEGIN\n"
        '{"check":"caps-dropped","result":"fail","detail":""}\n'
        "BOTAINER-SELFTEST-V1-END\n"
        '{"check":"no-new-privs","result":"fail","detail":"outside frame"}\n'
    )
    salv = salvage_results(txt)
    assert len(salv) == 1 and salv[0].check == PreflightCheck.CAPS_DROPPED


def test_extract_block_line_exact() -> None:
    # A sentinel mentioned mid-line (e.g. in a detail) must not false-trigger.
    txt = "prefix BOTAINER-SELFTEST-V1-BEGIN not a real frame\n"
    assert extract_block(txt, "BOTAINER-SELFTEST-V1-BEGIN", "BOTAINER-SELFTEST-V1-END") is None
