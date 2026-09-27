"""#215: two things that can redeem one refresh token, and nothing said so.

These tests model providers that invalidate an older refresh token on reuse.
Broker refreshes take a host-side lock; container-side refreshes take no such
lock. The detector reports competing holders; these tests do not establish
actual provider behavior or refresh-token lifetimes.

These tests pin the RULE, not the plumbing: the walk over live sessions is I/O
and is exercised through holder_for_plugins with the records it would find.
"""
from __future__ import annotations

import pytest

from botainer.core.credential_holders import (
    CONTAINER_PRIVATE,
    CONTAINER_UNSERIALIZED,
    HOST_SERIALIZED,
    UNKNOWN,
    Holder,
    collisions,
    holder_for_plugins,
)

# What the installed manifests declare. Passed in explicitly so these tests do
# not depend on which plugins happen to be installed where they run.
INDEX = {
    "agent-claude-broker": ("anthropic", "broker"),
    "agent-claude-shared": ("anthropic", "shared"),
    "agent-claude": ("anthropic", "isolated"),
    "agent-codex-broker": ("openai", "broker"),
    "agent-codex-shared": ("openai", "shared"),
    "agent-codex": ("openai", "isolated"),
}


def _holder(plugins, *, sid="s1", handle=None) -> Holder | None:
    return holder_for_plugins(
        plugins, session_id=sid, project_uuid=f"uuid-{sid}",
        project_label=f"proj-{sid}", broker_handle=handle, index=INDEX)


# ───────────────────────── what each mode is ─────────────────────────

@pytest.mark.parametrize("plugin,mode,kind", [
    ("agent-claude-broker", "broker", HOST_SERIALIZED),
    ("agent-claude-shared", "shared", CONTAINER_UNSERIALIZED),
    ("agent-claude", "isolated", CONTAINER_PRIVATE),
    ("agent-codex-broker", "broker", HOST_SERIALIZED),
    ("agent-codex-shared", "shared", CONTAINER_UNSERIALIZED),
    ("agent-codex", "isolated", CONTAINER_PRIVATE),
])
def test_each_mode_reports_how_it_redeems_the_token(plugin, mode, kind) -> None:
    h = _holder([plugin])
    assert h is not None and h.mode == mode and h.refresh_kind == kind


def test_a_session_with_no_auth_plugin_is_not_a_holder() -> None:
    """`botainer shell`, or a project that never logged in. Genuinely not a
    holder — distinct from "we could not tell", which is UNKNOWN below."""
    assert _holder(["git", "nudge"]) is None
    assert _holder([]) is None


# ───────────────────────── the rule ─────────────────────────

def test_shared_alongside_broker_collides() -> None:
    """THE CASE IN THE TITLE, and it must fire in BOTH directions — the defect
    was that neither mode knew about the other."""
    shared = _holder(["agent-claude-shared"], sid="a")
    broker = _holder(["agent-claude-broker"], sid="b")
    hits, unknown = collisions(shared, [broker])
    assert [h.session_id for h in hits] == ["b"], "shared did not see the broker"
    hits, unknown = collisions(broker, [shared])
    assert [h.session_id for h in hits] == ["a"], "broker did not see the shared"
    assert not unknown


def test_two_shared_sessions_collide() -> None:
    """The case that actually bit: several projects in shared mode at once.
    They read different PATHS — each project keeps a working copy — and still
    collide, because both copies descend from one authorization."""
    a = _holder(["agent-claude-shared"], sid="a")
    b = _holder(["agent-claude-shared"], sid="b")
    hits, _ = collisions(a, [b])
    assert [h.session_id for h in hits] == ["b"]


def test_two_brokers_do_not_collide() -> None:
    """Both refresh host-side through botainer/broker/refresh_lock, which is
    what that lock is FOR. Warning here would be crying wolf on the one
    arrangement that is designed to work concurrently."""
    a = _holder(["agent-claude-broker"], sid="a")
    b = _holder(["agent-claude-broker"], sid="b")
    hits, unknown = collisions(a, [b])
    assert hits == [] and unknown == []


@pytest.mark.parametrize("other", [
    "agent-claude-broker", "agent-claude-shared", "agent-claude"])
def test_isolated_never_collides_with_anything(other) -> None:
    """Its credential is a per-project copy from its own login, so nothing it
    does can revoke anyone else's."""
    iso = _holder(["agent-claude"], sid="a")
    hits, _ = collisions(iso, [_holder([other], sid="b")])
    assert hits == []
    hits, _ = collisions(_holder([other], sid="b"), [iso])
    assert hits == []


def test_different_families_never_collide() -> None:
    """An OpenAI token and an Anthropic token are unrelated secrets. Warning
    across families would make the warning worthless."""
    claude = _holder(["agent-claude-shared"], sid="a")
    codex = _holder(["agent-codex-shared"], sid="b")
    assert collisions(claude, [codex])[0] == []
    assert collisions(codex, [claude])[0] == []


def test_a_broker_on_an_ISOLATED_store_does_not_collide_with_shared() -> None:
    """credential_scope is a real setting, and a broker pointed at a project's
    own store is not touching the host-wide lineage."""
    broker = _holder(["agent-claude-broker"], sid="b",
                     handle={"credential_scope": "isolated"})
    shared = _holder(["agent-claude-shared"], sid="a")
    assert broker.scope == "isolated"
    assert collisions(broker, [shared])[0] == []
    assert collisions(shared, [broker])[0] == []


# ───────────────────── the third state ─────────────────────

def test_a_record_without_plugins_enabled_is_UNKNOWN_not_safe() -> None:
    """An older botainer wrote records without plugins_enabled. Reporting "no
    conflict" because the evidence was unreadable is the null-anchor defect:
    an absent answer read as a reassuring one."""
    h = _holder(None)
    assert h.is_unknown and "plugins_enabled" in h.why_unknown

    shared = _holder(["agent-claude-shared"], sid="a")
    hits, unknown = collisions(shared, [h])
    assert hits == [], "an unreadable record must not be reported as a collision"
    assert [u.session_id for u in unknown] == ["s1"], (
        "an unreadable record must not vanish either — it is UNKNOWN")


def test_two_auth_plugins_at_once_is_UNKNOWN() -> None:
    """compose_session refuses this, so it should not occur — but a record on
    disk is not something this module gets to assume things about."""
    h = _holder(["agent-claude-shared", "agent-claude-broker"])
    assert h.is_unknown
    assert "more than one" in h.why_unknown


def test_an_unknown_candidate_treats_every_other_holder_as_unjudged() -> None:
    """If we cannot classify OURSELVES, we cannot claim anyone is safe."""
    me = _holder(None)
    others = [_holder(["agent-claude-shared"], sid="a"),
              _holder(["agent-codex"], sid="b")]
    hits, unknown = collisions(me, others)
    assert hits == []
    assert len(unknown) == 2, "silently reported OK against an unknown self"


def test_a_mode_with_no_known_refresh_kind_is_UNKNOWN() -> None:
    """`proxy` was removed in #59. A live record from before that cannot be
    reasoned about — so it reads UNKNOWN rather than being dropped, which
    would be indistinguishable from "no other holders"."""
    h = holder_for_plugins(
        ["agent-claude-proxy"], session_id="p", project_uuid="u",
        project_label="l", index={"agent-claude-proxy": ("anthropic", "proxy")})
    assert h.is_unknown and "proxy" in h.why_unknown


# ── live_holders: the walk over real state on disk ────────────────────────
#
# Added because a manual observation caught what reading did not. The first
# fixture created project dirs and session records and got ZERO holders back:
# `list_projects()` keys on a per-project `meta.json` that a real install
# writes at identity resolution, and the fixture had never written one. The
# code was right; the fixture made the path run while testing none of it —
# which is indistinguishable from a detector that cannot fire.

def _realistic_state_root(tmp_path, monkeypatch, sessions):
    """A state root shaped like one botainer actually produces.

    `sessions` is [(uuid, name, plugin, session_id, runtime_handle|None)].
    Every project gets the meta.json a real install has; leaving it out is what
    made the first attempt silently empty.

    monkeypatch.setenv, NOT os.environ. A bare assignment here would leave
    MY_BOTAINER pointing at a deleted tmp_path for every test that runs after
    it in the same process — a failure that lands somewhere else and looks
    unrelated.
    """
    import json
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path))
    from botainer.core.spec import SessionSpec
    from botainer.state import dir as state_dir
    from botainer.state import session_record as sr

    paths = state_dir.ensure_user_state_dir()
    for uuid, name, plugin, sid, handle in sessions:
        p = state_dir.ensure_project_dirs(paths, uuid)
        proj = tmp_path / name
        proj.mkdir(parents=True, exist_ok=True)
        (p.base / "meta.json").write_text(
            json.dumps({"display_name": name, "path_history": [str(proj)]}),
            encoding="utf-8")
        spec = SessionSpec(session_id=sid, project_uuid=uuid,
                           project_root=str(proj), state_dir=str(p.base),
                           runtime="mock", image="img",
                           plugins_enabled=[plugin] if plugin else [])
        d = p.sessions_dir / sid
        d.mkdir(parents=True, exist_ok=True)
        rec = sr.from_spec(spec)
        if handle is not None:
            rec.extra_runtime_handle["broker"] = handle
        sr.write(d, rec)
    return paths


def test_live_holders_finds_every_running_session(tmp_path, monkeypatch) -> None:
    from botainer.core import credential_holders as ch
    from botainer.state import liveness
    monkeypatch.setattr(liveness, "is_session_alive", lambda rec: True)
    paths = _realistic_state_root(tmp_path, monkeypatch, [
        ("uuid-aaaa", "proj-a", "agent-claude-shared", "sesAAAA0001", None),
        ("uuid-bbbb", "proj-b", "agent-claude-broker", "sesBBBB0001",
         {"credential_scope": "shared"}),
        ("uuid-cccc", "proj-c", "agent-codex", "sesCCCC0001", None),
        ("uuid-dddd", "proj-d", "", "sesDDDD0001", None),   # no auth plugin
    ])
    holders = {h.session_id: h for h in ch.live_holders(paths)}
    assert set(holders) == {"sesAAAA0001", "sesBBBB0001", "sesCCCC0001"}, (
        "a session with no auth plugin should not be a holder, and the other "
        "three should all be found")
    assert holders["sesAAAA0001"].mode == "shared"
    assert holders["sesBBBB0001"].mode == "broker"
    assert holders["sesBBBB0001"].scope == "shared"   # read from the handle
    assert holders["sesCCCC0001"].family == "openai"


def test_live_holders_skips_the_excluded_session(tmp_path, monkeypatch) -> None:
    """The launching session must not be reported as colliding with itself."""
    from botainer.core import credential_holders as ch
    from botainer.state import liveness
    monkeypatch.setattr(liveness, "is_session_alive", lambda rec: True)
    paths = _realistic_state_root(tmp_path, monkeypatch, [
        ("uuid-aaaa", "proj-a", "agent-claude-shared", "sesAAAA0001", None),
    ])
    assert ch.live_holders(paths, exclude_session_id="sesAAAA0001") == []


def test_a_dead_session_is_not_a_holder(tmp_path, monkeypatch) -> None:
    """A record left behind by a crashed session must not warn forever. This
    is the half that turns a useful warning into noise nobody reads."""
    from botainer.core import credential_holders as ch
    from botainer.state import liveness
    monkeypatch.setattr(liveness, "is_session_alive", lambda rec: False)
    paths = _realistic_state_root(tmp_path, monkeypatch, [
        ("uuid-aaaa", "proj-a", "agent-claude-shared", "sesAAAA0001", None),
    ])
    assert ch.live_holders(paths) == []


def test_a_broker_handle_naming_an_isolated_store_is_honoured(
        tmp_path, monkeypatch) -> None:
    """Read from the RECORD, not assumed. A broker on a per-project store is
    not touching the host-wide lineage, and warning about it would be noise."""
    from botainer.core import credential_holders as ch
    from botainer.state import liveness
    monkeypatch.setattr(liveness, "is_session_alive", lambda rec: True)
    paths = _realistic_state_root(tmp_path, monkeypatch, [
        ("uuid-bbbb", "proj-b", "agent-claude-broker", "sesBBBB0001",
         {"credential_scope": "isolated"}),
    ])
    (holder,) = ch.live_holders(paths)
    assert holder.scope == "isolated"
