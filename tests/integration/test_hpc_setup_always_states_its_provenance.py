"""`botainer hpc setup` must say how much to trust the profile it just wrote.

THE DEFECT (onboarding tzar). `_echo_profile_trust` was built for
the maintainer's directive "clearly mark what's tested and not tested". It had
two callers: the single-autodetect branch, and `hpc info`.

The three branches where the USER IS GUESSING — an explicit `--profile`, the
ambiguous-hostname prompt, and the no-match `example` fallback — all skipped it
and landed on `✓ wrote cluster profile`, then printed `partition: standard`,
`Lmod: /etc/profile.d/lmod.sh` and a scratch policy as bare fact. The file
itself recorded `status: community-contributed, source: documentation example
only`.

So the trust label appeared when botainer was CONFIDENT and vanished when it
was GUESSING. A stranger presses Enter, gets a green tick, and submits against
a partition that may not exist.

The fix is structural, and this test is what pins it: the label prints at the
ONE point every branch reaches — immediately after the write — so a fourth way
to choose a profile cannot silently skip it.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
import yaml

REPO_PROFILES = "cluster_profiles"


def _run_setup(tmp_path, *args):
    home = tmp_path / "home"
    (home / ".botainer").mkdir(parents=True)
    env = {**os.environ, "HOME": str(home),
           "MY_BOTAINER": str(home / ".botainer"), "BOTAINER_NO_TIPS": "1"}
    r = subprocess.run(
        [sys.executable, "-m", "botainer.cli.main", "hpc", "setup",
         "--non-interactive", *args],
        env=env, capture_output=True, text=True, cwd=tmp_path)
    return r, home


def _some_profile_name():
    import glob
    for f in sorted(glob.glob(f"{REPO_PROFILES}/*.yaml")):
        d = yaml.safe_load(open(f)) or {}
        name = (d.get("cluster") or {}).get("name")
        if name:
            return name
    pytest.skip("no bundled cluster profiles")


def test_explicit_profile_still_states_its_provenance(tmp_path):
    """The branch a user takes when they picked the profile THEMSELVES."""
    name = _some_profile_name()
    r, home = _run_setup(tmp_path, "--profile", name)
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    assert "✓ wrote cluster profile" in out, out

    # The written file records a provenance; the OUTPUT must mention it too.
    written = yaml.safe_load(
        (home / ".botainer" / "cluster.yaml").read_text(encoding="utf-8")) or {}
    status = (written.get("verification") or {}).get("status", "")
    assert status, "fixture: the bundled profile records no verification status"

    markers = ("tested on real hardware", "transcribed", "public documentation",
               "UNVERIFIED", "probing", "submitted", "community")
    assert any(m.lower() in out.lower() for m in markers), (
        f"hpc setup wrote a profile whose provenance is {status!r} and said "
        f"nothing about it — a green tick over an unverified profile. The "
        f"trust label must print on EVERY branch, not only autodetect.\n{out}")
