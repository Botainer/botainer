"""Three defects found while verifying a fourth. All reproduced BY RUNNING.

DEFECT 1 — `config explain` PRINTED SHORT CREDENTIALS IN FULL. The env loop
rendered values through a hand-rolled truncation:

    display_v = v if len(v) <= 40 else v[:40] + "..."

under a comment claiming it truncated "values that look like secrets". It does
no such thing for anything shorter than 40 characters. Observed on the real CLI:

    Extra env vars (passed to container):
      ANTHROPIC_API_KEY=sk-ant-fake

`config get` — IN THE SAME FILE, forty lines below — already called
`redact(key, value, mode="safe")`. And `botainer/inspect/_redact.py`'s own
docstring records task #298 closing exactly this class ("information leakage via
first-4/last-4") and says safe mode redacts "regardless of length" BECAUSE short
tokens leak. So `explain` reintroduced #298 inside the file that holds its fix.

I found this while verifying something else, one commit after editing `explain`
without noticing the leak three lines below my own change. That is worth writing
down: reading a function is not the same as reading its output.

DEFECT 2 — THE LEAK CHECK RECOMMENDED THE MODE IT BREAKS. `config check` and the
shared refusal both told the user to fix a credential-shaped env var by enabling
`agent-claude-proxy`. Run that advice and `botainer auth use proxy` replies:

    every session REFUSE TO START.
      - The proxy hands the agent an ephemeral ANTHROPIC_API_KEY,
        but botainer's credential-leak guard refuses any env var
        named ANTHROPIC_API_KEY on principle.

The remedy named by the leak check is the one mode the leak check breaks. Not a
cage hole — `auth use proxy` warns at length, requires y/N and defaults to No,
and the mode stayed `isolated` when I ran it — but it sends the user in a circle
at the moment they are trying to do the right thing.

DEFECT 3 — `init` ADVERTISED THE RETIRED MODE IN THE USER'S OWN FILE. A fresh
`botainer init --agent claude` wrote three live-looking proxy references into
the project's config.yaml with no hint it was retired. That is #130's class
("shipped output names commands that do not exist — including two written into
the user's own config.yaml") recurring.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.cli.config_cmd import config
from botainer.core import credential_leak_check
from botainer.core.refusal import Refused

UID = "33333333-3333-4333-8333-333333333333"

_BASE = ("version: config-v1\nagent: claude\nruntime: docker\n"
         "profile: default\nnetwork:\n  mode: internet\n"
         "plugins_enabled: [agent-claude]\n")

#: SHORT on purpose. The old truncation only fired above 40 characters, so a
#: long fixture would have passed against the defect and proved nothing.
_SHORT_SECRET = "sk-ant-fake"


def _project(tmp_path: Path, monkeypatch, extra: str = "") -> Path:
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(UID + "\n")
    (proj / ".botainer" / "config.yaml").write_text(_BASE + extra)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    return proj


def _run(*args):
    return CliRunner().invoke(config, list(args))


# ── defect 1: the credential must not reach the terminal ───────────────────

def test_explain_does_not_print_a_SHORT_credential(tmp_path, monkeypatch):
    """THE DEFECT. Truncation at 40 chars printed this one in full."""
    _project(tmp_path, monkeypatch, f"env:\n  ANTHROPIC_API_KEY: {_SHORT_SECRET}\n")

    out = _run("explain").output

    assert _SHORT_SECRET not in out, (
        f"the credential was printed to the terminal:\n{out}")
    assert "<redacted>" in out, out


def test_explain_redacts_by_KEY_SHAPE_not_a_hardcoded_name(
        tmp_path, monkeypatch):
    """STRUCTURAL. Pins that it goes through the shared helper.

    `HUGGINGFACE_TOKEN` is named nowhere in botainer's source. If it is redacted,
    the decision is `looks_credential()`'s, which every other render surface
    shares — so a new credential-shaped name is covered before anyone thinks of
    it. If this passes only for names someone typed in, the surfaces drift.
    """
    _project(tmp_path, monkeypatch,
             "env:\n  HUGGINGFACE_TOKEN: hf_short\n")

    out = _run("explain").output

    assert "hf_short" not in out, (
        f"only known names are redacted, so this will drift:\n{out}")


def test_explain_still_shows_an_ORDINARY_value(tmp_path, monkeypatch):
    """Redacting everything would make the command useless."""
    _project(tmp_path, monkeypatch, "env:\n  MY_SETTING: plain-value\n")

    out = _run("explain").output

    assert "plain-value" in out, out


def test_config_get_and_config_explain_agree(tmp_path, monkeypatch):
    """They disagreed, in the same file, for the same value.

    Pinned together so the next person to touch one is failed by the other.
    """
    _project(tmp_path, monkeypatch, f"env:\n  ANTHROPIC_API_KEY: {_SHORT_SECRET}\n")

    explain_out = _run("explain").output
    get_out = _run("get", "env.ANTHROPIC_API_KEY").output

    assert _SHORT_SECRET not in explain_out, explain_out
    assert _SHORT_SECRET not in get_out, get_out


# ── defect 2: name a remedy that works ─────────────────────────────────────

def test_the_leak_error_does_not_send_you_to_proxy(tmp_path, monkeypatch):
    """`auth use proxy` says proxy makes every session refuse to start."""
    _project(tmp_path, monkeypatch, f"env:\n  ANTHROPIC_API_KEY: {_SHORT_SECRET}\n")

    out = _run("check").output

    assert "proxy" not in out.lower(), (
        f"recommended the mode this very guard breaks:\n{out}")


def test_the_leak_error_names_broker_which_does_work(tmp_path, monkeypatch):
    """A refusal with no working way forward just moves the problem."""
    _project(tmp_path, monkeypatch, f"env:\n  ANTHROPIC_API_KEY: {_SHORT_SECRET}\n")

    out = _run("check").output

    assert "broker" in out.lower(), out


def test_the_shared_refusal_also_names_broker_not_proxy() -> None:
    """The same advice is emitted from the guard itself, not only the CLI.

    Driven through the real callable rather than asserting on source text — a
    substring of the module would pass even if the string were unreachable.
    """
    with pytest.raises(Refused) as exc:
        credential_leak_check.check_env_for_leaks(
            {"ANTHROPIC_API_KEY": _SHORT_SECRET}, source="test config")

    msg = str(exc.value)
    assert "broker" in msg.lower(), msg
    assert "agent-claude-proxy" not in msg, (
        f"the shared refusal still recommends the retired mode:\n{msg}")


def test_the_refusal_does_not_print_the_credential_either(tmp_path) -> None:
    """A refusal that quotes the value defeats its own purpose."""
    with pytest.raises(Refused) as exc:
        credential_leak_check.check_env_for_leaks(
            {"ANTHROPIC_API_KEY": _SHORT_SECRET}, source="test config")

    assert _SHORT_SECRET not in str(exc.value), str(exc.value)


# ── defect 3: the generated config must not advertise a retired mode ───────

def test_a_fresh_config_offers_no_retired_mode(tmp_path, monkeypatch) -> None:
    """`init` wrote three live-looking proxy references into the user's file.

    Generated through the real writer, so this cannot pass by my having edited
    one of the three strings and missed the others — which is what happened the
    first time.
    """
    from botainer.core.config import write_initial_config

    proj = tmp_path / "fresh"
    (proj / ".botainer").mkdir(parents=True)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    write_initial_config(proj, agent="claude", force=True)

    text = (proj / ".botainer" / "config.yaml").read_text()

    assert "proxy" not in text, (
        "the generated config still advertises proxy mode:\n"
        + "\n".join(l for l in text.splitlines() if "proxy" in l))


def test_the_fresh_config_still_offers_broker(tmp_path, monkeypatch) -> None:
    """Removing the dead option must not remove the live one with it."""
    from botainer.core.config import write_initial_config

    proj = tmp_path / "fresh2"
    (proj / ".botainer").mkdir(parents=True)
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    write_initial_config(proj, agent="claude", force=True)

    text = (proj / ".botainer" / "config.yaml").read_text()
    assert "agent-claude-broker" in text, text
