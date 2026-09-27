"""A partition whose PROSE shouts PREEMPTIBLE must say so in its FIELD (#211).

WHY THIS EXISTS. `preemptible` and `exclusive` were added to `PartitionSpec`
while ~40 real sites were transcribed, with a comment saying exactly why they
matter — the affected partitions are the cheapest and fastest to start, so they
are what an optimising agent or a user reading a queue table will pick. Then
only the job-profile generator read them, and they were set on 2 of 51 bundled
profiles.

That was survivable while nothing consumed them. It stopped being survivable
when `partition_warnings` shipped (#109), because a warning's SILENCE reads as
an all-clear. And the fact was not missing — it was sitting in the same file,
in prose, sometimes two lines from the unset field:

    serial_requeue:
      max_time_minutes: 4320        # PREEMPTIBLE — can be requeued any time

Two writers of one fact and nothing comparing them: the shape this project
keeps finding (the default auth mode in two places, the .sif name in four).
This test is the comparison.

WHAT IT CHECKS, AND WHAT IT DELIBERATELY DOES NOT. Only the assertive POSITIVE
direction: a partition block whose prose says PREEMPTIBLE or EXCLUSIVE in
capitals must carry the matching field. It does NOT require a negative to be
recorded — "we did not check" is a legitimate state, which is the whole reason
the fields became three-state. A gate that demanded a value everywhere would
push people to write `false` where they mean "unknown", which is the defect
this work removed.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
PROFILES = sorted((REPO / "cluster_profiles").glob("*.yaml"))

#: Capitalised because that is how the profiles emphasise it, and because a
#: lowercase match would fire on prose ABOUT the property ("no documented
#: preemptible partition") rather than an assertion of it.
_POSITIVE = {
    "preemptible": re.compile(r"\bPREEMPTIBLE\b"),
    "exclusive": re.compile(r"\bEXCLUSIVE\b"),
}
#: A line that MENTIONS the word while denying or merely discussing it.
_NEGATED = re.compile(
    r"non-preemptible|not\s+preemptible|no\s+`?exclusive|there\s+is\s+no|"
    r"did\s+not\s+find|unlike\b|no\s+documented",
    re.IGNORECASE,
)


def _partition_blocks(path: pathlib.Path) -> dict[str, list[str]]:
    """Raw source lines belonging to each partition, comments included.

    Reads the TEXT, not the parsed model: the whole point is to compare what a
    human wrote in a comment against what the loader will see. A yaml.safe_load
    would throw the comments away — which is exactly how the two copies drifted.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.rstrip() == "  partitions:")
    except StopIteration:
        return {}
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("    "):
            break                                   # left the partitions map
        m = re.match(r"^    ([A-Za-z0-9_.\-]+):\s*$", line)
        if m:
            current = m.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return blocks


def _declared(path: pathlib.Path) -> dict[str, dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (data.get("slurm") or {}).get("partitions") or {}


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.stem)
def test_prose_saying_PREEMPTIBLE_is_matched_by_the_field(path):
    declared = _declared(path)
    missing = []
    for part, body in _partition_blocks(path).items():
        for field, pattern in _POSITIVE.items():
            hits = [l for l in body if pattern.search(l) and not _NEGATED.search(l)]
            if not hits:
                continue
            if (declared.get(part) or {}).get(field) is not True:
                missing.append(
                    f"  {part}: prose says {field.upper()} but the field is "
                    f"{(declared.get(part) or {}).get(field)!r}\n"
                    f"      {hits[0].strip()}")
    assert not missing, (
        f"{path.name}: the fact is in the file twice and the copies disagree.\n"
        + "\n".join(missing)
        + "\n\n  The structured field is what `partition_warnings` reads, so a "
          "partition\n  documented as preemptible in a comment and unset in the "
          "field submits\n  SILENTLY — and silence from a warning that exists "
          "reads as an all-clear."
    )


def test_the_gate_is_not_vacuous():
    """A parametrised test over 51 files passes trivially if the extractor
    returns nothing. Pin that it actually finds blocks and flags."""
    found = {p.stem: _partition_blocks(p) for p in PROFILES}
    assert len(PROFILES) > 40, "profiles disappeared"
    assert sum(len(b) for b in found.values()) > 100, (
        "the partition-block extractor found almost nothing — it has stopped "
        "matching the file layout, so every assertion above is vacuous"
    )
    flagged = sum(
        1 for p in PROFILES
        for part, spec in _declared(p).items()
        if (spec or {}).get("preemptible") is True or (spec or {}).get("exclusive") is True
    )
    assert flagged >= 20, (
        f"only {flagged} partitions carry a recorded flag; #211 transcribed 22. "
        f"If they were removed, this gate is now guarding nothing."
    )


def test_unknown_stays_expressible():
    """The third state is the point. If every partition ended up with a bool,
    someone has written `false` where they meant "nobody checked" — and a
    warning that stays silent on a false is indistinguishable from one that
    stays silent on an unknown."""
    unknown = sum(
        1 for p in PROFILES
        for part, spec in _declared(p).items()
        if (spec or {}).get("preemptible") is None
    )
    assert unknown > 0, (
        "every partition now records a preemptible value. That is either "
        "genuine site-by-site research, or someone filled the field in to "
        "silence this file. If it is the former, delete this test and say so."
    )


# ── no profile may DEFAULT onto a partition it marks as costly ───────────────


def test_no_profile_defaults_to_a_partition_it_marks_preemptible():
    """The transcription in #211 made this question answerable, and the answer
    was three profiles.

    `default_partition` chooses the partition for an INTERACTIVE AGENT SESSION.
    Preemption there is not a trade — it kills a long-running stateful agent
    mid-task, with no checkpoint and nothing on screen to explain it.

    A default copied from a sibling profile can become stale when that
    sibling is corrected. Check every profile against its own recorded
    partition flags to detect this drift.

    Deliberately keyed on `is True` — a partition whose flag is unrecorded is
    NOT a violation, because we do not know it is preemptible and a gate that
    guessed would be worse than the silence it replaces.
    """
    offenders = []
    for path in PROFILES:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        slurm = data.get("slurm") or {}
        default = slurm.get("default_partition") or ""
        if not default:
            continue
        spec = (slurm.get("partitions") or {}).get(default) or {}
        if spec.get("preemptible") is True:
            offenders.append(f"  {path.name}: default_partition: {default}")
    assert not offenders, (
        "a bundled profile defaults an agent session onto a partition it "
        "itself marks PREEMPTIBLE:\n" + "\n".join(offenders)
        + "\n\n  Pick a non-preemptible partition as the default. The costly "
          "one stays\n  available via --partition or "
          "plugins.hpc-launcher.partition; what it must\n  not be is what "
          "someone gets for not choosing."
    )


def test_no_profile_defaults_to_a_partition_it_marks_exclusive():
    """The same rule for the other costly property. Nothing violates it today,
    which is the point of writing it now rather than after someone adds a
    profile whose default bills a whole node for a one-core session."""
    offenders = []
    for path in PROFILES:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        slurm = data.get("slurm") or {}
        default = slurm.get("default_partition") or ""
        if not default:
            continue
        spec = (slurm.get("partitions") or {}).get(default) or {}
        if spec.get("exclusive") is True:
            offenders.append(f"  {path.name}: default_partition: {default}")
    assert not offenders, (
        "a bundled profile defaults onto a WHOLE-NODE partition — a one-core "
        "agent session would be billed for every core on the node:\n"
        + "\n".join(offenders))


def test_the_default_partition_rule_is_checking_real_defaults():
    """Both assertions above pass trivially if no profile declares a default.
    Pin that a real number of them do."""
    have_default = 0
    for path in PROFILES:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if ((data.get("slurm") or {}).get("default_partition") or ""):
            have_default += 1
    assert have_default >= 30, (
        f"only {have_default} of {len(PROFILES)} profiles declare a "
        f"default_partition — the two rules above are guarding almost nothing"
    )
