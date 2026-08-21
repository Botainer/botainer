#!/usr/bin/env python3
"""hpc-launcher status — list this user's botainer Slurm jobs.

Task #136: squeue --name is an EXACT match, not a prefix. 'botainer-'
returned zero rows because no job is literally named that. Same shape
fix as #161 in the launcher CLI.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys


def main() -> int:
    if not shutil.which("squeue"):
        sys.stderr.write("hpc-launcher status: `squeue` not on PATH.\n")
        return 5
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        sys.stderr.write("hpc-launcher status: USER env not set.\n")
        return 1
    fmt = "%18i %.9P %.20j %.8u %.2t %.10M %.6D %R"
    argv = ["squeue", "--user", user, "--format", fmt]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode
    lines = proc.stdout.splitlines()
    if not lines:
        return 0
    # Header
    sys.stdout.write(lines[0] + "\n")
    # NAME column is third field (per format string above).
    for ln in lines[1:]:
        parts = ln.split()
        if len(parts) >= 3 and parts[2].startswith("botainer-"):
            sys.stdout.write(ln + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
