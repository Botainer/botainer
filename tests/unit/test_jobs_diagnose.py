"""`botainer hpc jobs-doctor` — the built-in job-dispatch diagnostic. Covers each
SILENT failure mode that left the caged agent with no jobs (live-HPC
debugging), so the tool always names the specific cause + fix."""
from __future__ import annotations

from pathlib import Path

from botainer.hpc.diagnose import diagnose


def _proj(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".botainer").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".botainer" / "config.yaml").write_text(body)
    return tmp_path


def test_good_config_wires_dispatch(tmp_path: Path) -> None:
    ok, lines = diagnose(_proj(tmp_path,
        "version: config-v1\njob_profiles:\n  gpu: {partition: GPU}\n"))
    assert ok is True
    assert any("VERDICT: ✓" in ln for ln in lines)


def test_missing_config_file(tmp_path: Path) -> None:
    ok, lines = diagnose(tmp_path)  # no .botainer/config.yaml
    assert ok is False
    assert any("does not exist" in ln for ln in lines)


def test_nested_under_plugins_is_pinpointed(tmp_path: Path) -> None:
    ok, lines = diagnose(_proj(tmp_path,
        "version: config-v1\nplugins:\n  job_profiles:\n    gpu: {partition: GPU}\n"))
    assert ok is False
    assert any("NESTED UNDER `plugins:`" in ln for ln in lines)
    assert any("VERDICT: ✗" in ln for ln in lines)


def test_wrong_key_profiles_is_pinpointed(tmp_path: Path) -> None:
    ok, lines = diagnose(_proj(tmp_path,
        "version: config-v1\nprofiles:\n  gpu: {partition: GPU}\n"))
    assert ok is False
    assert any("must be named `job_profiles:`" in ln for ln in lines)


def test_empty_job_profiles_is_pinpointed(tmp_path: Path) -> None:
    ok, lines = diagnose(_proj(tmp_path, "version: config-v1\njob_profiles:\n"))
    assert ok is False
    assert any("EMPTY" in ln for ln in lines)
    assert any("indented as its children" in ln for ln in lines)


def test_yaml_error_is_reported(tmp_path: Path) -> None:
    ok, lines = diagnose(_proj(tmp_path,
        "version: config-v1\njob_profiles:\n\tgpu: x\n"))  # tab → YAML error
    assert ok is False
    assert any("YAML parse error" in ln for ln in lines)


def test_cli_command_runs_and_exits_nonzero_on_failure(tmp_path: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    from botainer.cli.hpc import hpc
    _proj(tmp_path, "version: config-v1\nprofiles:\n  gpu: {partition: GPU}\n")
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(hpc, ["jobs-doctor"])
    assert res.exit_code == 1
    assert "job-dispatch diagnosis" in res.output
    assert "job_profiles" in res.output


def test_config_rejects_job_profiles_nested_under_plugins() -> None:
    """The silent-swallow gap: `job_profiles` (or any top-level field) misplaced
    under the free-form `plugins:` dict must now be REJECTED at parse, not
    silently ignored."""
    import pytest
    from pydantic import ValidationError

    from botainer.core.config import ProjectConfig

    with pytest.raises(ValidationError) as exc:
        ProjectConfig.model_validate(
            {"plugins": {"job_profiles": {"gpu": {"partition": "GPU"}}}})
    assert "job_profiles" in str(exc.value) and "TOP-LEVEL" in str(exc.value)
    # a resources/network misplacement is caught too
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate({"plugins": {"resources": {"cpu": 4}}})
    # legit third-party plugin config is NOT rejected
    ProjectConfig.model_validate({"plugins": {"git": {"mode": "guarded"}}})
    # correct top-level placement passes
    assert len(ProjectConfig.model_validate(
        {"job_profiles": {"gpu": {"partition": "GPU"}}}).job_profiles) == 1
