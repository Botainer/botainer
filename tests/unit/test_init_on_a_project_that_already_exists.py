"""`botainer init` on a project that ALREADY EXISTS — the two things it got wrong.

`init` has two jobs on an existing project and did neither. Both were measured
by running the real CLI, not by reading it.

THE FIRST — `--force` HAS NEVER DONE WHAT IT SAYS. Its help reads "Overwrite
existing Main/.botainer/ contents"; `init_project` fell through to the MINT
branch and called `write_project_id()` without `overwrite=True`, so the O_EXCL
guard fired on every project that had a project-id — which is every project
`--force` exists for. Observed: exit 2, with

    refused: project-id-tampered: project-id already exists at ...
      (concurrent init race). Read the file to get the existing UUID; do not
      overwrite.

accusing the user of a race they did not run and telling them not to do the one
thing they asked for. The config was never rewritten either, because the refusal
happens before `write_initial_config` is reached.

RE-MINTING IS NOT THE ALTERNATIVE. A new UUID orphans the project's state dir —
`sessions/`, `data/` (which holds the credential in isolated and shared modes)
and `packages/` — with nothing left pointing at it. Giving a copy its own
identity is the clone/fork path, and that one asks first. So `--force` resets
CONFIG and never touches identity.

THE SECOND — `--agent` ON AN EXISTING PROJECT WAS SILENTLY DISCARDED, AND THEN
ACTED ON ANYWAY. `botainer init --agent codex` in an existing claude project
exited 0, printed "Initialized", and told the user to

    botainer image build agent-codex   # build docker image (~8-12 min ...)
    botainer auth login --isolated --agent codex

while the config still said `agent: claude`. Four wrong instructions: an image
build that takes ten minutes and will never be used, a login to the wrong
provider, a plugin form naming a plugin this project does not enable, and the
word "Initialized" for a project that was only recognised.

AND `init` MUST NOT BE A BACK DOOR AROUND THE AGENT SWITCH. `config set agent`
warns that switching agent starts a FRESH history and names both directories,
because the two agents' state is not interchangeable. If `--force` rewrote the
config with a new `agent:`, it would perform that switch with none of that said.
So a differing explicit `--agent` is REFUSED and points at the command that does
it properly.
"""
from __future__ import annotations

import subprocess

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.init import init
from botainer.core import identity
from botainer.state import dir as state_dir


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    state_dir.ensure_user_state_dir(create_if_missing=True)
    from botainer.plugins import builtin as plugin_builtin
    plugin_builtin.install_all_builtin()

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    monkeypatch.chdir(proj)

    def _run(*args: str):
        return CliRunner().invoke(init, [*args, "--non-interactive"])

    return proj, _run


@pytest.fixture
def claude_project(project):
    """A project that has already been initialized — the state both rows are
    about. THE HEALTHY FIXTURE IS CHECKED, not assumed: if the first init did
    not work, every assertion below would be measuring the wrong thing."""
    proj, run = project
    first = run("--agent", "claude")
    assert first.exit_code == 0, f"the fixture's own init failed:\n{first.output}"
    cfg = proj / ".botainer" / "config.yaml"
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["agent"] == "claude"
    return proj, run, cfg


def _hand_edit(cfg) -> str:
    marker = "# a hand edit that --force is supposed to be able to clear\n"
    cfg.write_text(cfg.read_text(encoding="utf-8") + marker, encoding="utf-8")
    return marker


# ── `--force` ────────────────────────────────────────────────────────────────

def test_force_does_not_refuse_on_an_existing_project(claude_project):
    """THE DEFECT. Every project `--force` is for already has a project-id."""
    _, run, _ = claude_project

    res = run("--force")

    assert res.exit_code == 0, (
        f"`init --force` still refuses on the projects it exists for:\n"
        f"{res.output}")
    assert "concurrent init race" not in res.output, (
        f"the user is still accused of a race they did not run:\n{res.output}")


def test_force_actually_rewrites_the_config(claude_project):
    """Its documented purpose. This never executed once, because the refusal
    happened before `write_initial_config` was reached."""
    _, run, cfg = claude_project
    marker = _hand_edit(cfg)

    res = run("--force")

    assert marker not in cfg.read_text(encoding="utf-8"), (
        f"--force left the config exactly as it found it:\n{res.output}")


def test_force_keeps_the_identity_and_the_state_dir(claude_project):
    """THE DIRECTION THAT MATTERS. A new UUID would orphan sessions/, data/ —
    which holds the credential — and packages/, with nothing pointing back."""
    proj, run, _ = claude_project
    pid_file = proj / ".botainer" / "project-id"
    before = pid_file.read_text(encoding="utf-8").strip()

    res = run("--force")

    assert pid_file.read_text(encoding="utf-8").strip() == before, (
        f"--force re-minted the project identity, orphaning its state dir:\n"
        f"{res.output}")


def test_force_keeps_a_copy_of_the_config_it_overwrote(claude_project):
    """Making --force work must not make it destroy hand edits silently."""
    proj, run, cfg = claude_project
    marker = _hand_edit(cfg)

    res = run("--force")

    bak = proj / ".botainer" / "config.yaml.bak"
    assert bak.exists(), (
        f"the overwritten config was not kept anywhere:\n{res.output}")
    assert marker in bak.read_text(encoding="utf-8"), (
        "the backup is not the file that was overwritten")
    assert "config.yaml.bak" in res.output, (
        f"a backup nobody is told about is not a recovery path:\n{res.output}")


def test_without_force_nothing_is_rewritten_and_no_backup_is_left(claude_project):
    """THE OPPOSITE DIRECTION, and it is the common case: a plain re-`init`
    must stay inert, and must not litter the project with a .bak."""
    proj, run, cfg = claude_project
    marker = _hand_edit(cfg)

    res = run()

    assert marker in cfg.read_text(encoding="utf-8"), (
        f"a plain `init` overwrote an existing config:\n{res.output}")
    assert not (proj / ".botainer" / "config.yaml.bak").exists(), (
        "a backup was written for a run that overwrote nothing")


def test_a_genuine_concurrent_init_still_refuses(claude_project, monkeypatch):
    """THE GUARD MUST SURVIVE (#113). Two parallel inits both read "no
    project-id" and both try to write one; the loser must not clobber the
    winner. That is reproduced by making the READ see nothing while the file is
    there — which is exactly the race's shape, not a stand-in for it."""
    proj, _, _ = claude_project
    from botainer.core import identity as ident
    monkeypatch.setattr(ident, "read_project_id", lambda *a, **k: None)

    from botainer.core.refusal import Refused
    with pytest.raises(Refused) as caught:
        ident.init_project(proj, agent="claude", force=True,
                           non_interactive=True)

    assert "already exists" in str(caught.value), (
        f"the race guard no longer names what it found: {caught.value}")


def test_a_forced_reinit_is_recorded(claude_project):
    """`force` would otherwise be a parameter this function accepts and never
    reads — the shape that invites a later reader to 'fix' it by wiring it back
    to the mint path. It leaves a fact instead: config.yaml was reset, and when."""
    proj, run, _ = claude_project
    run("--force")

    from botainer.state import dir as sd
    paths = sd.ensure_user_state_dir(create_if_missing=True)
    uid = (proj / ".botainer" / "project-id").read_text(encoding="utf-8").strip()
    meta = sd.read_meta(sd.ensure_project_dirs(paths, uid))

    assert meta.get("last_forced_reinit"), (
        f"a forced re-init left no trace in meta.json: {sorted(meta)}")


# ── `--agent` ────────────────────────────────────────────────────────────────

def test_a_different_explicit_agent_is_refused_not_ignored(claude_project):
    """THE DEFECT. Observed before the fix: exit 0, "Initialized", and a
    ten-minute image build for an agent the project does not use."""
    _, run, cfg = claude_project

    res = run("--agent", "codex")

    assert res.exit_code != 0, (
        f"`--agent codex` on a claude project still reports success:\n"
        f"{res.output}")
    assert "image build agent-codex" not in res.output, (
        f"the user is still told to spend ten minutes building an image this "
        f"project will never use:\n{res.output}")
    assert "login --isolated --agent codex" not in res.output, (
        f"the user is still sent to the wrong provider's login:\n{res.output}")
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["agent"] == "claude", (
        "the refusal did not leave the project alone")


def test_the_refusal_names_the_command_that_does_it_properly(claude_project):
    """`config set agent` warns that the switch starts a FRESH history and
    names both directories. A refusal that does not point there leaves the user
    to find it, or to hand-edit the config and get no warning at all."""
    _, run, _ = claude_project

    res = run("--agent", "codex")

    assert "config set agent" in res.output, (
        f"the refusal names no way forward:\n{res.output}")


def test_force_is_not_a_back_door_around_the_agent_switch(claude_project):
    """--force resets CONFIG; it must not become the one path that changes
    `agent:` with none of the history warning said."""
    _, run, cfg = claude_project

    res = run("--agent", "codex", "--force")

    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["agent"] == "claude", (
        f"--force switched the agent, bypassing the history warning:\n"
        f"{res.output}")


def test_saying_nothing_about_the_agent_is_not_read_as_asking_for_claude(project):
    """`--agent` defaulted to "claude" in the click option, so "I did not say"
    and "I said claude" were the same value. On a codex project that made a
    bare `init --force` look like a request to switch."""
    proj, run = project
    first = run("--agent", "codex")
    assert first.exit_code == 0, f"codex fixture init failed:\n{first.output}"

    res = run("--force")

    cfg = proj / ".botainer" / "config.yaml"
    assert res.exit_code == 0, (
        f"a bare `--force` on a codex project was read as a switch request:\n"
        f"{res.output}")
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["agent"] == "codex", (
        f"the project's agent changed although nothing asked it to:\n"
        f"{res.output}")


def test_the_next_steps_name_the_agent_the_project_actually_has(project):
    """The visible harm of reading the REQUESTED agent instead of the WRITTEN
    one. `_mode_of_written_config` and `runtime_hint` already re-read the file;
    the agent did not."""
    proj, run = project
    first = run("--agent", "codex")
    assert first.exit_code == 0, f"codex fixture init failed:\n{first.output}"

    res = run()

    assert "agent-claude" not in res.output, (
        f"a codex project was given claude's next steps:\n{res.output}")
    assert "agent-codex" in res.output, (
        f"the next steps do not name this project's agent:\n{res.output}")
