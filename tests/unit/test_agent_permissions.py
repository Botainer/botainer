"""#53 / T0-2: agent in-cage permission posture (bypass|prompt).

Covers the full chain: config parse/validate → policy ceiling → compose
resolution + env injection → entrypoint-wrapper flag mapping → capability-summary
disclosure. See DN-010.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from botainer.core import composition, identity
from botainer.core import config as config_module
from botainer.core import policy as policy_module
from botainer.core.config import ProjectConfig
from botainer.core.policy import AgentPolicy, SitePolicy, intersect
from botainer.core.refusal import Refused
from botainer.inspect import capability_summary
from botainer.state import dir as state_dir

REPO = Path(__file__).resolve().parents[2]
CLAUDE_WRAP = REPO / "plugins" / "agent-claude" / "entrypoint_wrap.sh"
CODEX_WRAP = REPO / "plugins" / "agent-codex" / "entrypoint_wrap.sh"


# ───────────────────────── config layer ─────────────────────────


def test_config_default_is_bypass() -> None:
    assert ProjectConfig().agent_permissions == "bypass"


def test_config_accepts_prompt() -> None:
    assert ProjectConfig(agent_permissions="prompt").agent_permissions == "prompt"


def test_config_refuses_bad_value() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError wrapping ValueError
        ProjectConfig(agent_permissions="bypss")


# ───────────────────────── policy ceiling ─────────────────────────


def test_policy_default_ceiling_is_bypass() -> None:
    assert SitePolicy().agent.max_permissions == "bypass"


def test_policy_refuses_bad_ceiling() -> None:
    with pytest.raises(Exception):
        AgentPolicy(max_permissions="nope")


def test_ceiling_intersect_takes_most_restrictive() -> None:
    site = SitePolicy(agent=AgentPolicy(max_permissions="prompt"))
    user = SitePolicy(agent=AgentPolicy(max_permissions="bypass"))
    # prompt (0) < bypass (1) → most restrictive wins regardless of order.
    assert intersect(site, user).agent.max_permissions == "prompt"
    assert intersect(user, site).agent.max_permissions == "prompt"


def test_ceiling_intersect_both_bypass() -> None:
    assert intersect(SitePolicy(), SitePolicy()).agent.max_permissions == "bypass"


# ───────────────────────── compose resolution + env injection ─────────────────────────


def _prepare_project(tmp_path: Path, *, permissions: str | None = None) -> Path:
    from tests.conftest import append_image_to_config

    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    append_image_to_config(proj)
    if permissions is not None:
        cfg_path = proj / ".botainer" / "config.yaml"
        cfg_path.write_text(
            cfg_path.read_text() + f"\nagent_permissions: {permissions}\n"
        )
    return proj


def test_compose_default_bypass_sets_spec_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert spec.agent_permissions == "bypass"
    # Injected as an in-container posture signal.
    assert spec.env.values.get("BOTAINER_AGENT_PERMISSIONS") == "bypass"
    # The FLAG is appended to the innermost entrypoint wrap by first-party
    # compose (rebuild-independent) — NOT the image wrapper. This is what
    # actually makes the agent run with no prompts on ANY image version.
    flat = [tok for wrap in spec.entrypoint_wraps for tok in wrap]
    assert "--dangerously-skip-permissions" in flat
    # …and it is the LAST token (trailing arg → the wrapper's "$@" → claude).
    assert flat[-1] == "--dangerously-skip-permissions"


def test_compose_prompt_config_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path, permissions="prompt")
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert spec.agent_permissions == "prompt"
    assert spec.env.values.get("BOTAINER_AGENT_PERMISSIONS") == "prompt"
    # prompt → NO bypass flag appended.
    flat = [tok for wrap in spec.entrypoint_wraps for tok in wrap]
    assert "--dangerously-skip-permissions" not in flat


def test_compose_refuses_bypass_when_ceiling_is_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path, permissions="bypass")
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    # Site policy caps at prompt; project requests bypass → refuse (fail-closed).
    monkeypatch.setattr(
        policy_module,
        "load_site_policy",
        lambda: SitePolicy(agent=AgentPolicy(max_permissions="prompt")),
    )
    monkeypatch.setattr(policy_module, "site_policy_present", lambda: True)
    with pytest.raises(Refused) as ei:
        composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert "agent_permissions" in str(ei.value)
    assert "prompt" in str(ei.value)


def test_compose_prompt_allowed_under_prompt_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "s"))
    proj = _prepare_project(tmp_path, permissions="prompt")
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    monkeypatch.setattr(
        policy_module,
        "load_site_policy",
        lambda: SitePolicy(agent=AgentPolicy(max_permissions="prompt")),
    )
    spec = composition.compose_session(proj, runtime_choice="mock", identity_accept=False)
    assert spec.agent_permissions == "prompt"


# ───── entrypoint-wrapper forwarding contract (the flag is appended by compose,
#       so the wrapper must FORWARD its "$@" verbatim to the agent CLI) ─────


def _run_wrapper(wrapper: Path, agent_bin: str, extra_args: list[str], tmp_path: Path) -> str:
    """Run the wrapper with a fake agent binary on PATH that echoes its argv,
    passing extra_args as the wrapper's own args (mimicking the compose-appended
    trailing flags)."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake = fake_bin / agent_bin
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "OPENAI_API_KEY_FILE": str(tmp_path / "nope"),
        "HOME": str(tmp_path),
    }
    r = subprocess.run(
        ["sh", str(wrapper), *extra_args],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_claude_wrapper_forwards_appended_flag_to_claude(tmp_path: Path) -> None:
    """The wrapper is a dumb forwarder: a flag compose appended (as the
    wrapper's arg) must reach claude's argv. This is the contract the
    rebuild-independent compose-append relies on."""
    out = _run_wrapper(CLAUDE_WRAP, "claude", ["--dangerously-skip-permissions"], tmp_path)
    assert "--dangerously-skip-permissions" in out


def test_claude_wrapper_adds_no_flag_on_its_own(tmp_path: Path) -> None:
    """With NO appended args, the wrapper must NOT invent a permission flag
    (the posture decision lives in compose, not the image)."""
    out = _run_wrapper(CLAUDE_WRAP, "claude", [], tmp_path)
    assert "--dangerously-skip-permissions" not in out


def test_codex_wrapper_forwards_appended_flags_to_codex(tmp_path: Path) -> None:
    out = _run_wrapper(
        CODEX_WRAP, "codex",
        ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        tmp_path,
    )
    for tok in ("--sandbox", "danger-full-access", "--ask-for-approval", "never"):
        assert tok in out


def test_codex_wrapper_adds_no_flag_on_its_own(tmp_path: Path) -> None:
    out = _run_wrapper(CODEX_WRAP, "codex", [], tmp_path)
    assert "danger-full-access" not in out


# ───────────────────────── capability-summary disclosure ─────────────────────────


def _spec_with_perms(perms: str):
    from tests.unit.test_capability_summary import _make_spec

    return _make_spec().model_copy(update={"agent_permissions": perms})


def test_summary_discloses_bypass_loudly() -> None:
    out = capability_summary.render_multiline(_spec_with_perms("bypass"))
    assert "bypass" in out.lower()
    assert "UNATTENDED" in out
    assert "nothing will ask" in out.lower()


def test_the_banner_never_asserts_what_the_agent_will_do() -> None:
    """REPLACES `test_summary_discloses_prompt_and_batch_warning`, WHICH PINNED
    A FALSE CLAIM IN PLACE.

    That test asserted the banner said "DEADLOCK" for the non-bypass posture.
    The full sentence was: "the agent asks before consequential actions; needs
    a TTY — an unattended/batch job would DEADLOCK at the first prompt."

    botainer appends NOTHING about approvals in that mode, so both halves were
    claims about a third-party binary's behaviour. If the agent's own default
    is a selective mode rather than ask-about-everything, the first half is
    false — and so is the deadlock warning, which is the half that matters on a
    cluster, because it may tell you a batch job will hang when it will not.

    The test is inverted rather than deleted: the banner must now state what
    BOTAINER did (the appended argv, which botainer knows) and must NOT predict
    the agent's behaviour (which it does not). A future edit that re-introduces
    a behavioural promise fails here.
    """
    out = capability_summary.render_multiline(_spec_with_perms("prompt"))

    # It still names the posture the user set, in their own words.
    assert "prompt" in out, f"the banner no longer says which posture is set:\n{out}"

    # It states what botainer did — the checkable half.
    assert "botainer appended" in out, (
        f"the banner does not say what botainer actually appended, which is the "
        f"only part of this it knows for certain:\n{out}")

    # And it does NOT predict the agent.
    assert "DEADLOCK" not in out.upper(), (
        f"the banner predicts a deadlock. botainer appends nothing in this "
        f"mode, so it cannot know that:\n{out}")
    assert "asks before consequential" not in out, (
        f"the banner asserts the agent asks. botainer appends nothing in this "
        f"mode, so that is the AGENT's choice, not botainer's:\n{out}")


def test_the_bypass_banner_does_not_name_a_remedy_that_breaks_codex() -> None:
    """THE DEFAULT BANNER USED TO HAND THE READER A BROKEN INSTRUCTION.

    `bypass` is the shipped default, so its branch is the one nearly every user
    sees. It ended: "set `agent_permissions: prompt` in .botainer/config.yaml to
    restore per-action prompts."

    Measured on codex-cli 0.145.0: following that does not restore prompts. It
    produces an agent that starts, warns once, and then fails every command,
    because codex's own sandbox cannot start inside a container. The remedy
    broke the session it claimed to fix — the #130 class (shipped output naming
    commands that do not exist), one step worse, because this command runs.

    The banner must not print a config-change remedy it cannot stand behind for
    the agent actually in use.
    """
    out = capability_summary.render_multiline(_spec_with_perms("bypass"))
    assert "agent_permissions: prompt" not in out, (
        f"the default banner still tells the reader to set `prompt`, which on "
        f"codex produces an agent that cannot run a command:\n{out}")
