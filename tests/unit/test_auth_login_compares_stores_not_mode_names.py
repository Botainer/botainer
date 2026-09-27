"""`auth login` must not tell a correct command it will fail.

THE WARNING. `auth login --shared/--isolated` compares what you asked for
against the mode THIS project uses, and warns when they disagree, because a
login that lands in a store the project never reads "succeeds" while leaving the
project unauthenticated. That is a real hazard and the warning should exist.

IT COMPARED MODE NAMES, AND MODE NAMES ARE NOT STORES. Broker has no login of
its own — the command's own `--mode` help says so: "broker mode has no login of
its own — it reads the SHARED login credential, so `--mode broker` performs a
shared login", and `credential_scope` defaults to "shared" in both broker
plugins. So a broker project running `botainer auth login --shared` is doing
exactly the right thing, and was told:

    ⚠ this project uses broker mode for anthropic, but you asked for a shared
      login.
    This writes the shared credential store; THIS project reads the broker one,
    so it will still be logged out afterwards.

THREE THINGS WRONG AT ONCE:
  * the PREMISE — there is no separate "broker credential store";
  * the CONSEQUENCE — that login works, and the user is not logged out;
  * the REMEDY, which emitted `botainer auth login --broker` and, verified by
    running it, produces `Error: No such option '--broker'`. The flags are
    `--mode` and `--shared/--isolated`; `--broker` never existed.

THE FIX IS A CHANGE OF SUBJECT, not a special case for broker. The question is
which STORE each side reads, so that is what is compared — and because a store
is always `shared` or `isolated`, both of which ARE flags, the remedy cannot
name something that fails to parse. `--broker` and `--proxy` are unreachable as
suggestions by construction.

PROXY IS NOT COMPARED AT ALL. It reads the per-project store, but a proxy
session refuses to start at v0.1.0, so a store comparison would answer a
question the user cannot act on. It gets its own message.

BOTH DIRECTIONS ARE PINNED. A genuine mismatch must still warn — trading a
false alarm for silence on the real hazard would be no improvement.
"""
from __future__ import annotations

import subprocess

import pytest
import yaml
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
    from botainer.plugins import builtin as plugin_builtin
    plugin_builtin.install_all_builtin()

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    config_module.write_initial_config(proj, agent="claude", force=False)

    def _run(plugin: str, *login_args: str, scope: str | None = None):
        cfg = proj / ".botainer" / "config.yaml"
        d = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        d["agent"] = "claude"
        d["plugins_enabled"] = [plugin]
        if scope is not None:
            d.setdefault("plugins", {})[plugin] = {"credential_scope": scope}
        cfg.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
        # INIT ONCE. `init_project` refuses to overwrite an existing
        # project-id even with force=True (it reads that as a concurrent-init
        # race), so a test that switches plugins on one project must not
        # re-init. Only the config changes between calls, which is the axis
        # under test.
        if not (proj / ".botainer" / "project-id").exists():
            identity.init_project(proj, agent="claude", force=True,
                                  non_interactive=True)
        monkeypatch.chdir(proj)
        return CliRunner().invoke(auth, ["login", *login_args])

    return _run


def test_a_broker_project_asking_for_shared_is_not_told_it_will_fail(project):
    """THE DEFECT. Broker READS the shared store, so this is the right command."""
    res = project("agent-claude-broker", "--shared", "--agent", "claude")

    assert "still be logged out afterwards" not in res.output, (
        f"the correct command was told it would leave the project logged "
        f"out:\n{res.output[:900]}")
    assert "reads the broker one" not in res.output, (
        f"the output still claims a separate broker credential store "
        f"exists:\n{res.output[:900]}")


def test_no_suggestion_ever_names_a_flag_that_does_not_exist(project):
    """THE THIRD LAYER, and the one a user would actually copy and run.

    `--broker` and `--proxy` are not options of `auth login`; only `--mode`,
    `--shared` and `--isolated` are.
    """
    for plugin in ("agent-claude-broker", "agent-claude-shared",
                   "agent-claude"):
        res = project(plugin, "--isolated", "--agent", "claude")
        for bogus in ("--broker", "--proxy"):
            assert f"login {bogus}" not in res.output, (
                f"{plugin} produced a remedy naming {bogus}, which exits with "
                f"\"No such option\":\n{res.output[:900]}")


def test_a_broker_project_with_isolated_scope_DOES_warn(project):
    """OPPOSITE DIRECTION, and the reason this is a store comparison rather
    than 'broker is always fine'. With `credential_scope: isolated` the broker
    reads the PER-PROJECT store, so a shared login really would miss it."""
    res = project("agent-claude-broker", "--shared", "--agent", "claude",
                  scope="isolated")

    assert "still be logged out afterwards" in res.output, (
        f"a genuine store mismatch was silently accepted — the false-alarm fix "
        f"has been traded for silence on the real hazard:\n{res.output[:900]}")


def test_a_real_mismatch_still_warns_and_names_the_right_store(project):
    """An isolated project asked for a shared login is the original hazard."""
    res = project("agent-claude", "--shared", "--agent", "claude")

    assert "still be logged out afterwards" in res.output, (
        f"the genuine mismatch stopped warning:\n{res.output[:900]}")
    assert "isolated" in res.output, (
        f"the warning does not name the store this project reads:\n"
        f"{res.output[:900]}")


def test_matching_store_says_nothing_at_all(project):
    """Silence is the correct output for a correct command."""
    res = project("agent-claude-shared", "--shared", "--agent", "claude")

    assert "⚠ this project uses" not in res.output, (
        f"a login that matches the project's store was warned about:\n"
        f"{res.output[:900]}")
