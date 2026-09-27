"""`auth login` must follow the project you are standing in, and say so.

The previous mode-resolution order was:
    explicit flag  >  policy default_auth_mode
with the project's OWN configured mode never consulted. Two consequences, both
silent:

  * In a project configured `isolated`, `botainer auth login --agent claude`
    performed a SHARED login. It succeeded, printed nothing unusual, and left
    the project still logged out.
  * `--shared` inside an isolated project writes a store that project never
    reads — again "successful", again logged out.

Order is now: explicit flag > THIS PROJECT's active mode > policy default.

These drive the real click command and assert on OUTPUT and on which login
plugin got invoked — not on source text, which would stay green if the
behaviour were deleted.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from botainer.cli import auth as auth_mod

FAMILIES = {"anthropic": {"shared": "agent-claude-shared",
                          "isolated": "agent-claude"}}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Stub discovery/policy/login so only mode RESOLUTION is exercised."""
    called: dict[str, object] = {}

    monkeypatch.setattr(auth_mod, "_discover_families", lambda: FAMILIES)
    monkeypatch.setattr(auth_mod, "_read_plugins_enabled", lambda root: set())

    def _fake_login(plugin, *, shared, profile):
        called["plugin"] = plugin
        called["shared"] = shared
        return 0
    monkeypatch.setattr(auth_mod, "_invoke_plugin_login", _fake_login)

    class _Pol:
        default_auth_mode = "shared"
    from botainer.core import policy as _policy
    monkeypatch.setattr(_policy, "load_site_policy", lambda: _Pol())
    monkeypatch.setattr(_policy, "load_user_policy", lambda: _Pol())
    monkeypatch.setattr(_policy, "intersect", lambda *a: _Pol())

    def _set_project(mode: str | None):
        """mode=None → not inside a project."""
        if mode is None:
            monkeypatch.setattr(auth_mod._common, "find_project_root",
                                lambda *a, **k: None)
        else:
            monkeypatch.setattr(auth_mod._common, "find_project_root",
                                lambda *a, **k: tmp_path)
            monkeypatch.setattr(auth_mod, "_resolve_active_family_mode",
                                lambda e, f: {"anthropic": mode})
    return _set_project, called


def _run(args):
    return CliRunner().invoke(auth_mod.auth, args, input="y\n" * 5)


def test_isolated_project_gets_an_isolated_login_not_the_policy_shared(harness):
    """THE reported bug: policy default is shared, project is isolated."""
    set_project, called = harness
    set_project("isolated")
    res = _run(["login", "--agent", "claude"])
    assert called.get("plugin") == "agent-claude", (
        f"logged into the wrong store: {called}\n{res.output}")
    assert "THIS PROJECT" in res.output


def test_outside_a_project_the_policy_default_is_used(harness):
    set_project, called = harness
    set_project(None)
    res = _run(["login", "--agent", "claude"])
    assert called.get("plugin") == "agent-claude-shared", res.output
    assert "policy default" in res.output


def test_isolated_outside_a_project_is_refused_not_invented(harness):
    """Their requirement: don't create per-project state in a random folder."""
    set_project, called = harness
    set_project(None)
    res = _run(["login", "--isolated", "--agent", "claude"])
    assert "refused" in res.output.lower(), res.output
    assert "plugin" not in called, "an isolated login ran outside a project"


def test_explicit_shared_in_an_isolated_project_warns_it_wont_help(harness):
    """It "succeeds" and the project stays logged out — say so."""
    set_project, called = harness
    set_project("isolated")
    res = _run(["login", "--shared", "--agent", "claude"])
    assert called.get("plugin") == "agent-claude-shared", res.output
    assert "still be logged out" in res.output, res.output
    assert "/login" in res.output, "isolated users need the in-session route"
