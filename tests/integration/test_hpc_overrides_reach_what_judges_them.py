"""An override that does not reach the thing judging it is worse than no override.

TWO DEFECTS, ONE SHAPE, both found by the refuting review of the commit that made
`hpc submit` accept `--agent` / `--auth-mode` / `--auth-profile` at all. In each case
the flag reached the launch and NOT the code that decides whether the launch is
allowed or which image it gets:

  * `--agent agent-codex` (the prefixed spelling every botainer-side reader
    tolerates) was assigned straight to `agent_name`, so the resolver built
    `images/botainer-agent-agent-codex.sif` and refused naming a file nobody typed —
    while `start --agent agent-codex` launched the real image. A laptop/cluster
    divergence.
  * `--auth-mode` never updated `plan.plugins_enabled`, which is read from the YAML,
    so the proxy guard judged stale data in BOTH directions: config `-proxy` plus
    `--auth-mode shared` was FALSELY refused (and the refusal told the user to make
    the persistent change they had just asked to avoid), and config `-shared` plus
    `--auth-mode proxy` BYPASSED the guard entirely.

The second direction was latent only because the proxy hook refuses unconditionally
and its escape hatches are not in the hook env allowlist — i.e. it was saved by an
unrelated accident, which is not the same as safe.

EVERY TEST HERE DRIVES `make_plan`, the real caller. Both defects were originally
visible only through it: calling the resolver or the guard directly with a
hand-written value passes whatever you pass, which is how a "component test" can
agree with a broken system.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_HELPER = REPO / "plugins/hpc-launcher/host_helper/_common.py"
_SUBMIT = REPO / "plugins/hpc-launcher/host_helper/submit.py"


def _load(name: str, path: Path):
    """Import a standalone helper by path — they are not installed modules.

    Registered in sys.modules BEFORE exec because the helper defines dataclasses,
    and `dataclasses` resolves string annotations via the module's sys.modules
    entry. Omitting it fails on None.__dict__.

    AND `submit.py` does `from _common import …` — a sibling import that only
    resolves with the helper directory on sys.path and `_common` importable under
    that exact name. Loading it under a unique alias left a half-executed module
    whose missing attributes looked like the test being wrong rather than the
    loader: my first version failed with AttributeError on `_refuse_proxy_on_hpc`.
    So: the directory goes on the path, and `_common` is registered as `_common`.
    """
    helper_dir = str(_HELPER.parent)
    if helper_dir not in sys.path:
        sys.path.insert(0, helper_dir)
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    if path == _HELPER:
        # The name `submit.py` will import it by.
        sys.modules.setdefault("_common", mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hpc(monkeypatch, tmp_path):
    """A state root + images dir + project factory, with the env `make_plan` reads."""
    common = _load("hpc_launcher_common_overrides", _HELPER)
    root = tmp_path / "root"
    (root / "images").mkdir(parents=True)
    monkeypatch.setenv("BOTAINER_STATE_ROOT", str(root))
    monkeypatch.setenv("BOTAINER_PROJECT_UUID", str(uuid.uuid4()))
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)

    counter = {"n": 0}

    def project(agent: str = "claude", plugins: tuple[str, ...] = ()) -> Path:
        counter["n"] += 1
        proj = tmp_path / f"proj{counter['n']}"
        (proj / ".botainer").mkdir(parents=True)
        body = f"agent: {agent}\n"
        if plugins:
            body += "plugins_enabled:\n" + "".join(f"  - {p}\n" for p in plugins)
        (proj / ".botainer" / "config.yaml").write_text(body)
        return proj

    return common, root, project


def test_the_PREFIXED_agent_flag_resolves_the_same_image_as_the_short_one(hpc):
    """THE DEFECT. `--agent agent-codex` built `botainer-agent-agent-codex.sif`.

    The prefix rule lived inside `_load_agent_name`, which only the CONFIG path goes
    through — so the flag got no rule at all. It is one function now, called by both.
    """
    common, root, project = hpc
    proj = project(agent="claude")

    short = common.make_plan(proj, agent_override="codex")
    prefixed = common.make_plan(proj, agent_override="agent-codex")

    assert short.agent_name == "codex"
    assert prefixed.agent_name == "codex", (
        f"the flag kept its prefix: {prefixed.agent_name!r}")
    assert prefixed.apptainer_image == short.apptainer_image, (
        f"two spellings of one agent resolved different images:\n"
        f"  --agent codex       -> {short.apptainer_image}\n"
        f"  --agent agent-codex -> {prefixed.apptainer_image}")
    assert "agent-agent" not in prefixed.apptainer_image, prefixed.apptainer_image


def test_a_DOUBLED_prefix_on_the_FLAG_is_refused_by_name(hpc):
    """One prefix is a spelling confusion; two is not, and guessing would be worse.

    The refusal must name the short form — the previous behaviour named a `.sif`
    path the user never typed, which sends them to rebuild an image rather than to
    fix the flag.
    """
    common, root, project = hpc
    proj = project(agent="claude")

    with pytest.raises((ValueError, SystemExit)) as exc:
        common.make_plan(proj, agent_override="agent-agent-codex")

    msg = str(exc.value)
    assert "agent-agent-codex" in msg and "--agent claude" in msg, msg
    assert ".sif" not in msg, (
        f"the refusal talks about an image file instead of the flag: {msg}")


def test_auth_mode_shared_is_not_refused_as_PROXY_because_the_config_says_proxy(hpc):
    """Direction one, and it made `--auth-mode` useless for the case that needs it.

    A user on a proxy-configured project asking for `--auth-mode shared` — i.e.
    "not proxy, just this once" — was refused BECAUSE the config said proxy, and the
    refusal told them to edit the config, which is the persistent change the flag
    exists to avoid.
    """
    common, root, project = hpc
    submit = _load("hpc_launcher_submit_overrides", _SUBMIT)
    proj = project(agent="claude", plugins=("agent-claude-proxy", "git"))

    plan = common.make_plan(proj, auth_mode_override="shared")

    assert submit._refuse_proxy_on_hpc(plan) == 0, (
        f"refused a shared-mode submit because the FILE says proxy; effective "
        f"plugins were {plan.plugins_enabled}")
    assert "agent-claude-shared" in plan.plugins_enabled, plan.plugins_enabled
    assert "agent-claude-proxy" not in plan.plugins_enabled, plan.plugins_enabled
    assert "git" in plan.plugins_enabled, (
        f"a non-agent plugin was dropped while applying the auth mode: "
        f"{plan.plugins_enabled}")


def test_auth_mode_proxy_IS_refused_even_though_the_config_says_shared(hpc):
    """Direction two, and it is the one that mattered: the guard was BYPASSED.

    Proxy auth is unsupported on the sbatch path, so asking for it must refuse
    wherever the request came from. Before this, the flag route reached the launch
    and the guard never saw it — latent only because the proxy hook happens to
    refuse unconditionally, which is an accident and not this guard working.
    """
    common, root, project = hpc
    submit = _load("hpc_launcher_submit_overrides", _SUBMIT)
    proj = project(agent="claude", plugins=("agent-claude-shared",))

    plan = common.make_plan(proj, auth_mode_override="proxy")

    assert submit._refuse_proxy_on_hpc(plan) != 0, (
        f"`--auth-mode proxy` walked past the proxy guard; effective plugins were "
        f"{plan.plugins_enabled}")


def test_agent_AND_auth_mode_together_leave_exactly_ONE_agent_plugin(hpc):
    """A DEFECT IN MY OWN FIX, caught by measuring the combination.

    The first version stripped only variants of the CHOSEN base name, so
    `--agent codex --auth-mode shared` on a claude-configured project returned BOTH
    `agent-claude-shared` and `agent-codex-shared`. The proxy guard and the consent
    disclosure then read a two-agent set that no session can be — and "exactly one
    INNER-layer agent wrap" is a compose invariant, so the set was not merely
    cosmetically wrong.

    A submission runs ONE agent: the one named. Another family's agent plugin is
    never part of the answer.
    """
    common, root, project = hpc
    proj = project(agent="claude", plugins=("agent-claude-shared", "git"))

    plan = common.make_plan(proj, agent_override="codex",
                            auth_mode_override="shared")

    agents = [p for p in plan.plugins_enabled if p.startswith("agent-")]
    assert agents == ["agent-codex-shared"], (
        f"expected exactly the requested agent's plugin, got {agents} "
        f"(full set {plan.plugins_enabled})")
    assert "git" in plan.plugins_enabled, plan.plugins_enabled


def test_no_auth_mode_flag_changes_NOTHING(hpc):
    """The control, and it is the point of the whole override design: a submission
    with no flags must render exactly what it rendered before. A fix that rewrote
    the plugin set unconditionally would pass every test above."""
    common, root, project = hpc
    proj = project(agent="claude", plugins=("agent-claude-proxy", "git", "nudge"))

    plan = common.make_plan(proj)

    assert plan.plugins_enabled == ("agent-claude-proxy", "git", "nudge"), (
        f"the config's plugin list was altered with no flag given: "
        f"{plan.plugins_enabled}")


def test_isolated_mode_means_the_BARE_agent_plugin(hpc):
    """`isolated` is spelled by the ABSENCE of a suffix, not by `-isolated`.

    Getting this wrong would name `agent-claude-isolated`, which is not a plugin
    that exists — and the guard would then see no agent plugin at all, which reads
    as "nothing to check".
    """
    common, root, project = hpc
    proj = project(agent="claude", plugins=("agent-claude-shared",))

    plan = common.make_plan(proj, auth_mode_override="isolated")

    assert "agent-claude" in plan.plugins_enabled, plan.plugins_enabled
    assert not any(p.startswith("agent-claude-") for p in plan.plugins_enabled), (
        f"a suffixed variant survived an isolated-mode override: "
        f"{plan.plugins_enabled}")


# FOUR TESTS WERE DELETED HERE WITH THE CODE THEY PINNED (2026-09-13). They held
# the launcher's own projection of `plugins_enabled` to its stated rules: that a
# not-installed variant changes nothing, that `variant_is_installed` answers the
# real question, that the mode→plugin convention matched
# `config._agent_variant_for_mode`, and that an override with no agent name drops
# agent plugins rather than guessing. All four described a SECOND
# implementation, which is gone: `make_plan` now asks
# `composition.apply_plugin_overrides`, the one compose calls. A parity test
# holding two conventions equal is only meaningful while there are two.
#
# ── ONE SOURCE OF TRUTH for "which plugins will this session activate" ───────
#
# The refuting review of the commit that added `plan.plugins_enabled` MEASURED
# it diverging from what `compose_agent_exec_for_hpc` composes, because the
# launcher mirrored ONE of compose's three transforms. (Its COUNT and its
# example are struck: the figure "4 of 7" and the case `--agent codex
# --auth-mode shared` did not reproduce — that combination exits 0. The
# measured matrix is below, and the corrected example is one flag, not two.)
# The worst case is a FALSE REFUSAL through the real CLI: `--agent codex` on an
# `agent-claude-proxy` project is refused AS PROXY while the composition that
# would run has no proxy plugin at all.
#
# `composition.apply_plugin_overrides` is now all three transforms, and
# `make_plan` calls it, so `plan.plugins_enabled` IS the composed answer before
# any guard or hook runs. This test drives BOTH sides for the exact combinations
# that diverged.

# HOW MANY COMBINATIONS DIVERGE — and my own first answer here was WRONG, which
# is why the count is now attributed to the run that produced it. I measured 15
# combinations and wrote "2 diverge, both `--agent X` with no `--auth-mode`".
# The loop tzar's checkpoint-9 matrix is 72 (6 project shapes × 12 flag pairs)
# and finds 18, including two classes my matrix could not contain:
#
#   * NO FLAGS AT ALL, on any project `auth use shared` has touched — it enables
#     the variant for EVERY installed family, so the config holds
#     [agent-claude-shared, agent-codex-shared, git]; the launcher passed all
#     three, compose activates agent-claude-shared alone.
#   * `--agent codex --auth-mode proxy`, where no `agent-codex-proxy` is
#     installed: the launcher returns the config set unchanged while compose
#     still applies the agent swap. Guard verdict flips rc 2 → 0.
#
# So "divergence needs `--agent` and no `--auth-mode`" is false, and the
# narrower earlier claim (from the review of the previous commit) is not
# something I can adjudicate from a 15-cell matrix. The lesson recorded, because
# it is the third time in this run: a matrix is an existence proof, never a
# completeness one.
#
# The cases below are the ones I can drive here. `_DIVERGED`'s first two are
# from my own matrix; the zero-flag case has its own test underneath.
_DIVERGED = [
    # (config plugins, --agent, --auth-mode)
    (("agent-claude-shared", "git"), "codex", None),
    (("agent-claude-proxy", "git"), "codex", None),
    # Combinations that agreed already and must keep agreeing.
    (("agent-claude-shared", "git"), "codex", "shared"),
    (("agent-claude-proxy", "git"), "codex", "shared"),
    (("agent-codex-shared", "git"), "claude", "shared"),
    (("agent-claude-shared", "git"), None, None),
]


@pytest.mark.parametrize("plugins,agent,mode", _DIVERGED)
def test_the_guards_judge_the_set_COMPOSE_will_activate(
        hpc, tmp_path, monkeypatch, plugins, agent, mode):
    """The launcher's guard input == the composition's own answer.

    Not "the launcher's rule matches a rule I wrote down": both sides are
    driven, and the assertion is equality of the two real outputs. A second
    derivation that agrees today is still a second derivation, so the fix was to
    delete the launcher's and call compose's — this test is what would catch it
    coming back.
    """
    from botainer.core import composition, identity

    common, root, project = hpc
    submit = _load("hpc_launcher_submit_onesource", _SUBMIT)
    proj = project(agent="claude", plugins=plugins)
    (proj / ".botainer" / "config.yaml").write_text(
        (proj / ".botainer" / "config.yaml").read_text() + "\nimage: ubuntu:24.04\n")
    monkeypatch.setenv("MY_BOTAINER", str(root))
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)

    plan = common.make_plan(proj, agent_override=agent, auth_mode_override=mode)
    judged = plan.plugins_enabled

    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=True,
        agent_override=agent, auth_mode_override=mode)

    # SETS, not tuples: `compose_session` sorts the field on the spec
    # (composition.py — `plugins_enabled=tuple(sorted(enabled))`, so that the
    # one-agent guard cannot depend on hash order), while the guards only ask
    # membership. Asserting order here would pin a detail neither side's
    # behaviour depends on — and would have failed on the very fix it is meant
    # to protect.
    assert set(judged) == set(spec.plugins_enabled), (
        f"the guards would judge {sorted(judged)} while the session composes "
        f"{sorted(spec.plugins_enabled)} — a second source of truth is back"
    )
    # And the invariant that made the divergence a real defect, not cosmetic.
    agents = [p for p in judged if p.startswith("agent-")]
    assert len(agents) <= 1, f"two agent plugins in the judged set: {agents}"


def test_the_GUARD_ITSELF_is_given_the_composed_set(hpc, tmp_path, monkeypatch):
    """Pins the CALL SITE, not just the helper.

    The previous test proves `make_plan`'s answer agrees with compose; it would
    still pass if the guards were handed something else. So: drive
    `submit.main()` for the combination that produced the false refusal, capture
    the plan the proxy guard actually receives, and refuse from the stub so
    nothing downstream runs (no hook, no sbatch).
    """
    from botainer.core import identity

    common, root, project = hpc
    submit = _load("hpc_launcher_submit_callsite", _SUBMIT)
    proj = project(agent="claude", plugins=("agent-claude-proxy", "git"))
    cfg = proj / ".botainer" / "config.yaml"
    cfg.write_text(cfg.read_text() + "\nimage: ubuntu:24.04\n")
    monkeypatch.setenv("MY_BOTAINER", str(root))
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    monkeypatch.chdir(proj)

    seen: dict[str, tuple[str, ...]] = {}

    def _capture(plan):
        seen["plugins"] = tuple(plan.plugins_enabled)
        return 2                       # refuse here: nothing after this runs

    monkeypatch.setattr(submit, "_refuse_proxy_on_hpc", _capture)
    monkeypatch.setattr(submit, "_refuse_unenforceable_network", lambda plan: 0)

    # `main()` reads sys.argv (it is a script entry point, not a library call).
    monkeypatch.setattr(sys, "argv", [
        "submit.py", "--time", "120", "--partition", "day", "--yes",
        "--agent", "codex"])
    rc = submit.main()

    assert rc == 2, "the stub guard's refusal did not reach the caller"
    assert "plugins" in seen, "the proxy guard was never called"
    assert "agent-claude-proxy" not in seen["plugins"], (
        f"the guard was handed the config's proxy plugin, which this session "
        f"would not activate — the false refusal is back: {seen['plugins']}")
    assert any(p.startswith("agent-codex") for p in seen["plugins"]), seen["plugins"]


def test_a_TWO_FAMILY_project_with_NO_FLAGS_is_judged_as_ONE_agent(hpc, monkeypatch):
    """THE CASE A MUTATION AND A CHECKPOINT BOTH CAUGHT ME MISSING.

    `botainer auth use shared` enables the shared variant for EVERY installed
    family by design, so a perfectly ordinary project config can list
    `[agent-claude-shared, agent-codex-shared, git]`. With NO flags at all the
    launcher passed all three to the guards, while the session activates exactly
    one — and "exactly one INNER-layer agent wrap" is a compose invariant, so the
    guards were judging a set no session can be.

    Mutation M8 at checkpoint 9 (consult the composed set ONLY when `--agent` is
    given) passed 422 tests before this existed.
    """
    from botainer.core import composition, identity

    common, root, project = hpc
    submit = _load("hpc_launcher_submit_twofamily", _SUBMIT)
    proj = project(agent="claude",
                   plugins=("agent-claude-shared", "agent-codex-shared", "git"))
    cfg = proj / ".botainer" / "config.yaml"
    cfg.write_text(cfg.read_text() + "\nimage: ubuntu:24.04\n")
    monkeypatch.setenv("MY_BOTAINER", str(root))
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)

    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=True)

    # THROUGH `submit.main()`, not through the helper: the first version of this
    # test asked the composer directly and the checkpoint-9 mutation (ask it only
    # when `--agent` is given) still passed. What the GUARD receives is the thing
    # under test, so the guard has to be the one that reports.
    monkeypatch.chdir(proj)
    seen: dict[str, tuple[str, ...]] = {}

    def _capture(plan):
        seen["plugins"] = tuple(plan.plugins_enabled)
        return 2                       # refuse here: nothing after this runs

    monkeypatch.setattr(submit, "_refuse_proxy_on_hpc", _capture)
    monkeypatch.setattr(submit, "_refuse_unenforceable_network", lambda plan: 0)
    monkeypatch.setattr(sys, "argv",
                        ["submit.py", "--time", "120", "--partition", "day", "--yes"])

    assert submit.main() == 2, "the stub guard's refusal did not reach the caller"
    judged = seen.get("plugins")
    assert judged is not None, "the proxy guard was never called"
    assert set(judged) == set(spec.plugins_enabled), (
        f"guards judge {sorted(judged)}, session composes "
        f"{sorted(spec.plugins_enabled)}")
    agents = [p for p in judged if p.startswith("agent-")]
    assert agents == ["agent-claude-shared"], (
        f"the guards are judging {agents} — a set no session can be")


def test_the_no_botainer_FALLBACK_says_which_answer_you_got(hpc, tmp_path, capsys):
    """The fallback is honest only if it is audible. A mutation proved it was not.

    When botainer cannot be asked — a login node without the package, or a config
    the strict loader refuses — the guards fall back to the config's list, which
    reflects neither `--agent`/`--auth-mode` nor the one-agent rule. That is the
    behaviour that shipped for months and it is better than no guard, but silence
    would leave a user unable to tell which of the two answers they were given.
    Silencing the note passed every other test in this file.
    """
    common, root, _project = hpc
    listed = ("agent-claude-shared", "agent-codex-shared", "git")

    got = common.authoritative_plugins_enabled(
        tmp_path / "no-such-project", listed, auth_mode_override="broker")

    assert got == listed, f"the fallback invented an answer: {got}"
    err = capsys.readouterr().err
    assert "could not ask botainer" in err and "config's list" in err, (
        f"the fallback was silent, so nobody can tell which answer they got: "
        f"{err!r}")


def test_BROKER_is_refused_BEFORE_the_broker_starts(hpc, tmp_path, monkeypatch, capsys):
    """A mode that cannot work here must not first touch the credential it breaks.

    Compose already refuses broker on the sbatch path — the claude broker's
    pre_session hook contributes a `unix-socket` bind and `_refuse_cross_node_binds`
    rejects it (verified against the real function: a unix-socket bind raises
    `unsupported-runtime-feature`, the same bind stored `rw` does not). But that
    refusal is AFTER `run_pre_session_hooks`, so the broker has already started and
    done a host-side OAuth refresh, which can rotate the refresh token and log the
    user's other projects out — for a session that then does not launch.

    The assertion that matters is not the exit code but WHAT DID NOT HAPPEN:
    `compose_agent_exec_for_hpc` is replaced with a bomb, so reaching compose at
    all fails the test.
    """
    from botainer.core import composition, identity

    common, root, project = hpc
    submit = _load("hpc_launcher_submit_brokergate", _SUBMIT)
    proj = project(agent="claude", plugins=("agent-claude-broker", "git"))
    cfg = proj / ".botainer" / "config.yaml"
    cfg.write_text(cfg.read_text() + "\nimage: ubuntu:24.04\n")
    monkeypatch.setenv("MY_BOTAINER", str(root))
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    monkeypatch.chdir(proj)

    def _bomb(*a, **k):
        raise AssertionError(
            "compose_agent_exec_for_hpc was reached — which runs pre_session "
            "hooks, i.e. starts the broker and may rotate the refresh token. The "
            "refusal must come BEFORE this.")

    monkeypatch.setattr(composition, "compose_agent_exec_for_hpc", _bomb)
    monkeypatch.setattr(sys, "argv",
                        ["submit.py", "--time", "120", "--partition", "day", "--yes"])

    rc = submit.main()
    err = capsys.readouterr().err

    # RC ALONE PROVED NOTHING — two mutations (no gate at all; the gate matching a
    # name that cannot occur) both left this test passing, because this fixture
    # has no credential and the credential-presence gate also returns 2. Assert
    # the sentence, which only the broker gate writes.
    assert rc == 2, f"broker on the sbatch path was not refused (rc={rc})\n{err}"
    assert "broker auth" in err and "cannot run" in err, (
        f"refused for some other reason, so this says nothing about broker:\n{err}")
    assert "salloc" in err and "auth use shared" in err, (
        f"the refusal names no route that works:\n{err}")
