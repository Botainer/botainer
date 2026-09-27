"""What a partition COSTS, said at the moment the job is submitted (#109).

`PartitionSpec.preemptible` and `.exclusive` were added while ~40 real sites
were transcribed, with a comment saying exactly why they matter: the affected
partitions are the cheapest and fastest to start, so they are precisely what an
optimising agent — or a user reading a queue table — will pick. Then only the
job-profile GENERATOR ever read them. Both submit paths stayed silent.

These tests are in two halves, and the second half is the point. The first
checks the text. The second checks it is CONNECTED — the defect was never that
the wording was wrong, it was that a correct function was called by nothing.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from botainer.hpc.partition_warnings import partition_warnings
from botainer.state.cluster_profile import ClusterProfile, PartitionSpec

REPO = Path(__file__).resolve().parents[2]
HPC_HOST_HELPER = REPO / "plugins" / "hpc-launcher" / "host_helper"


def _profile(_status: str = "tested-on-hardware", **specs) -> ClusterProfile:
    """A cluster profile whose partitions carry the two costly properties.

    Defaults to `tested-on-hardware` so the existing tests read the WARNING
    without the provenance clause attached; the clause has its own tests below.
    """
    return ClusterProfile(
        name="somewhere",
        slurm_default_partition="day",
        verification_status=_status,
        partitions=tuple(
            PartitionSpec(name=n, **kw) for n, kw in specs.items()),
    )


# ── half one: does it say the consequence, or just repeat the flag? ──────────


def test_preemptible_says_what_HAPPENS_not_that_a_flag_is_set():
    """"partition is preemptible" is a fact you must already understand to act
    on. The reader needs to know their job dies without warning."""
    lines = partition_warnings("scavenge", _profile(scavenge={"preemptible": True}))

    text = " ".join(lines).lower()
    assert "killed" in text and "requeued" in text
    assert "checkpoint" in text, (
        "a warning that does not say what to DO differently is decoration"
    )


def test_exclusive_says_you_pay_for_the_whole_node():
    lines = partition_warnings("bigmem", _profile(bigmem={"exclusive": True}))

    text = " ".join(lines).lower()
    assert "whole node" in text
    assert "1-core" in text or "one core" in text, (
        "the number that changes behaviour is the cost of a SMALL job here"
    )


def test_both_properties_produce_both_warnings():
    lines = partition_warnings(
        "scavenge", _profile(scavenge={"preemptible": True, "exclusive": True}))

    text = " ".join(lines).lower()
    assert "killed" in text and "whole node" in text


def test_an_ordinary_partition_is_silent():
    """A warning that fires for every partition is one nobody reads by the time
    it matters. Silence here is what makes the other cases legible."""
    assert partition_warnings("day", _profile(day={})) == []


def test_an_unknown_partition_says_nothing_rather_than_guessing():
    """We do not know this partition is safe; we know we have nothing to say
    about it. Inferring 'scavenge-like names are preemptible' is the guess this
    project already refused for cluster detection."""
    assert partition_warnings("mystery", _profile(day={"preemptible": True})) == []


def test_a_hostile_partition_name_cannot_reach_the_terminal():
    """These lines go to a human's stderr, which makes them an escape-sequence
    sink. Nothing filters the string — nothing needs to, because a line is only
    produced when the name EQUALS one in the cluster profile, and that file is
    operator config the agent cannot write. Structure, not a charset check.
    """
    hostile = "day\x1b]0;pwned\x07"
    assert partition_warnings(hostile, _profile(day={"preemptible": True})) == []


def test_no_cluster_profile_is_not_an_error():
    """The laptop case. No profile, no partitions, no traceback."""
    assert partition_warnings("day", None) == []
    assert partition_warnings("", _profile(day={"preemptible": True})) == []


# ── half two: the wiring. This is what #109 actually was. ───────────────────


def _load_submit():
    """Load the hpc-launcher host helper the way the plugin runs it.

    `submit.py` does a bare `from _common import ...`, so its own directory has
    to be importable — the same condition the real subprocess runs under.
    """
    sys.path.insert(0, str(HPC_HOST_HELPER))
    try:
        spec = importlib.util.spec_from_file_location(
            "hpc_launcher_submit_pw", HPC_HOST_HELPER / "submit.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hpc_launcher_submit_pw"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(HPC_HOST_HELPER))


def _load_common():
    sys.path.insert(0, str(HPC_HOST_HELPER))
    try:
        spec = importlib.util.spec_from_file_location(
            "hpc_launcher_common_pw", HPC_HOST_HELPER / "_common.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hpc_launcher_common_pw"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(HPC_HOST_HELPER))


def _agent_argv(image: str = "botainer-claude.sif") -> tuple[str, ...]:
    # The compute-node exec argv the plan chokepoint requires: apptainer exec,
    # the §4 cage flags, the image, an agent entrypoint — never `botainer`.
    return (
        "apptainer", "exec", "--containall", "--cleanenv", "--no-privs",
        "--drop-caps", "all", "--bind=/home/u/proj:/workspace", image,
        "/usr/local/bin/agent-claude-entrypoint",
    )


def _plan(common, tmp_path: Path, partition: str):
    return common.SubmissionPlan(
        project_root=tmp_path,
        project_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        state_root=tmp_path / "state",
        profile="default",
        partition=partition,
        account="prj1",
        time_minutes=60,
        cpus=1,
        memory_gb=None,
        gpus=0,
        gpu_type=None,
        apptainer_image="botainer-claude.sif",
        submission_mode="submit",
        existing_jobid=None,
        agent_exec_argv=_agent_argv(),
    )


def test_hpc_submit_says_what_the_partition_COSTS(tmp_path, monkeypatch):
    """THE regression guard for #109: the helper must actually CALL this."""
    import io

    from botainer.state import cluster_profile as cp
    monkeypatch.setattr(
        cp, "active_profile", lambda: _profile(scavenge={"preemptible": True}))

    submit = _load_submit()
    common = _load_common()
    buf = io.StringIO()
    submit.emit_partition_warnings(_plan(common, tmp_path, "scavenge"), stream=buf)

    out = buf.getvalue()
    assert "killed" in out and "requeued" in out, (
        "hpc-launcher submits to a preemptible partition without saying so. "
        "partition_warnings() exists and is correct; nothing called it — that "
        "is #109, and it is the shape this test defends."
    )


def test_hpc_submit_stays_quiet_on_an_ordinary_partition(tmp_path, monkeypatch):
    """The other half of a real wiring test: it must be the PARTITION that
    produces the text, not the call site producing it unconditionally."""
    import io

    from botainer.state import cluster_profile as cp
    monkeypatch.setattr(
        cp, "active_profile",
        lambda: _profile(day={}, scavenge={"preemptible": True}))

    submit = _load_submit()
    common = _load_common()
    buf = io.StringIO()
    submit.emit_partition_warnings(_plan(common, tmp_path, "day"), stream=buf)

    assert buf.getvalue() == ""


def test_the_cost_is_stated_BEFORE_the_user_is_asked_to_confirm():
    """POSITION, not just presence — and this is what the first version got
    wrong.

    Emitted inside `_do_submit`, the warning landed AFTER `_consent` had
    already printed "Launch this session? [y/N]" and been answered. A cost
    disclosed after the decision is not a disclosure. Worse, the only test path
    was `--dry-run`, which skips `_consent` entirely — so the guard exercised
    the one mode in which the defect could not appear.

    Checked by AST position inside `main`, because the alternative — driving
    `main()` — needs a full real install (project, state root, composed spec)
    and belongs in an integration test. What this pins is exactly the property
    that broke: the emission statement comes first.
    """
    import ast

    src = (HPC_HOST_HELPER / "submit.py").read_text(encoding="utf-8")
    main_fn = next(n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.FunctionDef) and n.name == "main")

    def _first_line(name: str) -> int | None:
        return min((n.lineno for n in ast.walk(main_fn)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == name),
                   default=None)

    warn_at = _first_line("emit_partition_warnings")
    consent_at = _first_line("_consent")
    assert warn_at is not None, (
        "main() no longer emits partition warnings at all — they are back to "
        "being computed somewhere that cannot precede the consent prompt"
    )
    assert consent_at is not None, "the consent gate moved; re-check this test"
    assert warn_at < consent_at, (
        f"partition warnings are emitted at line {warn_at}, AFTER the consent "
        f"prompt at line {consent_at}. The user is told what the partition "
        f"costs once they have already said yes."
    )


def test_the_warning_uses_the_EFFECTIVE_partition_not_the_flag(tmp_path):
    """WHY the warning lives in the helper and not in `botainer hpc submit`.

    The CLI knows only `--partition`. The effective partition comes from
    .botainer/config.yaml, then the flag, then the cluster default — and only
    the helper's plan holds the answer. Warning from the CLI would have been
    silent for every user who set a partition in config, which is most of them.

    Pinned by shape, and by AST rather than by a substring: a comment saying
    where the warning lives must not be able to fail this, and a real call must
    not be able to hide behind one.
    """
    import ast

    tree = ast.parse((REPO / "botainer/cli/hpc.py").read_text(encoding="utf-8"))
    calls = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "partition_warnings"]
    assert not calls, (
        f"botainer/cli/hpc.py:{calls} computes partition warnings from the "
        f"--partition flag alone. That is a second answer to a question the "
        f"helper already answers correctly, and it is wrong for every user who "
        f"set a partition in .botainer/config.yaml — which is most of them."
    )


def test_a_broken_cluster_profile_does_not_block_a_submission(
    tmp_path, monkeypatch, capsys
):
    """A warning is advice. An unreadable cluster.yaml must not turn a working
    submission into a traceback — the failure mode would be worse than the one
    the warning prevents."""
    from botainer.state import cluster_profile as cp

    def boom():
        raise RuntimeError("unreadable cluster.yaml")

    monkeypatch.setattr(cp, "active_profile", boom)

    submit = _load_submit()
    common = _load_common()
    outcome = submit._do_submit(_plan(common, tmp_path, "scavenge"), None,
                                dry_run=True)
    out = capsys.readouterr().out

    # rc 0 alone cannot tell "the warning was skipped and the plan printed"
    # from "the whole dry run was skipped", which is the failure this test is
    # named for. Assert the PLAN came out: a `scavenge` submission survived a
    # cluster profile that raises on every read.
    assert outcome.rc == 0, out
    assert "#SBATCH --partition=scavenge" in out, (
        "the broken profile swallowed the dry-run plan; a warning failing "
        f"must cost the user nothing but the warning. Got:\n{out}")


# ── the dispatcher: nobody is watching a terminal, so it goes on the status ──


@pytest.fixture
def mb(tmp_path):
    from botainer.hpc import jobs
    m = jobs.JobMailbox(
        root=tmp_path, in_dir=tmp_path / "in",
        out_dir=tmp_path / "out", run_dir=tmp_path / "run")
    for d in (m.in_dir, m.out_dir, m.run_dir):
        d.mkdir()
    return m


def test_a_dispatched_job_records_the_partition_cost_on_its_status(mb, monkeypatch):
    """An AGENT choosing a partition is the case the PartitionSpec comment
    warned about: it optimises for start time, which selects the preemptible
    queue. There is no terminal here, so the warning goes where `hpc
    jobs-status` and the agent both read it.
    """
    from botainer.core.config import JobProfile
    from botainer.core.policy import JobPolicy
    from botainer.hpc import dispatcher
    from botainer.state import cluster_profile as cp

    monkeypatch.setattr(
        cp, "active_profile", lambda: _profile(scavenge={"preemptible": True}))

    jid = "abcdef0123456789"
    (mb.in_dir / f"{jid}.json").write_text(json.dumps(
        {"version": "botainer-job-v1", "id": jid, "profile": "cheap",
         "command": ["python", "train.py"], "submitted_at": "now"}))

    dispatcher.submit_request(
        mb, jid + ".json", {"cheap": JobProfile(partition="scavenge",
                                                time="04:00:00")},
        JobPolicy(), "img.sif", sbatch=lambda p, _prof="": "1")

    rec = json.loads((mb.out_dir / f"{jid}.status.json").read_text())
    assert rec["state"] == "queued", "the warning must not block the submission"
    warns = " ".join(rec.get("warnings", []))
    assert "killed" in warns and "requeued" in warns


def test_an_ordinary_dispatched_job_gains_no_warning(mb, monkeypatch):
    from botainer.core.config import JobProfile
    from botainer.core.policy import JobPolicy
    from botainer.hpc import dispatcher
    from botainer.state import cluster_profile as cp

    monkeypatch.setattr(
        cp, "active_profile", lambda: _profile(day={}, scavenge={"preemptible": True}))

    jid = "abcdef0123456789"
    (mb.in_dir / f"{jid}.json").write_text(json.dumps(
        {"version": "botainer-job-v1", "id": jid, "profile": "normal",
         "command": ["python", "train.py"], "submitted_at": "now"}))

    dispatcher.submit_request(
        mb, jid + ".json", {"normal": JobProfile(partition="day", time="04:00:00")},
        JobPolicy(), "img.sif", sbatch=lambda p, _prof="": "1")

    rec = json.loads((mb.out_dir / f"{jid}.status.json").read_text())
    assert "killed" not in " ".join(rec.get("warnings", []))


# ── the warning says WHERE ITS CLAIM CAME FROM ──────────────────────────────


def test_a_docs_derived_claim_says_so_in_the_warning():
    """Warnings must distinguish documentation-derived partition metadata
    from values checked against a live scheduler. State the source and how
    to verify it at the point where the user acts on the warning.
    """
    lines = partition_warnings(
        "scavenge", _profile("from-public-docs", scavenge={"preemptible": True}))

    # Asserted on the RETURNED LIST, not a joined blob: these are the strings
    # the caller writes to the user's terminal one per line.
    assert any("killed" in ln for ln in lines), "the warning itself must still be there"
    caveat = [ln for ln in lines if "cluster profile" in ln]
    assert len(caveat) == 1, f"expected exactly one provenance line, got {lines!r}"
    assert "NOT checked against the live scheduler" in caveat[0]
    assert "sinfo" in caveat[0], (
        "naming the doubt without naming the check leaves the reader nowhere"
    )
    assert "login node" in caveat[0], (
        "a command with no WHERE is the failure this project already has a "
        "rule about — `sinfo` is not runnable from inside the session"
    )


def test_a_hardware_verified_claim_gets_NO_caveat():
    """The other half, and the reason this is conditional rather than a footer
    on everything: a clause attached to every warning trains people to discount
    all of them, including the one that was actually measured."""
    lines = partition_warnings(
        "scavenge", _profile("tested-on-hardware", scavenge={"preemptible": True}))

    assert any("killed" in ln for ln in lines)
    assert not [ln for ln in lines if "cluster profile" in ln], (
        "a profile verified on hardware has nothing to caveat, and saying "
        "something anyway devalues the caveat where it is real"
    )


def test_no_provenance_clause_without_a_warning_to_attach_it_to():
    """An ordinary partition stays completely silent. A profile-provenance
    footer on a session that had nothing to warn about is pure noise."""
    assert partition_warnings("day", _profile("from-public-docs", day={})) == []


def test_an_unrecorded_provenance_is_not_read_as_verified():
    """A profile that does not say how it was made must not be treated as if
    it had been checked — that is the direction the mistake goes."""
    lines = partition_warnings(
        "scavenge", _profile("", scavenge={"preemptible": True}))
    assert any("does not record where it came from" in ln for ln in lines), lines
