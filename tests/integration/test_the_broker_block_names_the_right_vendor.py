"""The confirm gate must not tell a codex user about their Anthropic account.

A Codex broker launch must describe OpenAI credentials and the Codex
login store. Reusing Claude's broker disclosure gives incorrect provider,
credential-store and billing information at the launch confirmation gate.

WHY IT HAPPENED. The block was gated on `_auth_mode(spec) == "broker"` and on
nothing else, while the predicate that knows WHICH family is in that mode —
`auth_modes_by_family` — was already being used two dozen lines above it. The
same generalized-interface defect was found and fixed once in `auth doctor`;
nobody applied it to its sibling. That is the sibling-drift class this repo
keeps paying for.

THE BILLING SENTENCE IS NOT TRANSLATED, DELIBERATELY. It is a measured fact
about Claude Code's display and Anthropic's console. There is no verified
equivalent for codex, so the other families get NO billing sentence rather than
a plausible invented one — which would be the same defect pointing the other
way.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml
from click.testing import CliRunner

from botainer.cli.main import cli
from botainer.core import composition
from botainer.inspect.capability_summary import render_multiline


@pytest.fixture
def broker_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    r = CliRunner()
    assert r.invoke(cli, ["setup"]).exit_code == 0

    def _render(agent: str, plugin: str) -> str:
        proj = tmp_path / agent
        proj.mkdir()
        monkeypatch.chdir(proj)
        assert r.invoke(cli, ["init"]).exit_code == 0
        cfg = proj / ".botainer" / "config.yaml"
        d = yaml.safe_load(cfg.read_text())
        d["plugins_enabled"] = [plugin]
        d["agent"] = agent
        cfg.write_text(yaml.safe_dump(d, sort_keys=False))
        spec = composition.compose_session(
            proj, runtime_choice="mock", identity_accept=True)
        return render_multiline(spec)

    return _render


def test_a_codex_broker_session_is_not_told_about_ANTHROPIC(broker_session):
    """Codex broker disclosure must not describe Anthropic credentials."""
    text = broker_session("codex", "agent-codex-broker")

    assert "Auth mode: BROKER" in text, "the broker block did not render at all"
    assert "Anthropic" not in text, (
        f"a codex session is told about an Anthropic credential:\n"
        f"{_broker_block(text)}")
    assert "Claude Code" not in text, (
        f"a codex session is told about its 'native Claude Code credential':\n"
        f"{_broker_block(text)}")
    assert "OpenAI" in text, (
        f"the block names no vendor, or the wrong one:\n{_broker_block(text)}")


def test_a_codex_session_gets_NO_billing_claim_rather_than_a_guessed_one(
        broker_session):
    """A wrong billing sentence is worse than no billing sentence.

    The Claude Code one is measured. Nobody has measured what codex displays,
    so the honest output is silence — and that silence has to be pinned, or the
    next person to "improve consistency" will invent the missing half.
    """
    text = broker_session("codex", "agent-codex-broker")

    assert "API Usage Billing" not in text, (
        "a billing claim measured for one agent was shown for another")
    assert "console" not in _broker_block(text), (
        f"the block sends a codex user to a console nobody verified:\n"
        f"{_broker_block(text)}")


def test_a_claude_broker_session_STILL_gets_the_measured_billing_note(
        broker_session):
    """OPPOSITE DIRECTION. Deleting the sentence would pass both tests above
    and lose a fact that exists precisely because the billing display is
    confusing."""
    text = broker_session("claude", "agent-claude-broker")

    assert "your real Anthropic credential" in text, (
        f"the claude wording regressed:\n{_broker_block(text)}")
    assert "API Usage Billing" in text, (
        "the measured billing note is gone — it exists because Claude Code's "
        "display says something that looks alarming and is not")


def test_the_wording_is_not_spliced_into_nonsense(broker_session):
    """My first fix produced "your real your agent provider's credential".

    Grammatical damage in a security disclosure is not cosmetic: it is the
    sentence a user is deciding on, and it was invisible in the diff — only
    rendering the output showed it.
    """
    for agent, plugin in (("claude", "agent-claude-broker"),
                          ("codex", "agent-codex-broker")):
        block = _broker_block(broker_session(agent, plugin))
        assert "your real your" not in block, block
        assert "native native" not in block, block
        assert "credential credential" not in block, block


def _broker_block(text: str) -> str:
    out, keep = [], False
    for line in text.splitlines():
        if "Auth mode: BROKER" in line:
            keep = True
        elif keep and line.strip() and not line.startswith("    "):
            break
        if keep:
            out.append(line)
    return "\n".join(out)
