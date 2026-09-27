"""`hpc submit` must accept the overrides `botainer start` accepts. (queue 118)

REPRODUCED BY RUNNING both --help:

    $ botainer start --help
        --agent TEXT                    One-shot override of WHICH AGENT runs this
        --auth-mode [isolated|shared|broker|proxy]
        --auth-profile TEXT             One-shot override of auth profile for this

    $ botainer hpc submit --help
        --image TEXT                    Override apptainer .sif path.

    $ botainer hpc submit --agent codex --dry-run
    Usage: botainer hpc submit [OPTIONS]        <- rejected, unknown option

So the laptop could say "run this project with codex tonight" and the cluster
could not. Apptainer is a first-class peer to docker in this project, not a
documentation afterthought: a capability the laptop path gains and the cluster
path does not is a defect. `bot1 image build` is the canonical bad example —
it shipped docker-only, with the apptainer side left as a manual workaround.

NOT TWO IMPLEMENTATIONS. `compose_agent_exec_for_hpc` has always CALLED
`compose_session` — its own docstring says it runs "the identical composition
the direct apptainer path runs". It declared only `image_override`, so the other
three were dropped at the wrapper. The CLI does not call composition directly:
it builds an argv for the
plugin's own argparse, which calls `make_plan`, which builds the
`SubmissionPlan`. Five places, not one.

WHY agent_override IS SEPARATE FROM agent_name. `agent_name` is "which agent is
this session", which config supplies and which `_resolve_apptainer_image` uses
to pick the .sif. `agent_override` is "the user typed --agent". Only the latter
reaches `compose_session`, so a submission with no flags passes None for all
three and renders byte-identical argv — verified by diffing `hpc submit
--dry-run` output across the change.
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HPC_HOST_HELPER = REPO / "plugins" / "hpc-launcher" / "host_helper"


def _load(name: str, filename: str):
    """Load a plugin host_helper module by path.

    NO skip fallback. If these cannot load, every assertion below is vacuous
    and the file must fail loudly rather than report green.
    """
    path = HPC_HOST_HELPER / filename
    assert path.is_file(), f"missing {path}"
    # submit.py does `from _common import ...` — a sibling import that works
    # because the helper runs with its own directory on sys.path. Reproduce
    # that here rather than rewriting the import: the point is to exercise the
    # module as it actually runs.
    if str(HPC_HOST_HELPER) not in sys.path:
        sys.path.insert(0, str(HPC_HOST_HELPER))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def submit_mod():
    return _load("hpc_submit_overrides_test", "submit.py")


@pytest.fixture(scope="module")
def common_mod():
    return _load("hpc_common_overrides_test", "_common.py")


def _project(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "proj"
    (root / ".botainer").mkdir(parents=True)
    (root / ".botainer" / "project-id").write_text("6" * 32 + "\n")
    (root / ".botainer" / "config.yaml").write_text(
        "version: config-v1\nagent: claude\nruntime: apptainer\n"
        "profile: default\nnetwork:\n  mode: internet\n"
        "plugins_enabled: [agent-claude]\n")
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    monkeypatch.setenv("BOTAINER_PROJECT_UUID", "6" * 32)
    return root


# ── the plugin's own parser accepts them ────────────────────────────────────

def test_the_parser_accepts_all_three(submit_mod) -> None:
    """THE DEFECT: `--agent codex` was an unknown option."""
    ns = submit_mod.parse_args(
        ["--agent", "codex", "--auth-mode", "broker",
         "--auth-profile", "work", "--dry-run"])

    assert ns.agent == "codex", ns
    assert ns.auth_mode == "broker", ns
    assert ns.auth_profile == "work", ns


@pytest.mark.parametrize("flag,value", [
    ("--agent", "../../etc"),
    ("--agent", "a\nb"),
    ("--auth-profile", "a\n#SBATCH --uid=0"),
    ("--auth-profile", "has space"),
    ("--auth-profile", "semi;colon"),
])
def test_a_hostile_value_is_refused_BY_THE_VALIDATOR(
        submit_mod, capsys, flag, value) -> None:
    """These are free text that ends up near an #SBATCH line and in an argv.

    ASSERTS THE MESSAGE, NOT JUST THE EXIT. An earlier version of this test was
    a bare `pytest.raises(SystemExit)`, and the pre-commit assertion-shape gate
    blocked the commit for it — correctly. argparse ALSO exits on a value that
    merely looks like an option, so a bare raises() stays green with `type=` on
    the option deleted. It would have been evidence of nothing.

    Matching on the charset text proves `_sbatch_token` is what refused, which
    is the actual claim: these values never reach an sbatch directive. Observed
    on the real CLI too — `--agent '../../etc'` prints exactly this.
    """
    with pytest.raises(SystemExit):
        submit_mod.parse_args([flag, value])

    err = capsys.readouterr().err
    assert "contains characters outside [A-Za-z0-9._-]" in err, (
        f"{flag}={value!r} was refused, but not by _sbatch_token — so nothing "
        f"here shows the value is validated:\n{err}")
    assert "newline injection" in err, err


def test_the_validator_accepts_an_ordinary_value(submit_mod) -> None:
    """Or the check above passes by refusing everything, which is not a fix."""
    ns = submit_mod.parse_args(["--auth-profile", "work-2.0_b"])
    assert ns.auth_profile == "work-2.0_b"


# ── the plan carries them, and picks the right image ────────────────────────

def test_agent_override_changes_which_image_is_resolved(
        common_mod, tmp_path, monkeypatch) -> None:
    """THE POINT of applying it before `_resolve_apptainer_image`.

    Without this the job would run one agent's entrypoint out of another
    agent's .sif. Observed on the real CLI: `hpc submit --agent codex` resolves
    `botainer-agent-codex.sif` where the unflagged run resolves the claude one.
    """
    root = _project(tmp_path, monkeypatch)

    default_plan = common_mod.make_plan(root)
    codex_plan = common_mod.make_plan(root, agent_override="codex")

    assert default_plan.agent_name == "claude", default_plan.agent_name
    assert codex_plan.agent_name == "codex", codex_plan.agent_name
    assert "codex" in codex_plan.apptainer_image, codex_plan.apptainer_image
    assert codex_plan.apptainer_image != default_plan.apptainer_image


def test_unset_overrides_are_None_on_the_plan(
        common_mod, tmp_path, monkeypatch) -> None:
    """"Adds a capability" must not mean "changes every existing submission".

    All three stay None, so `submit.py` forwards None and compose_session
    behaves exactly as before.
    """
    root = _project(tmp_path, monkeypatch)

    plan = common_mod.make_plan(root)

    assert plan.agent_override is None, plan.agent_override
    assert plan.auth_mode_override is None, plan.auth_mode_override
    assert plan.auth_profile_override is None, plan.auth_profile_override


def test_auth_profile_override_beats_the_environment(
        common_mod, tmp_path, monkeypatch) -> None:
    """`profile` arrives via BOTAINER_PROFILE; an explicit flag must win.

    Also the reason the overrides are real parameters and not three more env
    vars: env-as-parameter is why `--auth-profile` had nowhere to land here.
    """
    root = _project(tmp_path, monkeypatch)
    monkeypatch.setenv("BOTAINER_PROFILE", "from-env")

    assert common_mod.make_plan(root).profile == "from-env"
    assert common_mod.make_plan(
        root, auth_profile_override="from-flag").profile == "from-flag"


# ── composition accepts what submit sends ───────────────────────────────────

def test_compose_for_hpc_accepts_the_three(  ) -> None:
    """Pins the wrapper's signature against the CLI that now passes them.

    A signature check rather than a compose run: composing needs a built .sif.
    It still fails if someone removes a parameter, which is the drift this
    whole row is about.
    """
    from botainer.core.composition import compose_agent_exec_for_hpc

    params = inspect.signature(compose_agent_exec_for_hpc).parameters
    for name in ("image_override", "agent_override",
                 "auth_mode_override", "auth_profile_override"):
        assert name in params, f"{name} missing: {list(params)}"
        assert params[name].default is None, f"{name} must default to None"


# Options `start` has that `hpc submit` deliberately does NOT, each with the
# reason it does not apply to a batch submission. This list is the POINT of the
# test below: a new `start` option belongs on `hpc submit` or belongs here, and
# until someone decides which, the test fails. That is the "answer the HPC
# question before calling it done" rule, mechanised.
START_ONLY = {
    "--runtime": "an sbatch submission is apptainer by definition",
    "--background": "an sbatch job is already detached; Slurm owns the lifetime",
    "--detach": "same as --background",
    "-d": "short form of --detach",
    "--fork": "forking a project is a local-project operation, not a submission",
    "--preflight": "`hpc submit --dry-run` is this path's compose-and-verify",
    "--json": "shapes `start`'s own launch output",
    "--quiet": "shapes `start`'s own launch output",
    "--no-auto-onboard": "onboarding is an interactive first-run flow",
    "--accept-identity-change": "identity confirmation is interactive",
}


def _opts(cmd) -> set[str]:
    return {o for p in cmd.params for o in getattr(p, "opts", [])}


def test_hpc_submit_offers_every_override_start_offers() -> None:
    """THE PARITY ASSERTION ITSELF, and it is genuinely comparative.

    IT WAS NOT, AND A REFUTING REVIEW CAUGHT THAT. The first version of this
    test computed both option sets and then iterated a hardcoded
    ("--agent", "--auth-mode", "--auth-profile") — exactly the fixed list its
    own docstring said it was not. Appending a synthetic fourth option to
    `start.params` and running the body verbatim PASSED. The docstring was the
    only thing asserting parity; the code asserted three names.

    So it now subtracts: anything `start` offers that `hpc submit` does not
    must be in START_ONLY with a stated reason. A new laptop-path option fails
    here until someone decides which side it belongs on.
    """
    from botainer.cli.hpc import submit as hpc_submit
    from botainer.cli.start import start as start_cmd

    unexplained = _opts(start_cmd) - _opts(hpc_submit) - set(START_ONLY)

    assert not unexplained, (
        f"`start` offers {sorted(unexplained)} and `hpc submit` does not. "
        f"Either add it to `hpc submit` (HPC parity) or add it to START_ONLY "
        f"with the reason it does not apply to a batch submission.")


def test_that_parity_test_actually_fails_when_parity_breaks() -> None:
    """The check above is a subtraction, so prove the subtraction can fail.

    Without this, START_ONLY could quietly grow to cover everything and the
    parity test would pass forever while asserting nothing — the same shape as
    the defect it replaced.
    """
    import click

    from botainer.cli.hpc import submit as hpc_submit
    from botainer.cli.start import start as start_cmd

    fake = click.Option(["--auth-account"], default=None)
    start_cmd.params.append(fake)
    try:
        unexplained = _opts(start_cmd) - _opts(hpc_submit) - set(START_ONLY)
        assert "--auth-account" in unexplained, (
            "a new `start` option did not show up as unexplained — the parity "
            "test cannot fail, so it is not checking anything")
    finally:
        start_cmd.params.remove(fake)


def test_every_start_only_entry_is_really_a_start_option() -> None:
    """A stale exclusion silently re-opens the gap it was written to close.

    If `start` drops an option, its START_ONLY row outlives it and would go on
    excusing a name nothing offers. Worse, a typo'd row excuses nothing while
    LOOKING like it covers a case.
    """
    from botainer.cli.start import start as start_cmd

    stale = set(START_ONLY) - _opts(start_cmd)

    assert not stale, (
        f"START_ONLY names {sorted(stale)}, which `start` no longer offers — "
        f"remove the row rather than leaving a dead exclusion")


def test_auth_mode_is_a_choice_not_free_text() -> None:
    """REFUTING REVIEW FINDING, and the one real defect in this change.

    `--auth-mode` shipped as a bare string option. The plugin's `_sbatch_token`
    accepts any word, and `_apply_auth_mode_override_in_memory` treats an
    unrecognised mode as a NO-OP plus one line on stderr. So:

        $ botainer hpc submit ... --yes --auth-mode isolate    # one typo
        [botainer] --auth-mode='isolate': no installed 'isolate' variant ...
        Submitted batch job 999001                             # exit 0

    The user asked to keep the credential out of the container, and got the
    config's mode instead — with `--yes` auto-accepting the consent screen that
    would have disclosed it. The typo was the PERMISSIVE outcome, while the
    correctly-spelled value refused. Every other auth-mode surface in the
    product already used `click.Choice(AUTH_MODES)`; this was the only one that
    did not.

    Asserting the shared tuple, not a copy of it: a second hand-written list is
    the drift this project keeps finding.
    """
    import click

    from botainer.auth_modes import AUTH_MODES
    from botainer.cli.hpc import submit as hpc_submit

    param = next(p for p in hpc_submit.params if "--auth-mode" in p.opts)

    assert isinstance(param.type, click.Choice), (
        f"--auth-mode is {param.type!r}, not a Choice — an unrecognised mode "
        f"becomes a silent no-op that submits in the config's mode")
    assert tuple(param.type.choices) == tuple(AUTH_MODES), (
        "--auth-mode must use the shared AUTH_MODES tuple, not its own list")
