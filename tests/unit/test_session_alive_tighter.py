"""Task #92: is_session_alive should be tighter on can't-tell paths.

Old behavior: apptainer record with no slurm_jobid → assumed alive
("foreground; we can't tell — assume alive"). This left every
foreground-exited apptainer session as a zombie 'running forever'.

New behavior: no jobid → assume DEAD. A session that was supposed to
be alive has a jobid recorded.
"""

from __future__ import annotations

from botainer.state import liveness, session_record


def _make_apptainer_record(*, slurm_jobid: str | None = None):
    rec = session_record.SessionRecord(
        session_id="test-session",
        project_uuid="test-project",
        project_root="/host/proj",
        host="localhost",
        runtime="apptainer",
        started_at="2026-05-24T00:00:00Z",
        image="apptainer:test",
        docker=None,
        apptainer=session_record.ApptainerHandle(slurm_jobid=slurm_jobid),
        spec={},
    )
    return rec


def test_apptainer_no_jobid_is_dead() -> None:
    """The pinning bug: foreground-exit sessions show 'running forever'."""
    rec = _make_apptainer_record(slurm_jobid=None)
    assert liveness.is_session_alive(rec) is False


def test_apptainer_empty_jobid_is_dead() -> None:
    """Empty string slurm_jobid is treated identically to None."""
    rec = _make_apptainer_record(slurm_jobid="")
    assert liveness.is_session_alive(rec) is False
