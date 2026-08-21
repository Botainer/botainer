"""`botainer doctor` must report the commonest first-run failure.

THE DEFECT (onboarding tzar's road-walk). In the exact state a new
user gets stuck in — `setup` and `init` done, shared mode, NOT logged in —
`botainer doctor` exited 0 and `doctor --auth-only` printed a single green tick
about shell environment variables. In the same directory, `selftest` and
`dry-run --include-hooks` both refused with "no shared credential", and
`botainer auth status` reported the problem correctly WITH the fix.

`doctor` is the command the product points people at when things break, so a
confident false negative there is worse than no check — and `collect_auth_findings`'s
own docstring claimed it "reports whether the .credentials.json file exists per
project state dir found" while the code checked env vars only. Named as Q8 in
the audit; still true three weeks later.

The fix calls `auth.collect_auth_rows` — the same function `auth status`
renders — rather than re-deriving. These tests drive the real CLI end to end,
because the failure was never in the logic; it was that the logic was not
reached.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
import yaml


def _host(tmp_path, mode="shared"):
    home = tmp_path / "home"
    (home / ".botainer").mkdir(parents=True)
    (home / ".botainer" / "policy.yaml").write_text(
        yaml.safe_dump({"default_auth_mode": mode}), encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {**os.environ, "HOME": str(home),
           "MY_BOTAINER": str(home / ".botainer"), "BOTAINER_NO_TIPS": "1"}
    for args in (["setup", "--non-interactive"], ["init", "--agent", "claude"]):
        subprocess.run([sys.executable, "-m", "botainer.cli.main", *args],
                       env=env, capture_output=True, cwd=proj, check=False)
    return home, proj, env


def _doctor(proj, env):
    r = subprocess.run(
        [sys.executable, "-m", "botainer.cli.main", "doctor", "--auth-only"],
        env=env, capture_output=True, text=True, cwd=proj)
    return r.stdout + r.stderr


def test_doctor_names_the_missing_credential_and_the_fix(tmp_path):
    home, proj, env = _host(tmp_path)
    # PRECONDITION: the project really is in shared mode. If setup failed and
    # init fell back to isolated, this test would pass for the wrong reason.
    cfg = yaml.safe_load((proj / ".botainer" / "config.yaml").read_text())
    assert "agent-claude-shared" in (cfg.get("plugins_enabled") or []), (
        "fixture did not reach the case: project is not in shared mode")

    out = _doctor(proj, env)
    assert "no shared credential" in out, (
        f"doctor is silent about the commonest first-run blocker. `auth status` "
        f"reports it in the same directory.\n{out}")
    assert "botainer auth login --shared --agent claude" in out, (
        f"doctor named the problem without naming the fix. It must also say "
        f"WHERE to run it.\n{out}")
    assert "--agent anthropic" not in out, (
        f"doctor printed the FAMILY name where the flag takes an AGENT name; "
        f"that command does not work.\n{out}")


def test_doctor_goes_quiet_once_logged_in(tmp_path):
    """A check that fires when everything is fine is how the warn channel dies.

    This project has the rule written down because a permanently-firing warn
    trains everyone to skim, and then the real finding goes with it.
    """
    home, proj, env = _host(tmp_path)
    creds = home / ".botainer" / "shared-auth" / "agent-claude"
    creds.mkdir(parents=True, exist_ok=True)
    (creds / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat-" + "x" * 60,
        "refreshToken": "sk-ant-ort-" + "x" * 60,
        "expiresAt": int((time.time() + 9999) * 1000)}}), encoding="utf-8")

    out = _doctor(proj, env)
    assert "no shared credential" not in out, (
        f"doctor still reports a missing credential after a valid login:\n{out}")
    assert "credential present" in out, (
        f"doctor said nothing at all about a credential that IS there — the "
        f"same silence, inverted.\n{out}")


def test_doctor_reports_an_expired_credential_as_expired(tmp_path):
    """Present-but-dead is a different state from absent, and a different fix."""
    home, proj, env = _host(tmp_path)
    creds = home / ".botainer" / "shared-auth" / "agent-claude"
    creds.mkdir(parents=True, exist_ok=True)
    (creds / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat-" + "x" * 60,
        "refreshToken": "sk-ant-ort-" + "x" * 60,
        "expiresAt": int((time.time() - 9999) * 1000)}}), encoding="utf-8")

    out = _doctor(proj, env)
    assert "EXPIRED" in out, (
        f"doctor reported an expired credential as fine:\n{out}")
