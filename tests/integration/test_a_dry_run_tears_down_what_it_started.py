"""`--dry-run` composes for real. That is correct, and it has consequences.

MEASURED on a real state root (a fake `.sif`, a project with `agent-claude-shared`
enabled, `hpc submit --dry-run`): the run created the session directory, the
per-project shared-mode credential SYMLINK and its README, and republished the
agent-readable `out/profiles.json`.

I "CORRECTED" THE QUEUE ROW AND THE CORRECTION WAS WRONG. I measured a dry run
leaving the shared master byte- and mtime-identical, concluded the row's "MUTATES the
host credential store" was overstated, and wrote that into three places. A refuting
review changed ONE thing in this fixture — a per-project `.credentials.json` that is a
REGULAR FILE with a strictly-later `expiresAt`, which is exactly what an in-container
`rename()` refresh leaves behind and the only reason `reconcile_shared_credential`
exists — and the dry run COPIED THOSE TOKEN BYTES OVER THE SHARED MASTER.

So the row was right and my generalisation from one benign state was the error. The
branch that writes was simply unreachable in the fixture I had built: with only a
master present, reconcile takes the "just make the symlink" path. A test whose
arrangement cannot reach the dangerous branch proves the safe one and reads as
proving both.

This file now sets up the reachable state and pins what ACTUALLY happens, so the
behaviour is visible rather than described.

WHY THE HOOKS MUST KEEP RUNNING. Their bind/env contributions are what make the
printed sbatch script the script that would actually run. Skipping them on
`--dry-run` would print a fiction — the inspect-vs-start divergence this project
already fixed once — so "make dry-run not write" by skipping hooks trades a write for
a lie.

SO THE DEFECT IS THE OTHER HALF: a pre_session hook may START A HOST PROCESS
(wolfram-sidecar Popens a helper, the broker hooks start a daemon), and every exit
that composed without launching returned without stopping it. compose already runs
the teardown on its own cross-node refusal path; this extends that rule to the
remaining exits instead of inventing a new one. And a "dry run" that wrote host state
now says which state, because silence there is a false impression by omission.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
_HELPER = REPO / "plugins/hpc-launcher/host_helper/_common.py"
_SUBMIT = REPO / "plugins/hpc-launcher/host_helper/submit.py"


def _load_submit():
    """`submit.py` does `from _common import …`, so the helper dir goes on the path
    and `_common` must be importable under that exact name."""
    helper_dir = str(_HELPER.parent)
    if helper_dir not in sys.path:
        sys.path.insert(0, helper_dir)
    if "hpc_launcher_submit_teardown" in sys.modules:
        return sys.modules["hpc_launcher_submit_teardown"]
    if "_common" not in sys.modules:
        cspec = importlib.util.spec_from_file_location("_common", _HELPER)
        cmod = importlib.util.module_from_spec(cspec)
        sys.modules["_common"] = cmod
        cspec.loader.exec_module(cmod)
    spec = importlib.util.spec_from_file_location(
        "hpc_launcher_submit_teardown", _SUBMIT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hpc_launcher_submit_teardown"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def composed(monkeypatch, tmp_path):
    """A project that composes: real state root, real project-id, a `.sif` that
    exists, `agent-claude-shared` enabled so a plugin's pre_session actually runs."""
    from botainer.core import config as cfgm
    from botainer.core import identity
    from botainer.state import dir as state_dir

    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "root"))
    monkeypatch.delenv("BOTAINER_STATE_ROOT", raising=False)
    monkeypatch.delenv("BOTAINER_PROFILE", raising=False)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    sif = paths.apptainer_sif_path("agent-claude")
    sif.parent.mkdir(parents=True, exist_ok=True)
    sif.write_bytes(b"NOT-A-REAL-SIF" * 100)

    proj = tmp_path / "proj"
    proj.mkdir()
    cfgm.write_initial_config(proj, agent="claude", force=True)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    cfg_path = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["plugins_enabled"] = ["agent-claude-shared"]
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))

    shared = paths.root / "shared-auth" / "agent-claude"
    shared.mkdir(parents=True, exist_ok=True)
    cred = shared / ".credentials.json"
    cred.write_text('{"claudeAiOauth":{"accessToken":"MASTER","refreshToken":"r",'
                    '"expiresAt":9999999999999}}')
    cred.chmod(0o600)

    monkeypatch.setenv("BOTAINER_PROJECT_ROOT", str(proj))
    monkeypatch.setenv("BOTAINER_PROJECT_UUID", identity.read_project_id(proj))
    monkeypatch.chdir(proj)
    return paths, proj, cred


@pytest.fixture
def composed_with_refreshed_local(composed, tmp_path):
    """`composed`, plus the state a live in-container refresh leaves behind.

    A per-project `.credentials.json` that is a REGULAR FILE (not the symlink) with a
    strictly-later `expiresAt`, real-shaped so the #158 charset/shape checks accept
    it. This is the ONLY arrangement that reaches the back-fill branch, and its
    absence is why my first attempt at this file certified the opposite behaviour.
    """
    import json
    import time

    from botainer.core import identity

    paths, proj, cred = composed
    uid = identity.read_project_id(proj)
    now_ms = int(time.time() * 1000)

    def _oauth(tag: str, exp_ms: int) -> str:
        return json.dumps({"claudeAiOauth": {
            "accessToken": "sk-ant-oat" + tag * 70,
            "refreshToken": "sk-ant-ort" + tag * 70,
            "expiresAt": exp_ms}})

    cred.write_text(_oauth("M", now_ms + 3_600_000))
    cred.chmod(0o600)
    per_project = (paths.root / "state" / uid / "data" / "agent-claude"
                   / "profiles" / "default")
    per_project.mkdir(parents=True, exist_ok=True)
    local = per_project / ".credentials.json"
    local.write_text(_oauth("F", now_ms + 7_200_000))
    local.chmod(0o600)
    return paths, proj, cred, local


def _argv(monkeypatch, *extra: str) -> None:
    monkeypatch.setattr(sys, "argv", [
        "submit", "--partition", "day", "--account", "acct", "--time", "60",
        *extra])


def test_a_dry_run_runs_the_post_session_teardown(composed, monkeypatch, capsys):
    """THE DEFECT. Nothing stopped what a pre_session hook started.

    Asserted at the teardown call itself, because that is the observable: whether a
    sidecar process survives depends on which plugins are installed, and a test that
    needed a process-starting plugin would pin the plugin rather than the rule.
    """
    submit = _load_submit()
    from botainer.core import composition

    calls: list[object] = []
    monkeypatch.setattr(composition, "run_post_session_hooks",
                        lambda spec: calls.append(spec))
    _argv(monkeypatch, "--dry-run", "--yes")

    rc = submit.main()

    assert rc == 0, capsys.readouterr()
    assert len(calls) == 1, (
        f"post_session ran {len(calls)} times on a dry run; anything a pre_session "
        f"hook started is still running")
    assert getattr(calls[0], "session_id", None), (
        "teardown was handed something that is not the composed spec")


def test_a_dry_run_SAYS_what_host_state_it_wrote(composed, monkeypatch, capsys):
    """Silence would be a false impression by omission.

    A user who reads "dry run" and later finds a new session directory has been
    misled by the word. The disclosure names the directory and says plainly that no
    token bytes are copied — which is the part the queue row had wrong, so stating it
    is also the correction reaching the user rather than only the log.
    """
    submit = _load_submit()
    from botainer.core import composition

    monkeypatch.setattr(composition, "run_post_session_hooks", lambda spec: None)
    _argv(monkeypatch, "--dry-run", "--yes")

    rc = submit.main()

    err = capsys.readouterr().err
    assert rc == 0
    assert "COMPOSED THIS SESSION FOR REAL" in err, err
    assert "session dir:" in err and "/sessions/" in err, err
    assert "Nothing was submitted" in err, err
    # THE SENTENCE THIS ASSERTION USED TO DEMAND WAS FALSE. It was
    # "no token bytes are copied", which a refuting review disproved in shared mode.
    # What must be present now is the warning, and what must be ABSENT is any claim
    # that a preview cannot touch a credential.
    assert "NOT CREDENTIAL-INERT" in err, err
    assert "ROTATE your refresh token" in err, err
    assert "no token bytes are copied" not in err, (
        "the retracted claim is back in the user-facing text")


def test_a_dry_run_DOES_promote_a_newer_project_token_to_the_shared_login(
        composed_with_refreshed_local, monkeypatch, capsys):
    """THE CORRECTION, pinned. A preview is NOT credential-inert.

    My first version of this test asserted the opposite — "a dry run does not touch
    the master credential" — and passed, because its fixture created only the master,
    so `reconcile_shared_credential` took the make-a-symlink path and never reached
    the back-fill. Its docstring promised "if a future change starts copying token
    bytes during a dry run, this fails". Code that already copied token bytes was
    passing it.

    What this pins is the CURRENT SHIPPED BEHAVIOUR, not an endorsement of it: a
    non-launching compose runs the shared-mode reconcile, and a newer per-project
    token is promoted machine-wide. Whether a preview should do that is a credential
    decision put to the maintainer (REVIEW-QUEUE), because the per-project profile
    dir is bound rw into the cage, so this is a trigger an agent can arm and a user
    believes is a no-op. If the decision is "do not", this test flips and says so.
    """
    submit = _load_submit()
    _paths, _proj, cred, local = composed_with_refreshed_local
    master_before = cred.read_bytes()
    local_before = local.read_bytes()
    _argv(monkeypatch, "--dry-run", "--yes")

    rc = submit.main()
    err = capsys.readouterr().err

    assert rc == 0, err
    assert cred.read_bytes() == local_before, (
        "the shared master did NOT take the newer per-project token — if this fix "
        "landed, invert this test and the disclosure text with it")
    assert cred.read_bytes() != master_before
    assert "refreshed its login token" in err, (
        f"it promoted a token machine-wide and did not say so: {err[-1500:]}")
    # And the launcher's own disclosure must not contradict the hook standing right
    # next to it. This is the sentence I got wrong.
    assert "no token bytes are copied" not in err, (
        "the dry-run disclosure claims no token bytes are copied, in the same "
        "stderr as the hook announcing that it copied them")
    assert "NOT CREDENTIAL-INERT" in err, err[-1500:]


def test_the_CONCURRENCY_CAP_refusal_tears_down_too(composed, monkeypatch, capsys):
    """The third non-launching exit, and mutating its teardown away killed NO test
    until this one existed — which is the protocol's signal that a branch was
    written and nothing checked it.

    This exit is reached AFTER compose (the cap check needs the plan), so it leaks
    exactly what the other two do. It is also the one a user hits repeatedly: the cap
    exists to be hit.
    """
    submit = _load_submit()
    from botainer.core import composition

    calls: list[object] = []
    monkeypatch.setattr(composition, "run_post_session_hooks",
                        lambda spec: calls.append(spec))
    monkeypatch.setattr(submit, "_refuse_if_concurrency_cap_exceeded",
                        lambda plan, dry_run: 7)
    _argv(monkeypatch, "--yes")

    rc = submit.main()

    assert rc == 7, capsys.readouterr()
    assert len(calls) == 1, (
        "the concurrency-cap refusal left whatever pre_session started running")


def test_a_REFUSED_CONSENT_tears_down_too(composed, monkeypatch, capsys):
    """The generalisation, and the reason this is not a dry-run special case.

    Consent is asked AFTER compose (it shows the composed capability summary — that
    is the point of asking it there). So answering "no" also leaves a helper running
    unless the teardown runs on that exit too. A fix that only covered `--dry-run`
    would leave the path a cautious user takes.
    """
    submit = _load_submit()
    from botainer.core import composition
    from botainer.inspect import capability_summary

    calls: list[object] = []
    monkeypatch.setattr(composition, "run_post_session_hooks",
                        lambda spec: calls.append(spec))
    monkeypatch.setattr(capability_summary, "print_and_maybe_confirm",
                        lambda *a, **k: False)
    _argv(monkeypatch)                    # no --dry-run, no --yes

    rc = submit.main()

    assert rc == 3, capsys.readouterr()
    assert len(calls) == 1, (
        "a refused consent left whatever pre_session started running")


# ── The six exits INSIDE the dispatch functions ──────────────────────────────
#
# The three tests above cover exits in `main()` itself. `_teardown()` was a
# CLOSURE in that function, so `_do_submit` / `_do_attach` / `_do_here` could
# not call it at all — six more non-launching returns, every one of them after
# compose has run the pre_session hooks, and none able to reach the cleanup
# defined a few lines above. The fix makes the dispatch return `Outcome(rc,
# launched)` so `main()` judges it in one place.
#
# WHY EACH TEST BELOW DRIVES `submit.main()`: at checkpoint 11 a test of mine
# called the helper directly and the entire fix could be reverted with the suite
# still green. The call site is the thing under test.


def _teardown_calls(monkeypatch) -> list:
    from botainer.core import composition
    calls: list = []
    monkeypatch.setattr(composition, "run_post_session_hooks",
                        lambda spec: calls.append(spec))
    return calls


def test_NO_PARTITION_tears_down(composed, monkeypatch, capsys):
    """`--partition` absent: refused inside `_do_submit`, after compose."""
    submit = _load_submit()
    calls = _teardown_calls(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["submit", "--account", "a", "--time", "60", "--yes"])

    rc = submit.main()

    assert rc == 4, capsys.readouterr()
    assert len(calls) == 1, "the no-partition refusal left a hook's process running"


def test_SBATCH_NOT_ON_PATH_tears_down(composed, monkeypatch, capsys):
    submit = _load_submit()
    calls = _teardown_calls(monkeypatch)
    monkeypatch.setattr(submit, "have_slurm", lambda: False)
    _argv(monkeypatch, "--yes")

    rc = submit.main()

    assert rc == 5, capsys.readouterr()
    assert len(calls) == 1, "'sbatch not on PATH' left a hook's process running"


def _fake_sbatch(tmp_path, monkeypatch, *, rc: int, stdout: str = "", stderr: str = ""):
    """A REAL `sbatch` on PATH, not a patched `subprocess.run`.

    My first version monkeypatched `submit.subprocess.run` — which also caught
    the plugin hook runner, so the agent-claude-shared pre_session hook "failed"
    with my fake sbatch's stderr and the test measured COMPOSE REFUSING instead
    of sbatch rejecting. Two teardowns, and a green-looking assertion for the
    wrong reason. A fixture has to match the real arrangement, so this puts an
    executable where the code looks for one and lets `have_slurm()` find it.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "sbatch"
    script.write_text(
        "#!/bin/sh\n"
        + (f"printf '%s' {stdout!r}\n" if stdout else "")
        + (f"printf '%s' {stderr!r} >&2\n" if stderr else "")
        + f"exit {rc}\n")
    script.chmod(0o755)
    # `have_slurm()` is `which("sbatch") and which("squeue")` — BOTH, so an
    # sbatch alone leaves the launcher saying "not on a Slurm cluster" and the
    # test measures the wrong exit. Found by running it.
    squeue = bindir / "squeue"
    squeue.write_text("#!/bin/sh\nexit 0\n")
    squeue.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    return script


def test_SBATCH_REJECTING_THE_JOB_tears_down(composed, monkeypatch, tmp_path, capsys):
    """THE COMMON REAL-CLUSTER CASE, and the one that leaked longest.

    A bad account, a full partition, a malformed directive — sbatch exits
    non-zero and NOTHING is queued, so whatever a pre_session hook started has
    no job to serve. `_do_submit` returned `completed.returncode` straight out
    of the function, past a teardown it could not reach.
    """
    submit = _load_submit()
    calls = _teardown_calls(monkeypatch)
    _fake_sbatch(tmp_path, monkeypatch, rc=1,
                 stderr="sbatch: error: invalid account\n")
    _argv(monkeypatch, "--yes")

    rc = submit.main()

    assert rc == 1, capsys.readouterr()
    assert len(calls) == 1, (
        "sbatch REJECTED the job and nothing tore down what a hook started — "
        "no job was queued, so there is nothing for that process to serve")


def test_a_SUCCESSFUL_SUBMIT_does_NOT_tear_down(composed, monkeypatch, tmp_path, capsys):
    """THE OPPOSITE DIRECTION, and without it "always tear down" passes.

    A submitted job runs LATER on a compute node. A host helper a pre_session
    hook started may be needed for its lifetime, so tearing down here would
    break the job this command just queued. `launched=True` exists to say so.
    """
    submit = _load_submit()
    calls = _teardown_calls(monkeypatch)
    _fake_sbatch(tmp_path, monkeypatch, rc=0,
                 stdout="Submitted batch job 999001\n")
    _argv(monkeypatch, "--yes")

    rc = submit.main()

    assert rc == 0, capsys.readouterr()
    assert calls == [], (
        "a SUBMITTED job was torn down — the job runs later on a compute node "
        "and may need what the hook started")


def test_ATTACH_WITHOUT_A_JOBID_tears_down(composed, monkeypatch, capsys):
    submit = _load_submit()
    calls = _teardown_calls(monkeypatch)
    monkeypatch.setenv("SLURM_JOB_ID", "")
    _argv(monkeypatch, "--mode", "attach", "--yes")

    rc = submit.main()

    assert rc == 4, capsys.readouterr()
    assert len(calls) == 1, "attach-with-no-jobid left a hook's process running"


def test_HERE_OUTSIDE_AN_ALLOCATION_tears_down(composed, monkeypatch, capsys):
    submit = _load_submit()
    calls = _teardown_calls(monkeypatch)
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    _argv(monkeypatch, "--mode", "here", "--yes")

    rc = submit.main()

    assert rc == 4, capsys.readouterr()
    assert len(calls) == 1, "here-mode outside an allocation left a hook running"


def test_the_teardown_runs_ONCE_even_when_two_paths_ask(composed, monkeypatch, capsys):
    """`--dry-run` is judged by BOTH the single site and the disclosure block.

    Running this project's post_session hooks twice is not free — a hook that
    stops a daemon is not obliged to survive being told twice — so the teardown
    is run-once, which is what makes a single judging site safe.
    """
    submit = _load_submit()
    calls = _teardown_calls(monkeypatch)
    _argv(monkeypatch, "--dry-run")

    rc = submit.main()

    assert rc == 0, capsys.readouterr()
    assert len(calls) == 1, f"post_session ran {len(calls)} times, not once"


# ── The SAME hole on `botainer start`, which is where it actually bites ──────
#
# A refuting review's most useful finding: the sbatch launcher's fix was aimed
# at the path where its two named examples CANNOT happen. `agent-claude-broker`
# is refused before compose on the sbatch path, and wolfram-sidecar contributes
# a unix-socket bind, which the cross-node check refuses inside compose (and
# that path already tore down). Both run for real on `botainer start` — and
# declining the consent prompt there left the helper running, measured with a
# probe plugin.

def test_DECLINING_the_start_consent_prompt_tears_down(composed, monkeypatch):
    """`start` asks consent AFTER compose, so a decline leaks what a hook began.

    Driven through `cli.start`'s own command with the prompt answered `n`, not
    by calling a helper: the whole point of the finding is that the teardown
    lived in the launch `finally`, which a decline never reaches.
    """
    from click.testing import CliRunner

    from botainer.cli.start import start as start_cmd
    from botainer.core import composition
    from botainer.inspect import capability_summary

    calls: list = []
    monkeypatch.setattr(composition, "run_post_session_hooks",
                        lambda spec: calls.append(spec))
    monkeypatch.setattr(capability_summary, "print_and_maybe_confirm",
                        lambda *a, **k: False)

    result = CliRunner().invoke(start_cmd, ["--runtime", "apptainer"])

    assert result.exit_code == 0, result.output
    assert "Aborted by user." in result.output or "Aborted by user." in str(result.stderr_bytes or b"")
    assert len(calls) == 1, (
        "declining the launch left whatever pre_session started running — the "
        "only teardown on this path was inside the launch `finally`")


def test_ATTACH_WITH_SRUN_MISSING_tears_down(composed, monkeypatch, capsys):
    """The sixth exit, and it had no test until a refuting review counted them.

    Five of the six were pinned; this one was right by inspection and held by
    nothing, which is the state a mutation walks through.
    """
    submit = _load_submit()
    calls = _teardown_calls(monkeypatch)
    monkeypatch.setattr(submit, "have_slurm", lambda: False)
    _argv(monkeypatch, "--mode", "attach", "--jobid", "12345", "--yes")

    rc = submit.main()

    assert rc == 5, capsys.readouterr()
    assert len(calls) == 1, "'srun not on PATH' left a hook's process running"


def test_a_HERE_SESSION_THAT_RAN_tears_down_when_it_ENDS(
        composed, monkeypatch, tmp_path, capsys):
    """`apptainer exec` is SYNCHRONOUS: when it returns, the session is over.

    So post_session is due — the same moment `botainer start` runs it in its
    launch `finally`. My first version marked this `launched=True`, copying the
    submit path's justification ("the job runs later on a compute node"), which
    is false here and skipped the shared-mode credential reconcile: a refresh
    inside the container was never promoted to the shared login, silently. A
    refuting review measured that against a stub that simulates the refresh.

    Pins the DIRECTION a mutation flips: marking it launched again leaves the
    teardown unrun.
    """
    submit = _load_submit()
    calls = _teardown_calls(monkeypatch)
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    for name in ("apptainer", "singularity"):
        exe = bindir / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SLURM_JOB_ID", "424242")
    _argv(monkeypatch, "--mode", "here", "--yes")

    rc = submit.main()

    assert rc == 0, capsys.readouterr()
    assert len(calls) == 1, (
        "a here-mode session ENDED and post_session never ran — that is when "
        "the shared-mode credential reconcile is due, and skipping it is the "
        "cross-project 'login expired' symptom that hook exists to prevent")


def test_an_ATTACH_SESSION_THAT_RAN_tears_down_when_it_ENDS(
        composed, monkeypatch, tmp_path, capsys):
    """The attach twin of the here-mode test above, and it was unpinned too.

    Flipping `_do_attach`'s completion return to `launched=True` survived every
    other test in this file. Two synchronous paths, one shared justification —
    so both need their own assertion, not one standing in for the pair.
    """
    submit = _load_submit()
    calls = _teardown_calls(monkeypatch)
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    for name in ("srun", "squeue", "sbatch"):
        exe = bindir / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    _argv(monkeypatch, "--mode", "attach", "--jobid", "777", "--yes")

    rc = submit.main()

    assert rc == 0, capsys.readouterr()
    assert len(calls) == 1, (
        "an attached session ENDED and post_session never ran — `srun` is "
        "synchronous, so its return IS the end of the session")


# ─────────────────────────────────────────────────────────────────────────────
# Everything above pins the CALL. This pins the EFFECT.
#
# Gutting `run_post_session_hooks` to `pass` keeps every test above green, because
# they replace it with a recorder. That is defensible where the claim IS "which
# exits call the teardown" — the call is the observable there — and it is a gap for
# the file as a whole, because the defect is a LEAKED PROCESS. Nothing in the suite
# watched one die.
#
# The bundled process-starting plugins need real inputs (wolfram-sidecar wants its
# helper, the broker a credential and a port), so this installs a PROBE: a
# host_helper whose pre_session Popens a real `sleep` and records its pid, and whose
# post_session kills it. Then the assertion is `kill(pid, 0)` — the operating
# system's answer, not botainer's.
# ─────────────────────────────────────────────────────────────────────────────

PROBE = "teardown-probe"


def _install_probe(paths, tmp_path) -> Path:
    """A host_helper plugin that starts a real process and records its pid.

    `host_helper` and not `agent`: the project already enables an agent plugin, and
    a second one trips the one-agent-wrap guard — a refusal, which would make this
    test pass for the wrong reason.

    Installed the way trust actually works: a lock entry, because a bare directory
    with a self-declared `tier: first-party` is REFUSED at compose (that is the
    point of the mechanism-based trust rule). The tree hash is computed, not
    invented, so this stays honest if verification is ever tightened.
    """
    from botainer.plugins import provenance as prov

    pdir = paths.plugins_dir / PROBE
    (pdir / "hooks").mkdir(parents=True, exist_ok=True)
    pidfile = tmp_path / "probe.pid"

    (pdir / "botainer-plugin.yaml").write_text(
        "apiVersion: botainer-plugin-v1\n"
        f"name: {PROBE}\n"
        "version: 0.1.0\n"
        "description: test-only probe that starts a real host process\n"
        "license: Apache-2.0\n"
        "maintainer: botainer-tests\n"
        "botainer_min_version: 0.1.0a0\n"
        "tier: first-party\n"
        "kind: host_helper\n"
        "trust_required: hooked\n"
        "runtimes:\n  - docker\n  - apptainer\n"
        "hooks:\n"
        "  - when: pre_session\n    script: hooks/pre_session.py\n"
        "    timeout_seconds: 20\n"
        "  - when: post_session\n    script: hooks/post_session.py\n"
        "    timeout_seconds: 20\n"
    )
    # Paths are BAKED IN, not read from the environment: run_hook scrubs the env to
    # an allowlist, so a hook that looked up an env var here would silently find
    # nothing and the test would measure the scrubber instead of the teardown.
    (pdir / "hooks" / "pre_session.py").write_text(
        "#!/usr/bin/env python3\n"
        "import json, subprocess\n"
        # DEVNULL on all three, and NOT decoration: a child that inherits the
        # hook's stdout keeps that pipe open, the runner waits for EOF that never
        # comes, and pre_session dies on its 20s timeout — measured, first try.
        # wolfram-sidecar detaches for the same reason. The refusal blames the hook,
        # which is not where the wait is; that is tracked separately.
        "p = subprocess.Popen(['sleep', '300'], start_new_session=True,\n"
        "                     stdin=subprocess.DEVNULL,\n"
        "                     stdout=subprocess.DEVNULL,\n"
        "                     stderr=subprocess.DEVNULL)\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "print(json.dumps({'version': 'plugin-contribution-v1',\n"
        "                  'kind': 'pre_session'}))\n"
    )
    (pdir / "hooks" / "post_session.py").write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, time\n"
        f"pid = int(open({str(pidfile)!r}).read())\n"
        "os.kill(pid, signal.SIGTERM)\n"
        # A delivered signal is asynchronous. This fixture promises to stop
        # its helper, so its hook must observe that effect before returning.
        # Keep the caller's immediate assertion and the no-teardown control:
        # neither should depend on signal scheduling or init's reap timing.
        "deadline = time.monotonic() + 3\n"
        "while True:\n"
        "    try:\n"
        "        os.kill(pid, 0)\n"
        "    except ProcessLookupError:\n"
        "        break\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise RuntimeError('fixture helper survived SIGTERM')\n"
        "    time.sleep(0.01)\n"
        "print(json.dumps({'version': 'plugin-contribution-v1',\n"
        "                  'kind': 'post_session'}))\n"
    )
    for h in (pdir / "hooks").iterdir():
        h.chmod(0o755)

    prov.append_lock(paths.installed_lock_path, prov.ProvenanceEntry(
        name=PROBE, version="0.1.0", source="test-fixture",
        tree_sha=prov.compute_tree_sha(pdir), image_digest=None,
        installed_at="t", tier="first-party"))
    return pidfile


def _enable_probe(proj) -> None:
    cfg_path = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["plugins_enabled"] = [*data.get("plugins_enabled", []), PROBE]
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:      # exists, owned by someone else — still alive
        return True
    return True


@pytest.fixture
def probe(composed, tmp_path):
    """Yields the pidfile, and REAPS whatever is still running afterwards.

    A test that leaks a `sleep 300` is worse than no test: this repo has already
    lost an evening to two orphaned busy-loops from a previous harness pinning two
    cores and corrupting every wall-clock number taken afterwards.
    """
    paths, proj, _cred = composed
    pidfile = _install_probe(paths, tmp_path)
    _enable_probe(proj)
    yield pidfile
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text()), 9)
        except (ProcessLookupError, ValueError):
            pass


def test_the_teardown_KILLS_THE_PROCESS_not_just_calls_the_function(
        probe, monkeypatch, capsys):
    """The effect, observed: `kill(pid, 0)` after a non-launching exit.

    Nothing is monkeypatched here. `run_post_session_hooks` runs for real, finds the
    probe's post_session, and the process it started is gone afterwards — which is
    the claim the teardown makes and which, until this test, no assertion here
    could have detected the loss of.
    """
    submit = _load_submit()
    _argv(monkeypatch, "--dry-run", "--yes")

    rc = submit.main()

    out = capsys.readouterr()
    assert rc == 0, out
    assert probe.exists(), (
        f"the probe's pre_session never ran, so this test proves nothing about "
        f"teardown. stderr:\n{out.err}")
    pid = int(probe.read_text())
    assert not _alive(pid), (
        f"pid {pid} — started by a pre_session hook — is STILL RUNNING after a "
        f"dry run returned. On a login node that is the leak this file is about.")


def test_WITHOUT_the_teardown_that_same_process_SURVIVES(
        probe, monkeypatch, capsys):
    """The control, and the reason the test above is not vacuous.

    If `sleep` died on its own — or was never started — the assertion above would
    pass with the teardown deleted. Here post_session is stubbed out and the SAME
    arrangement leaves the process alive, so the difference between the two tests
    is the teardown and nothing else.
    """
    submit = _load_submit()
    from botainer.core import composition
    monkeypatch.setattr(composition, "run_post_session_hooks", lambda spec: None)
    _argv(monkeypatch, "--dry-run", "--yes")

    rc = submit.main()

    assert rc == 0, capsys.readouterr()
    pid = int(probe.read_text())
    assert _alive(pid), (
        "the control is broken: the process was gone without any teardown, so the "
        "companion test would pass even with the fix deleted")
    os.kill(pid, 9)


def test_a_REJECTED_SBATCH_kills_the_process_too(probe, tmp_path, monkeypatch,
                                                 capsys):
    """THE PATH THE FIX ACTUALLY ADDED, watched by the operating system.

    A loop checkpoint measured that my two process-observing tests both drove
    `--dry-run` — and the dry-run teardown is a PRE-EXISTING call site, not one
    of the six exits this work made reachable. So the only test in the suite
    that watched a process die was covering a path that was never broken, while
    the commit message claimed it pinned the new one. It did not: deleting the
    judging site failed eight OTHER tests and left both of those green.

    `sbatch` rejecting is the common real-cluster case and goes through
    `_do_submit`'s non-dry-run return, which is exactly what `Outcome(rc,
    launched=False)` made tear down. This is that path, with a real child
    process and `kill(pid, 0)` as the assertion.
    """
    submit = _load_submit()
    _fake_sbatch(tmp_path, monkeypatch, rc=1, stderr="sbatch: error: invalid account\n")
    _argv(monkeypatch, "--yes")

    rc = submit.main()

    out = capsys.readouterr()
    assert rc != 0, out
    assert probe.exists(), (
        f"the probe's pre_session never ran, so this proves nothing:\n{out.err}")
    pid = int(probe.read_text())
    assert not _alive(pid), (
        f"pid {pid} — started by a pre_session hook — survived a REJECTED "
        f"sbatch. On a login node that is the leak, on the path that actually "
        f"had it.")
