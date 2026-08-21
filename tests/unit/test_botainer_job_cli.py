"""#54: the in-container botainer-job CLI (writes /jobs/in, reads /jobs/out)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[2] / "plugins" / "hpc-launcher" / "agent_helper" / "botainer-job"


def _run(args, in_dir: Path, out_dir: Path):
    env = {
        "BOTAINER_JOBS_IN": str(in_dir),
        "BOTAINER_JOBS_OUT": str(out_dir),
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        env=env, capture_output=True, text=True, timeout=10,
    )


@pytest.fixture
def mailbox(tmp_path: Path):
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    return tmp_path / "in", tmp_path / "out"


def test_submit_writes_argv_request(mailbox) -> None:
    in_dir, out_dir = mailbox
    r = _run(["submit", "gpu", "--", "python", "train.py", "--epochs", "50"], in_dir, out_dir)
    assert r.returncode == 0, r.stderr
    job_id = r.stdout.strip()
    req = json.loads((in_dir / f"{job_id}.json").read_text())
    assert req["profile"] == "gpu"
    assert req["command"] == ["python", "train.py", "--epochs", "50"]  # argv, not shell
    assert req["version"] == "botainer-job-v1"
    assert len(job_id) == 16


def test_submit_without_dashes(mailbox) -> None:
    in_dir, out_dir = mailbox
    r = _run(["submit", "quick", "echo", "hi"], in_dir, out_dir)
    assert r.returncode == 0
    req = json.loads((in_dir / f"{r.stdout.strip()}.json").read_text())
    assert req["command"] == ["echo", "hi"]


def test_status_pending_when_only_in_inbox(mailbox) -> None:
    in_dir, out_dir = mailbox
    jid = _run(["submit", "quick", "echo", "hi"], in_dir, out_dir).stdout.strip()
    r = _run(["status", jid], in_dir, out_dir)
    assert r.returncode == 0
    assert json.loads(r.stdout)["state"] == "pending"


def test_status_reads_out_record(mailbox) -> None:
    in_dir, out_dir = mailbox
    jid = "abcdef0123456789"
    (out_dir / f"{jid}.status.json").write_text(
        json.dumps({"id": jid, "state": "running", "slurm_job_id": 42, "profile": "gpu"})
    )
    r = _run(["status", jid], in_dir, out_dir)
    assert json.loads(r.stdout)["state"] == "running"


def test_cancel_writes_marker(mailbox) -> None:
    in_dir, out_dir = mailbox
    jid = "abcdef0123456789"
    r = _run(["cancel", jid], in_dir, out_dir)
    assert r.returncode == 0
    assert (in_dir / f"{jid}.cancel").exists()


def test_refuses_bad_id_and_profile(mailbox) -> None:
    in_dir, out_dir = mailbox
    assert _run(["status", "../etc/passwd"], in_dir, out_dir).returncode != 0
    assert _run(["submit", "bad name", "echo"], in_dir, out_dir).returncode != 0


def test_profiles_lists_from_manifest(mailbox) -> None:
    in_dir, out_dir = mailbox
    (out_dir / "profiles.json").write_text(json.dumps({
        "version": "botainer-job-profiles-v1",
        "profiles": {"gpu": {"description": "1 A100", "partition": "gpu", "gpus": 1}},
    }))
    r = _run(["profiles"], in_dir, out_dir)
    assert r.returncode == 0
    assert "gpu" in r.stdout and "1 A100" in r.stdout
