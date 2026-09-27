"""Wiring guardrail for `bot1 start` (shared mode) — mock runtime.

#129: renamed from tests/integration/test_session_smoke.py.
The original name implied end-to-end coverage; in reality the mock
adapter is used throughout (see "Notes on the test scope" below).
Mislabeling integration coverage hid the absence of real-runtime
tests. New name + new location (tests/unit/) match the actual scope.

This test exercises the contract every wiring bug we hit during v0.1.0
development violated, without needing a real Docker daemon:

    init project (shared mode)
    → write a fake credential to the shared-auth dir
    → compose_session (resolves image, builds base mount plan)
    → run_pre_session_hooks (runs agent-claude-shared/hooks/pre_session.py
      against a real fixture, merges its contributions)
    → assert the resulting SessionSpec is internally consistent and
      every wiring point the launcher relies on is present

Bugs this would have caught at commit time:

  - manifest.image.tag missing → spec.image refusal (5b96842)
  - resolver requires "@" in tag → refusal (5b96842)
  - BOTAINER_STATE_ROOT not propagated to pre_session hook → hook
    reads ~/.botainer and refuses with "no shared credential" (7d7ae39)
  - null-bind-anchor name mismatch → docker bind source missing (4204c5b)
  - @digest form for locally-built images → docker pull refusal (5d6da02)
  - per_project_dir missing .claude.json symlink → Claude inside
    container shows login prompt despite valid credential (f9b814f)
  - virtiofs nested-bind placeholders missing → docker mount refusal
    on Mac (2f5bf37)

Notes on the test scope:
  - Docker is not invoked. The mock runtime adapter is used end-to-end.
  - The host filesystem IS touched: per_project_dir, symlinks, and
    placeholder files are created under tmp_path. We assert their
    presence and structure.
  - The credential file content is a minimal-but-valid Claude OAuth
    payload so pre_session's content validation accepts it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from botainer.core import composition, identity
from botainer.core import config as config_module
from botainer.plugins import builtin as plugin_builtin
from botainer.state import dir as state_dir

# Note: no `append_image_to_config` import. We test the resolver's
# natural path (no `image:` override in config) so the manifest tag
# wiring is actually exercised.


@pytest.fixture
def installed_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A state-dir with all bundled plugins installed, env primed."""
    state = tmp_path / "state"
    monkeypatch.setenv("MY_BOTAINER", str(state))
    state_dir.ensure_user_state_dir(create_if_missing=True)
    plugin_builtin.install_all_builtin()
    return state


_FAKE_OAUTH = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-test-token-not-real",
        "refreshToken": "sk-ant-ort01-test-refresh-not-real",
        "expiresAt": 9999999999999,  # far future
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }
}
_FAKE_CLAUDE_JSON = {
    "oauthAccount": {
        "emailAddress": "test@example.com",
        "subscriptionType": "max",
    }
}


def _make_shared_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    config_module.write_initial_config(proj, agent="claude", force=False)
    cfg_path = proj / ".botainer" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    enabled = [
        p for p in data.get("plugins_enabled", [])
        if not p.startswith("agent-claude")
    ]
    enabled.insert(0, "agent-claude-shared")
    data["plugins_enabled"] = enabled
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
    return proj


def _seed_shared_credential(state_root: Path) -> Path:
    """Pre-populate the shared-auth dir as `bot1 auth login --shared`
    would. Returns the credential path."""
    shared_dir = state_root / "shared-auth" / "agent-claude"
    shared_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(shared_dir, 0o700)
    creds = shared_dir / ".credentials.json"
    creds.write_text(json.dumps(_FAKE_OAUTH))
    os.chmod(creds, 0o600)
    claude_json = shared_dir / ".claude.json"
    claude_json.write_text(json.dumps(_FAKE_CLAUDE_JSON))
    os.chmod(claude_json, 0o600)
    return creds


def test_session_shared_mode_wiring_with_mock(
    installed_state: Path, tmp_path: Path
) -> None:
    """The end-to-end contract every wiring bug today violated."""
    state_root = installed_state
    creds = _seed_shared_credential(state_root)
    assert creds.exists()

    proj = _make_shared_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)

    # ── Compose ────────────────────────────────────────────────────────
    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=False
    )

    # Image: must resolve via manifest tag (5b96842) and NOT use
    # `name:tag@digest` form for locally-built dockerfile plugins
    # (5d6da02 — docker can't pull a local image ID).
    # Bundled manifest declares: tag: botainer/agent-claude:0.1 +
    # source: dockerfile. With no `image:` override in config and no
    # digest in installed.lock, resolver must return the plain tag.
    assert spec.image == "botainer/agent-claude:0.1", (
        f"resolver should return the plain manifest tag for a locally-"
        f"built bundled plugin with no digest recorded; got {spec.image!r}"
    )
    assert "@sha256:" not in spec.image, (
        f"locally-built image must not get @digest splice: {spec.image!r}"
    )

    # ── Pre-session hooks run; contributions merge into spec ───────────
    spec_after = composition.run_pre_session_hooks(spec)

    # Pre_session must NOT have refused. The credential we seeded is at
    # the user-wide-root path; if BOTAINER_STATE_ROOT propagated the
    # PER-PROJECT path (the original bug from 7d7ae39), the hook would
    # look at <root>/state/<uuid>/shared-auth and refuse.
    target_set = {b.target for b in spec_after.mount_plan.binds}
    assert "/home/agent/.claude" in target_set, (
        "pre_session must contribute /home/agent/.claude bind; if missing, "
        "the hook refused — most likely BOTAINER_STATE_ROOT propagation."
    )
    assert "/shared-auth/agent-claude" in target_set, (
        "pre_session must contribute /shared-auth/agent-claude bind."
    )

    # CLAUDE_CONFIG_DIR must reach the container env. Without this,
    # Claude reads ~/.claude (which isn't bound) and re-prompts login.
    assert spec_after.env.values.get("CLAUDE_CONFIG_DIR") == "/home/agent/.claude", (
        "CLAUDE_CONFIG_DIR must be set in spec.env so the container's "
        "Claude reads from the bound dir."
    )

    # ── On-host filesystem after pre_session ───────────────────────────
    uid = identity.read_project_id(proj)
    per_project_dir = (
        state_root / "state" / uid / "data" / "agent-claude"
        / "profiles" / "default"
    )
    assert per_project_dir.is_dir(), (
        "pre_session must create per_project_dir on host."
    )

    # .credentials.json symlink → /shared-auth/agent-claude/.credentials.json
    creds_link = per_project_dir / ".credentials.json"
    assert creds_link.is_symlink(), (
        ".credentials.json must be a symlink in per_project_dir."
    )
    assert (
        os.readlink(creds_link)
        == "/shared-auth/agent-claude/.credentials.json"
    )

    # .claude.json symlink → /shared-auth/agent-claude/.claude.json
    # (f9b814f: without this, Claude shows login prompt despite valid
    # credential because account state lives in .claude.json.)
    claude_link = per_project_dir / ".claude.json"
    assert claude_link.is_symlink(), (
        ".claude.json must be a symlink in per_project_dir (otherwise "
        "Claude in the container falls back to its own fresh empty "
        "config and re-prompts for login)."
    )
    assert (
        os.readlink(claude_link)
        == "/shared-auth/agent-claude/.claude.json"
    )

    # ── Null-bind anchor exists and is named consistently ──────────────
    # 4204c5b: composition.py creates <data>/null-bind-anchor; mount_plan
    # references the same path. Mismatch breaks docker bind.
    null_anchor = state_root / "state" / uid / "data" / "null-bind-anchor"
    assert null_anchor.is_dir(), (
        "null-bind anchor dir must exist at <data>/null-bind-anchor. "
        "If it's at <data>/_null-bind-anchor (with underscore), the bug "
        "from 4204c5b regressed."
    )

    # ── Nested-bind placeholders (Mac virtiofs fix from 2f5bf37) ───────
    composition._prepare_nested_bind_placeholders(spec_after.mount_plan)
    expected_placeholders = {
        null_anchor / "AGENT_HINTS.md",
        null_anchor / "AGENT_ACCESS.txt",
    }
    for p in expected_placeholders:
        if not p.exists():
            # Some binds may not be nested in all configs; surface any
            # missing placeholder that pertains to a NESTED bind in the
            # plan. (We assert per-bind rather than absolute set.)
            pass
    for b in spec_after.mount_plan.binds:
        if b.nested_under != "/workspace/.botainer":
            continue
        rel = Path(b.target).relative_to(b.nested_under)
        placeholder = null_anchor / rel
        assert placeholder.exists(), (
            f"placeholder missing for nested bind {b.target} → "
            f"{placeholder}; would break Mac virtiofs."
        )

    # ── No stray @digest in spec.image ─────────────────────────────────
    # (Double-check; 5d6da02 fixed this for the digest-pinned branch.)
    if spec_after.image.startswith("botainer/agent-claude"):
        assert "@sha256:" not in spec_after.image, (
            f"spec.image still uses @digest for a locally-built image: "
            f"{spec_after.image!r}"
        )


def test_populated_null_bind_anchor_is_reset_not_refused(
    installed_state: Path, tmp_path: Path
) -> None:
    """Grace host-test: a prior crashed session leaves files in the
    internal null-bind masking dir (data/null-bind-anchor/). botainer must RESET
    it silently and proceed — NOT refuse and force the user (or a developer!) to
    `rm -rf` an internal path they should never know exists."""
    state_root = installed_state
    _seed_shared_credential(state_root)
    proj = _make_shared_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)

    # First compose creates the anchor + the project id.
    composition.compose_session(proj, runtime_choice="mock", identity_accept=False,
                                # These exercise the LAUNCH contract — the masking-dir
                                # reset and the refusal when it fails — so they must
                                # opt in the way `start` does. A preview does not, and
                                # test_preview_does_not_mutate_state.py pins that. (#227)
                                reset_null_anchor=True)
    uid = identity.read_project_id(proj)
    anchor = state_root / "state" / uid / "data" / "null-bind-anchor"

    # Simulate a prior session's leftovers: files + a nested subdir.
    (anchor / "AGENT_ACCESS.txt").write_text("stale")
    (anchor / "agent-claude").mkdir(exist_ok=True)
    (anchor / "agent-claude" / "junk").write_text("x")
    assert list(anchor.iterdir()), "precondition: anchor is populated"

    # Re-compose must NOT raise (was: Refused mount-path-null-bind-violated); it
    # resets the anchor to the required-empty invariant.
    composition.compose_session(proj, runtime_choice="mock", identity_accept=False,
                                # These exercise the LAUNCH contract — the masking-dir
                                # reset and the refusal when it fails — so they must
                                # opt in the way `start` does. A preview does not, and
                                # test_preview_does_not_mutate_state.py pins that. (#227)
                                reset_null_anchor=True)
    assert list(anchor.iterdir()) == [], "anchor must be reset to empty on re-compose"

# ─────────── the masking-dir refusal: three states, one destructive ───────────
#
# Reported 2026-09-04: `bot1 start` with a session already running refused with
# "(leftover from a prior session)" and an internal path. Not a leftover, not
# prior — it was running, its container held the directory, and the only
# actionable thing in the sentence read as "delete this".
#
# The FIRST fix had two states and folded "cannot tell" into "nothing is
# running", which review showed is worse than the bug: `is_session_alive`
# returns False for an apptainer record with no slurm_jobid, and the adapter
# only records one under SLURM — so a direct start on a login node reads as
# DEAD WHILE RUNNING and would have been handed `rm -rf`. Hence three states,
# and a test for each: the destructive branch is the one that most needs
# pinning, and originally had no test at all.


def _anchor_for(state_root, proj):
    uid = identity.read_project_id(proj)
    return state_root / "state" / uid / "data" / "null-bind-anchor"


def _refuse_with_unresettable_anchor(installed_state, tmp_path, monkeypatch,
                                     records, alive):
    """Drive the REAL compose_session into the REAL failure path."""
    import os

    from botainer.state import liveness as _liveness
    from botainer.state import session_record as _sr

    _seed_shared_credential(installed_state)
    proj = _make_shared_project(tmp_path)
    identity.init_project(proj, agent="claude", force=True, non_interactive=True)
    composition.compose_session(proj, runtime_choice="mock", identity_accept=False,
                                # These exercise the LAUNCH contract — the masking-dir
                                # reset and the refusal when it fails — so they must
                                # opt in the way `start` does. A preview does not, and
                                # test_preview_does_not_mutate_state.py pins that. (#227)
                                reset_null_anchor=True)
    anchor = _anchor_for(installed_state, proj)
    (anchor / "agent-claude").mkdir(exist_ok=True)

    monkeypatch.setattr(_sr, "list_sessions", records)
    monkeypatch.setattr(_liveness, "is_session_alive", alive)
    os.chmod(anchor, 0o500)            # readable, NOT writable -> removal fails
    try:
        from botainer.core.refusal import Refused
        with pytest.raises(Refused) as exc:
            composition.compose_session(proj, runtime_choice="mock",
                                        identity_accept=False,
                                        reset_null_anchor=True)  # launch contract (#227)
    finally:
        os.chmod(anchor, 0o700)
    return str(exc.value)


class _Rec:
    """Enough of a SessionRecord for this path; runtime is load-bearing."""
    def __init__(self, sid, runtime="docker", jobid=""):
        self.session_id = sid
        self.runtime = runtime
        self.ended_at = None
        self.apptainer = type("A", (), {"slurm_jobid": jobid})()


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root ignores the mode bits this test relies on")
def test_a_live_session_is_named_not_blamed_on_a_leftover(
        installed_state, tmp_path, monkeypatch) -> None:
    msg = _refuse_with_unresettable_anchor(
        installed_state, tmp_path, monkeypatch,
        records=lambda d: [_Rec("ses-live-abc123")],
        alive=lambda r: True)
    assert "ses-live-abc123" in msg, "must name the session that is blocking"
    assert "botainer attach ses-live-abc123" in msg
    assert "botainer stop ses-live-abc123" in msg
    assert "leftover" not in msg.lower()
    assert "rm -rf" not in msg, "never point at deleting what a live session uses"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode bits")
def test_an_apptainer_session_gets_slurm_commands_not_docker_ones(
        installed_state, tmp_path, monkeypatch) -> None:
    """`botainer attach` REFUSES for apptainer (cli/attach.py). Naming it would
    be a remedy guaranteed to fail — the defect class this project calls
    'shipped output names commands that do not exist'."""
    msg = _refuse_with_unresettable_anchor(
        installed_state, tmp_path, monkeypatch,
        records=lambda d: [_Rec("ses-hpc-1", runtime="apptainer", jobid="12345")],
        alive=lambda r: True)
    assert "botainer hpc attach --jobid" in msg
    assert "scancel" in msg
    assert "botainer attach ses-hpc-1" not in msg


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode bits")
def test_an_undecidable_session_never_produces_a_delete_instruction(
        installed_state, tmp_path, monkeypatch) -> None:
    """THE ONE THAT MATTERS ON HPC.

    An apptainer record with no slurm_jobid is what a direct `botainer start`
    on a login node writes, and `is_session_alive` calls it DEAD. Treating that
    as "nothing is running" would hand the user `rm -rf` for a session that is
    running right now. Undecidable must say undecidable.
    """
    msg = _refuse_with_unresettable_anchor(
        installed_state, tmp_path, monkeypatch,
        records=lambda d: [_Rec("ses-nojob", runtime="apptainer", jobid="")],
        alive=lambda r: False)
    assert "could not determine" in msg
    assert "rm -rf" not in msg, "an unknown must never produce a delete command"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode bits")
def test_a_failure_to_read_the_bookkeeping_is_unknown_not_none(
        installed_state, tmp_path, monkeypatch) -> None:
    """A wedged docker daemon or an unreadable state dir is a plausible
    CO-CAUSE of the error being handled. Collapsing it into "nothing is
    running" is fail-open in the advice direction."""
    def _boom(d):
        raise OSError(13, "Permission denied")
    msg = _refuse_with_unresettable_anchor(
        installed_state, tmp_path, monkeypatch,
        records=_boom, alive=lambda r: True)
    assert "could not determine" in msg
    assert "rm -rf" not in msg


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode bits")
def test_a_confidently_empty_scan_gives_a_quoted_command_and_no_verdict(
        installed_state, tmp_path, monkeypatch) -> None:
    """Only here may a delete be suggested — and it states a FACT about what
    the directory holds rather than rendering a safety verdict ("safe"), which
    this project forbids on a short surface."""
    msg = _refuse_with_unresettable_anchor(
        installed_state, tmp_path, monkeypatch,
        records=lambda d: [], alive=lambda r: False)
    assert "rm -rf" in msg
    assert "recreates this directory empty" in msg
    assert "is safe" not in msg, "state the fact; do not render a safety verdict"
