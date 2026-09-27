""""No profiles yet" must mean none exist, not that we could not look.

WHAT IT DID. `collect_profiles` wrapped the identity lookup in a blanket
`except Exception: return []`, and `auth profiles` renders an empty list as:

    No profiles yet — one appears the first time you log in.
      botainer auth login --profile <name>

So a project whose identity could NOT be resolved was told it HAS no profiles.
The profiles are still on disk; only the lookup failed. And the advice is worse
than the statement: following it would create a SECOND account slot rather than
find the first, on a command whose own docstring says "A profile separates
ACCOUNTS".

WHEN THAT HAPPENS, and it is not exotic. `resolve_identity` is called with
`identity_accept=False`, which correctly REFUSES to silently rebind a project
that has moved. Moving a directory is the ordinary case; the refusal is the
feature. Swallowing it turned a careful refusal into a confident wrong answer.

"I COULD NOT ANSWER" AND "THE ANSWER IS NONE" ARE DIFFERENT, and only the caller
can tell them apart without inventing a sentinel. So the lookup no longer
swallows, and the command says which question failed and how to settle it.

BOTH DIRECTIONS. A genuinely profile-less project must STILL say "No profiles
yet" — replacing a false negative with a false alarm would be no improvement,
and that is the state every new project is in.
"""
from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner

from botainer.cli.auth import auth
from botainer.core import config as config_module
from botainer.core import identity
from botainer.state import dir as state_dir


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    state_dir.ensure_user_state_dir(create_if_missing=True)

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    config_module.write_initial_config(proj, agent="claude", force=False)
    identity.init_project(proj, agent="claude", force=True,
                          non_interactive=True)
    monkeypatch.chdir(proj)

    def _run():
        return CliRunner().invoke(auth, ["profiles"])

    return proj, _run


def test_a_project_with_no_profiles_still_says_so(project):
    """THE OPPOSITE DIRECTION FIRST, because it is every new project."""
    _, run = project

    res = run()

    assert res.exit_code == 0, f"a healthy empty project failed:\n{res.output}"
    assert "No profiles yet" in res.output, (
        f"the genuinely-empty case lost its answer:\n{res.output}")


def test_an_unresolvable_identity_is_not_reported_as_no_profiles(project,
                                                                monkeypatch):
    """THE DEFECT. The identity lookup fails; the profiles are unknown, not
    absent."""
    proj, run = project
    from botainer.core import identity as ident

    def _boom(*a, **k):
        raise RuntimeError("project moved; identity would need rebinding")

    monkeypatch.setattr(ident, "resolve_identity", _boom)

    res = run()

    assert "No profiles yet" not in res.output, (
        f"a failed lookup was rendered as a statement about the user's "
        f"ACCOUNTS:\n{res.output}")
    assert res.exit_code != 0, (
        f"a question that could not be answered exited 0:\n{res.output}")
    assert "NOT 'you have no profiles'" in res.output, (
        f"the output does not distinguish 'could not look' from 'none "
        f"exist':\n{res.output}")


def test_the_failure_names_a_way_out(project, monkeypatch):
    """A refusal that does not say what to do next is the guided-errors defect
    this project keeps recording."""
    _, run = project
    from botainer.core import identity as ident
    monkeypatch.setattr(
        ident, "resolve_identity",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("moved")))

    res = run()

    assert "accept-identity-change" in res.output, (
        f"the failure names no remedy:\n{res.output}")


def test_the_advice_that_would_make_a_second_account_is_not_offered(project,
                                                                   monkeypatch):
    """The specific harm. `auth login --profile <name>` on a project whose
    identity did not resolve creates a NEW slot; it does not recover the old
    one. Offering it here is how a user ends up with two half-logged-in
    accounts."""
    _, run = project
    from botainer.core import identity as ident
    monkeypatch.setattr(
        ident, "resolve_identity",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("moved")))

    res = run()

    assert "auth login --profile" not in res.output, (
        f"the failed-lookup path still suggests creating a profile:\n"
        f"{res.output}")
