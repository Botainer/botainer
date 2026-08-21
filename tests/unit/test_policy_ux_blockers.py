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
