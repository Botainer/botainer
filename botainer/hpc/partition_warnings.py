"""What a partition costs you, said at SUBMIT time (#109).

`PartitionSpec` has carried `preemptible` and `exclusive` since ~40 real sites
were transcribed, and its own comment says why they matter:

    "These are not cosmetic labels. They are the two facts that make an
     otherwise sensible partition choice expensive or destructive, and in both
     cases the trap is baited: the affected partitions are the CHEAPEST and
     FASTEST TO START, so they are precisely what an optimising agent (or a
     user reading a queue table) will pick."

And then only the job-profile GENERATOR read them. Nothing consulted either
flag when a job was actually submitted, so the trap stayed baited and unlabelled
at the exact moment it springs. Verified before writing this: `preemptible`
appears nowhere in `cli/hpc.py` or `hpc/dispatcher.py`.

STATE THE CONSEQUENCE, NOT THE FLAG. "partition is preemptible" is a fact the
reader must already understand to act on. "this job can be killed and requeued
at any moment — checkpoint, or expect to lose the work" is the same fact in a
form that changes what they do. Same for `exclusive`: the number that matters
is not the boolean, it is that a 1-core job is billed for the whole node.
"""
from __future__ import annotations


def partition_warnings(partition: str, profile) -> list[str]:
    """Lines to show before a job goes to the scheduler. Empty when there is
    nothing to say — which is the common case, and a warning that fires for
    ordinary partitions would train people to skip the ones that matter.

    `profile` is a ClusterProfile or None; None (no profile, or an unknown
    partition) yields no lines. That is deliberate rather than a guess: we do
    not know this partition is safe, we know we have nothing to say about it,
    and inventing a warning from the name would be exactly the "guessing by
    common names" mistake this project already refused elsewhere.

    WHAT REACHES THE TERMINAL, and why no filter is needed here. These lines
    go to a human's stderr, so an escape sequence in `partition` would be a
    terminal-injection sink. It cannot arrive: a line is only produced when
    `partition` EQUALS the name of a partition in the cluster profile, and the
    profile is host-operator config (~/.botainer/cluster.yaml or a bundled
    cluster_profiles/ YAML) — never project config the agent or a cloned repo
    can write. The string printed is therefore always one the operator already
    put in their own file. Structural, not a charset check; see
    docs/CAPABILITY-SURFACE.md on which config sources are trusted.
    """
    if not partition or profile is None:
        return []
    spec = None
    for p in getattr(profile, "partitions", ()) or ():
        if getattr(p, "name", None) == partition:
            spec = p
            break
    if spec is None:
        return []

    out: list[str] = []
    # WHERE THE CLAIM COMES FROM, said in the warning itself.
    #
    # Bundled partition facts may be transcribed from public documentation
    # rather than tested against the scheduler. Preserve that distinction in
    # the warning and direct users to local verification.
    #
    # The warning is still worth printing. What it must not do is print
    # "this job can be killed at any moment" in the register of a measured
    # fact when the source is a page somebody read. A FACT and a POINTER, per
    # the tips rule — never a verdict, and never bare confidence either.
    prov = ""
    try:
        prov = profile.provenance_clause()
    except Exception:                                            # noqa: BLE001
        prov = ""

    if getattr(spec, "preemptible", False):
        out.append(
            f"partition {partition!r} is PREEMPTIBLE: this job can be killed "
            f"and requeued at any moment, with no warning and no grace period."
        )
        out.append(
            "  Checkpoint your work, or expect to lose everything since the "
            "last write. A long job here will probably not finish in one run."
        )
    if getattr(spec, "exclusive", False):
        out.append(
            f"partition {partition!r} allocates a WHOLE NODE: you are charged "
            f"for every core on it regardless of how few you asked for."
        )
        out.append(
            "  A 1-core job costs the same as a full-node job. If your site "
            "has a '-shared' variant of this partition, that is the one you "
            "want for small work."
        )
    if out and prov:
        # Only when there IS a warning, and only when there is something to
        # caveat — a profile verified on hardware gets no clause, because
        # adding one there would train people to discount all of them.
        out.append(f"  ({prov}. `sinfo -o \'%P %l %m\'` on a login node is "
                   f"the check, if it matters to you.)")
    return out
