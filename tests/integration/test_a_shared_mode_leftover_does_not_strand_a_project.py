"""Switching out of shared mode must not leave the project unable to start.

THE DEAD END. In shared mode a project's credential is replaced by a SYMLINK to
an in-container path under `/shared-auth/`, which dangles when read from the
host. That is correct while the project IS in shared mode — the shipped README
beside it says so in as many words: "DANGLING when viewed from the host
filesystem, by design. Don't 'fix' them."

Switch that project to isolated and the link stays. Measured semantics of what
is left behind:

    exists()  -> False        is_symlink() -> True
    lexists() -> True         stat()       -> FileNotFoundError

So every check that asks `exists()` sees nothing and every check that asks
`lexists()` sees something, which is why the surfaces disagreed. The isolated
pre_session hook asks `exists()`, so it refused with "no credentials; run login"
— and the login it named could not write through the dangling link either: the
login container has no `/shared-auth` bound, so the write is ENOENT and lands
nowhere. Told to run a command that cannot succeed, by a check that cannot see
what is wrong, while the README says not to touch it.

TWO MECHANISMS, AND THEY ARE NOT THE SAME KIND.

  * STRUCTURE: `auth use` clears the leftovers on the way out of shared mode, so
    the state never forms on the supported path. That is the fix.
  * A RULE, BACKING UP A NAMED GAP: the hook now recognises the leftover and
    says what to run. `auth use` is not the only way a mode changes — editing
    `.botainer/config.yaml` by hand bypasses it entirely — so the rule covers
    what the structure cannot reach, and this file tests both.

WHAT IS DELETED, AND WHAT IS NEVER DELETED. Only the symlink and the README this
project wrote. Unlinking a symlink does not touch its target, so the shared
credential itself survives — asserted below, because "we only remove the link"
is exactly the sentence that would be worth nothing if it were wrong.

THE "UNKILLABLE MUTANT" I CLAIMED HERE WAS WRONG, TWICE OVER. A refuting review
disproved it and the correction is worth keeping, because the false version was
shipped into the security contract before anyone checked it.

I wrote that a cleanup rewritten to resolve-then-delete could not be caught by
any test. Measured: installing `entry.resolve().unlink()` fails
`test_auth_use_CLEARS_the_residue_leaving_the_project_startable` on the first
run — `resolve()` on a dangling link raises `FileNotFoundError`, the surrounding
`except OSError` swallows it, and the link is never removed. Not a silent no-op:
a visible behaviour change the suite already caught.

I also wrote that catching it "would need `/shared-auth/` to exist at the real
root of the filesystem". Also false. The prefix is a module constant, so a test
can point it at a temp directory and make the target reachable — which is what
`test_the_cleanup_removes_the_LINK_and_not_its_TARGET` below now does, killing
the one variant that genuinely did survive (delete the resolved target, THEN
unlink the link).

The lesson kept rather than the claim: a mutant that "survives" is a claim about
the tests, and claims about tests get checked by running them, not by reasoning
about the fixture.

DRIVEN THROUGH `dry-run --include-hooks`, NOT `start`, AND THAT IS A LIMIT WORTH
STATING. `start` refuses earlier in this environment ("no docker or apptainer
found"), so the hook is unreachable from it here. `dry-run --include-hooks` runs
the same `run_pre_session_hooks`. One difference the tests below encode: dry-run
lists a failing hook by its HEADLINE only, while `start` renders headline plus
the full tail — which is why the remedy is on the first line rather than the
last.
"""
from __future__ import annotations

import os
import pathlib

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.main import cli

_CONTAINER_LINK = "/shared-auth/agent-claude/.credentials.json"


@pytest.fixture
def install(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    r = CliRunner()
    assert r.invoke(cli, ["setup"]).exit_code == 0
    root = tmp_path / "root"

    def _project(name: str, plugin: str) -> tuple[pathlib.Path, pathlib.Path]:
        """Returns (project root, the project's credential dir)."""
        proj = tmp_path / name
        proj.mkdir()
        monkeypatch.chdir(proj)
        assert r.invoke(cli, ["init"]).exit_code == 0
        cfg = proj / ".botainer" / "config.yaml"
        d = yaml.safe_load(cfg.read_text())
        d["plugins_enabled"] = [plugin]
        cfg.write_text(yaml.safe_dump(d, sort_keys=False))

        from botainer.core import identity
        uid, _ = identity.resolve_identity(proj, identity_accept=True)
        creds = (root / "state" / uid / "data" / "agent-claude"
                 / "profiles" / "default")
        creds.mkdir(parents=True, exist_ok=True)
        return proj, creds

    return r, root, _project


def _leave_shared_mode_residue(creds: pathlib.Path) -> pathlib.Path:
    """Exactly what a shared-mode pre_session hook leaves on disk."""
    link = creds / ".credentials.json"
    os.symlink(_CONTAINER_LINK, link)
    (creds / "README.shared-mode.txt").write_text("dangling by design\n")
    return link


def _real_shared_credential(root: pathlib.Path) -> pathlib.Path:
    shared = root / "shared-auth" / "agent-claude"
    shared.mkdir(parents=True, exist_ok=True)
    f = shared / ".credentials.json"
    f.write_text('{"account":"the real one"}')
    return f


def test_the_residue_is_invisible_to_exists_and_visible_to_lexists(install):
    """The split that made every surface disagree, pinned as a fact.

    If this ever stops being true the rest of this file is reasoning about a
    situation that no longer exists, and should fail loudly rather than pass.
    """
    _r, _root, project = install
    _proj, creds = project("p", "agent-claude")
    link = _leave_shared_mode_residue(creds)

    assert link.is_symlink() and os.path.lexists(link)
    assert not link.exists(), (
        "a dangling symlink now reports exists() True, so the refusal this "
        "file is about would not fire and these tests prove nothing")


def test_auth_use_CLEARS_the_residue_leaving_the_project_startable(install):
    r, _root, project = install
    _proj, creds = project("p", "agent-claude-shared")
    link = _leave_shared_mode_residue(creds)

    res = r.invoke(cli, ["auth", "use", "isolated", "--family", "anthropic",
                         "--yes"])

    assert res.exit_code == 0, res.output
    assert not os.path.lexists(link), (
        f"switching out of shared mode left the container symlink behind. The "
        f"isolated hook reads it as 'no credentials' and the login it names "
        f"cannot write through it:\n{res.output}")
    assert not (creds / "README.shared-mode.txt").exists(), (
        "the shared-mode README survived a switch out of shared mode, so the "
        "directory still documents a mode this project is not in")


def test_clearing_the_link_does_NOT_touch_the_shared_CREDENTIAL(install):
    """The claim the cleanup rests on. Worthless if untested, and destructive
    if wrong: the shared credential is the login every other project uses."""
    r, root, project = install
    _proj, creds = project("p", "agent-claude-shared")
    _leave_shared_mode_residue(creds)
    shared_file = _real_shared_credential(root)

    r.invoke(cli, ["auth", "use", "isolated", "--family", "anthropic", "--yes"])

    assert shared_file.exists(), (
        "clearing this project's leftover link deleted the HOST-WIDE shared "
        "credential — every shared-mode project on this machine is now logged "
        "out")
    assert shared_file.read_text() == '{"account":"the real one"}'


def test_a_REAL_isolated_credential_is_never_removed(install):
    """The opposite direction. A cleanup that deleted regular files would pass
    every test above and destroy the login the user just performed."""
    r, _root, project = install
    _proj, creds = project("p", "agent-claude-shared")
    real = creds / ".credentials.json"
    real.write_text('{"account":"a real isolated login"}')

    r.invoke(cli, ["auth", "use", "isolated", "--family", "anthropic", "--yes"])

    assert real.exists() and not real.is_symlink(), (
        "a REGULAR credential file was removed by the shared-mode cleanup — "
        "that is a real login, and deleting it is data loss")
    assert real.read_text() == '{"account":"a real isolated login"}'


def test_a_link_pointing_somewhere_ELSE_is_left_alone(install):
    """Only links into the container are shared-mode residue.

    A symlink the user made themselves — to a credential they keep elsewhere —
    is not ours to delete, and a cleanup keyed on "is a symlink" rather than on
    the target would take it.
    """
    r, _root, project = install
    _proj, creds = project("p", "agent-claude-shared")
    elsewhere = creds.parent / "my-own-store.json"
    elsewhere.write_text('{"mine":true}')
    link = creds / ".credentials.json"
    os.symlink(elsewhere, link)

    r.invoke(cli, ["auth", "use", "isolated", "--family", "anthropic", "--yes"])

    assert os.path.lexists(link), (
        "a symlink that does NOT point into the container was deleted — the "
        "cleanup is keyed on the wrong thing")


def test_switching_TO_shared_does_not_clear_anything(install):
    """The residue belongs to shared mode, so shared mode keeps it."""
    r, _root, project = install
    _proj, creds = project("p", "agent-claude")
    link = _leave_shared_mode_residue(creds)

    r.invoke(cli, ["auth", "use", "shared", "--family", "anthropic", "--yes"])

    assert os.path.lexists(link), (
        "switching INTO shared mode deleted the shared-mode link, which is the "
        "very file that mode uses")


def test_a_HAND_EDITED_config_still_gets_told_what_is_wrong(install):
    """THE BACKUP RULE, and the gap it backs up.

    `auth use` is not the only way a project changes mode — editing
    config.yaml by hand skips it, and then the structure above never ran. The
    hook has to recognise the leftover on its own.
    """
    r, _root, project = install
    _proj, creds = project("p", "agent-claude")       # isolated, never switched
    _leave_shared_mode_residue(creds)

    res = r.invoke(cli, ["dry-run", "--include-hooks"])

    assert "leftover SHARED-mode credential link" in res.output, (
        f"the hook still reports this as 'no credentials', which sends the "
        f"user to a login that cannot write through the dangling link:\n"
        f"{res.output}")


def test_the_REMEDY_is_on_the_headline_where_dry_run_can_show_it(install):
    """`dry-run --include-hooks` prints a failing hook's HEADLINE only.

    A first line that only states the problem leaves that surface with nothing
    to act on, so the runnable command lives there rather than at the end.
    """
    r, _root, project = install
    _proj, creds = project("p", "agent-claude")
    _leave_shared_mode_residue(creds)

    res = r.invoke(cli, ["dry-run", "--include-hooks"])

    assert "botainer auth use isolated --family anthropic" in res.output, (
        f"the refusal reaches this surface without the command that fixes it:\n"
        f"{res.output}")


def test_the_message_does_not_claim_the_shared_login_is_affected(install):
    """It says the shared login is untouched — which the cleanup test proves.
    Pinned so the sentence cannot be dropped while the behaviour stays."""
    r, _root, project = install
    _proj, creds = project("p", "agent-claude")
    _leave_shared_mode_residue(creds)

    res = r.invoke(cli, ["dry-run", "--include-hooks"])

    assert "shared login is untouched" in res.output, (
        f"a user told to run a command against their credentials is not told "
        f"what it will NOT do, which is the part that makes it safe to "
        f"run:\n{res.output}")


def test_an_ORDINARY_isolated_project_is_unaffected(install):
    """No leftover, no new message — or the check is crying wolf."""
    r, _root, project = install
    _proj, creds = project("p", "agent-claude")
    (creds / ".credentials.json").write_text('{"account":"fine"}')

    res = r.invoke(cli, ["dry-run", "--include-hooks"])

    assert "leftover SHARED-mode" not in res.output, (
        f"a healthy isolated project was told it has shared-mode "
        f"residue:\n{res.output}")


def test_unlinking_a_symlink_does_not_follow_it(tmp_path):
    """The property the cleanup depends on, tested where it can actually fail.

    The cleanup removes links, not what they name. On a real install the link's
    target is an in-container path that resolves to nothing, which makes the
    ordinary test above unable to distinguish "removed the link" from "removed
    the link and its target". Here the target is reachable, so the distinction
    is real: if `unlink()` ever followed the final symlink, this fails and the
    host-wide credential is at risk.
    """
    target = tmp_path / "the-shared-credential.json"
    target.write_text('{"account":"shared"}')
    link = tmp_path / "project-link.json"
    link.symlink_to(target)

    link.unlink()

    assert not os.path.lexists(link)
    assert target.exists() and target.read_text() == '{"account":"shared"}', (
        "unlink() followed the symlink and deleted its target — the cleanup "
        "would destroy the login every shared-mode project uses")


def test_the_cleanup_removes_the_LINK_and_not_its_TARGET(install, tmp_path,
                                                         monkeypatch):
    """The variant that genuinely survived the first mutation round.

    `delete the resolved target, THEN unlink the link` passes every other test
    here: on a real install the leftover points at an in-container path that
    resolves to nothing, so the destructive half is inert in the fixture while
    the visible half still removes the link.

    It is reachable after all. The container prefix is a module constant, so
    pointing it at a temp directory makes the target real and the distinction
    observable. This is the test whose absence I wrote up as an impossibility.

    DRIVEN THROUGH `auth use`, NOT THROUGH THE HELPER. My first attempt called
    `_stale_shared_mode_artefacts` and then unlinked the results itself — which
    exercises the FINDER and not the CLEANUP, so the mutant living in the
    cleanup passed it. A test that reimplements the step under test cannot see a
    defect in that step.
    """
    from botainer.cli import auth as auth_mod

    r, _root, project = install
    _proj, creds = project("p", "agent-claude-shared")

    fake_prefix = tmp_path / "shared-auth"
    (fake_prefix / "agent-claude").mkdir(parents=True)
    target = fake_prefix / "agent-claude" / ".credentials.json"
    target.write_text('{"account":"the host-wide login"}')
    monkeypatch.setattr(auth_mod, "_SHARED_LINK_PREFIX", str(fake_prefix) + "/")

    link = creds / ".credentials.json"
    link.symlink_to(target)

    res = r.invoke(cli, ["auth", "use", "isolated", "--family", "anthropic",
                         "--yes"])
    assert res.exit_code == 0, res.output

    assert not os.path.lexists(link), (
        f"the leftover link was not removed:\n{res.output}")
    assert target.exists() and target.read_text() == '{"account":"the host-wide login"}', (
        "the cleanup deleted what the link POINTED AT. On a real install that "
        "is the host-wide credential every shared-mode project uses, and the "
        "project running the cleanup is not the only one that loses it")


def test_the_DETAIL_block_renders_real_paths_not_format_placeholders(install):
    """The bug every other test in this file was blind to.

    The message was authored by a generator that used `str.replace`, so the
    doubled braces meant to escape a format placeholder went through verbatim:
    users saw the literal text `{creds_file}` and `{os.readlink(creds_file)}`
    where the two facts they need should be. Both agent families.

    Eleven tests passed over it because every one of them asserted a line-1
    string, and `dry-run --include-hooks` shows only a hook's headline — so the
    surface a test could reach was the one surface the defect was not on.

    This drives `run_pre_session_hooks` the way `dry-run` does and reads the
    REFUSAL, which carries the headline plus the tail, so the Detail block is
    actually in view.
    """
    from botainer.core import composition

    r, _root, project = install
    proj, creds = project("p", "agent-claude")
    _leave_shared_mode_residue(creds)

    refusals: list = []
    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=True)
    composition.run_pre_session_hooks(spec, on_hook_error=refusals.append)

    text = "\n".join(str(x) for x in refusals)
    assert text, "the hook did not refuse at all, so nothing was rendered"
    assert "{creds_file}" not in text and "{os.readlink" not in text, (
        f"the message shows format placeholders instead of the path and its "
        f"target — the two facts the user needs to act:\n{text}")
    assert str(creds / ".credentials.json") in text, (
        f"the leftover's real path is not in the message:\n{text}")
    assert _CONTAINER_LINK in text, (
        f"the link's target is not in the message, so the user cannot see WHY "
        f"it is unreadable:\n{text}")
