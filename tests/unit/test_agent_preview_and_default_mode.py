"""Two things a user could not do, and one default that was never asserted.

1. PREVIEW THE OTHER AGENT. `start` has had `--agent` since #112, but `inspect`
   and `dry-run` — the two commands whose entire job is "show me what will
   happen" — did not. So a project inited for claude had no way to see what a
   codex session would look like short of launching one. The config template
   also omitted Codex, leaving the available agent choices undisclosed.

2. THE DEFAULT AUTH MODE. It was `shared`, and NOTHING asserted it — every
   test sets the value explicitly in its fixture, which is good hygiene and
   also why changing the default broke no test. A default that decides which
   credential architecture new users get should not be changeable in silence.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner


def test_the_default_auth_mode_is_isolated(tmp_path) -> None:
    """New policies default to isolated authentication.

    Asserted on the POLICY OBJECT rather than on a string in the source, so it
    fails if the schema default moves regardless of how it is spelled.
    """
    from botainer.core.policy import SitePolicy

    assert SitePolicy().default_auth_mode == "isolated", (
        "shared mode's directory holds a real refresh token the container can "
        "read AND overwrite, and rotation makes its N+1 holders unsound; a "
        "default is a recommendation, so it should not name the mode with the "
        "worst isolation story"
    )


def test_init_writes_the_isolated_agent_plugin(tmp_path, monkeypatch) -> None:
    """The default has to reach the file the user actually gets, not just the
    schema. `agent-claude` is the isolated plugin; `agent-claude-shared` is not.
    """
    from botainer.cli.init import init

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)

    CliRunner().invoke(init, [], catch_exceptions=False)
    cfg = yaml.safe_load((proj / ".botainer" / "config.yaml").read_text())

    assert "agent-claude" in cfg["plugins_enabled"]
    assert "agent-claude-shared" not in cfg["plugins_enabled"], (
        "a new project must not land on shared mode by default"
    )


def test_the_generated_config_tells_you_codex_exists(tmp_path, monkeypatch) -> None:
    """The template named exactly one agent and never mentioned the other, so
    a user had no way to learn codex was supported, let alone how to reach it.
    Asserts on the RENDERED file — this is what someone opens after `init`.
    """
    from botainer.cli.init import init

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)

    CliRunner().invoke(init, [], catch_exceptions=False)
    written = (proj / ".botainer" / "config.yaml").read_text()

    assert "codex" in written, "the other agent must be discoverable"
    assert "--agent codex" in written, "and the one-shot route named"
    assert "config set agent codex" in written, "and the persistent one"


@pytest.mark.parametrize("command_name", ["inspect", "dry_run"])
def test_inspect_and_dry_run_accept_agent(command_name) -> None:
    """Parity with `start`. Asserted on the command's real parameter list, not
    on help text, so a flag that exists but is not wired still fails.
    """
    import importlib

    mod = importlib.import_module(f"botainer.cli.{command_name}")
    cmd = getattr(mod, command_name)
    names = {p.name for p in cmd.params}

    assert "agent_override" in names, (
        f"`botainer {command_name.replace('_', '-')}` cannot preview the other "
        f"agent, so the only way to find out what it does is to launch it"
    )
