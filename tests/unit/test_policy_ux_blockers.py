"""UX audit (B1/B2/B3): a bad site policy bricked every command, and
the three tools you'd reach for to fix it were the ones that couldn't help.

B1 docs/SITE-ADMIN.md's copy-pasteable snippet wrote `version: 1`, which YAML
   parses as an int -> refused -> EVERY botainer command for EVERY user on that
   host fails, with a raw pydantic dump.
B2 `botainer policy ...` had no @handle_refusals, so the command you use to
   inspect a broken policy emitted a traceback.
B3 `botainer doctor` only stat()'d policy.yaml, so it printed a green tick on the
   very file that was breaking everything, and exited 0.
"""
from __future__ import annotations

import pytest


def test_int_version_error_names_the_fix() -> None:
    """The message must say what to write, not just that the type is wrong."""
    from botainer.core.policy import SitePolicy
    with pytest.raises(Exception) as exc:
        SitePolicy.model_validate({"version": 1})
    msg = str(exc.value)
    assert "policy-v1" in msg, msg
    assert "integer" in msg or "int" in msg, msg


def test_valid_version_still_accepted() -> None:
    from botainer.core.policy import SitePolicy
    assert SitePolicy.model_validate({"version": "policy-v1"}).version == "policy-v1"


def test_site_admin_doc_snippet_is_valid_policy() -> None:
    """The doc's snippet must actually parse — this is B1's root cause."""
    import re
    from pathlib import Path

    import yaml

    from botainer.core.policy import SitePolicy

    doc = Path(__file__).resolve().parents[2] / "docs" / "SITE-ADMIN.md"
    body = doc.read_text(encoding="utf-8")
    blocks = re.findall(r"/etc/botainer/policy\.yaml <<'YAML'\n(.*?)\nYAML", body, re.S)
    assert blocks, "the site-policy heredoc disappeared from SITE-ADMIN.md"
    for block in blocks:
        SitePolicy.model_validate(yaml.safe_load(block))   # must not raise


def test_policy_show_renders_a_clean_refusal_not_a_traceback(tmp_path, monkeypatch) -> None:
    """B2 (behavioural): `policy show` is what you reach for WHEN policy is the
    problem. With an invalid policy on disk it must print a typed refusal and
    exit non-zero — not a ~25-line Python traceback."""
    from click.testing import CliRunner

    from botainer.cli.policy import policy as policy_grp
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    (paths.root / "policy.yaml").write_text("version: 1\n", encoding="utf-8")

    res = CliRunner().invoke(policy_grp, ["show"])
    assert res.exit_code != 0
    combined = res.output + (str(res.exception) if res.exception else "")
    assert "Traceback" not in combined, combined[:400]
    assert "policy-v1" in combined, combined[:400]


def test_doctor_flags_an_invalid_policy_instead_of_green_ticking_it(
    tmp_path, monkeypatch
) -> None:
    """B3: doctor must PARSE policy.yaml, not just stat it."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    from botainer.cli import doctor as doctor_mod
    from botainer.state import dir as state_dir

    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    (paths.root / "policy.yaml").write_text("version: 1\n", encoding="utf-8")

    rows = [f for f in doctor_mod.collect_findings(for_setup=False)
            if f.check == "policy.yaml"]
    assert rows, "doctor no longer reports on policy.yaml at all"
    assert rows[0].severity == "err", f"doctor still blesses an invalid policy: {rows[0]}"
    assert "policy-v1" in (rows[0].remediation or ""), rows[0]


# ── B4: `policy set` reported success while changing nothing ───────────────
#
# Same family as B1-B3 above: the tool you reach for is the one that misleads
# you. Observed by running it — widening `plugins.allowed_tiers` to include
# `third-party` printed `✓ policy.yaml updated`, and `policy show` still said
# `['first-party']`, and the install it was meant to unblock still refused.
# The write DOES land in your file; the site ceiling wins, so the effective
# value never moves. A command that reports success while changing nothing is
# worse than one that refuses — it sends you off to debug the wrong thing.
#
# There was already a warning for three SITE-ONLY fields, but that is a
# hand-maintained list and this class is every INTERSECTED field in the
# widening direction. So the check asks the real `intersect()` what the
# effective value would BE rather than consulting a list — a field added
# later is covered with nothing to remember.

def _policy_at(tmp_path, monkeypatch):
    import yaml
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    from botainer.state import dir as sd
    root = sd.ensure_user_state_dir(create_if_missing=True).root
    (root / "policy.yaml").write_text(yaml.safe_dump({"version": "policy-v1"}))
    return root / "policy.yaml"


def _run_policy_set(key: str, value: str):
    from click.testing import CliRunner
    from botainer.cli.policy import policy
    # `input="n"` — decline the write. The warning has to fire BEFORE the
    # confirm, or it is an explanation delivered after the decision.
    return CliRunner().invoke(policy, ["set", key, value], input="n\n")


def test_widening_a_capped_field_says_it_will_not_take_effect(tmp_path,
                                                              monkeypatch) -> None:
    _policy_at(tmp_path, monkeypatch)
    res = _run_policy_set("plugins.allowed_tiers",
                          '["first-party","third-party"]')
    out = res.output
    assert "NOT take effect" in out, (
        "widening a ceiling-capped field printed no warning — the user is "
        "told the write succeeded and the effective value never moves"
    )
    # Naming BOTH values is the point: "won't work" without saying what you
    # will actually get leaves the user guessing.
    assert "first-party" in out and "third-party" in out
    # And it must point at the command that tells the truth.
    assert "policy show" in out


def test_tightening_stays_quiet(tmp_path, monkeypatch) -> None:
    """Narrowing genuinely takes effect, so there is nothing to warn about.

    Pinned because the lazy way to pass the test above is to warn on every
    set — which would make this a permanently-firing warning, the scenery
    problem this project has already been bitten by.
    """
    _policy_at(tmp_path, monkeypatch)
    res = _run_policy_set("plugins.allowed_tiers", "[]")
    assert "NOT take effect" not in res.output
