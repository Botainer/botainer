"""Bundled cluster profiles must be distinguishable from each other.

The catalogue went from 5 profiles to dozens on, which turns a
latent ambiguity into a real one. Cluster names are NOT unique across
institutions — "Grace" is both Yale's and Texas A&M's, "Delta" and "Discovery"
are each used by several sites — so `cluster.name: grace` cannot identify a
profile on its own.

The filename does not save us: `list_bundled()` globs `*.yaml` and parses each
one, and nothing anywhere keys on the file's stem. Identity is carried entirely
by `cluster.name` (what a human selects) and `hostname_patterns` (what
autodetect matches). Both therefore have to be unambiguous, and these tests are
what make that true by construction rather than by everyone remembering.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from botainer.state.cluster_profile import ClusterProfile

PROFILE_DIR = Path(__file__).resolve().parents[2] / "cluster_profiles"


def _all():
    out = []
    for path in sorted(PROFILE_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        out.append((path, ClusterProfile.from_dict(raw)))
    return out


def test_every_profile_parses() -> None:
    """A profile that fails to parse is silently DROPPED by list_bundled()
    (it catches ValueError/KeyError and continues), so a broken file would
    just vanish from the catalogue instead of erroring."""
    profiles = _all()
    assert len(profiles) >= 5, f"only found {len(profiles)} profiles"


def test_cluster_names_are_unique() -> None:
    """Two profiles named `grace` are indistinguishable at the point a human
    picks one, and there is no tiebreak anywhere in the code."""
    seen = defaultdict(list)
    for path, prof in _all():
        seen[prof.name].append(path.name)
    dupes = {n: f for n, f in seen.items() if len(f) > 1}
    assert not dupes, (
        f"duplicate cluster.name values: {dupes}. Cluster names collide across "
        f"institutions (Grace: Yale and Texas A&M; Delta; Discovery), so "
        f"`name` must carry the operator too — e.g. `yale-grace`.")


def test_filenames_are_unique_per_cluster_name() -> None:
    """Filename and cluster.name should not drift apart — the file is how a
    contributor finds the profile, the name is how a user selects it."""
    for path, prof in _all():
        stem = path.stem.replace("_", "-").lower()
        name = prof.name.replace("_", "-").lower()
        assert name in stem or stem.endswith(name), (
            f"{path.name}: cluster.name {prof.name!r} is not recoverable from "
            f"the filename. Someone reading a PR diff for {path.name} should "
            f"not have to open it to learn which cluster it configures.")


def test_hostname_patterns_do_not_collide_across_profiles() -> None:
    """Two profiles matching the same hostname makes autodetect a coin flip.

    An overbroad pattern is the likely cause — e.g. a bare `login*`, which
    matches the login node of nearly every cluster on earth.
    """
    seen = defaultdict(list)
    for path, prof in _all():
        for pat in prof.hostname_patterns:
            seen[pat].append(path.name)
    dupes = {p: f for p, f in seen.items() if len(f) > 1}
    assert not dupes, f"identical hostname patterns in multiple profiles: {dupes}"


# Host prefixes that are CONVENTIONS rather than site names. A pattern built
# from one of these matches machines at unrelated institutions, so autodetect
# would silently apply the wrong partition limits, scratch paths and account
# rules. `nid` is the one that motivated this list: it is the standard Cray
# node prefix, so `nid*` matches compute nodes on every Cray EX in the world —
# including at least three other profiles in this directory.
_GENERIC_HOST_PREFIXES = frozenset({
    "login", "node", "nodes", "compute", "head", "master", "batch", "submit",
    "nid", "cn", "gn", "gpu", "cpu", "worker", "host", "server", "hpc",
    "cluster", "frontend", "access", "interactive", "devel", "test",
})


@pytest.mark.parametrize("path", sorted(PROFILE_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_hostname_patterns_are_specific_enough_to_identify_a_site(path) -> None:
    """A pattern built from a naming CONVENTION is worse than no pattern.

    The check is deliberately about convention-vs-site-name, not punctuation.
    An earlier version of this test required a dot or hyphen, which rejected
    `expanse*` (perfectly specific — no other cluster is called Expanse) while
    the thing it needed to catch, `nid*`, happened to be caught for the wrong
    reason. Matching on the actual failure mode is both more accurate and
    explains itself to whoever trips it.
    """
    prof = ClusterProfile.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
    for pat in prof.hostname_patterns:
        bare = pat.replace("*", "").strip("-.")
        # Floor of 3, not 4: real sites use short but entirely specific node
        # prefixes (`mn5`, `uan`, `jwb`, `lrd`, `ls6`, `fir`), and rejecting
        # those taught nothing while catching nothing. One- and two-character
        # prefixes stay banned because in practice they are conventions
        # (`cn`, `gn`, `n`), which is the hazard this test is actually about.
        assert len(bare) >= 3, f"{path.name}: pattern {pat!r} is too broad"
        # A domain qualifies ANY hostname: `login*.rc.fas.harvard.edu` is
        # entirely specific even though it starts with the most generic
        # possible prefix. Only an UNQUALIFIED generic prefix is dangerous.
        if "." in bare:
            continue
        head = re.split(r"[.\-]", bare, maxsplit=1)[0].rstrip("0123456789")
        assert head.lower() not in _GENERIC_HOST_PREFIXES, (
            f"{path.name}: pattern {pat!r} is an unqualified host-naming "
            f"convention rather than a site name — it would match machines at "
            f"other institutions and apply this profile's partition limits, "
            f"scratch paths and account rules to them. Add the site domain "
            f"(e.g. '{head}*.example.edu').")


@pytest.mark.parametrize("path", sorted(PROFILE_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_declared_default_partition_exists(path) -> None:
    """`hpc submit` with no -p uses this; if it is not a real partition the
    first submission on a fresh install fails for a reason nobody can see."""
    prof = ClusterProfile.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
    if not prof.slurm_default_partition or not prof.partitions:
        return
    names = {p.name for p in prof.partitions}
    assert prof.slurm_default_partition in names, (
        f"{path.name}: default_partition {prof.slurm_default_partition!r} is "
        f"not among {sorted(names)}")


@pytest.mark.parametrize("path", sorted(PROFILE_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_default_time_fits_inside_the_default_partition(path) -> None:
    """Defaults that contradict each other produce a job the scheduler
    refuses, on the very first submission, before the user has any model of
    what went wrong."""
    prof = ClusterProfile.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
    for part in prof.partitions:
        if part.name != prof.slurm_default_partition:
            continue
        if part.max_time_minutes:
            assert prof.slurm_default_time_minutes <= part.max_time_minutes, (
                f"{path.name}: default_time_minutes "
                f"{prof.slurm_default_time_minutes} exceeds {part.name}'s max "
                f"{part.max_time_minutes}")


# --------------------------------------------------------------------------
# Aliases. Added after renaming profiles to unique identifiers
# silently broke two lookups — caught in review, not by any existing test.
# --------------------------------------------------------------------------

def test_renamed_profiles_still_answer_to_their_site_name() -> None:
    """`cluster.name` serves the CATALOGUE; `aliases` serve the CLUSTER.

    Renaming `grace` to `us-yale-grace` (needed, because Texas A&M also runs a
    Grace) broke both `autodetect()`'s env fallback and `hpc setup --profile
    grace`, because both matched on `name`. Neither `$SLURM_CLUSTER_NAME` nor a
    user is ever going to say `us-yale-grace`.
    """
    for path, prof in _all():
        if "-" not in prof.name or path.stem in ("example", "generic-slurm"):
            continue
        # The site's own name cannot be derived mechanically — splitting on
        # the last hyphen yields "ng" for `de-lrz-supermuc-ng` and "alps" for
        # `ch-cscs-alps`, only one of which is right. So require that an alias
        # is DECLARED, and that it is not just the catalogue name again (which
        # would satisfy the letter of this test while restoring the bug).
        assert prof.aliases, (
            f"{path.name}: qualified name {prof.name!r} with no aliases. "
            f"$SLURM_CLUSTER_NAME reports the SITE's name, never ours, so "
            f"autodetect on a compute node would silently miss this profile. "
            f"Add `cluster.aliases: [\"<what the site calls itself>\"]`.")
        assert any(a.lower() != prof.name.lower() for a in prof.aliases), (
            f"{path.name}: every alias just repeats the catalogue name")


def test_aliases_do_not_collide_across_profiles() -> None:
    """Two profiles answering to `grace` makes the lookup a coin flip — which
    is the exact ambiguity the rename existed to remove. Aliases restore
    convenience; they must not reintroduce the collision."""
    seen = defaultdict(list)
    for path, prof in _all():
        for alias in prof.aliases:
            seen[alias.lower()].append(path.name)
    dupes = {a: f for a, f in seen.items() if len(f) > 1}
    assert not dupes, (
        f"aliases claimed by more than one profile: {dupes}. Drop the alias "
        f"from all but the site that genuinely reports it.")
