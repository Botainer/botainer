"""Derive starter `job_profiles:` from a cluster profile.

THE GAP THIS CLOSES. botainer ships cluster profiles recording every partition
name, walltime ceiling, memory limit and GPU type for ~40 sites. It also ships
`examples/hpc-job-profiles.yaml`, whose starter block says:

    partition: "day"          # <- replace with your cluster's CPU partition
    account: "my_allocation"       # <- replace with your allocation

and `botainer init` contained no reference to `cluster`, `partition` or
`job_profiles` at all. So the product knew the answer and asked the user to go
find it — the reported symptom was a user asking for help writing the config
file for CPU and GPU jobs on their cluster, alongside a
`job-profiles`-vs-`job_profiles` typo that failed silently.

FOUR RULES make this trustworthy rather than merely convenient:

 1. Only ever emit a partition the profile LISTS. Selection is a filter over
    `profile.partitions`, so an invented name is impossible by construction
    rather than caught by a later validation pass.
 2. Never emit a time exceeding that partition's ceiling. Clamp, and say so.
    A generated config the scheduler rejects on first use is worse than none.
 3. Choose by ROLE, not by name. Matching the literal string "day" works on
    one cluster and nowhere else; we pick "a general CPU partition" from
    recorded properties.
 4. Never silently hand someone a PREEMPTIBLE or EXCLUSIVE partition. Those are
    typically the cheapest and quickest to start, so a naive "pick the best
    default" lands on them — and they respectively lose your work without
    warning or bill a whole node for one core. They are excluded from defaults
    and mentioned only with their cost stated.
"""
from __future__ import annotations

from botainer.state.cluster_profile import ClusterProfile, PartitionSpec

#: Anything longer is a poor default even where the partition allows it: a
#: first job should surface problems quickly, not sit in the queue for a week.
_SANE_DEFAULT_MINUTES = 60
_SANE_MAX_MINUTES = 240


def _comment(text: str, prefix: str = "# ") -> list[str]:
    """Every line of `text` as a comment line.

    NOT cosmetic. Profile fields are free text and several real ones contain
    NEWLINES — `verification.source` on the ALCF Polaris profile is a paragraph
    describing which URLs 403'd. Commenting only the first line let the rest
    land as live YAML, which broke parsing outright.

    It is also the one place a hostile profile could inject: a
    community-contributed `description` containing a newline followed by
    `job_profiles:` would otherwise write arbitrary config into a file the user
    is about to run jobs from. Commenting every line makes that impossible
    rather than merely unlikely, which is the difference between a property and
    a filter. Also strips CR so a CRLF profile cannot smuggle a line break past
    the split.
    """
    out: list[str] = []
    for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        out.append(f"{prefix}{line}".rstrip())
    return out


def _fmt_hms(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def _usable(p: PartitionSpec) -> bool:
    """Excludes what must never become someone's silent default (rule 4)."""
    return not p.preemptible and not p.exclusive


def pick_cpu_partition(profile: ClusterProfile) -> PartitionSpec | None:
    """A general-purpose CPU partition, chosen by properties (rule 3).

    Prefers the SHORTEST adequate one. A partition capped at 24 h and one
    capped at 7 days are equally fine for a 1-hour job, and the shorter queue
    almost always starts sooner — so "smallest ceiling that still fits" is a
    better default than "largest limits", which is the tempting wrong answer.
    """
    cands = [p for p in profile.partitions
             if _usable(p) and not p.gpu_types
             and (p.max_time_minutes or 0) >= _SANE_DEFAULT_MINUTES]
    if not cands:
        return None
    return min(cands, key=lambda p: (p.max_time_minutes or 1 << 30, p.name))


def pick_gpu_partition(profile: ClusterProfile) -> PartitionSpec | None:
    cands = [p for p in profile.partitions
             if _usable(p) and p.gpu_types
             and (p.max_time_minutes or 0) >= _SANE_DEFAULT_MINUTES]
    if not cands:
        return None
    return min(cands, key=lambda p: (p.max_time_minutes or 1 << 30, p.name))


def pick_debug_partition(profile: ClusterProfile) -> PartitionSpec | None:
    """A short-turnaround queue, if the site has one. Under two hours is the
    usual marker; these exist precisely so a broken job fails fast."""
    cands = [p for p in profile.partitions
             if _usable(p) and not p.gpu_types
             and p.max_time_minutes and p.max_time_minutes <= 120]
    if not cands:
        return None
    return max(cands, key=lambda p: (p.max_time_minutes or 0, p.name))


def _clamp(minutes: int, part: PartitionSpec) -> int:
    """Rule 2. Never emit a time the partition would reject."""
    ceiling = part.max_time_minutes
    return min(minutes, ceiling) if ceiling else minutes


def render_job_profiles(profile: ClusterProfile) -> str:
    """A `job_profiles:` YAML block valid for THIS cluster.

    Returns a comment-only block when nothing suitable exists, rather than
    inventing a partition — the correct outcome for a site whose profile records
    no usable partitions (or is not Slurm at all).
    """
    lines: list[str] = []
    lines.append("# ─────────────────────────────────────────────────────────")
    lines += _comment(f"Generated for: {profile.description or profile.name}")
    lines += _comment(f"Profile trust: {profile.verification_label()}")
    if profile.scheduler != "slurm":
        lines.append(f"# WARNING: this site runs {profile.scheduler.upper()},")
        lines.append("#   NOT Slurm. botainer v0.1 cannot submit jobs here.")
    lines.append("#")
    lines.append("# Partition names and time ceilings come from that profile.")
    lines.append("# If it was transcribed from documentation rather than probed,")
    lines.append("# verify it against this cluster before relying on it:")
    lines.append("#     sinfo -o '%P %l %m %c %G'   /   scontrol show partition")
    lines.append("#   (an automated check is planned, not built)")
    lines.append("# ─────────────────────────────────────────────────────────")
    lines.append("")

    cpu = pick_cpu_partition(profile)
    gpu = pick_gpu_partition(profile)
    debug = pick_debug_partition(profile)
    if not any((cpu, gpu, debug)):
        lines.append("# No usable partition found in this cluster profile, so")
        lines.append("# nothing is generated — inventing one would produce a")
        lines.append("# config the scheduler rejects. Read partitions off the")
        lines.append("# cluster with `sinfo` and add them to the profile by hand")
        lines.append("# (automated discovery is planned, not built).")
        return "\n".join(lines) + "\n"

    lines.append("job_profiles:")
    lines.append("")

    def emit(key: str, part: PartitionSpec, desc: str, minutes: int,
             gpus: int = 0) -> None:
        t = _clamp(minutes, part)
        mx = _clamp(_SANE_MAX_MINUTES, part)
        lines.append(f"  {key}:")
        lines.append(f'    description: "{desc}"')
        lines.append(f'    partition: "{part.name}"')
        # Rule 5 from the plan: we cannot know the account, but we can say
        # exactly how to find it instead of leaving a bare placeholder.
        lines.append('    account: ""       # REQUIRED on most clusters. Find yours:')
        lines.append("    #   sacctmgr -nP show assoc user=$USER format=account")
        lines.append(f'    time: "{_fmt_hms(t)}"')
        if mx > t:
            lines.append(f'    max_time: "{_fmt_hms(mx)}"'
                         f"   # agent may request up to this")
        if gpus:
            lines.append(f"    gpus: {gpus}")
            if part.gpu_types:
                lines.append(f"    # available here: {', '.join(part.gpu_types)}")
        if part.max_cpus:
            lines.append(f"    cpus: {min(4, part.max_cpus)}")
            lines.append(f"    max_cpus: {min(16, part.max_cpus)}"
                         f"   # partition allows {part.max_cpus}")
        if part.charge_factor and part.charge_factor != 1:
            verb = "COSTS" if part.charge_factor > 1 else "discounted to"
            lines.append(f"    # {verb} {part.charge_factor}x your allocation")
        if part.max_time_minutes:
            lines.append(f"    # partition ceiling: {part.max_time_minutes} min")
        lines.append("")

    if debug and (not cpu or debug.name != cpu.name):
        emit("quick", debug, "Short test job — fails fast", 15)
    if cpu:
        emit("cpu-small", cpu, "General CPU work", _SANE_DEFAULT_MINUTES)
    if gpu:
        emit("gpu", gpu, "Single-GPU work", _SANE_DEFAULT_MINUTES, gpus=1)

    skipped = [p for p in profile.partitions if not _usable(p)]
    if skipped:
        lines.append("# NOT offered as defaults, deliberately:")
        for p in skipped:
            why = []
            if p.preemptible:
                why.append("PREEMPTIBLE — the job can be killed at any moment")
            if p.exclusive:
                why.append("EXCLUSIVE — bills a whole node even for one core")
            if p.charge_factor and p.charge_factor > 1:
                why.append(f"charged {p.charge_factor}x")
            lines.append(f"#   {p.name}: {'; '.join(why)}")
        lines.append("# They are often cheaper or start sooner. Use them only")
        lines.append("# deliberately, and checkpoint on preemptible ones.")
    return "\n".join(lines) + "\n"
