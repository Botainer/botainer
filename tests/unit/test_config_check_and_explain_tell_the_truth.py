"""`config check` must not green-tick a config `start` refuses, and `config
explain` must show what the session is GRANTED. (queue items 30 and 32)

BOTH REPRODUCED BY RUNNING the real CLI.

ITEM 30 — A GREEN TICK FOR AN UNLAUNCHABLE CONFIG. The `config check` docstring
has always listed "env vars on the denylist" among its sanity checks. Nothing
implemented it. Observed:

    $ botainer config check --strict          # env.LD_PRELOAD: /tmp/evil.so
    ✓ schema valid (config matches ProjectConfig)
    ✓ no issues found                          <- exit 0

    $ botainer start --preflight
    refused: env-var-denied: env var 'LD_PRELOAD' is on policy denylist [...]

THE ROW WAS NARROWER THAN ITS LABEL, and saying so matters. The audit filed this
as "env.LD_PRELOAD passes `check --strict`", which reads as a hole in the cage.
It is not: the denylist is genuinely enforced, in policy.py, composition.py and
preflight.py, and `start` refuses by name. What was broken is the VALIDATOR —
it told the user everything was fine about a config that cannot launch, which is
worse than having no validator because they stop looking.

THE CHECK ASKS THE POLICY OBJECT. It does not carry its own copy of the list;
`test_the_check_reads_the_POLICY_not_a_hardcoded_list` is what pins that. A
second copy agrees on the day it is written and drifts silently afterwards —
the #218 defect, where a host check carried its own implementation instead of
asking the product, and quietly stopped matching.

ITEM 32 — THE SUMMARY OMITTED THE GRANTS. `config explain` is advertised as the
summary of the effective config. Run with `agent_permissions`, `caps.kernel.keep`
and `job_profiles` all set to valid values, it named none of them. The sharp one
is `caps.kernel.keep`: a Linux capability grant, invisible in the command whose
whole job is telling you what your config does. `agent_permissions` matters for
a different reason — its default is `bypass`, so the common case is an agent
acting without asking, and nothing said so.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from botainer.cli.config_cmd import config

UID = "22222222-2222-4222-8222-222222222222"

_BASE = ("version: config-v1\nagent: claude\nruntime: docker\n"
         "profile: default\nnetwork:\n  mode: internet\n"
         "plugins_enabled: [agent-claude]\n")


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


# ── item 30 ──────────────────────────────────────────────────────────────────

def test_check_reports_an_env_var_the_policy_denies(tmp_path, monkeypatch):
    """The defect: this printed "no issues found"."""
    _project(tmp_path, monkeypatch, "env:\n  LD_PRELOAD: /tmp/evil.so\n")

    res = _run("check")

    assert "LD_PRELOAD" in res.output, res.output
    assert "no issues found" not in res.output, (
        "still reporting a clean bill for a config `start` refuses")


def test_strict_exits_nonzero_for_a_config_start_will_refuse(
        tmp_path, monkeypatch):
    """`--strict` exists so scripts can gate on it; exit 0 here misleads them.

    The exit code alone is NOT enough to assert, and the assertion-shape gate
    caught that: a nonzero exit for some unrelated reason would keep this green
    while the denylist check was deleted. Pin the exit code AND the reason.
    """
    _project(tmp_path, monkeypatch, "env:\n  LD_PRELOAD: /tmp/evil.so\n")

    res = _run("check", "--strict")

    assert res.exit_code != 0, res.output
    assert "LD_PRELOAD" in res.output, (
        f"exited nonzero, but not for the reason this test names:\n{res.output}")


def test_the_message_says_START_WILL_REFUSE_not_just_that_it_is_denied(
        tmp_path, monkeypatch):
    """The user's next question is "so can I run it?". Answer it here."""
    _project(tmp_path, monkeypatch, "env:\n  LD_PRELOAD: /tmp/evil.so\n")

    out = _run("check").output

    assert "refuse" in out.lower(), out


def test_the_check_reads_the_POLICY_not_a_hardcoded_list(
        tmp_path, monkeypatch):
    """THE STRUCTURAL ONE.

    A var that appears nowhere in botainer's source must still be reported when
    a policy denies it. If this passes only for names someone typed into
    config_cmd.py, the validator will drift away from the enforcer the first
    time the policy changes — which is exactly how this defect class recurs.
    """
    from botainer.core import policy as policy_module

    real_intersect = policy_module.intersect

    def _with_extra(site, user):
        pol = real_intersect(site, user)
        pol.capabilities.env_var_denylist = list(
            pol.capabilities.env_var_denylist) + ["ZZ_INVENTED_FOR_THIS_TEST"]
        return pol

    monkeypatch.setattr(policy_module, "intersect", _with_extra)
    _project(tmp_path, monkeypatch,
             "env:\n  ZZ_INVENTED_FOR_THIS_TEST: anything\n")

    out = _run("check").output

    assert "ZZ_INVENTED_FOR_THIS_TEST" in out, (
        "the check is not consulting the effective policy; it has its own "
        f"copy of the denylist and will drift from the enforcer:\n{out}")


def test_an_ordinary_env_var_is_still_fine(tmp_path, monkeypatch):
    """Or the check becomes noise and gets ignored, per the cry-wolf rule."""
    # QUOTED deliberately: bare `yes` is a YAML boolean and env values are
    # strings, so an unquoted value fails schema validation and this test would
    # have passed for the wrong reason. Same trap as the `mode: off` defect.
    _project(tmp_path, monkeypatch,
             'env:\n  MY_PROJECT_SETTING: "yes"\n')

    res = _run("check", "--strict")

    assert res.exit_code == 0, res.output
    assert "no issues found" in res.output, res.output


# ── item 32 ──────────────────────────────────────────────────────────────────

def test_explain_shows_the_agent_permission_posture(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, "agent_permissions: prompt\n")

    out = _run("explain").output

    assert "prompt" in out, out


def test_explain_says_what_bypass_MEANS_because_it_is_the_default(
        tmp_path, monkeypatch):
    """"Permissions: bypass" tells a new user nothing.

    A FACT and a POINTER, never a safety verdict — a one-liner cannot carry the
    caveats a security claim needs.
    """
    _project(tmp_path, monkeypatch, "agent_permissions: bypass\n")

    out = _run("explain").output

    assert "without asking" in out, out
    assert "CAPABILITY-SURFACE" in out, f"stated a fact but pointed nowhere:\n{out}"
    for verdict in ("safe", "safer", "secure", "protects"):
        assert verdict not in out.lower(), (
            f"rendered a safety verdict {verdict!r} in a one-liner:\n{out}")


def test_explain_shows_kernel_capability_grants(tmp_path, monkeypatch):
    """The sharpest omission: a Linux capability grant, invisible."""
    _project(tmp_path, monkeypatch,
             "caps:\n  kernel:\n    keep: [CAP_SYS_PTRACE]\n")

    out = _run("explain").output

    assert "CAP_SYS_PTRACE" in out, out


def test_explain_states_the_absence_of_capability_grants_too(
        tmp_path, monkeypatch):
    """Silence reads as "not applicable", not as "none" — the partition lesson."""
    _project(tmp_path, monkeypatch)

    out = _run("explain").output

    assert "Kernel capabilities" in out, out
    assert "none" in out.lower(), out


def test_explain_shows_job_profiles(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch,
             "job_profiles:\n  big:\n    cpus: 32\n")

    out = _run("explain").output

    assert "big" in out and "32" in out, out


def test_job_profiles_show_what_you_SET_not_every_default(
        tmp_path, monkeypatch):
    """Dumping the whole model buries the two fields that matter.

    Observed on the first attempt: `big: description=, partition=, time=,
    cpus=32, memory=, ...`.
    """
    _project(tmp_path, monkeypatch,
             "job_profiles:\n  big:\n    cpus: 32\n")

    out = _run("explain").output

    assert "description=" not in out, f"dumped unset defaults:\n{out}"
    assert "partition=" not in out, f"dumped unset defaults:\n{out}"
