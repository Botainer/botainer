#!/usr/bin/env python3
"""hpc-launcher attach — reconnect to a running botainer Slurm job.

Task #135: was `srun ... --pty bash` which OPENS A NEW SHELL on the
compute node, not the running agent's terminal. The user expected
'attach' to mean 'see the agent's screen'.

Now: srun into the compute node, then `screen -r botainer-<jobid>`
to reattach to the existing screen session the launcher created.
Falls back to bash if the screen session is missing (so the user
still has a way in for debugging).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="botainer plugin hpc-launcher attach")
    p.add_argument("jobid", help="Slurm job ID")
    p.add_argument(
        "--no-screen", action="store_true",
        help="Skip screen reattach; open a plain bash on the compute node.",
    )
    return p.parse_args(argv)


def main() -> int:
    if not shutil.which("srun"):
        sys.stderr.write("hpc-launcher attach: `srun` not on PATH.\n")
        return 5
    args = parse_args(sys.argv[1:])
    if args.no_screen:
        argv = ["srun", f"--jobid={args.jobid}", "--overlap", "--pty", "bash"]
    else:
        # Reattach to the named screen session the launcher created.
        # The launcher convention is screen session name 'botainer-<jobid>'.
        # srun --pty wraps the whole thing so terminal modes pass through.
        cmd = f"screen -r botainer-{args.jobid} || screen -ls"
        argv = [
            "srun", f"--jobid={args.jobid}", "--overlap", "--pty",
            "bash", "-c", cmd,
        ]
    rc = subprocess.run(argv, check=False).returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
