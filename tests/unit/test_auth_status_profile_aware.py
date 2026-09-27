"""`auth status` reports the profile the project USES, and what kind of credential.

Three defects, found 2026-08-28 while answering "are we clear and transparent
about profiles?" The honest answer was no, in three separate ways:

1. the per-project path was built from the literal string "default" and the
   function never loaded the project config at all — so a project on
   `profile: work` was told about a slot it does not use;
2. `except Exception: pass` wrapped the whole lookup, so an identity refusal and
   a genuine bug produced identical output: no line at all, no reason;
3. the credential PATH was printed but never its KIND — and for codex,
   `isolated` mode accepts either a pasted API key or an OAuth login, so the
   mode name does not answer the question either.

Observed together: a project that WAS signed in reported as not signed in, and
offered `botainer auth login --shared` — the wrong mode, for a project already
holding a working isolated credential.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from botainer.cli.auth import _credential_kind, _read_project_profile, collect_auth_rows


def _project(tmp_path: Path, monkeypatch, *, profile: str,
             plugins: tuple[str, ...] = ("agent-claude",)) -> Path:
    """A project whose config names a profile, with real state behind it."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    uid = "2833fc11-6d6c-4a62-b162-d8f3dae4d09d"
    (proj / ".botainer" / "project-id").write_text(uid)
    (proj / ".botainer" / "config.yaml").write_text(
        'version: "1"\nagent: claude\n'
        f"profile: {profile}\nimage: x:1\n"
        "plugins_enabled:\n" + "".join(f"  - {p}\n" for p in plugins)
    )

    from botainer.state import dir as _state_dir
    root = _state_dir.ensure_user_state_dir(create_if_missing=True).root
    proj_state = root / "state" / uid
    proj_state.mkdir(parents=True, exist_ok=True)
    # meta.json must exist or identity resolution refuses — which the command
    # now REPORTS rather than swallowing, but that is a different test.
    (proj_state / "meta.json").write_text(json.dumps(
        {"project_uuid": uid, "last_path": str(proj), "path_history": []}))
    return proj


def _row(rows, family):
    return next(r for r in rows if r["family"] == family)


def test_the_profile_the_project_uses_is_the_one_reported(tmp_path, monkeypatch) -> None:
    """The credential exists ONLY in `work`. On the old code the lookup went to
    `default`, found nothing, and reported a signed-in project as signed out."""
    proj = _project(tmp_path, monkeypatch, profile="work")
    creds = (tmp_path / "state" / "state"
             / "2833fc11-6d6c-4a62-b162-d8f3dae4d09d" / "data"
             / "agent-claude" / "profiles" / "work")
    creds.mkdir(parents=True)
    (creds / ".credentials.json").write_text('{"claudeAiOauth": {}}')

    row = _row(collect_auth_rows(proj), "anthropic")

    assert row["profile"] == "work"
    assert row["per_project_creds_present"] is True, (
        "the credential is right there in the profile the project uses"
    )
    assert "profiles/work" in str(row["per_project_creds_path"])
    assert "profiles/default" not in str(row["per_project_creds_path"])


def test_a_default_profile_still_resolves_to_default(tmp_path, monkeypatch) -> None:
    """The common case must not regress while fixing the uncommon one."""
    proj = _project(tmp_path, monkeypatch, profile="default")
    assert _row(collect_auth_rows(proj), "anthropic")["profile"] == "default"


@pytest.mark.parametrize("filename,expected", [
    (".credentials.json", "account login (OAuth)"),
    ("auth.json", "account login (OAuth)"),
    ("api_key", "API key (pasted)"),
])
def test_the_credential_KIND_is_named_not_just_its_path(filename, expected) -> None:
    """The mode name does not answer this and the path only answers it to
    someone who already knows what these filenames mean.

    codex's `isolated` accepts EITHER a pasted key or an OAuth login, so moving
    between them is a re-login inside one mode rather than a mode switch — and
    nothing said so.
    """
    assert _credential_kind(filename) == expected


def test_a_pasted_key_and_an_account_are_told_apart_in_a_real_row(
        tmp_path, monkeypatch) -> None:
    proj = _project(tmp_path, monkeypatch, profile="work",
                    plugins=("agent-claude", "agent-codex"))
    base = (tmp_path / "state" / "state"
            / "2833fc11-6d6c-4a62-b162-d8f3dae4d09d" / "data")
    (base / "agent-claude" / "profiles" / "work").mkdir(parents=True)
    (base / "agent-claude" / "profiles" / "work" / ".credentials.json").write_text("{}")
    (base / "agent-codex" / "profiles" / "work").mkdir(parents=True)
    (base / "agent-codex" / "profiles" / "work" / "api_key").write_text("sk-x")

    rows = collect_auth_rows(proj)

    assert _row(rows, "anthropic")["per_project_creds_kind"] == "account login (OAuth)"
    assert _row(rows, "openai")["per_project_creds_kind"] == "API key (pasted)"


def test_an_abandoned_lookup_says_so_instead_of_rendering_a_blank(
        tmp_path, monkeypatch) -> None:
    """`except Exception: pass` made a refusal and a bug indistinguishable.

    Reproduced through the real cause rather than a stub: a state directory with
    prior content and no meta.json is an identity refusal, and the command used
    to print no per-project line at all with nothing saying why.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    uid = "2833fc11-6d6c-4a62-b162-d8f3dae4d09d"
    (proj / ".botainer" / "project-id").write_text(uid)
    (proj / ".botainer" / "config.yaml").write_text(
        'version: "1"\nagent: claude\nprofile: work\nimage: x:1\n'
        "plugins_enabled:\n  - agent-claude\n")

    from botainer.state import dir as _state_dir
    root = _state_dir.ensure_user_state_dir(create_if_missing=True).root
    stale = root / "state" / uid / "data" / "agent-claude" / "profiles" / "work"
    stale.mkdir(parents=True)          # prior state, and NO meta.json

    row = _row(collect_auth_rows(proj), "anthropic")

    assert row["per_project_skipped"], (
        "the lookup was abandoned and the user must be told, not shown a blank"
    )
    assert "identity" in str(row["per_project_skipped"]).lower()
    assert row["profile"] == "work", "and which profile it was looking for"


def test_a_project_with_no_config_falls_back_to_default(tmp_path) -> None:
    """`_read_project_profile` is called before anything is known to exist."""
    assert _read_project_profile(tmp_path / "nope") == "default"
    (tmp_path / ".botainer").mkdir()
    (tmp_path / ".botainer" / "config.yaml").write_text("not: valid: yaml: [")
    assert _read_project_profile(tmp_path) == "default"


# ── `auth profiles`: the thing that made profiles discoverable at all ────────


def test_profiles_are_listed_from_the_directories_that_ARE_the_registry(
        tmp_path, monkeypatch) -> None:
    """Nothing listed profiles before this. They are created by typing a name
    and a directory appears, so a user discovered one only by remembering they
    had typed it — while §4cm treats each as an ACCOUNT boundary.

    There is no registry to read: the directories are it.
    """
    from botainer.cli.auth import collect_profiles

    proj = _project(tmp_path, monkeypatch, profile="work",
                    plugins=("agent-claude", "agent-codex"))
    base = (tmp_path / "state" / "state"
            / "2833fc11-6d6c-4a62-b162-d8f3dae4d09d" / "data")
    (base / "agent-claude" / "profiles" / "work").mkdir(parents=True)
    (base / "agent-claude" / "profiles" / "work" / ".credentials.json").write_text("{}")
    (base / "agent-claude" / "profiles" / "personal").mkdir()
    (base / "agent-claude" / "broker-state" / "work").mkdir(parents=True)
    (base / "agent-codex" / "profiles" / "work").mkdir(parents=True)
    (base / "agent-codex" / "profiles" / "work" / "api_key").write_text("sk-x")

    rows = collect_profiles(proj)
    by = {(r["agent"], r["where"], r["name"]): r for r in rows}

    assert ("agent-claude", "profiles", "personal") in by, (
        "a profile with no credential yet is still a profile that exists"
    )
    assert by[("agent-claude", "profiles", "work")]["credential"] == (
        "account login (OAuth)")
    assert by[("agent-codex", "profiles", "work")]["credential"] == (
        "API key (pasted)")
    assert by[("agent-claude", "broker-state", "work")]["where"] == "broker-state", (
        "broker keeps the same profile name in a different place; hiding that "
        "would make a broker project look profile-less"
    )
    assert by[("agent-claude", "profiles", "work")]["active"] is True
    assert by[("agent-claude", "profiles", "personal")]["active"] is False


def test_the_superseded_copies_are_not_mistaken_for_profiles(
        tmp_path, monkeypatch) -> None:
    """A carry leaves `<profile>.superseded-<ts>/` right beside the profile.

    Listing those as profiles would invite someone to `config set profile
    work.superseded-20260828T000000Z`, which is a directory of dead history and
    no credential.
    """
    from botainer.cli.auth import collect_profiles

    proj = _project(tmp_path, monkeypatch, profile="work")
    base = (tmp_path / "state" / "state"
            / "2833fc11-6d6c-4a62-b162-d8f3dae4d09d" / "data"
            / "agent-claude" / "profiles")
    (base / "work").mkdir(parents=True)
    (base / "work.superseded-20260828T000000Z").mkdir()
    (base / "work.archived-20260828T000000Z").mkdir()

    names = {r["name"] for r in collect_profiles(proj)}
    assert names == {"work"}, names


def test_the_note_comes_from_project_config_not_the_profile_directory(
        tmp_path, monkeypatch) -> None:
    """Load-bearing: the profile directory is bound into the container at
    /home/agent/.claude and the agent WRITES there. A note kept in it would be
    a label the agent could forge — and this label's whole job is to inform an
    account decision. Project config is host-side and read-only to the cage.
    """
    from botainer.cli.auth import collect_profiles

    proj = _project(tmp_path, monkeypatch, profile="work")
    cfg = proj / ".botainer" / "config.yaml"
    cfg.write_text(cfg.read_text()
                   + "profile_notes:\n  work: university account\n")
    prof = (tmp_path / "state" / "state"
            / "2833fc11-6d6c-4a62-b162-d8f3dae4d09d" / "data"
            / "agent-claude" / "profiles" / "work")
    prof.mkdir(parents=True)
    # What a compromised session might plant. It must not be read.
    (prof / "description").write_text("personal account, safe to use")
    (prof / ".botainer-profile.json").write_text('{"description": "forged"}')

    row = next(r for r in collect_profiles(proj) if r["name"] == "work")

    assert row["note"] == "university account"
    assert "forged" not in str(row["note"])
    assert "safe to use" not in str(row["note"])


def test_a_profile_note_must_be_one_short_line(tmp_path) -> None:
    """It is printed into a listing. Not a security control — the value is
    host-side and grants nothing — but a note with a newline wrecks the table
    it appears in, and a config that renders badly gets ignored."""
    from botainer.core.config import ProjectConfig

    ProjectConfig(profile_notes={"work": "university account"})

    with pytest.raises(Exception) as multiline:
        ProjectConfig(profile_notes={"work": "line one\nline two"})
    assert "newline" in str(multiline.value)

    with pytest.raises(Exception) as toolong:
        ProjectConfig(profile_notes={"work": "x" * 201})
    assert "200" in str(toolong.value)


def test_profile_notes_default_to_empty_and_are_optional(tmp_path) -> None:
    """Creating a profile needs only a name; its note is optional and empty by default."""
    from botainer.core.config import ProjectConfig

    assert ProjectConfig().profile_notes == {}


# ── `auth use` switches what the PROJECT has, not what the HOST installed ────


def _run_auth_use(proj: Path, args: list[str], answer: str = "n\n"):
    """Drive the real command with real keystrokes, from inside the project."""
    import os
    from click.testing import CliRunner

    from botainer.cli.auth import auth

    cwd = os.getcwd()
    os.chdir(proj)
    try:
        result = CliRunner().invoke(auth, args, input=answer,
                                    catch_exceptions=False)
    finally:
        os.chdir(cwd)
    text = result.output or ""
    try:
        if result.stderr:
            text += result.stderr
    except ValueError:
        pass
    return result, text


def test_a_bare_switch_does_not_enable_a_family_the_project_never_had(
        tmp_path, monkeypatch) -> None:
    """#185, observed live: `auth use broker` on a claude-only project also
    enabled agent-codex-broker, because the default target was every family
    INSTALLED ON THE HOST.

    The user asked to change a mode and got a family they never mentioned,
    finding out later at `start` from a notice that only makes sense to someone
    who already knew. "Switch" presupposes something to switch.
    """
    proj = _project(tmp_path, monkeypatch, profile="default",
                    plugins=("agent-claude",))

    _result, text = _run_auth_use(proj, ["use", "broker"], answer="y\n")

    # Assert on the CONFIG, not the console text: what the bug did was write a
    # plugin into this file, and that is the thing that must not happen.
    enabled = (proj / ".botainer" / "config.yaml").read_text()
    assert "agent-claude-broker" in enabled, "the family it DOES have switches"
    assert "agent-claude\n" not in enabled, "and its old variant is disabled"
    assert "agent-codex" not in enabled, (
        "a family this project never enabled must not be written into its "
        "config by a command asked only to change a mode"
    )
    assert "agent-codex" not in text, "nor shown in the diff"


def test_naming_a_family_explicitly_still_adds_it_and_says_so(
        tmp_path, monkeypatch) -> None:
    """Asking for a family by name is asking for it — but quietly gaining one
    is the defect, so the ADD is announced rather than rendered as a switch."""
    proj = _project(tmp_path, monkeypatch, profile="default",
                    plugins=("agent-claude",))

    _result, text = _run_auth_use(proj, ["use", "broker", "--family", "openai"],
                                  answer="y\n")

    enabled = (proj / ".botainer" / "config.yaml").read_text()
    assert "agent-codex-broker" in enabled, "naming a family is asking for it"
    assert "agent-claude" in enabled, (
        "and the family that WAS there is untouched — this is an add, not a "
        "replace"
    )
    assert "will ADD it, not switch it" in text, (
        "quietly gaining a family is the defect, so the add is announced"
    )


def test_a_project_with_no_agent_family_is_refused_with_the_way_out(
        tmp_path, monkeypatch) -> None:
    """Previously this silently enabled every installed family at once —
    turning "switch my mode" into "install everything"."""
    proj = _project(tmp_path, monkeypatch, profile="default", plugins=("git",))

    result, text = _run_auth_use(proj, ["use", "broker"])

    assert result.exit_code != 0
    assert "no agent family is enabled" in text
    assert "plugin enable" in text, "a refusal has to name the way out"
