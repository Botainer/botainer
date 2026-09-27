"""The sbatch body must not outlive-or-underlive the session it launched.

A detached Screen launch followed by polling can let the batch script exit
successfully before the container starts, or while its session is still alive.
These tests reproduce both mechanisms deterministically rather than relying
on scheduler timing.

THE SHAPE THAT WAS WRONG — launch, then poll for what you launched:

    screen -dmS "$SCREEN_SID" <composed container command>
    while screen -ls 2>/dev/null | grep -qE "[0-9]+\\.${SCREEN_SID}\\b"; do
        sleep 30
    done

TWO INDEPENDENT DEFECTS LIVED IN THOSE THREE LINES, with different
consequences, and either one ends the job while the work is unfinished:

  1. NO POSITIVE STARTUP BARRIER. `screen -dmS` forks; the parent returns
     success as soon as the fork succeeds, and the child creates its server
     socket afterwards. If the first `screen -ls` lands in that window the
     loop condition is false on its FIRST evaluation — so the loop body never
     runs, and a `while` whose condition starts false completes with status 0
     even under `set -euo pipefail`. The batch script then simply ends. Slurm
     records COMPLETED 0:0 and tears down the container that was still
     starting. Nothing reports a failure, because by the script's own lights
     nothing failed.

  2. `| grep -q` MAKES THE CONDITION A COIN TOSS. grep -q exits the instant it
     matches, the writer gets EPIPE and dies 141, and `pipefail` adopts that
     141 as the pipeline's status — so a MATCH is read as NO MATCH. Measured
     on this machine with a synthetic `screen -ls` listing, the match present
     every time:

         3 sessions   (~148 B)  →   0/300 wrong
        50 sessions   (~1.6 KB) → 174/300 wrong
       500 sessions   (~15 KB)  → 297/300 wrong

     Consequence differs from (1): here the loop exits on a LATER poll while
     the session is ALIVE, so Slurm kills a running agent mid-work. And the
     two compound — sessions leaked by (1) lengthen the listing, which makes
     (2) more likely.

     A maintainer-side suite that does not ship bans this pattern outright,
     but only for gate scripts under tools/ — so it could not see this one:
     at the time of writing this was the ONLY quiet-grep pipeline in shipped
     code, and it was in the sbatch generator.

THE FIX IS STRUCTURAL, not a tightened poll. Screen's nonforking mode (`-D -m`)
starts the session detached WITHOUT forking and returns when that session
terminates, so the batch process IS the session owner. There is no window to
race and no listing to parse, which is why this file asserts a PROPERTY (the
script outlives its agent) and only secondarily the absence of the old tokens.

WHY THE TEST EXECUTES INSTEAD OF GREPPING. A string match on the rendered
script would pass against a launch-then-poll that merely spelled its poll
differently. These tests run the REAL rendered script against a fake `screen`
whose `-dmS` reproduces the actual fork semantics — parent returns immediately,
socket appears later — and then ask the only question that matters: when the
batch script exited, had the agent finished?
"""
from __future__ import annotations

import importlib.util
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HPC_HOST_HELPER = REPO / "plugins" / "hpc-launcher" / "host_helper"

# The §4 cage flags render_sbatch_script REFUSES to go without. Spelled out
# rather than imported so this file does not pass by agreeing with itself.
CAGED_ARGV = (
    "apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
    "--drop-caps", "all", "botainer-agent.sif", "AGENT_PLACEHOLDER",
)


def _load_common():
    spec = importlib.util.spec_from_file_location(
        "hpc_launcher_common_lifetime", HPC_HOST_HELPER / "_common.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hpc_launcher_common_lifetime"] = mod
    spec.loader.exec_module(mod)
    return mod


def _render(tmp_path: Path, *, nudge: bool = True) -> str:
    common = _load_common()
    plan = common.SubmissionPlan(
        project_root=tmp_path, project_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        state_root=tmp_path / "state", profile="default", partition="",
        account="", time_minutes=60, cpus=1, memory_gb=None, gpus=0,
        gpu_type=None, apptainer_image="botainer-agent.sif",
        submission_mode="submit", existing_jobid=None,
        nudge_enabled=nudge, agent_exec_argv=CAGED_ARGV,
    )
    return plan.render_sbatch_script()


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _fake_tools(bin_dir: Path, marker_dir: Path, *, socket_delay: str) -> None:
    """A `screen` that forks like the real one, and an `apptainer` that takes time.

    `-dmS` is the heart of it: the parent MUST return 0 immediately while the
    session's socket appears `socket_delay` seconds later. That is real screen's
    documented behaviour and it is the window defect (1) falls into. A fake that
    created the socket synchronously would make the bug untestable — which is
    exactly how this survived in production.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    marker_dir.mkdir(parents=True, exist_ok=True)

    _exe(bin_dir / "screen", f'''#!/bin/bash
# Fake GNU screen. Sessions are files in {marker_dir}/sock-<name>.
MARKERS="{marker_dir}"
mode=""; name=""
while [ $# -gt 0 ]; do
  case "$1" in
    -ls|-list) echo "There are screens on:"
               n=0
               for f in "$MARKERS"/sock-*; do
                 [ -e "$f" ] || continue
                 n=$((n+1)); printf '\\t%d.%s\\t(Detached)\\n' $((1000+n)) "$(basename "$f" | sed 's/^sock-//')"
               done
               echo "$n Sockets in /run/screen/S-fake."
               exit 0 ;;
    -dmS)  mode=fork;   name="$2"; shift 2; break ;;
    -D)    mode=nofork; shift ;;
    -m)    shift ;;
    -S)    name="$2"; shift 2 ;;
    -c)    shift 2 ;;
    *)     break ;;
  esac
done
if [ "$mode" = fork ]; then
    # THE REAL SEMANTICS: fork, return success now, socket appears later.
    ( sleep {socket_delay}
      : > "$MARKERS/sock-$name"
      "$@"
      rm -f "$MARKERS/sock-$name" ) </dev/null >/dev/null 2>&1 &
    exit 0
else
    # Nonforking: this process IS the session owner and returns when it ends.
    : > "$MARKERS/sock-$name"
    "$@"; rc=$?
    rm -f "$MARKERS/sock-$name"
    exit $rc
fi
''')

    # The "agent": takes real time, and records that it finished.
    _exe(bin_dir / "apptainer", f'''#!/bin/bash
sleep 1.5
: > "{marker_dir}/AGENT_FINISHED"
exit 0
''')


def _run_batch(script: str, bin_dir: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    p = tmp_path / "job.sbatch"
    p.write_text(script, encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SLURM_JOB_ID"] = "424242"
    return subprocess.run(["bash", str(p)], env=env, capture_output=True,
                          text=True, timeout=90)


# ─────────────── the regression the dashboard asked for (acceptance 1) ───────

def test_the_batch_does_not_end_before_its_agent_does(tmp_path: Path) -> None:
    """DELAYED START: the session's socket appears AFTER the first listing
    opportunity, which is the whole defect.

    The assertion is a property, not a timing trial: at the instant the batch
    script exited, had the agent finished? With launch-then-poll the answer is
    no — the script is already gone while the container is still starting, and
    Slurm reports COMPLETED 0:0 over the top of it.
    """
    markers = tmp_path / "m"
    bins = tmp_path / "bin"
    # Socket appears at 0.5s; the agent runs for 1.5s. A first listing at
    # ~0.01s therefore misses, deterministically.
    _fake_tools(bins, markers, socket_delay="0.5")

    res = _run_batch(_render(tmp_path), bins, tmp_path)

    assert res.returncode == 0, f"the batch script itself errored:\n{res.stderr}"
    assert (markers / "AGENT_FINISHED").exists(), (
        "THE BATCH SCRIPT EXITED WHILE THE AGENT WAS STILL RUNNING.\n"
        "Slurm would record COMPLETED 0:0 and tear down a container that had "
        "not finished starting — the exact symptom reported on 2026-09-21.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


def test_a_long_session_listing_cannot_end_the_job_early(tmp_path: Path) -> None:
    """THE SECOND DEFECT, which the startup fix must also remove.

    Pre-seed many unrelated sessions so any `screen -ls | grep -q` condition
    is piping kilobytes into a reader that exits early. Measured at this size,
    the old pipeline misreports a present match well over half the time; the
    consequence is an ALIVE session whose job ends anyway.

    A `-D -m` owner parses no listing at all, so this passes structurally
    rather than by being lucky.
    """
    markers = tmp_path / "m"
    bins = tmp_path / "bin"
    _fake_tools(bins, markers, socket_delay="0")
    for i in range(400):
        (markers / f"sock-unrelated-{i:04d}").write_text("", encoding="utf-8")

    res = _run_batch(_render(tmp_path), bins, tmp_path)

    assert res.returncode == 0, res.stderr
    assert (markers / "AGENT_FINISHED").exists(), (
        "The job ended while its agent was still running, with a large session "
        "listing present. A pipeline into `grep -q` under `pipefail` reads a "
        "MATCH as a failure when the writer dies of EPIPE, so the wait loop "
        "exits over a LIVE session.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


# ─────────────── structural assertions: the shape itself ─────────────────────

def test_the_rendered_script_does_not_poll_for_what_it_just_launched(
        tmp_path: Path) -> None:
    """Back-stop for the two execution tests above.

    They would also pass if someone replaced the poll with a tighter poll that
    happened to win the race on this machine. Launch-then-poll is the defect;
    name it, so the fix cannot regress into a faster version of itself.
    """
    script = _render(tmp_path)
    assert "screen -dmS" not in script, (
        "`screen -dmS` forks and returns before its socket exists. Whatever "
        f"follows it is racing a child that has not started:\n{script}")
    assert not re.search(r"while\s+screen\s+-ls", script), (
        f"the script still waits by polling `screen -ls`:\n{script}")
    assert "| grep -q" not in script and "|grep -q" not in script, (
        "a pipeline into `grep -q` under `pipefail` reports a match as a "
        f"failure when the writer dies of EPIPE:\n{script}")


def test_the_session_is_still_named_so_hpc_attach_keeps_working(
        tmp_path: Path) -> None:
    """The fix must not cost the feature nudge exists for.

    `hpc attach` reconnects with `screen -r botainer-<jobid>`, so the session
    must still be CREATED with that exact name. A lifetime fix that silently
    broke reattach would trade one invisible failure for another.
    """
    script = _render(tmp_path)
    assert 'SCREEN_SID="botainer-${SLURM_JOB_ID' in script, (
        f"the session name is no longer derived from the job id:\n{script}")
    # Accept EITHER spelling of the session-name flag. This guard is about the
    # NAME surviving, not about which form creates it — pinning `-S` here would
    # have made it a restatement of the fix instead of an independent check on
    # the feature the fix must not cost.
    assert re.search(r'-(?:dm)?S\s+"\$SCREEN_SID"', script), (
        f"the session is no longer created under its expected name, so "
        f"`hpc attach` (screen -r botainer-<jobid>) cannot find it:\n{script}")


def test_the_no_screen_fallback_still_runs_the_agent(tmp_path: Path) -> None:
    """Compute nodes without `screen` must still run the job (T3-7).

    Unchanged behaviour, asserted here because this file rewrites the branch
    right next to it.
    """
    script = _render(tmp_path)
    assert "command -v screen" in script, (
        f"the guard that keeps screen-less nodes working is gone:\n{script}")
    head, _, tail = script.partition("else")
    assert "exec apptainer exec" in tail, (
        f"the screen-less path no longer execs the agent:\n{script}")


def test_nudge_disabled_is_untouched(tmp_path: Path) -> None:
    """With nudge off there is no screen at all — plain exec, as before."""
    script = _render(tmp_path, nudge=False)
    assert "screen" not in script, f"screen leaked into a non-nudge job:\n{script}"
    assert script.rstrip().endswith("AGENT_PLACEHOLDER"), (
        f"the non-nudge path no longer ends by exec-ing the agent:\n{script}")
