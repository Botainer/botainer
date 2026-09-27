"""A credential in `broker-state/` must be REPORTED, not denied.

THE DEFECT, measured through the installed CLI on a real `botainer init`
project with a real-shaped `.credentials.json` planted in
`broker-state/default`:

    botainer auth status    →  "✗ per-project (default): not present"
    botainer auth doctor    →  "No agent-claude credentials found anywhere
                                under this root."
    botainer auth profiles  →  "default  account login (OAuth) (broker state)"

Two of the three surfaces did not merely miss the file — they DENIED it, and
`auth doctor`'s is an explicit universal claim. Only `auth profiles` saw it.

`collect_auth_rows` looked solely in `profiles/<profile>`, and its own comment
justified that by assuming "a broker project holds no per-project credential by
design" — the very property the check should establish. The directory is bound
**rw** into the container, so "by design" described an intention, not an
invariant. `auth_doctor._collect` had the same one-directory walk.

WHY THIS MATTERS MORE THAN A MISSING LINE OF OUTPUT: `broker-state/` is bound
into the container on the next launch. Broker mode's whole premise is that the
container is given a sentinel and never a real token. A credential sitting in
that directory defeats that premise, and the two commands a user would run to
find out told them there was nothing there.

The diagnostic must actually scan broker-state for credential files; a
comment asserting that it does is not evidence. These tests exercise the
reported discovery and remediation through the real command.

"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from botainer.cli import auth as auth_mod
from botainer.cli import auth_doctor as doctor_mod

_PLANTED = json.dumps(
    {"claudeAiOauth": {"refreshToken": "sk-ant-ort01-PLANTED-NOT-REAL"}})


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A state root with one project, shaped like a real one.

    Built from the real directory layout rather than the minimum that makes the
    code path run: `state/<uuid>/data/agent-claude/{profiles,broker-state}/`,
    both present, because a fixture with only the directory under test cannot
    show that the OTHER one still works.
    """
    state = tmp_path / "st"
    uuid = "c0df6571-c651-4779-9527-6893e3204441"
    proj_state = state / "state" / uuid
    agent = proj_state / "data" / "agent-claude"
    (agent / "profiles" / "default").mkdir(parents=True)
    (agent / "broker-state" / "default").mkdir(parents=True)
    (state / "shared-auth" / "agent-claude").mkdir(parents=True)
    # meta.json is REQUIRED, and leaving it out is not a harmless shortcut:
    # `resolve_identity` refuses a state dir with prior content and no meta as
    # tampering, which aborts the whole per-project block — including the scan
    # under test. The first version of this fixture omitted it and the test
    # failed for that reason rather than the one it is about. A fixture must
    # match a real install, not the minimum that makes the code path run.
    (proj_state / "meta.json").write_text(
        '{"agent": "claude", "created_at": "2026-09-11T00:00:00Z", '
        f'"path_history": ["{tmp_path / "proj"}"]}}')
    monkeypatch.setenv("MY_BOTAINER", str(state))
    return agent


def _plant(agent_dir, kind="broker-state", profile="default"):
    f = agent_dir / kind / profile / ".credentials.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(_PLANTED)
    f.chmod(0o600)
    return f


def _doctor():
    return CliRunner().invoke(
        doctor_mod.auth_doctor, [], catch_exceptions=False)


# ───────────────────────── auth doctor ─────────────────────────

def test_doctor_does_not_DENY_a_credential_that_is_there(root):
    """THE DEFECT. A universal claim, falsified by one planted file."""
    planted = _plant(root)

    result = _doctor()

    assert "found anywhere under this root" not in result.output, (
        f"`auth doctor` denied, in a UNIVERSAL claim, a credential at "
        f"{planted}:\n{result.output}")
    assert str(planted) in result.output, (
        f"the credential is not reported at all:\n{result.output}")


def test_doctor_SAYS_the_credential_is_in_broker_state(root):
    """Reporting it is not enough — where it is IS the finding.

    A credential under `profiles/` is an ordinary isolated-mode login. The same
    file under `broker-state/` is pollution of a directory whose stated
    contract is that it holds none. Listing both identically would report the
    file and hide the problem.
    """
    _plant(root)

    result = _doctor()

    assert "broker-state" in result.output, result.output
    assert "not supposed to hold one" in result.output, (
        f"`auth doctor` lists it as though it were a normal login:\n"
        f"{result.output}")


def test_doctor_on_a_TRULY_empty_root_still_says_so(root):
    """THE CONTROL, and it is mutation-proven rather than asserted.

    If the fix reported a holder unconditionally — or scanned a directory that
    is always present and counted it — this fails. A control that cannot fail
    is what halted this loop once already, so this one is checked against the
    mutation "always append a holder" and does fail by name.
    """
    result = _doctor()

    assert "No agent-claude credentials found anywhere under this root" in result.output, (
        f"claimed to find a credential in a root that has none:\n{result.output}")
    assert "broker-state" not in result.output, result.output


def test_doctor_still_finds_the_ORDINARY_profiles_credential(root):
    """The half that already worked. Without this the fix could regress it."""
    planted = _plant(root, kind="profiles")

    result = _doctor()

    assert str(planted) in result.output, result.output
    assert "not supposed to hold one" not in result.output, (
        "a normal per-project login was labelled as pollution")


def test_doctor_scans_EVERY_profile_not_just_default(root):
    """A stray under an unused profile is bound the moment you switch back.

    Scanning only the project's current profile would leave a credential
    dormant-but-present, and it would become live again with one
    `botainer config set profile`.
    """
    planted = _plant(root, profile="other-account")

    result = _doctor()

    assert str(planted) in result.output, (
        f"a credential under a non-default profile was missed:\n{result.output}")


# ───────────────────────── auth status ─────────────────────────

def _status_rows(project_root=None):
    return auth_mod.collect_auth_rows(project_root=project_root)


def test_status_collector_reports_the_stray(root, tmp_path, monkeypatch):
    """`auth status` is where a user looks first, so it must not say 'absent'."""
    planted = _plant(root)
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "c0df6571-c651-4779-9527-6893e3204441\n")
    (proj / ".botainer" / "config.yaml").write_text("agent: claude\n")

    rows = _status_rows(project_root=proj)
    anthropic = [r for r in rows if r["family"] == "anthropic"]
    assert anthropic, f"no anthropic row at all: {[r['family'] for r in rows]}"

    assert str(planted) in anthropic[0]["stray_broker_creds"], (
        f"`auth status` did not surface the stray credential. It reports "
        f"per-project as "
        f"{anthropic[0]['per_project_creds_present']!r}, which is about "
        f"`profiles/` and says nothing about this file.\n"
        f"{anthropic[0]}")


def test_status_collector_is_SILENT_when_broker_state_is_clean(root, tmp_path):
    """The control for the status side.

    An alarm that fires on every launch is the scenery problem this project has
    a rule about — and `broker-state/` exists on every broker project, so a
    check keyed on the DIRECTORY rather than its CONTENTS would fire always.
    """
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "c0df6571-c651-4779-9527-6893e3204441\n")
    (proj / ".botainer" / "config.yaml").write_text("agent: claude\n")

    rows = _status_rows(project_root=proj)
    anthropic = [r for r in rows if r["family"] == "anthropic"]

    assert anthropic[0]["stray_broker_creds"] == [], (
        f"reported a stray credential in a clean broker-state dir: "
        f"{anthropic[0]['stray_broker_creds']}")


def test_status_scans_EVERY_profile_not_just_the_current_one(root, tmp_path):
    """GAP FOUND BY MUTATION, not by reading.

    The doctor side has this test; the status side did not, and the mutation
    `for _prof in [broker_root / project_profile]` passed all eight. A stray
    credential under a profile the project is not currently using is dormant,
    not gone — one `botainer config set profile` makes it live, and the command
    that was supposed to warn would have been looking at the wrong directory.
    """
    planted = _plant(root, profile="an-old-account")
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "c0df6571-c651-4779-9527-6893e3204441\n")
    (proj / ".botainer" / "config.yaml").write_text("agent: claude\n")

    rows = _status_rows(project_root=proj)
    anthropic = [r for r in rows if r["family"] == "anthropic"]

    assert str(planted) in anthropic[0]["stray_broker_creds"], (
        f"a stray under the non-active profile 'an-old-account' was missed; "
        f"got {anthropic[0]['stray_broker_creds']}")


def test_status_ignores_non_credential_files_in_broker_state(root, tmp_path):
    """broker-state is SUPPOSED to hold these. Flagging them is the false
    positive that would make the real warning unreadable."""
    for name in (".claude.json", "history.jsonl", "settings.json"):
        (root / "broker-state" / "default" / name).write_text("{}")
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "c0df6571-c651-4779-9527-6893e3204441\n")
    (proj / ".botainer" / "config.yaml").write_text("agent: claude\n")

    rows = _status_rows(project_root=proj)
    anthropic = [r for r in rows if r["family"] == "anthropic"]

    assert anthropic[0]["stray_broker_creds"] == [], (
        f"session state was reported as a credential: "
        f"{anthropic[0]['stray_broker_creds']}")


# ───────────────────────────────────────────────────────────────────────────
# Everything below was added after a refuting review. Each one closes a hole
# the nine tests above did not: three mutations passed all of them, and one
# of my own fixes was a regression that none of them saw.
# ───────────────────────────────────────────────────────────────────────────

def _status_cli(proj):
    from botainer.cli import auth as _a
    return CliRunner().invoke(_a.auth_status, [], catch_exceptions=False)


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "c0df6571-c651-4779-9527-6893e3204441\n")
    (proj / ".botainer" / "config.yaml").write_text(
        "agent: claude\nplugins_enabled:\n  - agent-claude-broker\n")
    return proj


def test_auth_status_the_COMMAND_prints_the_warning(root, tmp_path, monkeypatch):
    """The whole renderer was unprotected, and a mutation proved it.

    Every test above calls `collect_auth_rows` directly. Deleting the entire
    warning block from `auth status` — so the command printed its exact
    pre-fix output — left all nine of them green, because the data was still
    in the dict nobody rendered. A finding that reaches no surface is the
    void this queue keeps rediscovering; the fix has to be asserted where the
    user would actually read it.
    """
    planted = _plant(root)
    proj = _project(tmp_path)
    monkeypatch.chdir(proj)

    result = _status_cli(proj)

    assert str(planted) in result.output, (
        f"`auth status` did not PRINT the stray credential, whatever the "
        f"collector returned:\n{result.output}")
    assert "broker-state" in result.output, result.output


def test_auth_status_the_COMMAND_is_quiet_when_clean(root, tmp_path, monkeypatch):
    """The control for the rendered surface, not just for the collector."""
    proj = _project(tmp_path)
    monkeypatch.chdir(proj)

    result = _status_cli(proj)

    assert "broker-state" not in result.output, (
        f"warned about broker-state on a project with nothing in it:\n"
        f"{result.output}")


def test_the_warning_does_not_claim_a_bind_that_is_not_happening(
        root, tmp_path, monkeypatch):
    """It said "bound into the container on the next launch". Not in isolated mode.

    Measured by a reviewer: switch the project off broker mode and the stray
    stays in `broker-state/` while the launch binds `profiles/` instead. The
    warning still asserted the bind. A true sentence about the wrong mode is
    still a false statement to the person reading it.
    """
    _plant(root)
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(
        "c0df6571-c651-4779-9527-6893e3204441\n")
    (proj / ".botainer" / "config.yaml").write_text(
        "agent: claude\nplugins_enabled:\n  - agent-claude\n")
    monkeypatch.chdir(proj)

    result = _status_cli(proj)

    assert "NOT bound right now" in result.output, (
        f"the project is not in broker mode, so that directory is not bound; "
        f"the warning must not imply it is:\n{result.output}")


def test_the_warning_admits_it_did_not_OPEN_the_files(root, tmp_path, monkeypatch):
    """It matches NAMES. An empty logged-out stub looks identical.

    Calling a filename match a credential finding is the same overclaim as a
    count that implies completeness. Say which check ran.
    """
    _plant(root)
    proj = _project(tmp_path)
    monkeypatch.chdir(proj)

    result = _status_cli(proj)

    assert "has not opened" in result.output, (
        f"the warning asserts more than the check performed:\n{result.output}")


def test_a_SYMLINKED_profile_dir_is_still_reported(root, tmp_path):
    """A REGRESSION I INTRODUCED, caught by a reviewer and not by me.

    Adding the broker-state walk, I copied `if not prof.is_dir() or
    prof.is_symlink(): continue` from `where.py`, where skipping symlinks is
    correct (disk reclamation). Here it is a blind spot: `pre_session.py` binds
    the profile path whether or not it is a symlink, so the credential is
    delivered — and `auth doctor` went from listing that holder to printing
    "No agent-claude credentials found anywhere under this root".

    Making a command report MORE must not quietly make it report less.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / ".credentials.json").write_text(_PLANTED)
    link = root / "profiles" / "work-account"
    link.symlink_to(outside, target_is_directory=True)

    result = _doctor()

    assert "found anywhere under this root" not in result.output, (
        f"a symlinked profile dir made the whole report claim emptiness:\n"
        f"{result.output}")
    assert "work-account" in result.output, result.output


def test_a_credential_NESTED_below_the_profile_dir_is_found(root):
    """The profile directory is bound WHOLE, so depth changes nothing."""
    nested = root / "broker-state" / "default" / "backup" / ".credentials.json"
    nested.parent.mkdir(parents=True)
    nested.write_text(_PLANTED)

    result = _doctor()

    assert str(nested) in result.output, (
        f"a credential one directory deeper was missed, though it is bound "
        f"exactly the same:\n{result.output}")


def test_the_pre_shared_BACKUP_spelling_is_found(root):
    """`history_carry`'s own docs say the `.pre-shared` backups ARE credentials.

    The first version matched `CREDENTIAL_FILENAMES` exactly, so the backup a
    mode switch leaves behind was invisible to both surfaces.
    """
    backup = root / "broker-state" / "default" / ".credentials.json.pre-shared"
    backup.write_text(_PLANTED)

    result = _doctor()

    assert str(backup) in result.output, result.output


def test_a_lock_file_is_NOT_called_a_credential(root):
    """The other half of that decision, so it cannot drift into over-matching.

    `history_carry._is_credential_name` also has a PREFIX rule, which would
    make `.credentials.json.lock` read as a leaked secret. The shared helper
    deliberately does not use it.
    """
    (root / "broker-state" / "default" / ".credentials.json.lock").write_text("")

    result = _doctor()

    assert "No agent-claude credentials found anywhere under this root" in result.output, (
        f"a lock file was reported as a credential:\n{result.output}")


def test_the_doctor_does_not_tell_you_to_LAUNCH_the_polluted_project(root):
    """Remediation must not advise launching a credential-polluted broker state.

    The new holder's label contained "project ", so it fell into
    `broken_links` and the verdict reached the mount-mode remedy: "Run those
    projects before any other, so the newest token is the one that survives."
    Running a polluted broker project is precisely the action that hands the
    credential to the agent. Classification now comes from a typed flag rather
    than from display text, so the advice cannot follow the label around.
    """
    _plant(root)
    shared = root.parent.parent.parent.parent / "shared-auth" / "agent-claude"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / ".credentials.json").write_text(_PLANTED)

    result = _doctor()

    assert "Run those projects before any other" not in result.output, (
        f"the remedy tells the user to launch the polluted project:\n"
        f"{result.output}")
    assert "NO ACTION IS NEEDED" not in result.output.split(
        "broker-state")[0][-400:], result.output
    assert "no command to clear them" in result.output, (
        f"says there is a problem without saying botainer cannot fix it:\n"
        f"{result.output}")
