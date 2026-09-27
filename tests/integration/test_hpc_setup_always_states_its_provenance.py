"""HPC setup prints profile provenance on every successful selection path.

Autodetection, explicit selection, ambiguous-hostname selection and the example
fallback all reach the same post-write disclosure. The label must distinguish
measured profile facts from documentation-derived or example values. A successful
file write does not establish that its partitions, module paths or storage policy
match the site."""
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
