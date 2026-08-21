"""Unit tests for the self-test runner (botainer/preflight/runner.py).

The runner is exercised WITHOUT a real container by injecting a fake `run`
callable that returns canned probe stdout. This pins:
  - exit-code aggregation (0 all-pass, 4 any-fail, 3 runtime error) and the
    precedence that a CONFIRMED fail wins over a co-occurring runtime error
    (so "cage is broken" is never downgraded to "infra hiccup");
  - the completeness invariant (Fable-5 HIGH-1): a planned check with no
    result line → exit 3, NOT a false pass;
  - missing/empty /proc/self/mounts frame → exit 3 (Fable-5 MEDIUM-1), never a
    silent skip-to-0 and never mislabeled as a posture FAIL;
  - timeout salvage of confirmed fails + best-effort container reap (MEDIUM-2);
  - that the docker adapter is invoked non-interactively (no -it);
  - that the host mount-plan readback is folded into the results.

A real-docker end-to-end test is skipif-gated (needs a daemon + image).

Design authored via a verified Fable-5 subagent (wf_fecc8dc3-aa7); hardened
against its adversarial re-review.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from botainer.preflight.checks import CheckResult, PreflightCheck
from botainer.preflight.runner import (
    EXIT_CHECK_FAILED,
    EXIT_OK,
    EXIT_RUNTIME,
    SelftestReport,
    run_selftest,
)

US = "\x1f"

# The always-on checks a default (no-bind, network!=none) spec plans.
_ALWAYS = [
    "caps-dropped", "no-new-privs", "negative-etc-shadow",
    "negative-ssh-home", "negative-env-injection",
]
_DEFAULT_MOUNTS = "proc /proc proc rw 0 0\n/dev/sda1 / ext4 rw 0 0"


# ── pure exit-code aggregation + precedence ──

def test_report_exit_ok_all_pass() -> None:
    r = SelftestReport(results=[
        CheckResult(PreflightCheck.CAPS_DROPPED, "pass"),
        CheckResult(PreflightCheck.WORKSPACE_RW, "skip"),
    ])
    assert r.exit_code == EXIT_OK and r.ok


def test_report_exit_check_failed_beats_pass() -> None:
    r = SelftestReport(results=[
        CheckResult(PreflightCheck.CAPS_DROPPED, "pass"),
        CheckResult(PreflightCheck.NEGATIVE_ETC_SHADOW, "fail", "shadow readable"),
    ])
    assert r.exit_code == EXIT_CHECK_FAILED and not r.ok


def test_report_confirmed_fail_wins_over_runtime_error() -> None:
    # Fable-5 MEDIUM-2 precedence: a confirmed FAIL must NOT be downgraded to a
    # retryable runtime error. fail + runtime_error → exit 4, not 3.
    r = SelftestReport(
        results=[CheckResult(PreflightCheck.CAPS_DROPPED, "fail")],
        runtime_error="timed out",
    )
    assert r.exit_code == EXIT_CHECK_FAILED


def test_report_runtime_error_without_fail_is_exit_3() -> None:
    r = SelftestReport(results=[], runtime_error="boom")
    assert r.exit_code == EXIT_RUNTIME


# ── spec + fake-run harness ──

def _spec(binds=(), network_none=False, runtime="docker"):
    from botainer.core.spec import (
        Bind, BindMode, MountPlan, NetworkMode, NetworkSpec, Provenance, SessionSpec,
    )
    return SessionSpec(
        session_id="s1abc", project_uuid="u", project_root="/p", state_dir="/s",
        runtime=runtime, image="img",
        mount_plan=MountPlan(binds=tuple(binds)),
        network=NetworkSpec(mode=NetworkMode.NONE if network_none else NetworkMode.INTERNET),
    )


class _FakeProc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _line(check: str, result: str = "pass", detail: str = "", target: str = "") -> str:
    return f'{{"check":"{check}","result":"{result}","detail":"{detail}","target":"{target}"}}'


def _frame(check_lines: list[str], mounts: str | None = _DEFAULT_MOUNTS) -> str:
    body = "BOTAINER-SELFTEST-V1-BEGIN\n" + "\n".join(check_lines) + "\nBOTAINER-SELFTEST-V1-END\n"
    if mounts is not None:
        body += f"BOTAINER-PROC-MOUNTS-BEGIN\n{mounts}\nBOTAINER-PROC-MOUNTS-END\n"
    return "apptainer banner noise\n" + body + "trailing noise\n"


def _all_pass_frame(extra_lines: list[str] | None = None, mounts=_DEFAULT_MOUNTS) -> str:
    lines = [_line(c) for c in _ALWAYS] + (extra_lines or [])
    return _frame(lines, mounts=mounts)


def _runner(stdout: str, returncode: int = 0, stderr: str = ""):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _FakeProc(stdout, returncode=returncode, stderr=stderr)

    return fake_run, captured


def test_run_selftest_all_pass_exit_0() -> None:
    fake_run, captured = _runner(_all_pass_frame())
    rep = run_selftest(_spec(), run=fake_run)
    assert rep.exit_code == EXIT_OK, [(_.check.value, _.result, _.detail) for _ in rep.results]
    # docker adapter invoked non-interactively (no -it), still --rm.
    assert captured["argv"][:3] == ["docker", "run", "--rm"]
    assert "-it" not in captured["argv"]


def test_run_selftest_failing_check_exit_4() -> None:
    lines = [_line(c) for c in _ALWAYS[:-1]] + [
        _line("negative-env-injection", "fail", "BASH_ENV set"),
    ]
    fake_run, _ = _runner(_frame(lines))
    rep = run_selftest(_spec(), run=fake_run)
    assert rep.exit_code == EXIT_CHECK_FAILED
    assert any(r.result == "fail" for r in rep.results)


def test_run_selftest_incomplete_result_set_is_runtime_error() -> None:
    # Fable-5 HIGH-1: only 2 of the 5 planned always-on checks reported → the
    # probe silently skipped 3 → runtime error (exit 3), NOT a false all-pass.
    fake_run, _ = _runner(_frame([_line("caps-dropped"), _line("no-new-privs")]))
    rep = run_selftest(_spec(), run=fake_run)
    assert rep.exit_code == EXIT_RUNTIME
    assert "incomplete" in rep.runtime_error
    # names the missing checks
    assert "negative-etc-shadow" in rep.runtime_error


def test_run_selftest_incomplete_still_surfaces_observed_fail() -> None:
    # Incomplete AND a confirmed fail present → the fail wins (exit 4).
    lines = [_line("caps-dropped"), _line("negative-etc-shadow", "fail", "readable")]
    fake_run, _ = _runner(_frame(lines))
    rep = run_selftest(_spec(), run=fake_run)
    assert rep.exit_code == EXIT_CHECK_FAILED


def test_run_selftest_unplanned_check_is_dropped_not_padding_pass() -> None:
    # A probe emitting a result for a NOT-planned check can't pad the pass set;
    # the unplanned line is dropped and completeness still holds on the planned.
    lines = [_line(c) for c in _ALWAYS] + [_line("network-none", "pass")]  # net not none → unplanned
    fake_run, _ = _runner(_frame(lines))
    rep = run_selftest(_spec(), run=fake_run)
    assert rep.exit_code == EXIT_OK
    assert not any(r.check == PreflightCheck.NETWORK_NONE for r in rep.results)


def test_run_selftest_missing_frame_is_runtime_error() -> None:
    fake_run, _ = _runner("container failed to start\nno sentinels\n", returncode=127,
                          stderr="docker: Error response from daemon: no such image")
    rep = run_selftest(_spec(), run=fake_run)
    assert rep.exit_code == EXIT_RUNTIME
    assert rep.runtime_error and "no valid result frame" in rep.runtime_error
    assert "127" in rep.runtime_error
    # stderr tail surfaced (Fable-5 MEDIUM-2c) so the real cause isn't guessed.
    assert "no such image" in rep.runtime_error


def test_run_selftest_missing_mounts_is_runtime_error() -> None:
    # Fable-5 MEDIUM-1: no mounts frame → exit 3 (not a silent skip → 0).
    fake_run, _ = _runner(_all_pass_frame(mounts=None))
    rep = run_selftest(_spec(), run=fake_run)
    assert rep.exit_code == EXIT_RUNTIME
    assert "no /proc/self/mounts" in rep.runtime_error


def test_run_selftest_empty_mounts_is_runtime_error_not_posture_fail() -> None:
    # Fable-5 MEDIUM-1: empty mounts frame is a RUNTIME anomaly (exit 3), NOT a
    # posture failure (exit 4) — the 3-vs-4 conflation the design forbids.
    fake_run, _ = _runner(_all_pass_frame(mounts="   "))
    rep = run_selftest(_spec(), run=fake_run)
    assert rep.exit_code == EXIT_RUNTIME
    assert "no /proc/self/mounts" in rep.runtime_error


def test_run_selftest_timeout_reaps_and_salvages_fail() -> None:
    # Fable-5 MEDIUM-2: a cage failure that also hangs → the observed FAIL is
    # salvaged from partial stdout and wins (exit 4); the container is reaped.
    partial = "BOTAINER-SELFTEST-V1-BEGIN\n" + _line("negative-etc-shadow", "fail", "readable") + "\n"
    reap_calls = []

    def fake_run(argv, **kw):
        if argv[:3] == ["docker", "rm", "-f"]:
            reap_calls.append(argv)
            return _FakeProc("", returncode=0)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1, output=partial)

    rep = run_selftest(_spec(), timeout=1, run=fake_run)
    assert rep.exit_code == EXIT_CHECK_FAILED  # salvaged fail wins
    assert reap_calls and reap_calls[0] == ["docker", "rm", "-f", "botainer-s1abc"]


def test_run_selftest_timeout_no_partial_is_runtime_error() -> None:
    def fake_run(argv, **kw):
        if argv[:3] == ["docker", "rm", "-f"]:
            return _FakeProc("", returncode=0)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    rep = run_selftest(_spec(), timeout=1, run=fake_run)
    assert rep.exit_code == EXIT_RUNTIME
    assert "timed out" in rep.runtime_error


def test_run_selftest_launch_oserror_is_runtime_error() -> None:
    def fake_run(argv, **kw):
        raise OSError("docker: not found")

    rep = run_selftest(_spec(), run=fake_run)
    assert rep.exit_code == EXIT_RUNTIME
    assert "launch failed" in rep.runtime_error


def test_run_selftest_folds_in_mount_readback_pass() -> None:
    from botainer.core.spec import Bind, BindMode, Provenance
    b = Bind(source="/workspace", target="/workspace", mode=BindMode.RW,
             provenance=Provenance.CORE, self_test="SELFTEST_WORKSPACE_BIND")
    mounts = "proc /proc proc rw 0 0\n/dev/sda1 /workspace ext4 rw 0 0"
    # workspace bind adds a planned (WORKSPACE_RW, /workspace) probe → include it
    # WITH its target so it matches the (check, target) completeness identity.
    extra = [_line("workspace-rw", "pass", target="/workspace")]
    fake_run, _ = _runner(_all_pass_frame(extra, mounts=mounts))
    rep = run_selftest(_spec([b]), run=fake_run)
    mr = [r for r in rep.results if r.check == PreflightCheck.MOUNT_PLAN_READBACK]
    assert mr and all(r.result == "pass" for r in mr)
    assert rep.exit_code == EXIT_OK


def test_run_selftest_same_enum_two_targets_one_dropped_is_incomplete() -> None:
    """Codex Priority-A MEDIUM: two RO binds both emit `data-ro`. If the probe
    reports only ONE, an enum-set completeness check would pass — but keying on
    (check, target) catches the dropped one as incomplete (exit 3)."""
    from botainer.core.spec import Bind, BindMode, Provenance
    binds = [
        Bind(source="/data1", target="/data1", mode=BindMode.RO,
             provenance=Provenance.USER, self_test="SELFTEST_EXTRA_BIND"),
        Bind(source="/data2", target="/data2", mode=BindMode.RO,
             provenance=Provenance.USER, self_test="SELFTEST_EXTRA_BIND"),
    ]
    mounts = ("proc /proc proc rw 0 0\n/dev/sda1 /data1 ext4 ro 0 0\n"
              "/dev/sda1 /data2 ext4 ro 0 0")
    # Probe reports only /data1's data-ro — /data2's is silently missing.
    extra = [_line("data-ro", "pass", target="/data1")]
    fake_run, _ = _runner(_all_pass_frame(extra, mounts=mounts))
    rep = run_selftest(_spec(binds), run=fake_run)
    assert rep.exit_code == EXIT_RUNTIME, [(_.check.value, _.target) for _ in rep.results]
    assert "incomplete" in rep.runtime_error and "/data2" in rep.runtime_error
    # With BOTH reported it passes.
    extra2 = [_line("data-ro", "pass", target="/data1"),
              _line("data-ro", "pass", target="/data2")]
    fake_run2, _ = _runner(_all_pass_frame(extra2, mounts=mounts))
    assert run_selftest(_spec(binds), run=fake_run2).exit_code == EXIT_OK


def test_run_selftest_probe_rides_argv_no_extra_bind() -> None:
    """The probe must not add binds — it rides argv only. Assert the composed
    argv carries the probe sentinel arg and the bind flags are unchanged vs the
    plain render."""
    from botainer.core import composition
    spec = _spec()
    plain = composition.render_argv(spec)
    fake_run, captured = _runner(_all_pass_frame())
    run_selftest(spec, run=fake_run)
    probe_argv = captured["argv"]
    assert "botainer-probe" in probe_argv
    assert probe_argv.count("--mount") == plain.count("--mount")
    assert probe_argv.count("-v") == plain.count("-v")


# ── real docker end-to-end (skipif-gated) ──

@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="needs a real docker daemon + image",
)
def test_run_selftest_real_docker_alpine() -> None:
    spec = _spec().model_copy(update={"image": "alpine:3.19"})
    rep = run_selftest(spec, timeout=90)
    # Can't assert PASS (alpine lacks the full cage) — but the probe must RUN
    # and produce a parseable, COMPLETE frame → not a runtime error.
    assert rep.runtime_error is None, rep.runtime_error
    assert rep.results
