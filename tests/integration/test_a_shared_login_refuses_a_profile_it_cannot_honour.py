"""`--auth-profile` cannot select a credential a shared login does not have.

WHAT WAS WRONG. `--auth-profile` promised, in its own help, that you could
"keep personal and work logins side by side" at `<creds-dir>/profiles/<name>/`.
For a SHARED login that is false. The shared login hook writes
`shared-auth/agent-<agent>/` unconditionally — the string "profile" does not
occur in it anywhere — so:

    botainer auth login --shared --auth-profile work
    botainer auth login --shared --auth-profile personal

writes the SAME file twice, and the second overwrites the first account's
refresh token. Silently, in the mode the HPC guide teaches, with no way back:
the token is gone, not shadowed.

THE PROFILE AXIS IS NOT MEANINGLESS HERE, which is why this refuses the LOGIN
and not the flag. In shared mode the profile still selects the agent's history
and settings directory. It is only the CREDENTIAL that is host-wide, so the
message says which half it is refusing.

WHY IT KEYS ON THE RESOLVED PLUGIN AND NOT ON THE MODE NAME. The first version
of this guard tested `mode in ("shared", "proxy")` and was WRONG, which the
broker test below pins: broker mode has no login of its own and falls back to
the SHARED login plugin, so `--broker --auth-profile work` overwrote exactly the
same file while passing a mode-name check. Reading the variable the dispatch
just assigned cannot drift from the dispatch. Proxy is deliberately NOT covered
— its `login` command runs the proxy STARTER, so what it writes is not
established, and an unverified claim in a refusal is the thing this project
keeps having to take back.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.auth import _family_to_agent_name
from botainer.cli.main import cli


@pytest.fixture
def install(tmp_path, monkeypatch):
    """One real `setup`, then a project per test — not a hand-built state root."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    r = CliRunner()
    assert r.invoke(cli, ["setup"]).exit_code == 0

    def _project(plugin: str) -> pathlib.Path:
        proj = tmp_path / plugin
        proj.mkdir()
        monkeypatch.chdir(proj)
        assert r.invoke(cli, ["init"]).exit_code == 0
        cfg = proj / ".botainer" / "config.yaml"
        d = yaml.safe_load(cfg.read_text())
        d["plugins_enabled"] = [plugin]
        cfg.write_text(yaml.safe_dump(d, sort_keys=False))
        return proj

    return r, _project


def _login(r: CliRunner, *args: str):
    # "n" so that any path which does NOT refuse stops at the confirm prompt
    # instead of trying to run a real login container in the test environment.
    return r.invoke(cli, ["auth", "login", *args], input="n\n")


def test_a_SHARED_login_under_a_named_profile_is_refused(install):
    r, project = install
    project("agent-claude-shared")

    res = _login(r, "--auth-profile", "work")

    assert "cannot select a credential" in res.output, (
        f"`--auth-profile work` was accepted for a shared login. That writes "
        f"the one host-wide credential, so a second profile name overwrites "
        f"the first account's token:\n{res.output}")


def test_the_refusal_names_the_file_it_would_have_OVERWRITTEN(install):
    """A refusal that will not say what is at stake is not actionable.

    Pinned against `_family_to_agent_name` rather than the literal "claude":
    the first draft of this message interpolated the auth FAMILY and printed
    `shared-auth/agent-anthropic/`, a directory that does not exist on any
    install. Naming a path the user cannot find is worse than naming none.
    """
    r, project = install
    project("agent-claude-shared")

    res = _login(r, "--auth-profile", "work")

    assert f"shared-auth/agent-{_family_to_agent_name('anthropic')}/" in res.output, (
        f"the refusal does not name the credential it protects, or names one "
        f"that does not exist:\n{res.output}")


def test_BROKER_MODE_IS_REFUSED_TOO_because_its_login_is_the_shared_one(install):
    """THE HOLE IN THE FIRST VERSION OF THIS GUARD, and it is not hypothetical.

    Broker has no login hook of its own: `auth login --broker` resolves to the
    family's SHARED login plugin. A guard testing the requested MODE NAME let
    this through, so broker overwrote the identical file with no refusal — the
    whole defect, reachable by a second route.
    """
    r, project = install
    project("agent-claude-broker")

    res = _login(r, "--auth-profile", "work")

    assert "cannot select a credential" in res.output, (
        f"`--broker --auth-profile work` was accepted. Broker mode's login IS "
        f"the shared login, so this overwrites the host-wide credential just "
        f"as `--shared` would:\n{res.output}")


def test_broker_is_NOT_OFFERED_as_the_way_to_get_side_by_side_logins(install):
    """A remedy that does not work is worse than no remedy.

    An early draft offered "`auth use isolated` or `auth use broker`". Broker
    reads this same shared credential, so half that sentence sent the user
    round the loop they were already in.
    """
    r, project = install
    project("agent-claude-broker")

    res = _login(r, "--auth-profile", "work")

    assert "botainer auth use isolated" in res.output, (
        f"no working remedy is named:\n{res.output}")
    assert "broker is not an alternative" in res.output, (
        f"the broker user is not told that broker cannot give them what they "
        f"asked for, so the obvious next thing they try is the mode they are "
        f"already in:\n{res.output}")


def test_it_does_not_ANNOUNCE_a_login_it_then_refuses(install):
    """Broker mode printed "performing a shared login now" and then refused it.

    Two lines of output contradicting each other in the same breath is how a
    user learns not to read the output.
    """
    r, project = install
    project("agent-claude-broker")

    res = _login(r, "--auth-profile", "work")

    assert "performing a shared login" not in res.output, (
        f"announced a login that was then refused:\n{res.output}")


def test_the_DEFAULT_profile_is_still_allowed_to_log_in(install):
    """OPPOSITE DIRECTION. Without this, "refuse every shared login" passes
    every test above and removes the mode the HPC guide teaches."""
    r, project = install
    project("agent-claude-shared")

    res = _login(r)

    assert "cannot select a credential" not in res.output, (
        f"an ordinary shared login was refused:\n{res.output}")
    assert "Run agent-claude-shared login now?" in res.output, (
        f"the shared login never reached its confirm prompt:\n{res.output}")


def test_an_ISOLATED_login_under_a_named_profile_is_STILL_ALLOWED(install):
    """OPPOSITE DIRECTION, and the reason the flag still exists.

    Isolated mode's hook DOES read the profile and writes
    `profiles/<name>/`, so side-by-side logins are real there. A guard that
    refused every non-default profile would pass the tests above and delete a
    working, documented capability — which is the remedy this refusal names.
    """
    r, project = install
    project("agent-claude")

    res = _login(r, "--auth-profile", "work")

    assert "cannot select a credential" not in res.output, (
        f"isolated mode refused a named profile, which is the one mode that "
        f"honours it — and the remedy the shared refusal points at:\n"
        f"{res.output}")
    assert "profile=work" in res.output, (
        f"the isolated login did not carry the requested profile:\n{res.output}")


def test_the_SHARED_HOOK_really_ignores_the_profile(install):
    """The premise, checked against the hook rather than assumed.

    If a future shared hook grew per-profile credentials, this refusal would
    become the wrong behaviour — and nothing else here would notice, because
    every test above asserts the refusal fires. This one fails instead.
    """
    from botainer.plugins.builtin import find_builtin_plugins_root

    root = find_builtin_plugins_root()
    assert root is not None, "the bundled plugin tree was not found to check"
    hook = root / "agent-claude-shared" / "hooks" / "login.py"
    assert hook.is_file(), f"the shared login hook is not at {hook}"

    assert "profile" not in hook.read_text(), (
        "the shared login hook now mentions profiles. If it gained per-profile "
        "credentials, this refusal is no longer correct — and the bind that "
        "reads the credential has to move with it, which is the security "
        "surface, so neither half may change alone.")
