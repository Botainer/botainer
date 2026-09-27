"""A supplied cosmetic name must survive native init and catalog reads.

The original flag test covered help only: init accepted the option but never
stored it. These tests invoke the real command and inspect its resulting state.
They do not launch a container, scheduler job, plugin hook or agent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.init import init
from botainer.cli.list_cmd import list_
from botainer.core import identity
from botainer.core.refusal import RefusalCategory, Refused


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project, tmp_path / "state"


def _init(*args: str):
    return CliRunner().invoke(init, ["--non-interactive", *args])


def _catalog():
    result = CliRunner().invoke(list_, ["--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _meta(project: Path, state: Path):
    uid = (project / ".botainer" / "project-id").read_text().strip()
    path = state / "state" / uid / "meta.json"
    return path, json.loads(path.read_text())


def test_init_accepts_name_flag(tmp_path: Path) -> None:
    runner = CliRunner()
    # We invoke with --help to avoid actually creating a project; --help
    # exit code is 0 and Click parses all options first.
    result = runner.invoke(init, ["--help"])
    assert result.exit_code == 0
    assert "--name" in result.output
    assert "Friendly project name" in result.output


@pytest.mark.parametrize("runtime", ["docker", "apptainer"])
def test_name_round_trips_through_native_init_and_list(isolated_project, runtime):
    project, state = isolated_project
    result = _init("--runtime", runtime, "--name", "  Étude Δ / phase 2  ")

    assert result.exit_code == 0, result.output
    _, meta = _meta(project, state)
    assert meta["display_name"] == "Étude Δ / phase 2"
    catalog = _catalog()
    assert len(catalog) == 1
    assert catalog[0]["display_name"] == meta["display_name"]
    assert catalog[0]["uuid"] == meta["uuid"]
    assert catalog[0]["last_path"] == str(project.resolve())
    config = yaml.safe_load((project / ".botainer" / "config.yaml").read_text())
    assert config["runtime"] == runtime
    assert "display_name" not in config


def test_omitted_name_keeps_folder_fallback(isolated_project):
    project, state = isolated_project
    result = _init("--runtime", "docker")

    assert result.exit_code == 0, result.output
    _, meta = _meta(project, state)
    assert "display_name" not in meta
    assert _catalog()[0]["display_name"] == project.name


def test_existing_project_name_update_keeps_config_uuid_and_state(isolated_project):
    project, state = isolated_project
    first = _init("--runtime", "apptainer", "--name", "Original")
    assert first.exit_code == 0, first.output
    meta_path, meta = _meta(project, state)
    meta["custom_metadata"] = {"keep": "this"}
    meta_path.write_text(json.dumps(meta))
    config = project / ".botainer" / "config.yaml"
    config.write_text(config.read_text() + "\n# retained hand edit\n")
    before_config = config.read_bytes()
    before_id = (project / ".botainer" / "project-id").read_bytes()
    retained = meta_path.parent / "sessions" / "fixture-receipt.txt"
    retained.write_text("retained session evidence\n")

    result = _init("--name", "New name")

    assert result.exit_code == 0, result.output
    _, after = _meta(project, state)
    assert after["display_name"] == "New name"
    assert after["uuid"] == meta["uuid"]
    assert after["created_at"] == meta["created_at"]
    assert after["custom_metadata"] == {"keep": "this"}
    assert config.read_bytes() == before_config
    assert (project / ".botainer" / "project-id").read_bytes() == before_id
    assert retained.read_text() == "retained session evidence\n"
    assert not config.with_name("config.yaml.bak").exists()
    assert _catalog()[0]["display_name"] == "New name"


@pytest.mark.parametrize("extra_args", [[], ["--force"]])
def test_reinit_without_name_preserves_existing_display_name(isolated_project, extra_args):
    project, state = isolated_project
    first = _init("--runtime", "docker", "--name", "Chosen name")
    assert first.exit_code == 0, first.output
    result = _init(*extra_args)

    assert result.exit_code == 0, result.output
    assert _meta(project, state)[1]["display_name"] == "Chosen name"
    assert _catalog()[0]["display_name"] == "Chosen name"


@pytest.mark.parametrize("name", [
    "", "   ", "x" * 201, "line\nbreak", "carriage\rreturn", "tab\tname",
    "\x1b]0;title\x07", "delete\x7f", "next\x85line", "split\u2028line",
    "paragraph\u2029break", "reverse\u202eevil", "isolate\u2066text\u2069",
])
def test_invalid_name_refuses_before_creating_any_project_or_state(isolated_project, name):
    project, state = isolated_project
    result = _init("--runtime", "docker", "--name", name)

    assert result.exit_code == 2, result.output
    assert "project name" in result.output
    assert not (project / ".botainer").exists()
    assert not state.exists()


def test_invalid_name_with_force_preserves_existing_files(isolated_project):
    project, state = isolated_project
    first = _init("--runtime", "docker", "--name", "Keep name")
    assert first.exit_code == 0, first.output
    before = {p: p.read_bytes() for root in [project, state] for p in root.rglob("*") if p.is_file()}

    result = _init("--force", "--name", "bad\nname")

    assert result.exit_code == 2, result.output
    after = {p: p.read_bytes() for root in [project, state] for p in root.rglob("*") if p.is_file()}
    assert after == before


def test_identity_entrypoint_rejects_control_name_before_state_creation(isolated_project):
    project, state = isolated_project
    with pytest.raises(Refused) as caught:
        identity.init_project(project, agent="claude", force=False,
                              non_interactive=True, name="\x00name")
    assert caught.value.category == RefusalCategory.CONFIG_INVALID
    assert not (project / ".botainer").exists()
    assert not state.exists()


def test_explicit_name_does_not_change_after_move(isolated_project):
    project, state = isolated_project
    first = _init("--runtime", "docker", "--name", "Stable name")
    assert first.exit_code == 0, first.output
    uid = _meta(project, state)[1]["uuid"]
    moved = project.with_name("moved-project")
    project.rename(moved)

    resolved, _ = identity.resolve_identity(moved, identity_accept=False)

    assert resolved == uid
    row = _catalog()[0]
    assert row["display_name"] == "Stable name"
    assert row["last_path"] == str(moved.resolve())


def test_duplicate_display_names_are_not_identity(isolated_project, monkeypatch):
    project, _ = isolated_project
    first = _init("--runtime", "docker", "--name", "Shared label")
    assert first.exit_code == 0, first.output
    other = project.with_name("other-project")
    other.mkdir()
    monkeypatch.chdir(other)
    second = _init("--runtime", "apptainer", "--name", "Shared label")

    assert second.exit_code == 0, second.output
    rows = _catalog()
    assert len(rows) == 2
    assert {row["display_name"] for row in rows} == {"Shared label"}
    assert len({row["uuid"] for row in rows}) == 2


@pytest.mark.parametrize("name", ["x" * 200, "מחקר 👩\u200d🔬", "$(touch name-marker); 'quoted' / text"])
def test_names_remain_literal_unicode_data(isolated_project, name):
    project, state = isolated_project
    result = _init("--runtime", "docker", "--name", name)

    assert result.exit_code == 0, result.output
    assert _meta(project, state)[1]["display_name"] == name
    assert _catalog()[0]["display_name"] == name
    assert not (project / "name-marker").exists()


def test_name_updates_do_not_cross_state_roots(isolated_project, monkeypatch):
    project, first_state = isolated_project
    first = _init("--runtime", "apptainer", "--name", "First catalog name")
    assert first.exit_code == 0, first.output
    first_meta_path, first_meta = _meta(project, first_state)
    before = first_meta_path.read_bytes()
    second_state = first_state.with_name("second-state")
    monkeypatch.setenv("MY_BOTAINER", str(second_state))

    second = _init("--name", "Second catalog name")

    assert second.exit_code == 0, second.output
    row = _catalog()[0]
    assert row["display_name"] == "Second catalog name"
    assert row["uuid"] == first_meta["uuid"]
    assert (project / ".botainer" / "project-id").read_text().strip() == first_meta["uuid"]
    assert first_meta_path.read_bytes() == before
    monkeypatch.setenv("MY_BOTAINER", str(first_state))
    assert _catalog()[0]["display_name"] == "First catalog name"
