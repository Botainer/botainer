"""The first-launch capability gate, verified through the REAL launcher.

WHAT THIS REPLACES, AND WHY. `test_every_CALL_SITE_passes_the_facts_apart` pins
the KEYWORD NAMES two callers pass — `pre_authorised=` and `interactive=`
present, `auto_yes=` absent — by parsing the AST. That was the best available
check at the time and it is not enough: writing
`pre_authorised=(args.yes or not interactive)` re-creates the original defect
exactly, with both keywords present, and all seven of that file's tests pass.
It catches the old SPELLING of the defect, not the defect.

It existed because I wrote down that `_consent` was unreachable here — a closure
inside `submit.py::main`, reached only on a non-dry-run `here`/`attach`, which
would attempt a real launch. That was wrong, and the loop referee proved it in
ten minutes. This file is the recipe, encoded.

THE FIXTURE REACHES A REAL LAUNCHER DECISION POINT with no apptainer, no Slurm
and no real credential. `hpc submit --mode=here` composes the session, renders
the full capability disclosure, calls `_consent`, and only THEN discovers it is
not inside a Slurm allocation — so everything up to and including the consent
decision runs exactly as it does on a cluster.

WHAT IT ASSERTS, and it is a fact on disk rather than a code shape:
`record_shown` writes `last_capability_summary_fingerprint` into the project's
`meta.json`. A no-TTY show-only submit must leave that key ABSENT (the gate
stays armed for the next interactive launch); `--yes` must write it (a real
pre-authorisation consumes the gate).

Measured before this file existed, twice in each direction:

    hpc submit --mode=here            →  fingerprint absent   gate still armed
    hpc submit --mode=here --yes      →  fingerprint present  gate consumed

ONE STEP OF THE RECORDED RECIPE WAS MISSING, and the run stopped early without
it: a default config refuses at `network.mode=none` — "Apptainer adapter at
v0.1.0 does not enforce network.mode=none" — long before `_consent`. The recipe
as written in the protocol reached a different refusal than it claimed. It needs
`network: {mode: internet}`, which this fixture sets and says why.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    shutil.which("botainer") is None,
    reason="needs the installed CLI; this test drives the real entry point on "
           "purpose, because driving the composer directly is what let the "
           "original defect survive",
)


def _run(args, *, home, state, cwd):
    """The real CLI, with HOME and MY_BOTAINER pointed at scratch."""
    env = {
        **os.environ,
        "HOME": str(home),
        "MY_BOTAINER": str(state),
        "BOTAINER_TESTING": "1",
    }
    env.pop("BOTAINER_STATE_ROOT", None)
    return subprocess.run(["botainer", *args], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=180)


@pytest.fixture
def project(tmp_path):
    """A project `hpc submit --mode=here` will carry all the way to `_consent`.

    Every element is load-bearing and was found by running, not by reading:

    * `git init` — `init` wants a repo.
    * a `.sif` OVER 1 MiB at the canonical name — the build/verify path refuses
      a smaller file as implausible, and the resolver looks for the prefixed
      spelling.
    * a real-SHAPED credential in the isolated profile dir — the pre-submit
      credential-presence check refuses without one. Shaped, not real: no token
      is needed to reach a consent decision.
    * `network.mode: internet` — WITHOUT THIS the run stops at
      "Apptainer adapter at v0.1.0 does not enforce network.mode=none", which is
      a different refusal, before `_consent`. The recorded recipe omitted it.
    * `hpc-launcher` in `plugins_enabled` — `hpc submit` checks ENABLED.
    """
    home = tmp_path / "home"
    state = tmp_path / "state"
    proj = tmp_path / "proj"
    for d in (home, proj):
        d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=proj, check=True)

    r = _run(["setup", "--force"], home=home, state=state, cwd=proj)
    assert r.returncode == 0, f"setup failed:\n{r.stdout}\n{r.stderr}"
    r = _run(["init", "--agent", "claude"], home=home, state=state, cwd=proj)
    assert r.returncode == 0, f"init failed:\n{r.stdout}\n{r.stderr}"

    uuid = (proj / ".botainer" / "project-id").read_text().strip()

    images = state / "images"
    images.mkdir(parents=True, exist_ok=True)
    sif = images / "botainer-agent-claude.sif"
    sif.write_bytes(b"SIFDATA" * 200_000)          # ~1.4 MiB, over the floor
    assert sif.stat().st_size > 1024 * 1024

    creds = state / "state" / uuid / "data" / "agent-claude" / "profiles" / "default"
    creds.mkdir(parents=True, exist_ok=True)
    cred = creds / ".credentials.json"
    cred.write_text('{"claudeAiOauth":{"refreshToken":"sk-ant-ort01-FAKE"}}')
    cred.chmod(0o600)

    (proj / ".botainer" / "config.yaml").write_text(
        "agent: claude\n"
        "network:\n  mode: internet\n"
        "plugins_enabled:\n  - agent-claude\n  - hpc-launcher\n"
    )
    return home, state, proj, state / "state" / uuid / "meta.json"


def _fingerprint_recorded(meta_path: Path) -> bool:
    if not meta_path.exists():
        return False
    return "last_capability_summary_fingerprint" in json.loads(meta_path.read_text())


def test_a_show_only_here_submit_leaves_the_gate_ARMED(project):
    """THE DEFECT, end to end. The user was never asked, so nothing is confirmed.

    `--mode=here` has no terminal, so `_consent` was called with
    `interactive=False`, which the caller merged into `auto_yes=True`, which
    reached `record_shown`. The first-launch confirmation was satisfied on
    behalf of someone who was never asked — and a FAILED here/attach burned it
    for a session that never ran.
    """
    home, state, proj, meta = project
    assert not _fingerprint_recorded(meta), "precondition: gate armed"

    r = _run(["hpc", "submit", "--mode=here", "--time", "60"],
             home=home, state=state, cwd=proj)

    # It must have reached the consent point — otherwise this test proves
    # nothing about consent, which is exactly how the recorded recipe went
    # wrong.
    assert "not inside a Slurm allocation" in (r.stdout + r.stderr), (
        f"the run stopped BEFORE the consent decision, so this asserts nothing "
        f"about it:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    assert "auto-confirmed without prompt" in (r.stdout + r.stderr), (
        "the capability disclosure did not reach its auto-confirm branch")

    assert not _fingerprint_recorded(meta), (
        "a no-TTY show-only submit recorded the capability fingerprint, so the "
        "next INTERACTIVE launch will skip the confirmation the user never gave")

    # AND THE CONSENT TEXT KNOWS WHICH PATH IT IS ON. `print_and_maybe_confirm`
    # takes `on_sbatch_path` because the runtime cannot supply it, and only this
    # end-to-end run proves `hpc submit` passes it: flipping that one argument to
    # False changed no unit test at all. Broker mode is REFUSED on the sbatch
    # path — after the broker has started and possibly rotated the token — so the
    # credential paragraph must not offer it here.
    out = r.stdout + r.stderr
    assert "Credential delivery" in out, (
        f"the credential paragraph did not render, so this asserts nothing about "
        f"it:\n{out[-1500:]}")
    assert "sbatch path cannot reach" in out, (
        f"the sbatch consent block does not say broker is unreachable here:\n{out[-1500:]}")
    assert "auth use broker" not in out, (
        f"the sbatch consent block offers a mode this path refuses AFTER the "
        f"broker has refreshed:\n{out[-1500:]}")


def test_an_explicit_YES_does_record_it(project):
    """THE CONTROL, and it is the half that must keep working.

    Without it the fix could simply stop recording ever, which re-asks on every
    launch and trains people to type y without reading. `--yes` IS consent.
    """
    home, state, proj, meta = project
    assert not _fingerprint_recorded(meta)

    r = _run(["hpc", "submit", "--mode=here", "--time", "60", "--yes"],
             home=home, state=state, cwd=proj)
    assert "not inside a Slurm allocation" in (r.stdout + r.stderr), (
        f"did not reach the consent point:\n{r.stderr[-1500:]}")

    assert _fingerprint_recorded(meta), (
        "`--yes` is a real pre-authorisation and must consume the gate; not "
        "recording it means the confirmation is asked forever")


def test_the_two_runs_differ_ONLY_in_whether_consent_was_GIVEN(project):
    """The pair, in one test, because the contrast is the finding.

    Either assertion alone can be satisfied by a constant: "never record" passes
    the first, "always record" passes the second. Only the difference between
    them shows that the flag now carries the meaning it claims.
    """
    home, state, proj, meta = project

    _run(["hpc", "submit", "--mode=here", "--time", "60"],
         home=home, state=state, cwd=proj)
    after_show_only = _fingerprint_recorded(meta)

    _run(["hpc", "submit", "--mode=here", "--time", "60", "--yes"],
         home=home, state=state, cwd=proj)
    after_yes = _fingerprint_recorded(meta)

    assert (after_show_only, after_yes) == (False, True), (
        f"show-only recorded={after_show_only}, --yes recorded={after_yes}. "
        f"Both must differ: a no-TTY surface may SHOW the grant and must never "
        f"record consent for it.")
