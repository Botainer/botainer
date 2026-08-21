"""Tests for capability_summary rendering (Phase 1)."""
from __future__ import annotations

import json

from botainer.core.spec import (
    Bind,
    BindMode,
    MountPlan,
    NetworkMode,
    NetworkSpec,
    Provenance,
    SessionSpec,
)
from botainer.inspect import capability_summary


def _make_spec(runtime: str = "docker") -> SessionSpec:
    plan = MountPlan(
        binds=(
            Bind(
                source="/host/proj",
                target="/workspace",
                mode=BindMode.RW,
                provenance=Provenance.CORE,
            ),
            Bind(
                source="/host/anchor",
                target="/workspace/.botainer",
                mode=BindMode.NULL_BIND,
                provenance=Provenance.CORE,
            ),
            Bind(
                source="/host/aas.txt",
                target="/workspace/.botainer/AGENT_ACCESS.txt",
                mode=BindMode.RO,
                provenance=Provenance.CORE,
                nested_under="/workspace/.botainer",
            ),
            Bind(
                source="/host/data",
                target="/data",
                mode=BindMode.RO,
                provenance=Provenance.USER,
            ),
        )
    )
    return SessionSpec(
        session_id="abc123def456",
        project_uuid="11111111-1111-1111-1111-111111111111",
        project_root="/host/proj",
        state_dir="/state",
        runtime=runtime,
        image="botainer/agent-claude:0.1@sha256:" + "a" * 64,
        mount_plan=plan,
        network=NetworkSpec(mode=NetworkMode.NONE),
        plugins_enabled=("agent-claude", "git"),
    )


def test_render_one_line_contains_key_fields() -> None:
    s = capability_summary.render_one_line(_make_spec())
    assert "Network: none" in s
    assert "/workspace" in s
    assert "Auth:" in s
    assert "Plugins: agent-claude, git" in s


def test_render_one_line_truncates_many_mounts() -> None:
    s = capability_summary.render_one_line(_make_spec())
    # 4 binds; renders first 3 then "+1 more"
    assert "+1 more" in s


def test_render_multiline_includes_full_picture() -> None:
    s = capability_summary.render_multiline(_make_spec())
    assert "Session:" in s
    assert "Image:" in s
    assert "Network:" in s
    assert "Mounts:" in s
    assert "Plugins:" in s


def test_render_json_is_valid_json() -> None:
    s = capability_summary.render_json(_make_spec())
    parsed = json.loads(s)
    assert parsed["session_id"] == "abc123def456"
    assert parsed["network_mode"] == "none"
    assert parsed["plugins_enabled"] == ["agent-claude", "git"]
    assert len(parsed["mounts"]) == 4


def test_render_multiline_nudge_warning() -> None:
    """When the nudge plugin is enabled, the multiline summary surfaces it as
    a privilege grant the user should be aware of."""
    from botainer.core.spec import SessionSpec
    spec = _make_spec()
    # Re-build with nudge in plugins_enabled and an entrypoint_wrap.
    spec = SessionSpec(
        **{**spec.model_dump(),
           "plugins_enabled": ("agent-claude", "git", "nudge"),
           # §A19: the entrypoint_wrap is no longer nudge's responsibility;
           # an inner-layer wrap (e.g. agent-claude prompt injection) is
           # representative of what survives the cut.
           "entrypoint_wraps": (("/usr/local/bin/agent-claude-entrypoint",),),
           "mount_plan": spec.mount_plan,
           "network": spec.network,
           "resources": spec.resources,
           "kernel_caps": spec.kernel_caps,
           "env": spec.env,
        }
    )
    out = capability_summary.render_multiline(spec)
    assert "nudge" in out
    # §A19: nudge uses screen -X stuff on the host, not in-container tmux.
    assert "screen -X stuff" in out
    assert "shell access to this host" in out
    assert "Entrypoint wraps:" in out
    assert "/usr/local/bin/agent-claude-entrypoint" in out


def test_render_json_includes_nudge_flag() -> None:
    """JSON output exposes nudge_enabled + entrypoint_wraps for tooling."""
    from botainer.core.spec import SessionSpec
    spec = _make_spec()
    spec = SessionSpec(
        **{**spec.model_dump(),
           "plugins_enabled": ("agent-claude", "git", "nudge"),
           "entrypoint_wraps": (("/usr/local/bin/agent-claude-entrypoint",),),
           "mount_plan": spec.mount_plan,
           "network": spec.network,
           "resources": spec.resources,
           "kernel_caps": spec.kernel_caps,
           "env": spec.env,
        }
    )
    parsed = json.loads(capability_summary.render_json(spec))
    assert parsed["nudge_enabled"] is True
    assert parsed["entrypoint_wraps"] == [["/usr/local/bin/agent-claude-entrypoint"]]


# Implementation-review HIGH 3: --quiet must not bypass the
# first-launch confirmation gate. The gate is the user's chance to see
# what capabilities the agent is being granted; a silent bypass would
# let scripts authorise sessions without ever showing the grant.

def _gate_confirm() -> capability_summary.SummaryGate:
    return capability_summary.SummaryGate(
        confirm=True, reason="first launch of this project", last_shown_image=None
    )


def _gate_no_confirm() -> capability_summary.SummaryGate:
    return capability_summary.SummaryGate(
        confirm=False, reason="", last_shown_image="botainer/x:0.1@sha256:" + "a" * 64,
    )


def test_quiet_without_yes_refuses_first_launch(monkeypatch) -> None:
    """--quiet + first launch must refuse (no silent auth)."""
    spec = _make_spec()
    monkeypatch.setattr(capability_summary, "determine_gate", lambda s: _gate_confirm())
    proceed = capability_summary.print_and_maybe_confirm(
        spec, quiet=True, auto_yes=False
    )
    assert proceed is False


def test_quiet_with_yes_proceeds_first_launch(monkeypatch) -> None:
    """--quiet + --yes is the documented non-interactive auth path."""
    spec = _make_spec()
    monkeypatch.setattr(capability_summary, "determine_gate", lambda s: _gate_confirm())
    monkeypatch.setattr(capability_summary, "record_shown", lambda s: None)
    proceed = capability_summary.print_and_maybe_confirm(
        spec, quiet=True, auto_yes=True
    )
    assert proceed is True


def test_quiet_on_subsequent_launch_proceeds_silently(monkeypatch, capsys) -> None:
    """--quiet on a subsequent launch (no gate): no output, proceeds."""
    spec = _make_spec()
    monkeypatch.setattr(capability_summary, "determine_gate", lambda s: _gate_no_confirm())
    proceed = capability_summary.print_and_maybe_confirm(
        spec, quiet=True, auto_yes=False
    )
    assert proceed is True
    captured = capsys.readouterr()
    assert captured.out == ""  # no one-line either


def test_json_first_launch_without_yes_refuses(monkeypatch, capsys) -> None:
    """--json + first launch + no --yes: emit JSON then refuse (HIGH 3)."""
    spec = _make_spec()
    monkeypatch.setattr(capability_summary, "determine_gate", lambda s: _gate_confirm())
    proceed = capability_summary.print_and_maybe_confirm(
        spec, as_json=True, auto_yes=False
    )
    assert proceed is False
    captured = capsys.readouterr()
    # JSON was emitted on stdout
    parsed = json.loads(captured.out.splitlines()[0])
    assert parsed["session_id"] == "abc123def456"
    # Refusal was on stderr
    assert "refused" in captured.err


def test_json_first_launch_with_yes_proceeds(monkeypatch) -> None:
    """--json + --yes is the documented non-interactive tooling path."""
    spec = _make_spec()
    monkeypatch.setattr(capability_summary, "determine_gate", lambda s: _gate_confirm())
    monkeypatch.setattr(capability_summary, "record_shown", lambda s: None)
    proceed = capability_summary.print_and_maybe_confirm(
        spec, as_json=True, auto_yes=True
    )
    assert proceed is True


def test_render_multiline_surfaces_plugin_trust_warnings() -> None:
    """Impl review MEDIUM 9: trust degradation must appear in the
    capability-summary banner, not only on stderr."""
    spec = _make_spec()
    spec = type(spec)(
        **{**spec.model_dump(),
           "plugin_trust_warnings": (
               ("evil-plugin", "untrusted", "not in trusted_plugins.lock"),
           ),
           "mount_plan": spec.mount_plan,
           "network": spec.network,
           "resources": spec.resources,
           "kernel_caps": spec.kernel_caps,
           "env": spec.env,
        }
    )
    out = capability_summary.render_multiline(spec)
    assert "PLUGIN TRUST WARNING" in out
    assert "evil-plugin" in out
    assert "untrusted" in out
    assert "not in trusted_plugins.lock" in out


def test_gate_reconfirms_on_capability_change_same_image(monkeypatch, tmp_path) -> None:
    """Audit T8: the confirmation gate keys on the CAPABILITY-SET fingerprint,
    not just the image — flipping network mode (image unchanged) re-confirms."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    spec = _make_spec()
    # First launch confirms; record it.
    assert capability_summary.determine_gate(spec).confirm is True
    capability_summary.record_shown(spec)
    # Identical relaunch: no gate.
    assert capability_summary.determine_gate(spec).confirm is False
    # Same image, but network mode flipped none -> internet: MUST re-confirm.
    from botainer.core.spec import NetworkMode, NetworkSpec
    changed = spec.model_copy(update={"network": NetworkSpec(mode=NetworkMode.INTERNET)})
    assert changed.image == spec.image
    g = capability_summary.determine_gate(changed)
    assert g.confirm is True
    assert "capability set changed" in g.reason


def test_gate_reconfirms_on_new_bind_same_image(monkeypatch, tmp_path) -> None:
    """A newly added bind (e.g. an extra mount or a #160 software-root bind) with
    the same image re-confirms."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    spec = _make_spec()
    capability_summary.record_shown(spec)
    assert capability_summary.determine_gate(spec).confirm is False
    extra = Bind(source="/apps/x/bin", target="/apps/x/bin", mode=BindMode.RO,
                 provenance=Provenance.PLUGIN)
    changed = spec.model_copy(update={
        "mount_plan": MountPlan(binds=spec.mount_plan.binds + (extra,))
    })
    assert capability_summary.determine_gate(changed).confirm is True


def test_render_multiline_warns_git_unprotected(tmp_path) -> None:
    """Audit T11 (DN-028 §7): a git repo with no .git/hooks ro overlay (git
    plugin off) must warn that the agent can write host-executed git hooks."""
    (tmp_path / ".git").mkdir()
    spec = _make_spec().model_copy(update={"project_root": str(tmp_path)})
    # _make_spec has no /workspace/.git/hooks bind → overlay not active.
    out = capability_summary.render_multiline(spec)
    assert "GIT UNPROTECTED" in out
    assert ".git/hooks" in out


def test_render_multiline_no_git_warning_when_overlaid(tmp_path) -> None:
    """With the guarded .git/hooks ro overlay bind present, no warning."""
    (tmp_path / ".git").mkdir()
    base = _make_spec()
    overlay = Bind(source=str(tmp_path / ".git/hooks"),
                   target="/workspace/.git/hooks", mode=BindMode.RO,
                   provenance=Provenance.PLUGIN)
    spec = base.model_copy(update={
        "project_root": str(tmp_path),
        "mount_plan": MountPlan(binds=base.mount_plan.binds + (overlay,)),
    })
    assert "GIT UNPROTECTED" not in capability_summary.render_multiline(spec)


def test_render_multiline_no_git_warning_when_not_a_repo(tmp_path) -> None:
    """A non-git project doesn't get the git warning."""
    spec = _make_spec().model_copy(update={"project_root": str(tmp_path)})
    assert "GIT UNPROTECTED" not in capability_summary.render_multiline(spec)


def test_multiline_shows_what_fingerprint_keys_on() -> None:
    """Re-audit round 3 (#3/#4/#7): the consent re-confirm fingerprint keys on
    env-var names, env-files, and capabilities — so render_multiline must DISPLAY
    them, else a re-confirm shows the user an unchanged-looking summary and they
    can't tell what changed. Values are not shown (secret); names are."""
    from botainer.core.spec import CapabilityGrant, EnvSpec
    base = _make_spec()
    spec = base.model_copy(update={
        "env": EnvSpec(values={"FOO_TOKEN": "secret", "BAR_DIR": "/x"}),
        "env_files": ("/state/sessions/s/module-env.env",),
        "capabilities": (CapabilityGrant(name="host.subprocess", value=True),),
    })
    out = capability_summary.render_multiline(spec)
    assert "FOO_TOKEN" in out and "BAR_DIR" in out   # names shown
    assert "secret" not in out                        # values NOT shown
    assert "module-env.env" in out                    # env-files shown
    assert "host.subprocess" in out                   # capabilities shown


def test_internet_advice_recommends_none_on_docker() -> None:
    """Re-audit #37: on Docker, the internet-mode warning recommends switching
    to `network.mode: none` (Docker can actually enforce it)."""
    spec = _make_spec(runtime="docker").model_copy(
        update={"network": NetworkSpec(mode=NetworkMode.INTERNET)})
    out = capability_summary.render_multiline(spec)
    assert "INTERNET MODE" in out
    assert "network.mode: none" in out


def test_internet_advice_does_not_recommend_none_on_apptainer() -> None:
    """Re-audit #37: on Apptainer, `network.mode: none` is a NO-OP (shares the
    host network namespace), so the internet-mode warning must NOT recommend it;
    it points at the firewall / host-side proxy instead."""
    spec = _make_spec(runtime="apptainer").model_copy(
        update={"network": NetworkSpec(mode=NetworkMode.INTERNET)})
    out = capability_summary.render_multiline(spec)
    assert "INTERNET MODE" in out
    # It may NAME network.mode none (to say it's a no-op) but must not RECOMMEND
    # switching to it the way the Docker path does.
    assert "Switch to `network.mode: none`" not in out
    assert "does NOT isolate" in out
    assert "host network namespace" in out
    assert "firewall" in out


def test_none_mode_refused_on_apptainer() -> None:
    """T3-6: `none` on Apptainer is REFUSED at launch
    (adapters/apptainer.py::validate). The summary must say the session will
    NOT start — not imply it runs unisolated (the old 'NOT enforced, treat as
    having host network' text). (Docker stays silent: there `none` IS enforced.)
    """
    appt = _make_spec(runtime="apptainer").model_copy(
        update={"network": NetworkSpec(mode=NetworkMode.NONE)})
    out = capability_summary.render_multiline(appt)
    assert "REFUSED on Apptainer" in out
    assert "will NOT start" in out
    # The old text implied insecure operation — it must be gone.
    assert "Treat the agent as having whatever network" not in out
    # Docker: none is real isolation — no refusal/warning.
    dock = _make_spec(runtime="docker").model_copy(
        update={"network": NetworkSpec(mode=NetworkMode.NONE)})
    dock_out = capability_summary.render_multiline(dock)
    assert "REFUSED" not in dock_out
    assert "NOT enforced" not in dock_out


def test_allowlist_mode_shown_as_refused() -> None:
    """T3-6: endpoint-ip-allowlist / api-only is REFUSED at compose
    for every runtime (#176 — iptables enforcement retired). The summary must
    say the session will NOT start, not that it runs 'best-effort' / 'reaches
    the full internet today' (the old text implied weak-but-running)."""
    for rt in ("docker", "apptainer"):
        spec = _make_spec(runtime=rt).model_copy(update={
            "network": NetworkSpec(
                mode=NetworkMode.ENDPOINT_IP_ALLOWLIST,
                endpoints=("api.anthropic.com",),
            )})
        out = capability_summary.render_multiline(spec)
        assert "REFUSED" in out, rt
        assert "will NOT start" in out, rt
        # Old misleading phrasings must be gone.
        assert "best-effort" not in out, rt
        assert "reaches the full internet today" not in out, rt


def test_auth_mode_detects_codex_mount() -> None:
    """Audit T11: _auth_mode is agent-agnostic — a codex MOUNT (creds bound at
    /home/agent/.openai) reports 'mount', not 'none'."""
    base = _make_spec()
    codex_bind = Bind(source="/host/openai", target="/home/agent/.openai",
                      mode=BindMode.RO, provenance=Provenance.PLUGIN)
    spec = base.model_copy(update={
        "plugins_enabled": ("agent-codex",),
        "mount_plan": MountPlan(binds=base.mount_plan.binds + (codex_bind,)),
    })
    assert capability_summary._auth_mode(spec) == "mount"
    assert "MOUNT" in capability_summary.render_multiline(spec)
    assert "/home/agent/.openai" in capability_summary.render_multiline(spec)


def _spec_with_session(sid: str, *, hints_src: str | None = None) -> SessionSpec:
    """A spec whose per-session binds carry `sid`, like a real launch."""
    base = _make_spec()
    binds = tuple(
        b for b in base.mount_plan.binds
        if b.target != "/workspace/.botainer/AGENT_ACCESS.txt"
    ) + (
        Bind(source=hints_src or f"/state/sessions/{sid}/AGENT_HINTS.md",
             target="/workspace/.botainer/AGENT_HINTS.md",
             mode=BindMode.RO, provenance=Provenance.CORE,
             nested_under="/workspace/.botainer"),
        Bind(source=f"/state/sessions/{sid}/AGENT_ACCESS.txt",
             target="/workspace/.botainer/AGENT_ACCESS.txt",
             mode=BindMode.RO, provenance=Provenance.CORE,
             nested_under="/workspace/.botainer"),
    )
    # SessionSpec is not a dataclass; copy via its own mechanism.
    if hasattr(base, "model_copy"):
        return base.model_copy(update={"session_id": sid,
                                       "mount_plan": MountPlan(binds=binds)})
    import copy as _copy
    out = _copy.copy(base)
    object.__setattr__(out, "session_id", sid)
    object.__setattr__(out, "mount_plan", MountPlan(binds=binds))
    return out


def test_fingerprint_is_stable_across_launches_that_change_nothing():
    """The consent gate must not cry wolf.

    THE DEFECT. `_capability_fingerprint` hashed each bind's
    `source`. Two binds point at `<state>/sessions/<sid>/AGENT_ACCESS.txt` and
    `AGENT_HINTS.md`, and `<sid>` is minted per launch — so the fingerprint
    changed on EVERY launch and the gate announced "capability set changed since
    last launch" when nothing had.

    It is the same failure the code already guarded one bind-mode over: an
    `Audit L5` comment normalises the broker's per-session SOCKET source for
    exactly this reason, keyed on `mode == socket`. These two are `ro`, so they
    slipped past.

    Why this is a security test and not a cosmetic one: the gate is the only
    thing between the user and a REAL capability change — network none->internet,
    a new bind, a switched auth mode. A gate that fires every time teaches the
    user to approve without reading, and then the one real change goes through
    unread.
    """
    from botainer.inspect import capability_summary as cs
    a = cs._capability_fingerprint(_spec_with_session("aaaaaaaaaaaaaaaa"))
    b = cs._capability_fingerprint(_spec_with_session("bbbbbbbbbbbbbbbb"))
    assert a == b, (
        "the capability fingerprint changed between two launches whose ONLY "
        "difference is the session id. The consent gate will announce "
        "'capability set changed since last launch' every time, which trains "
        "the user to approve without reading.")


def test_fingerprint_still_changes_when_a_real_capability_changes():
    """The other half: normalising must not blind the gate.

    Dropping the source from the fingerprint entirely would make the test above
    pass and destroy the control. Binding a DIFFERENT file at the same target is
    a real change and must still re-confirm.
    """
    from botainer.inspect import capability_summary as cs
    sid = "cccccccccccccccc"
    normal = _spec_with_session(sid)
    swapped = _spec_with_session(
        sid, hints_src=f"/state/sessions/{sid}/AGENT_ACCESS.txt")
    assert cs._capability_fingerprint(normal) != cs._capability_fingerprint(swapped), (
        "a DIFFERENT per-session file bound at the same target produced the "
        "same fingerprint — the normalisation threw away too much and the gate "
        "is now blind to a real change")
