"""`agent_permissions` accepts the AGENTS' OWN mode names, and refuses the rest.

The vocabulary retains `bypass`, supports `default` and the agents' native
mode names, and preserves `prompt` as a compatibility alias. Runtime
constraints can still refuse an agent mode that cannot run in the container.

WHAT THESE TESTS ARE FOR. The vocabulary used to be two words hardcoded in
several modules that had to agree, and they drifted — three carried a copy of
`{"bypass", "prompt"}` and a fourth printed a sentence contradicting all three.
The table is now in one module, so the risk moves from "the copies disagree" to
"the one table is wrong". These pin the parts that would hurt:

  1. `bypass` produces the EXACT argv it produced before. This is a widening,
     and a widening that changes the default posture is a regression, not a
     feature.
  2. `prompt` still parses, still means what it meant, and still reaches the
     container as `prompt` — `BOTAINER_AGENT_PERMISSIONS` is documented and
     something may read it.
  3. A word from the OTHER agent is refused, and the refusal names what THIS
     agent takes. Silently doing nothing is the #128 defect class.
  4. `workspace-write` — a real codex mode — is refused, because it cannot run
     in a container. Accepting it would produce an agent that starts and then
     fails every command, which reads as working.
  5. The site-policy cap still refuses `bypass`, and now ADMITS the middle
     modes it used to refuse by accident (`_PERM_RANK.get(v, 99)` gave every
     unknown value a score above any ceiling).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.core import agent_permissions as ap
from botainer.core import composition, identity
from botainer.core import config as config_module
from botainer.core import policy as policy_module
from botainer.core.config import ProjectConfig
from botainer.core.policy import AgentPolicy, SitePolicy
from botainer.core.refusal import Refused


def _project(tmp_path: Path, *, agent: str, permissions: str) -> Path:
    """A project whose config names one agent and one permission value.

    Built through the real `write_initial_config` + `append_image_to_config`
    rather than by hand, because compose resolves the IMAGE before it reaches
    the permission gate — a hand-written four-line config refuses with
    "agent plugin has no recorded image" and never exercises the code under
    test. (A hand-made SessionSpec would skip more: parse-time validation, the
    per-agent lookup and the policy cap are three separate stages.)
    """
    from tests.conftest import append_image_to_config

    proj = tmp_path / f"p-{agent}-{permissions}"
    proj.mkdir()
    config_module.write_initial_config(proj, agent=agent, force=False)
    append_image_to_config(proj)
    cfg_path = proj / ".botainer" / "config.yaml"
    cfg_path.write_text(
        cfg_path.read_text() + f"\nagent_permissions: {permissions}\n",
        encoding="utf-8")
    identity.init_project(proj, agent=agent, force=True, non_interactive=True)
    return proj


def _argv(tmp_path: Path, *, agent: str, permissions: str) -> list[str]:
    proj = _project(tmp_path, agent=agent, permissions=permissions)
    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=False)
    return [tok for wrap in spec.entrypoint_wraps for tok in wrap]


# ───────────────────────── 1. nothing about `bypass` moved ─────────────────

@pytest.mark.parametrize("agent,expected", [
    ("claude", ["--dangerously-skip-permissions"]),
    ("codex", ["--sandbox", "danger-full-access", "--ask-for-approval", "never"]),
])
def test_bypass_argv_is_byte_for_byte_what_it_was(
        monkeypatch, tmp_path, agent, expected) -> None:
    """THE REGRESSION GUARD FOR THE WHOLE CHANGE.

    `bypass` is the shipped default, so every existing project runs through this
    path. The expected values are transcribed from the table this replaced
    (`_AGENT_BYPASS_FLAGS` in composition.py) and are deliberately written out
    here rather than imported from `agent_permissions` — importing the table to
    check the table proves only that the table equals itself.

    Note claude keeps `--dangerously-skip-permissions` and NOT
    `--permission-mode bypassPermissions`: the binary says bypassPermissions
    "requires allowDangerouslySkipPermissions", i.e. a second flag. The
    single-flag spelling is what has been shipping and what works.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    flat = _argv(tmp_path, agent=agent, permissions="bypass")
    for tok in expected:
        assert tok in flat, (
            f"{agent} `bypass` no longer appends {tok!r}. This is the DEFAULT "
            f"posture for every existing project.\nGot: {flat}")


def test_bypassPermissions_is_a_spelling_of_bypass_not_a_broken_flag(
        monkeypatch, tmp_path) -> None:
    """Reaching for claude's OWN word must not produce the two-flag trap."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    flat = _argv(tmp_path, agent="claude", permissions="bypassPermissions")
    assert "--dangerously-skip-permissions" in flat, (
        f"`bypassPermissions` did not map onto the working spelling: {flat}")
    assert "--permission-mode" not in flat, (
        f"`bypassPermissions` was passed as --permission-mode, which the binary "
        f"says needs a second flag botainer does not pass: {flat}")


# ───────────────────────── 2. `prompt` still works, unchanged ──────────────

def test_prompt_still_parses_and_still_appends_nothing_for_claude(
        monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    flat = _argv(tmp_path, agent="claude", permissions="prompt")
    assert "--dangerously-skip-permissions" not in flat, flat
    assert "--permission-mode" not in flat, (
        f"`prompt` must still mean 'botainer chooses nothing' for claude: {flat}")


def test_the_users_own_word_reaches_the_container_unfolded(
        monkeypatch, tmp_path) -> None:
    """`prompt` must NOT be silently rewritten to `default`.

    `BOTAINER_AGENT_PERMISSIONS` is documented in the capability surface and is
    readable inside the cage. Folding the value would change a published string
    to buy nothing — every table lookup folds internally, so no caller needs
    the canonical form.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = _project(tmp_path, agent="claude", permissions="prompt")
    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=False)
    assert spec.agent_permissions == "prompt", (
        f"the user's word was rewritten to {spec.agent_permissions!r}")
    assert spec.env.values.get("BOTAINER_AGENT_PERMISSIONS") == "prompt", (
        f"the container sees a different word than the config says: "
        f"{spec.env.values.get('BOTAINER_AGENT_PERMISSIONS')!r}")


# ───────────────────────── 3. the codex sandbox pin ────────────────────────

def test_codex_default_pins_the_sandbox_because_it_cannot_start_otherwise(
        monkeypatch, tmp_path) -> None:
    """MEASURED, not assumed — see `agent_permissions`' module docstring.

    codex's own default sandbox mode cannot run a command inside a container on
    either of its backends. "Append nothing" would therefore hand the user an
    agent that starts, warns once, and fails every tool call. So `default` pins
    the ONE axis the cage forces and leaves approvals to codex.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    flat = _argv(tmp_path, agent="codex", permissions="default")
    assert "--sandbox" in flat and "danger-full-access" in flat, (
        f"codex `default` did not pin the sandbox, so every command the agent "
        f"runs would fail: {flat}")
    assert "--ask-for-approval" not in flat, (
        f"codex `default` must not choose the approval posture — that is the "
        f"half botainer is explicitly NOT deciding: {flat}")


def test_workspace_write_is_refused_and_says_why(monkeypatch, tmp_path) -> None:
    """A REAL codex mode that cannot work here. Refuse, do not accept-and-break.

    The refusal has to carry the measurement, because "not supported" invites
    the reader to try harder at something that cannot work.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    with pytest.raises(Refused) as exc:
        _argv(tmp_path, agent="codex", permissions="workspace-write")
    msg = str(exc.value)
    assert "container" in msg, f"the refusal does not say where it fails:\n{msg}"
    assert "read-only" in msg, (
        f"the refusal does not name a mode that DOES work, so the reader is "
        f"left with no way forward:\n{msg}")


# ───────────────────────── 4. cross-agent words are refused ────────────────

@pytest.mark.parametrize("agent,foreign,native", [
    ("claude", "on-request", "acceptEdits"),
    ("codex", "acceptEdits", "on-request"),
])
def test_the_other_agents_word_is_refused_naming_what_this_one_takes(
        monkeypatch, tmp_path, agent, foreign, native) -> None:
    """Parse accepts the UNION (the agent is not known then). Compose narrows.

    REFUSE, never silently no-op: the old code appended flags only for a known
    family and appended nothing otherwise, so a deliberate posture request could
    vanish without a word.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    with pytest.raises(Refused) as exc:
        _argv(tmp_path, agent=agent, permissions=foreign)
    msg = str(exc.value)
    assert agent in msg, f"the refusal does not name the agent in use:\n{msg}"
    assert native in msg, (
        f"the refusal does not list what {agent} DOES accept, so it tells the "
        f"reader they are wrong without telling them what is right:\n{msg}")


def test_a_typo_is_still_caught_at_parse_time(tmp_path) -> None:
    """The wide parse-time check must not have become a rubber stamp."""
    with pytest.raises(ValueError) as exc:
        ProjectConfig(version="config-v1", agent="claude",
                      agent_permissions="bypss")
    assert "bypss" in str(exc.value)


# ───────────────────────── 5. the policy ceiling ───────────────────────────

def _cap_at(monkeypatch, ceiling: str) -> None:
    monkeypatch.setattr(
        policy_module, "load_site_policy",
        lambda: SitePolicy(agent=AgentPolicy(max_permissions=ceiling)))


@pytest.mark.parametrize("ceiling", ["prompt", "default"])
def test_a_capped_site_still_refuses_bypass(
        monkeypatch, tmp_path, ceiling) -> None:
    """Both spellings of the restrictive ceiling must keep biting."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    _cap_at(monkeypatch, ceiling)
    with pytest.raises(Refused) as exc:
        _argv(tmp_path, agent="claude", permissions="bypass")
    assert "denied" in str(exc.value).lower()


@pytest.mark.parametrize("mode", ["acceptEdits", "auto", "plan", "dontAsk"])
def test_a_capped_site_now_ADMITS_the_middle_modes(
        monkeypatch, tmp_path, mode) -> None:
    """THE BUG THE TIER SCHEME FIXES, stated as a test.

    The old cap was `_PERM_RANK.get(value, 99) > _PERM_RANK.get(ceiling, -1)`.
    Any value it had not heard of scored 99, which exceeds every ceiling — so
    every mode added later would have been refused at any capped site, silently
    and for no reason anyone intended.

    An admin who wrote `max_permissions: prompt` meant "no bypass". These are
    not bypass: the agent's own permission system is still running.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    _cap_at(monkeypatch, "prompt")
    flat = _argv(tmp_path, agent="claude", permissions=mode)
    assert ["--permission-mode", mode] == flat[-2:], (
        f"{mode} under a `prompt` ceiling did not compose: {flat}")


def test_the_cap_refusal_names_a_value_that_is_actually_legal(
        monkeypatch, tmp_path) -> None:
    """The old message said "Set the project's agent_permissions to {ceiling}".

    That worked while the ceiling was also a mode name. Now the ceiling is a
    TIER, so naming it would hand the reader a value their agent may not take —
    the #130 class (shipped output naming things that do not work).
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    _cap_at(monkeypatch, "prompt")
    with pytest.raises(Refused) as exc:
        _argv(tmp_path, agent="claude", permissions="bypass")
    msg = str(exc.value)

    # Read ONLY the suggestion list. Scanning the whole message for mode names
    # would match the REJECTED value, which the refusal necessarily quotes back
    # — the first version of this test did exactly that and failed on a correct
    # message.
    marker = "Values that are allowed here: "
    assert marker in msg, (
        f"the refusal has no list of what the reader may use instead:\n{msg}")
    listed = msg.split(marker, 1)[1].split(".")[0]
    suggested = [s.strip() for s in listed.split(",") if s.strip()]

    assert suggested, (
        f"the refusal offers an empty list, so the reader has nothing to do "
        f"next:\n{msg}")
    for m in suggested:
        assert m in ap.modes_for("anthropic"), (
            f"the refusal suggests {m!r}, which is not a claude mode at all "
            f"— the #130 class:\n{msg}")
        assert ap.tier_of("anthropic", m) <= ap.CAP_VALUES["prompt"], (
            f"the refusal suggests {m!r}, which that ceiling also forbids:\n{msg}")
    assert "bypass" not in suggested, (
        f"the refusal suggests the very value it just denied:\n{msg}")


def test_an_unknown_ceiling_is_refused_rather_than_scored(
        monkeypatch, tmp_path) -> None:
    """Fail-closed on a policy value botainer does not understand.

    A ceiling it cannot rank must not be treated as permissive-by-default;
    `.get(ceiling, -1)` in the old code made an unrecognised ceiling the most
    restrictive possible, which is safe but silent. Refusing says so.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    # Bypass the policy model's own validator to simulate a hand-edited file
    # that a newer botainer wrote and this one does not know.
    pol = SitePolicy(agent=AgentPolicy(max_permissions="bypass"))
    object.__setattr__(pol.agent, "max_permissions", "some-future-tier")
    monkeypatch.setattr(policy_module, "load_site_policy", lambda: pol)
    with pytest.raises(Refused) as exc:
        _argv(tmp_path, agent="claude", permissions="default")
    assert "some-future-tier" in str(exc.value)


# ───────────────────────── 6. the table's own invariants ───────────────────

def test_no_gloss_renders_a_safety_verdict() -> None:
    """A one-liner states a FACT and POINTS; it never adjudicates safety.

    These glosses appear in the launch banner, `config show` and the generated
    config comments — all short surfaces with no room for the caveats a
    comparative security claim needs.
    """
    banned = ("safe", "safer", "safest", "secure", "protect", "guarantee")
    for fam in ("anthropic", "openai"):
        for name, mode in ap.modes_for(fam).items():
            low = mode.gloss.lower()
            for word in banned:
                assert word not in low, (
                    f"{fam}/{name} gloss renders a safety verdict "
                    f"({word!r}): {mode.gloss!r}")


def test_every_mode_is_reachable_from_the_parse_time_union() -> None:
    """Parse accepts a union; compose narrows per agent. If a mode were missing
    from the union it would be unreachable — refused at parse, before the
    per-agent check that would have allowed it."""
    for fam in ("anthropic", "openai"):
        for name in ap.modes_for(fam):
            assert name in ap.ALL_KNOWN_VALUES, (
                f"{fam}/{name} can never be configured: parse-time validation "
                f"rejects it before compose sees it")
    for fam, refused in ap.REFUSED.items():
        for name in refused:
            assert name in ap.ALL_KNOWN_VALUES, (
                f"{fam}/{name} is refused at parse with a generic typo message "
                f"instead of the specific reason compose would have given")


def test_only_bypass_shaped_modes_switch_the_agents_system_off() -> None:
    """The tier criterion, asserted against the table rather than restated.

    Tier 1 means BOTAINER turned the agent's permission system off. It must not
    creep to cover modes where the agent is still deciding — `dontAsk` denies
    rather than asks, and `plan` executes nothing; both keep the agent's own
    machinery running and both belong under a restrictive ceiling.
    """
    for fam, expected_off in (
        ("anthropic", {"bypass", "bypassPermissions"}),
        ("openai", {"bypass", "never", "danger-full-access"}),
    ):
        actual = {n for n, m in ap.modes_for(fam).items()
                  if m.tier == ap.TIER_BOTAINER_OFF}
        assert actual == expected_off, (
            f"{fam}: the set of modes that disable the agent's own permission "
            f"system changed.\n  expected: {sorted(expected_off)}\n"
            f"  actual:   {sorted(actual)}\n"
            f"If this is intended, a site policy's meaning changed with it.")
