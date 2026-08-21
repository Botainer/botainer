"""What the two surfaces say about /scratch must match where /scratch IS.

User directive, then a correction from the user  that
inverted half of it.

ROUND 1. Two surfaces were each wrong in their own way:
  * AGENT_HINTS said "the user may delete this at any time" — the wrong THREAT
    if a filesystem purges on a schedule; "the user may" invites "so as long as
    I don't, it stays".
  * The session-start summary said NOTHING, so nobody was told at the one
    moment they are reading.

ROUND 2 — the fix was itself wrong, and these tests are why it
survived. The user asked: *"isn't 'our' scratch stored on cluster scratch?"*
It is not:

    /scratch's host side  = <state_root>/state/<uuid>/scratch
    state_root            = $MY_BOTAINER, default ~/.botainer
    scratch.template      = the cluster's purged filesystem — DISPLAY ONLY,
                            never a bind source

So "AUTO-DELETED after ~60 days by the cluster" was false by default on a
cluster ($HOME is not purged) and false on a laptop (nothing deletes it ever).
It was true only when MY_BOTAINER points into cluster scratch — the one
configuration the storage design refuses, because it purges credentials too.

An inverted warning is worse than a missing one: a user who believes the
directory self-cleans never goes looking for the GBs it is accumulating.

THE TESTING LESSON, which is why this docstring is long: round 1's test
asserted `days == 60` after monkeypatching a profile with
`scratch_cleanup_days = 60`. That is a test of PLUMBING — "does the number
reach the string" — and it passed for a sentence that was false in the real
layout, because the fixture never asked WHERE the directory was. Presence of
the number is not truth of the claim. Every test below pins the claim to the
bind path, not to the profile.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer.inspect import agent_hints, capability_summary


@pytest.fixture
def minimal_spec(tmp_path):
    """A really composed spec — the /scratch bind must be the real one."""
    import uuid as _uuid

    from botainer.core.composition import compose_session

    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "project-id").write_text(str(_uuid.uuid4()))
    (proj / ".botainer" / "config.yaml").write_text(
        "version: config-v1\nagent: claude\nruntime: docker\n"
        "image: ubuntu:24.04\nnetwork:\n  mode: internet\n"
        "plugins_enabled: [agent-claude]\n")
    return compose_session(proj, runtime_choice="docker", identity_accept=True)


def _profile(*, days=60, template="/fsA/sitename/scratch/${USER}/${SLURM_JOBID}"):
    class _Prof:
        scratch_cleanup_days = days
        scratch_template = template
    return _Prof()


# --------------------------------------------------------------------------
# The claim must follow the PATH, not the profile.
# --------------------------------------------------------------------------

def test_no_purge_claim_when_scratch_is_not_on_the_purged_filesystem(
        minimal_spec, monkeypatch) -> None:
    """THE REGRESSION TEST for the bug.

    A cluster profile is active and declares a 60-day purge, but our scratch
    lives under the state root (tmp_path here, ~/.botainer in reality) — which
    that purge does not touch. Claiming otherwise is the bug.
    """
    monkeypatch.setattr(
        "botainer.state.cluster_profile.active_profile", lambda: _profile())

    host, days = agent_hints.scratch_purge_note(minimal_spec)
    assert host, "the bind must still be reported"
    assert days is None, (
        f"claimed a {days}-day cluster purge for {host}, which is not under "
        f"the cluster's scratch filesystem")

    out = capability_summary.render_multiline(minimal_spec)
    assert "AUTO-DELETED" not in out, out[-600:]
    assert "60 days" not in out

    section = agent_hints.render(minimal_spec)
    section = section[section.index("## Scratch space"):]
    assert "AUTOMATICALLY DELETED" not in section, section[:400]


def test_purge_claim_when_scratch_really_is_on_the_purged_filesystem(
        minimal_spec, monkeypatch) -> None:
    """The true case: MY_BOTAINER points into cluster scratch, so the purge
    genuinely applies and both surfaces must say so with the real number."""
    host, _ = agent_hints.scratch_purge_note(minimal_spec)
    # Declare the purged filesystem to be the one our scratch is actually on.
    monkeypatch.setattr(
        "botainer.state.cluster_profile.active_profile",
        lambda: _profile(template=host + "/${SLURM_JOBID}"))

    _, days = agent_hints.scratch_purge_note(minimal_spec)
    assert days == 60

    section = agent_hints.render(minimal_spec)
    section = section[section.index("## Scratch space"):]
    assert "AUTOMATICALLY DELETED" in section, section[:400]
    # Normalised: these sentences are line-wrapped, so a raw substring search
    # depends on where the wrap happens to fall.
    flat = " ".join(section.split())
    assert "nothing warns you first" in flat
    assert "~60 days" in capability_summary.render_multiline(minimal_spec)


def test_a_sibling_directory_does_not_count_as_purged(
        minimal_spec, monkeypatch) -> None:
    """Prefix matching must respect directory boundaries: /scratch/foo must not
    match /scratch/foobar. A naive string-prefix check passes this wrongly."""
    host, _ = agent_hints.scratch_purge_note(minimal_spec)
    monkeypatch.setattr(
        "botainer.state.cluster_profile.active_profile",
        lambda: _profile(template=host + "-other/${USER}"))
    _, days = agent_hints.scratch_purge_note(minimal_spec)
    assert days is None, "matched a sibling directory as if it were ours"


# --------------------------------------------------------------------------
# Both branches must still be USEFUL — a correct sentence that says nothing
# actionable is not an improvement on a wrong one.
# --------------------------------------------------------------------------

def test_the_not_purged_branch_says_it_grows(minimal_spec) -> None:
    """The truthful message must carry the actual consequence: it accumulates."""
    out = capability_summary.render_multiline(minimal_spec)
    assert "nothing deletes it" in out.lower(), out[-600:]
    assert "where" in out, "no pointer to the command that shows its size"
    host, _ = agent_hints.scratch_purge_note(minimal_spec)
    assert host in out, "host path not named, so the user cannot find it"


def test_agent_is_always_told_where_things_that_matter_go(minimal_spec) -> None:
    """A warning with no alternative is not actionable — true in both branches."""
    text = agent_hints.render(minimal_spec)
    section = text[text.index("## Scratch space"):]
    assert "/workspace" in section, "no alternative destination offered"


def test_old_wrong_framing_is_gone(minimal_spec) -> None:
    text = agent_hints.render(minimal_spec)
    section = text[text.index("## Scratch space"):]
    assert "user may delete this at any time without warning" not in section


# --------------------------------------------------------------------------
# Prefix derivation.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("template,expected", [
    ("/fsA/sitename/scratch/${USER}/${SLURM_JOBID}", "/fsA/sitename/scratch/"),
    ("/scratch/${USER}/${SLURM_JOBID}", "/scratch/"),
    ("/data/{user}/scratch", "/data/"),
    ("/x/$USER/y", "/x/"),
    ("", ""),
    ("${SCRATCH}", ""),      # no literal prefix at all -> unknown, not "/"
])
def test_purge_prefix_derivation(template, expected) -> None:
    """Only the literal head of the template is a comparable path.

    Site paths are deliberately synthetic here — the logic is site-agnostic and
    the package must not learn any institution's filesystem layout. Real shipped
    templates are covered by the data-driven test below, which stays correct as
    profiles are added.
    """
    class _P:
        scratch_template = template
    assert agent_hints.cluster_purge_prefix(_P()) == expected


def test_every_shipped_profile_template_yields_a_usable_prefix() -> None:
    """Data-driven over cluster_profiles/*.yaml, so new profiles are covered
    automatically rather than needing a row added here.

    A template that reduces to "" or "/" would silently disable the purge claim
    (or, worse for a "/", match everything) — either way the profile's declared
    cleanup interval would be wrong at the point of use.
    """
    import yaml

    from botainer.state import cluster_profile as cp

    root = Path(__file__).resolve().parents[2] / "cluster_profiles"
    checked = 0
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tmpl = ((raw.get("scratch") or {}).get("template") or "")
        days = (raw.get("scratch") or {}).get("auto_cleanup_days")
        if not tmpl or not days:
            continue

        class _P:
            scratch_template = tmpl
        prefix = agent_hints.cluster_purge_prefix(_P())
        assert prefix not in ("", "/"), (
            f"{path.name}: template {tmpl!r} gives prefix {prefix!r}; it "
            f"declares a {days}-day purge that could never be attributed")
        assert prefix.startswith("/") and prefix.endswith("/"), (
            f"{path.name}: prefix {prefix!r} is not an absolute directory")
        assert "$" not in prefix and "{" not in prefix, (
            f"{path.name}: prefix {prefix!r} still contains a placeholder")
        checked += 1
    assert checked >= 3, f"only {checked} profiles declared a purge — glob broken?"
    # cluster_profile is imported to assert the module the profiles feed exists;
    # the prefix logic itself is deliberately independent of profile parsing.
    assert hasattr(cp, "ClusterProfile")


def test_a_profile_without_a_template_makes_no_claim(
        minimal_spec, monkeypatch) -> None:
    """Missing template means UNKNOWN. Unknown must not become 'purged'."""
    monkeypatch.setattr(
        "botainer.state.cluster_profile.active_profile",
        lambda: _profile(template=""))
    _, days = agent_hints.scratch_purge_note(minimal_spec)
    assert days is None


def test_a_broken_profile_makes_no_claim_and_still_renders(
        minimal_spec, monkeypatch) -> None:
    """Fail toward NOT asserting a purge. The section must still appear."""
    def _boom():
        raise RuntimeError("profile unreadable")
    monkeypatch.setattr("botainer.state.cluster_profile.active_profile", _boom)

    host, days = agent_hints.scratch_purge_note(minimal_spec)
    assert days is None
    out = capability_summary.render_multiline(minimal_spec)
    assert host in out and "AUTO-DELETED" not in out
