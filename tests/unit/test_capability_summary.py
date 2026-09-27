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
        spec, quiet=True, pre_authorised=False, on_sbatch_path=False)
    assert proceed is False


def test_quiet_with_yes_proceeds_first_launch(monkeypatch) -> None:
    """--quiet + --yes is the documented non-interactive auth path."""
    spec = _make_spec()
    monkeypatch.setattr(capability_summary, "determine_gate", lambda s: _gate_confirm())
    monkeypatch.setattr(capability_summary, "record_shown", lambda s: None)
    proceed = capability_summary.print_and_maybe_confirm(
        spec, quiet=True, pre_authorised=True, on_sbatch_path=False)
    assert proceed is True


def test_quiet_on_subsequent_launch_proceeds_silently(monkeypatch, capsys) -> None:
    """--quiet on a subsequent launch (no gate): no output, proceeds."""
    spec = _make_spec()
    monkeypatch.setattr(capability_summary, "determine_gate", lambda s: _gate_no_confirm())
    proceed = capability_summary.print_and_maybe_confirm(
        spec, quiet=True, pre_authorised=False, on_sbatch_path=False)
    assert proceed is True
    captured = capsys.readouterr()
    assert captured.out == ""  # no one-line either


def test_json_first_launch_without_yes_refuses(monkeypatch, capsys) -> None:
    """--json + first launch + no --yes: emit JSON then refuse (HIGH 3)."""
    spec = _make_spec()
    monkeypatch.setattr(capability_summary, "determine_gate", lambda s: _gate_confirm())
    proceed = capability_summary.print_and_maybe_confirm(
        spec, as_json=True, pre_authorised=False, on_sbatch_path=False)
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
        spec, as_json=True, pre_authorised=True, on_sbatch_path=False)
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


# ── CRITICAL-1: a broker reassurance must not cover a mount-mode companion ──
#
# From the companion-agents audit (2026-07-23 plan, "THE REAL BLOCKERS"):
# `_auth_mode` is FIRST-MATCH, so any `agent-*-broker` in plugins_enabled made
# the whole session report "broker" and the consent gate printed
#
#     "A compromised agent cannot read or exfiltrate your credential"
#
# while a mount-mode companion's REAL key sat bind-mounted and readable at
# /home/agent/.openai/api_key in the same container. An absolute safety verdict
# that is true of one credential and false of another in the same session is
# the exact statement this project refuses to make at a consent chokepoint —
# and the consent gate is where a user decides whether to proceed.
#
# The verdict sentence is now gone, and mixed sessions disclose per family.

def test_every_enabled_family_and_its_mode_is_resolved() -> None:
    from botainer.inspect.capability_summary import auth_modes_by_family

    class _Spec:
        def __init__(self, plugins): self.plugins_enabled = plugins

    # The case that produced the false verdict: broker primary, mount companion.
    modes = auth_modes_by_family(_Spec(["agent-claude-broker", "agent-codex", "git"]))
    assert modes == {"claude": "broker", "codex": "mount"}, (
        "a companion session must resolve BOTH families and their DIFFERENT "
        "modes; collapsing to one label is what let a broker reassurance cover "
        "a readable mounted key"
    )

    # Suffix parsing must not mistake the family for a mode, or vice versa.
    assert auth_modes_by_family(_Spec(["agent-claude-shared", "agent-codex-shared"])) == {
        "claude": "shared", "codex": "shared"}
    assert auth_modes_by_family(_Spec(["agent-claude"])) == {"claude": "mount"}
    assert auth_modes_by_family(_Spec(["git", "browser"])) == {}


def test_the_broker_block_no_longer_claims_the_agent_cannot_read_the_credential() -> None:
    """The sentence itself is the defect, so pin its absence FROM THE OUTPUT.

    It was true of a broker-only session and false of a companion one, and
    nothing at the point of printing knew which it was in.

    Written first as a grep of the module source, which the assertion-shape gate
    correctly refused: a source-text assertion passes just as happily when the
    string moves into a docstring, and it can never see a sentence assembled at
    runtime from pieces. So this renders the summary for the session that
    produced the false verdict — broker primary, mount companion — and reads
    what a user would actually be shown.
    """
    spec = _make_spec()
    spec = SessionSpec(
        **{**spec.model_dump(),
           "plugins_enabled": ("agent-claude-broker", "agent-codex"),
           "mount_plan": spec.mount_plan,
           "network": spec.network,
           "resources": spec.resources,
           "kernel_caps": spec.kernel_caps,
           "env": spec.env,
        }
    )

    shown = capability_summary.render_multiline(spec)

    assert "cannot read or exfiltrate your credential" not in shown, (
        "an absolute cannot-read verdict is back in the consent gate; it "
        "cannot be true for every family in a session that also has a "
        "READABLE mounted key for another agent"
    )
    assert "broker" in shown.lower(), (
        "sanity: this must be the broker rendering path, or the absence "
        "above is vacuous — a spec that never reaches the broker block "
        "would pass no matter what the block says"
    )


# ── #215: the credential-collision warning has to REACH the user ──────────
#
# The detector is tested in test_credential_holders.py. What is tested here is
# the half that made the original defect invisible: whether a warning computed
# by start.py actually gets printed, on the path the user is actually on.

def _warned(monkeypatch, *, quiet=False, as_json=False, pre_authorised=False,
            gated=False, capsys=None):
    """Run the REAL print_and_maybe_confirm and return (stdout, stderr)."""
    monkeypatch.setattr(
        capability_summary, "determine_gate",
        lambda spec: capability_summary.SummaryGate(
            confirm=gated, reason="test", last_shown_image=None))
    monkeypatch.setattr(capability_summary, "record_shown", lambda spec: None)
    capability_summary.print_and_maybe_confirm(
        _make_spec(), quiet=quiet, as_json=as_json,
        pre_authorised=pre_authorised,
        extra_warnings=["", "⚠ ANOTHER SESSION CAN ALREADY REFRESH THIS LOGIN",
                        "    Also running:  other-proj"], on_sbatch_path=False)
    return capsys.readouterr()


def test_the_warning_reaches_a_ROUTINE_launch(monkeypatch, capsys) -> None:
    """THE PATH THAT MATTERS. The obvious home for a launch warning is the
    first-launch confirmation gate — and that alone would not have shown the
    incident this exists for: six projects were logged out by sessions all
    confirmed weeks earlier. An ungated launch must print it."""
    out, _err = _warned(monkeypatch, gated=False, capsys=capsys)
    assert "ANOTHER SESSION CAN ALREADY REFRESH" in out
    assert "other-proj" in out


def test_quiet_does_not_suppress_it(monkeypatch, capsys) -> None:
    """--quiet drops the informational one-liner on a project you launch every
    day. A credential collision is not informational, and the daily launch is
    exactly when it happens."""
    out, _err = _warned(monkeypatch, quiet=True, gated=False, capsys=capsys)
    assert "ANOTHER SESSION CAN ALREADY REFRESH" in out
    # ...while the thing --quiet IS for stays suppressed.
    assert "Network:" not in out


def test_the_warning_reaches_an_auto_confirmed_launch(monkeypatch, capsys) -> None:
    """`--yes`, and every non-interactive path (hpc submit forces auto_yes on a
    compute node with no TTY). This branch returns before the prompt, so it is
    the one that silently drops a warning if it is only wired to the prompt."""
    out, _err = _warned(monkeypatch, gated=True, pre_authorised=True, capsys=capsys)
    assert "ANOTHER SESSION CAN ALREADY REFRESH" in out


def test_the_warning_reaches_the_INTERACTIVE_prompt(monkeypatch, capsys) -> None:
    """The first-launch gate, answered by a human.

    Found by mutation: deleting this print site left all the other tests green,
    because the gated test above passes pre_authorised=True and returns BEFORE the
    prompt. Four print sites, three covered — a gap that looks exactly like
    coverage until something deletes the line.

    The warning must be printed BEFORE the prompt, not after: a question the
    user has already answered cannot be informed by what follows it."""
    monkeypatch.setattr(
        capability_summary, "determine_gate",
        lambda spec: capability_summary.SummaryGate(
            confirm=True, reason="first launch", last_shown_image=None))
    monkeypatch.setattr(capability_summary, "record_shown", lambda spec: None)
    asked: list[str] = []

    def _fake_prompt(text, **kwargs):
        asked.append(capsys.readouterr().out)     # what was on screen by then
        return "y"

    monkeypatch.setattr(capability_summary.click, "prompt", _fake_prompt)
    assert capability_summary.print_and_maybe_confirm(
        _make_spec(),
        extra_warnings=["", "⚠ ANOTHER SESSION CAN ALREADY REFRESH THIS LOGIN",
                        "    Also running:  other-proj"], on_sbatch_path=False) is True
    assert asked, "the prompt was never reached"
    assert "ANOTHER SESSION CAN ALREADY REFRESH" in asked[0], (
        "the warning was not on screen when the user was asked to confirm")


def test_json_mode_keeps_stdout_parseable_and_warns_on_stderr(
        monkeypatch, capsys) -> None:
    """A machine consumer must still get valid JSON; a human reading the same
    terminal must still see the warning."""
    out, err = _warned(monkeypatch, as_json=True, gated=False, capsys=capsys)
    json.loads(out)                       # raises if the warning polluted it
    assert "ANOTHER SESSION CAN ALREADY REFRESH" in err


def test_no_warnings_prints_nothing_extra(monkeypatch, capsys) -> None:
    """The common case. An empty block must not leave a stray blank line or a
    header with nothing under it."""
    monkeypatch.setattr(
        capability_summary, "determine_gate",
        lambda spec: capability_summary.SummaryGate(
            confirm=False, reason="", last_shown_image=None))
    capability_summary.print_and_maybe_confirm(
        _make_spec(), quiet=True, extra_warnings=[], on_sbatch_path=False)
    out, err = capsys.readouterr()
    assert out == "" and err == ""


# ── The mounted-credential paragraph in the launch consent block ────────────
#
# It was three broken sentences in the text a user reads to decide whether to
# launch:
#
#     Credential delivery: MOUNTED INTO THE CONTAINER — your real
#       bound into the container at /home/agent/.claude (visible to the
#       visible to the agent. To keep them host-side, use a broker/proxy
#       for your agent family where one is available.
#
# "your real" dangles, "(visible to the" never closes, and the third line
# repeats the half it had already lost. Rendered from a POST-HOOK spec to see
# it at all: the credential bind is a pre_session contribution, so it is absent
# from the compose-time plan — which is why reading `inspect` output would not
# have found this.

def _spec_with_cred_bind(mode: BindMode = BindMode.RW) -> SessionSpec:
    spec = _make_spec()
    binds = (*spec.mount_plan.binds, Bind(
        source="/state/data/agent-claude/profiles/default",
        target="/home/agent/.claude",
        mode=mode,
        provenance=Provenance.PLUGIN,
        provenance_detail="plugin agent-claude-shared pre_session contribution",
    ))
    return spec.model_copy(update={"mount_plan": MountPlan(binds=binds)})


def _cred_paragraph(spec: SessionSpec, *, on_sbatch_path: bool = False) -> list[str]:
    out = capability_summary.render_multiline(
        spec, on_sbatch_path=on_sbatch_path).splitlines()
    start = next(i for i, ln in enumerate(out) if "Credential delivery" in ln)
    end = next(i for i, ln in enumerate(out[start:], start)
               if ln.startswith("Plugins:"))
    return out[start:end]


def test_the_mounted_credential_paragraph_is_whole_sentences() -> None:
    """Every fact a reader needs, and no fragment. The parenthesis check is
    not pedantry: an unclosed `(` is exactly what the broken version had, and
    it is the cheapest mechanical signal that a line was cut mid-clause."""
    para = _cred_paragraph(_spec_with_cred_bind())
    text = " ".join(ln.strip() for ln in para)

    assert "/home/agent/.claude" in text, text
    assert "(rw)" in text, f"the bind's actual mode is not stated: {text}"
    assert "docs/CAPABILITY-SURFACE.md" in text, (
        f"no pointer to what each mode does and does not keep out: {text}")
    assert text.count("(") == text.count(")"), (
        "a line was cut mid-clause:\n" + "\n".join(para))
    # THE WHOLE CLAUSE, as one assertion. The refuting review showed why: with
    # the consequence split across two appended lines, DELETING the
    # continuation left "…changes the account every" dangling — the exact
    # defect this test is named for — and both tests still passed. A
    # word-ending blocklist is a list of the old garble's endings, not a
    # property; a complete clause is a property.
    assert ("can read it — and overwrite it, which changes the account "
            "this project's sessions run as.") in text, text


def test_a_READ_ONLY_credential_mount_does_not_claim_it_can_be_overwritten() -> None:
    """The mode is READ FROM THE BIND, not assumed. "can overwrite it" is true
    of shared mode (the agent has to be able to save a refreshed token) and
    false of a read-only mount, and a consent block that says it anyway is the
    same class of defect as the garble it replaced — text that does not
    describe this session."""
    para = _cred_paragraph(_spec_with_cred_bind(BindMode.RO))
    text = " ".join(ln.strip() for ln in para)
    assert "(ro)" in text, text
    assert "overwrite" not in text, (
        f"claims a read-only mount can be overwritten: {text}")
    assert "read it" in text, text


def test_the_sbatch_path_does_not_send_you_to_a_mode_it_will_refuse() -> None:
    """`hpc submit` renders this same block, and the sbatch path REFUSES broker
    mode at compose — after the broker hook has already started and refreshed,
    which can rotate the refresh token and log other holders out. So on THAT
    path the paragraph must not print `auth use broker`: following it costs a
    rotation and still does not launch. Found by the refuting review."""
    text = " ".join(_cred_paragraph(_spec_with_cred_bind(), on_sbatch_path=True))
    assert "auth use broker" not in text, (
        f"tells a cluster user to switch to a mode the sbatch path refuses "
        f"AFTER the broker has refreshed:\n{text}")
    assert "salloc" in text and "--runtime apptainer" in text, (
        f"no reachable host-side route is named:\n{text}")


def test_an_APPTAINER_session_that_is_NOT_the_sbatch_path_is_told_broker_works():
    """The branch is about the CALLER, not the runtime — a mutation's worth of
    difference to a real user. `botainer start --runtime apptainer` inside an
    `salloc` is apptainer AND can reach a broker, so keying the advice on
    `spec.runtime` told that user to do the thing they were already doing and
    never mentioned the mode that would have helped. Found by the loop tzar at
    checkpoint 9, by rendering both paths."""
    appt = _spec_with_cred_bind().model_copy(update={"runtime": "apptainer"})
    text = " ".join(_cred_paragraph(appt))          # not the sbatch path
    assert "auth use broker" in text, (
        f"an salloc session is not told the host-side mode it CAN use:\n{text}")
    assert "salloc" not in text, (
        f"tells a user inside an salloc to get inside an salloc:\n{text}")


def test_the_scope_of_the_overwrite_comes_from_the_SPEC_not_the_mode_name() -> None:
    """Who else is affected is a FACT in the plan, not an adjective.

    The host-wide store is bound only in shared mode, so its presence in the
    binds decides between "this project's sessions" and "every project that
    shares this login". Asked for by the refuting review, which established by
    running that a write through the per-project mount does reach the shared
    master (the promotion runs at session start AND exit).
    """
    spec = _spec_with_cred_bind()
    binds = (*spec.mount_plan.binds, Bind(
        source="/state/shared-auth/agent-claude",
        target="/shared-auth/agent-claude",
        mode=BindMode.RW,
        provenance=Provenance.PLUGIN,
        provenance_detail="plugin agent-claude-shared pre_session contribution",
    ))
    shared = spec.model_copy(update={"mount_plan": MountPlan(binds=binds)})

    assert "every project that shares this login runs as." in " ".join(
        _cred_paragraph(shared)), _cred_paragraph(shared)
    assert "this project's sessions run as." in " ".join(
        _cred_paragraph(spec)), _cred_paragraph(spec)
