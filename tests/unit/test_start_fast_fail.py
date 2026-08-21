"""`botainer start` fast-fail hint: when a session dies almost instantly, point
the user at `botainer doctor` instead of a raw error they can't decode. The
foreground path can't capture docker's error text, so this pointer is the
reliable channel (feedback_plain_language_and_guided_errors)."""
from __future__ import annotations

from botainer.cli.start import _fast_fail_hint


def test_fast_error_points_to_doctor() -> None:
    h = _fast_fail_hint(rc=1, elapsed_s=1.2, runtime="docker")
    assert h is not None
    assert "botainer doctor" in h


def test_fast_docker_error_mentions_disk_remedy() -> None:
    h = _fast_fail_hint(rc=125, elapsed_s=0.8, runtime="docker")
    assert h and "docker system prune" in h and "Mac's free space" in h


def test_fast_apptainer_error_points_to_doctor_without_docker_disk() -> None:
    h = _fast_fail_hint(rc=2, elapsed_s=0.5, runtime="apptainer")
    assert h and "botainer doctor" in h
    assert "docker system prune" not in h  # docker-only remedy


def test_clean_exit_no_hint() -> None:
    assert _fast_fail_hint(rc=0, elapsed_s=0.5, runtime="docker") is None


def test_user_ctrl_c_is_not_flagged_as_a_failure() -> None:
    # 130 = SIGINT (Ctrl-C), 143 = SIGTERM — the user quit fast, not a launch fail.
    assert _fast_fail_hint(rc=130, elapsed_s=1.0, runtime="docker") is None
    assert _fast_fail_hint(rc=143, elapsed_s=1.0, runtime="docker") is None


def test_slow_error_is_real_work_not_launch_failure() -> None:
    # A session that ran 30s then errored is the agent's work, not a launch fail.
    assert _fast_fail_hint(rc=1, elapsed_s=30.0, runtime="docker") is None
