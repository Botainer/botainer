"""Parse the in-container self-test probe's stdout into CheckResults.

The probe (botainer/preflight/probe_script.py) emits two sentinel-framed
blocks on stdout:

    BOTAINER-SELFTEST-V1-BEGIN
    {"check":"workspace-rw","result":"pass","detail":""}
    {"check":"caps-dropped","result":"pass","detail":""}
    ...
    BOTAINER-SELFTEST-V1-END
    BOTAINER-PROC-MOUNTS-BEGIN
    <raw /proc/self/mounts>
    BOTAINER-PROC-MOUNTS-END

Sentinel framing makes apptainer's stderr-on-stdout warnings + image banners
harmless — anything outside the frames is ignored. Missing/corrupt selftest
frame → ProbeFrameError (the runner maps that to exit 3 "runtime error",
NEVER to a security-check failure). This module is pure + fully unit-testable.

Design authored via a verified Fable-5 subagent (wf_fecc8dc3-aa7).
"""

from __future__ import annotations

import json

from botainer.preflight.checks import CheckResult, PreflightCheck

SELFTEST_BEGIN = "BOTAINER-SELFTEST-V1-BEGIN"
SELFTEST_END = "BOTAINER-SELFTEST-V1-END"
MOUNTS_BEGIN = "BOTAINER-PROC-MOUNTS-BEGIN"
MOUNTS_END = "BOTAINER-PROC-MOUNTS-END"

_VALID_RESULTS = {"pass", "fail", "skip"}
_VALUE_TO_CHECK = {c.value: c for c in PreflightCheck}


class ProbeFrameError(ValueError):
    """The selftest sentinel frame is missing or corrupt — the probe did not
    run to completion (exec failure, no /bin/sh, killed). Distinct from a
    security-check FAILURE: the caller maps this to a runtime-error exit."""


def extract_block(text: str, begin: str, end: str) -> str | None:
    """Return the text strictly between the first `begin` line and the next
    `end` line (exclusive), or None if the frame isn't present. Matches whole
    lines so a sentinel mentioned inside a detail string can't false-trigger."""
    lines = text.splitlines()
    try:
        i = next(n for n, ln in enumerate(lines) if ln.strip() == begin)
    except StopIteration:
        return None
    try:
        j = next(n for n in range(i + 1, len(lines)) if lines[n].strip() == end)
    except StopIteration:
        return None
    return "\n".join(lines[i + 1:j])


def _parse_check_lines(block: str) -> list[CheckResult]:
    """Parse the JSON check lines inside a frame body. A line that isn't valid
    JSON / lacks keys / has an unknown check / invalid result is SKIPPED
    (tolerant of interleaved noise). Shared by the strict parser and the
    tolerant salvage."""
    results: list[CheckResult] = []
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # interleaved noise inside the frame — skip
        if not isinstance(obj, dict):
            continue
        cv, rv = obj.get("check"), obj.get("result")
        check = _VALUE_TO_CHECK.get(cv)
        if check is None or rv not in _VALID_RESULTS:
            continue
        detail = obj.get("detail", "")
        if not isinstance(detail, str):
            detail = str(detail)
        target = obj.get("target", "")
        if not isinstance(target, str):
            target = str(target)
        results.append(
            CheckResult(check=check, result=rv, detail=detail[:200], target=target[:200])
        )
    return results


def salvage_results(text: str) -> list[CheckResult]:
    """Best-effort recovery of check results from INCOMPLETE probe output — a
    frame that has BOTAINER-SELFTEST-V1-BEGIN but no END (the probe was killed
    mid-run, e.g. by a timeout). Everything after BEGIN (up to END if present,
    else EOF) is scanned for valid check lines. Never raises; returns [] if
    there's no BEGIN sentinel or no parseable line. The runner keeps only the
    confirmed FAILs from this — a check we never reached is not a pass."""
    lines = text.splitlines()
    try:
        i = next(n for n, ln in enumerate(lines) if ln.strip() == SELFTEST_BEGIN)
    except StopIteration:
        return []
    try:
        j = next(n for n in range(i + 1, len(lines)) if lines[n].strip() == SELFTEST_END)
    except StopIteration:
        j = len(lines)
    return _parse_check_lines("\n".join(lines[i + 1:j]))


def parse_selftest_output(text: str) -> tuple[list[CheckResult], str | None]:
    """Parse probe stdout → (check results, /proc/self/mounts text or None).

    Raises ProbeFrameError if the selftest frame is absent (the probe never
    completed). Within the frame: each non-empty line is parsed as JSON; a line
    that isn't valid JSON, lacks the required keys, has an unknown check value,
    or an invalid result is SKIPPED (tolerant of interleaved noise), not fatal —
    but a completely empty frame with no valid check lines IS a ProbeFrameError
    (the probe emitted the sentinels but no results = corrupt)."""
    block = extract_block(text, SELFTEST_BEGIN, SELFTEST_END)
    if block is None:
        raise ProbeFrameError(
            "self-test frame not found in probe output — the probe did not run "
            "to completion (exec failure / no /bin/sh / killed). This is a "
            "runtime error, not a security-check failure."
        )
    results = _parse_check_lines(block)
    if not results:
        raise ProbeFrameError(
            "self-test frame present but contained no valid check results — "
            "probe output is corrupt."
        )
    mounts = extract_block(text, MOUNTS_BEGIN, MOUNTS_END)
    return results, mounts
