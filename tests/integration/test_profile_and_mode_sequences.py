"""Profile and mode changes, as SEQUENCES — the order and timing matter.

Prepared per DN-006 §9. Written against
a list of scenarios rather than against whatever the code currently does, so the
list is the specification and the markers below record how much of it is met.

TWO CONFIRMED DEFECTS ARE PINNED AS `xfail(strict=True)`. Strict matters: when
the fix lands these turn into XPASS, which pytest reports as a FAILURE, forcing
whoever fixed it to come back here and drop the marker. A non-strict xfail would
let the fix land with the scenario still marked broken.

- **D1** (§3) shared mode ignores `--auth-profile` for credentials. Two
  profiles resolve to one host-wide file, so two ACCOUNTS share one login.
- **D2** (§3b) a config edit from isolated to shared promotes the project's
  PRIVATE credential — possibly a different account — into the host-wide store,
  where every other shared-mode project inherits it.

These drive the real hook as a subprocess, because the thing under test is what
the hook does to files on disk, and reading it is what let both defects sit
here unnoticed.

READ THIS BEFORE TRUSTING ANY PROFILE ASSERTION BELOW
-----------------------------------------------------
Driving the hook as a subprocess with `BOTAINER_PROFILE` set is NOT how sessions
run. Real sessions go through `botainer/plugins/hooks.py::run_hook`, which
scrubs the host environment to `_HOOK_ENV_ALLOWLIST` — and BOTAINER_PROFILE is
not in it. Verified through the real dispatcher: with BOTAINER_PROFILE=work set
exactly as `cli/start.py:359` does, the hook creates `profiles/default`.

So every scenario below that varies the profile is testing the hook's LOGIC
under an input it never actually receives. That is still worth pinning — the
logic is wrong and will be reachable the moment the plumbing is fixed — but it
must not be read as "this happens in production". `test_profile_never_reaches_a_session_hook`
is the one that pins the real, current behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHARED_HOOK = REPO / "plugins" / "agent-claude-shared" / "hooks" / "pre_session.py"
ISOLATED_HOOK = REPO / "plugins" / "agent-claude" / "hooks" / "pre_session.py"

HOUR_MS = 3_600_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _cred(account: str, expires_ms: int) -> str:
    """A credential that passes the #158 shape checks — as a real one does.

    Deliberately well-formed: the point of these scenarios is that a GENUINE
    second account satisfies every anti-poisoning check, because those checks
    were built against forgery, not against account-crossing.
    """
    return json.dumps({"claudeAiOauth": {
        "accessToken": f"sk-ant-oat-{account}" + "x" * 55,
        "refreshToken": f"sk-ant-ort-{account}" + "x" * 55,
        "expiresAt": expires_ms,
    }})


def _write(path: Path, content: str) -> None:
    """Write a credential, replacing a symlink the way `rename()` does.

    Not a convenience: this IS the break-away. Claude Code saves its refreshed
    credential to a temp file and `rename()`s it into place, which REPLACES a
    symlink with a regular file rather than writing through it. Writing through
    the link would simulate something that never happens — and here it cannot
    even be done, since the target is the container-absolute `/shared-auth/...`
    which does not exist on the host.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    path.write_text(content)
    path.chmod(0o600)


def _host_store(root: Path) -> Path:
    return root / "shared-auth" / "agent-claude" / ".credentials.json"


def _project_cred(root: Path, uid: str, profile: str) -> Path:
    return (root / "state" / uid / "data" / "agent-claude" / "profiles"
            / profile / ".credentials.json")


def _run(hook: Path, root: Path, uid: str, profile: str):
    env = dict(os.environ)
    env.update({
        "BOTAINER_PROJECT_UUID": uid,
        "BOTAINER_STATE_ROOT": str(root),
        "BOTAINER_PROFILE": profile,
    })
    return subprocess.run(
        [sys.executable, str(hook)], env=env, capture_output=True, text=True)


def _account_in(path: Path) -> str | None:
    """Which account the file at `path` currently resolves to."""
    try:
        blk = json.loads(path.read_text())["claudeAiOauth"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    tok = blk.get("accessToken", "")
    return tok[len("sk-ant-oat-"):].rstrip("x") or None


# ─────────────────────── profile sequences ───────────────────────

def test_profile_reaches_session_hooks_via_the_spec_not_the_host_env(tmp_path):
    """FIXED. This test previously asserted the GAP.

    `--auth-profile` used to set `os.environ["BOTAINER_PROFILE"]`, which reached
    nothing: `run_hook` scrubs the host environment to `_HOOK_ENV_ALLOWLIST` and
    BOTAINER_PROFILE was not on it. Measured then: with the variable set exactly
    as start.py did, the hook created `profiles/default`.

    The fix deliberately did NOT widen that allowlist. The allowlist governs
    what of the HOST's environment survives into a hook; a profile is not host
    state, it is a composition decision. Widening a scrub list to carry one
    value makes every future value a candidate. So the profile travels on the
    SPEC, and composition puts `spec.profile` into the hook env explicitly.

    Both halves are asserted: the allowlist stays closed, AND the profile
    arrives anyway.
    """
    from botainer.plugins.hooks import _HOOK_ENV_ALLOWLIST
    assert "BOTAINER_PROFILE" not in _HOOK_ENV_ALLOWLIST, (
        "the scrub allowlist was widened to carry the profile. It should not "
        "need to be — composition passes spec.profile explicitly. Widening it "
        "reopens the host env as a channel into hooks."
    )
    src = (REPO / "botainer" / "core" / "composition.py").read_text()
    assert src.count('"BOTAINER_PROFILE": spec.profile') == 3, (
        "composition builds three hook-env dicts (host_pre_launch, pre_session, "
        "post_session). All three must carry the profile, or a hook sees the "
        "wrong account depending on WHEN it runs."
    )


@pytest.mark.xfail(strict=True, reason=(
    "D1 (DN-006 §3a): the shared-mode SYMLINK TARGET is profile-less, so if "
    "the profile ever reaches the hook, two accounts would share one login. "
    "Hypothetical today — see test_profile_never_reaches_a_session_hook. "
    "Remove when auth/host/<agent>/<profile>/ lands."))
def test_two_profiles_in_shared_mode_get_separate_credentials(tmp_path):
    """SEQUENCE: login work -> start work -> start personal.

    HYPOTHETICAL: drives the hook directly with a profile it never receives in
    production. Pins the layout defect that becomes live the moment the env
    plumbing is fixed.
    """
    root = tmp_path / "state-root"
    _write(_host_store(root), _cred("WORK", _now_ms() + HOUR_MS))
    _run(SHARED_HOOK, root, "proj1", "work")
    _run(SHARED_HOOK, root, "proj1", "personal")

    work = _project_cred(root, "proj1", "work").resolve()
    personal = _project_cred(root, "proj1", "personal").resolve()
    assert work != personal, (
        "both profiles resolve to the same credential file — one account's "
        "token is serving both")


@pytest.mark.xfail(strict=True, reason=(
    "D2 (DN-006 §3b): switching mode in config.yaml promotes the project's "
    "private credential host-wide. Remove when auth/project/... lands."))
def test_switching_isolated_to_shared_does_not_promote_the_private_credential(tmp_path):
    """SEQUENCE: isolated login (account B) -> edit config to shared -> start.

    The private credential belongs to this project alone. Adopting it host-wide
    hands it to every other shared-mode project on the machine.
    """
    root = tmp_path / "state-root"
    now = _now_ms()
    _write(_host_store(root), _cred("ACCOUNT_A", now + HOUR_MS))
    _write(_project_cred(root, "proj1", "default"), _cred("ACCOUNT_B", now + 2 * HOUR_MS))

    _run(SHARED_HOOK, root, "proj1", "default")

    assert _account_in(_host_store(root)) == "ACCOUNT_A", (
        "a private per-project credential was promoted to the host-wide store")


def test_switching_shared_to_isolated_does_not_silently_use_the_shared_token(tmp_path):
    """SEQUENCE: shared start -> edit config to isolated -> start.

    Passes TODAY, but by accident: the symlink target is the container-absolute
    `/shared-auth/...`, which dangles on the host, so the isolated hook reports
    "no credentials". Nothing documents or tests that, and making the symlink
    resolve host-side — which looks like a bug fix — would turn this into a
    leak. Pinned so that change cannot land quietly.
    """
    root = tmp_path / "state-root"
    _write(_host_store(root), _cred("SHAREDACCT", _now_ms() + HOUR_MS))
    _run(SHARED_HOOK, root, "proj1", "default")

    res = _run(ISOLATED_HOOK, root, "proj1", "default")
    combined = res.stdout + res.stderr
    assert "SHAREDACCT" not in combined
    assert res.returncode != 0 or "no credentials" in combined.lower(), (
        "isolated mode resolved a credential through shared mode's symlink")


def test_profile_switch_between_runs_keeps_each_profiles_settings(tmp_path):
    """SEQUENCE: start work -> start personal -> start work.

    Settings dirs are per-profile today. Whether they SHOULD be is the open
    question (DN-006 §9b: history should follow the project, not the
    account, so a quota-driven account switch does not lose the conversation).
    This pins the current behaviour so the change is deliberate and visible.
    """
    root = tmp_path / "state-root"
    _write(_host_store(root), _cred("ACCT", _now_ms() + HOUR_MS))
    for prof in ("work", "personal", "work"):
        _run(SHARED_HOOK, root, "proj1", prof)
    base = root / "state" / "proj1" / "data" / "agent-claude" / "profiles"
    assert {p.name for p in base.iterdir()} == {"work", "personal"}


# ─────────────────────── timing / ordering ───────────────────────

def test_backfill_prefers_the_later_expiry_not_the_later_write(tmp_path):
    """ORDERING: an OLDER token written LAST must not win.

    mtime is untrustworthy (FS granularity, NFS clock skew, `touch`), which is
    why the check is on expiresAt. Written last, expiring first, must lose.
    """
    root = tmp_path / "state-root"
    now = _now_ms()
    _write(_host_store(root), _cred("NEWER", now + 4 * HOUR_MS))
    time.sleep(0.01)
    _write(_project_cred(root, "proj1", "default"), _cred("OLDER", now + HOUR_MS))

    _run(SHARED_HOOK, root, "proj1", "default")
    assert _account_in(_host_store(root)) == "NEWER"


def test_two_projects_same_profile_last_exit_does_not_clobber_a_newer_token(tmp_path):
    """SEQUENCE: A starts, B starts, A refreshes, B exits.

    B's break-away file is older than what A left in the host store. B exiting
    last must not roll the host store back.
    """
    root = tmp_path / "state-root"
    now = _now_ms()
    _write(_host_store(root), _cred("BASE", now + HOUR_MS))
    _run(SHARED_HOOK, root, "projA", "default")
    _run(SHARED_HOOK, root, "projB", "default")

    # A refreshed in-container: rename() replaced its symlink, host store gets
    # the newer token when A next reconciles.
    _write(_project_cred(root, "projA", "default"), _cred("REFRESHED", now + 5 * HOUR_MS))
    _run(SHARED_HOOK, root, "projA", "default")
    assert _account_in(_host_store(root)) == "REFRESHED"

    # B still holds a stale break-away copy and exits now.
    _write(_project_cred(root, "projB", "default"), _cred("BASE", now + HOUR_MS))
    _run(SHARED_HOOK, root, "projB", "default")
    assert _account_in(_host_store(root)) == "REFRESHED", (
        "a stale session exiting last rolled the host store backwards")


def test_absurd_expiry_values_do_not_win_the_comparison(tmp_path):
    """A container-writable file can hold anything.

    A far-future expiry is the cheapest way to make a forgery look freshest;
    the 60-day delta cap is what blocks it. Pinned because that cap is easy to
    "simplify" away while the shape checks look like the real defence.
    """
    root = tmp_path / "state-root"
    now = _now_ms()
    _write(_host_store(root), _cred("REAL", now + HOUR_MS))
    _write(_project_cred(root, "proj1", "default"),
           _cred("FORGED", now + 3650 * 24 * HOUR_MS))   # ten years

    _run(SHARED_HOOK, root, "proj1", "default")
    assert _account_in(_host_store(root)) == "REAL"


def test_malformed_expiry_does_not_crash_or_win(tmp_path):
    root = tmp_path / "state-root"
    now = _now_ms()
    _write(_host_store(root), _cred("REAL", now + HOUR_MS))
    bad = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat-BAD" + "x" * 55,
        "refreshToken": "sk-ant-ort-BAD" + "x" * 55,
        "expiresAt": "not-a-number",
    }})
    _write(_project_cred(root, "proj1", "default"), bad)

    res = _run(SHARED_HOOK, root, "proj1", "default")
    assert "Traceback" not in res.stderr
    assert _account_in(_host_store(root)) == "REAL"


# ────────────────── re-login inside a running session ──────────────────

@pytest.mark.xfail(strict=True, reason=(
    "D3 (DN-006 §3c): `/login` to a different account inside a shared "
    "session propagates that account HOST-WIDE on the next reconcile. No "
    "config edit, no flag, no warning. Remove when the promotion path "
    "verifies the account or refuses."))
def test_relogin_to_another_account_mid_session_is_not_promoted_host_wide(tmp_path):
    """SEQUENCE: start shared -> /login (different account) -> next start.

    The most likely of the three account-crossing paths, because it needs only
    one command typed inside the session. Claude Code writes the new credential
    with rename(), which replaces the symlink; the next reconcile sees a later
    expiry and promotes it to the host-wide store, where every other
    shared-mode project inherits it.

    Note what is NOT distinguishable here: a re-login and an ordinary token
    ROTATION produce the same on-disk shape — a regular file with a new token
    pair and a later expiry. So no local check can separate "refreshed" from
    "logged in as someone else". That is why DN-006 puts the account
    verification AT THE PROMOTION POINT rather than trying to classify the file.
    """
    root = tmp_path / "state-root"
    now = _now_ms()
    _write(_host_store(root), _cred("ORIGINAL", now + HOUR_MS))
    _run(SHARED_HOOK, root, "proj1", "default")

    # /login inside the container, different account.
    _write(_project_cred(root, "proj1", "default"), _cred("OTHERACCT", now + 2 * HOUR_MS))
    _run(SHARED_HOOK, root, "proj1", "default")

    assert _account_in(_host_store(root)) == "ORIGINAL", (
        "an in-session re-login propagated a different account host-wide")


def test_relogin_blast_radius_reaches_every_shared_project(tmp_path):
    """The consequence, stated separately so it is visible in the test names.

    Not xfail: this asserts what CURRENTLY happens, so the scope of D3 is
    recorded rather than implied. When D3 is fixed this test must be rewritten,
    not deleted — the question "who else sees it" stays worth asserting.
    """
    root = tmp_path / "state-root"
    now = _now_ms()
    _write(_host_store(root), _cred("ORIGINAL", now + HOUR_MS))
    _run(SHARED_HOOK, root, "projA", "default")
    _run(SHARED_HOOK, root, "projB", "default")

    _write(_project_cred(root, "projA", "default"), _cred("OTHERACCT", now + 2 * HOUR_MS))
    _run(SHARED_HOOK, root, "projA", "default")
    _run(SHARED_HOOK, root, "projB", "default")

    # projB never logged in as OTHERACCT and is now using it.
    assert _project_cred(root, "projB", "default").is_symlink()
    assert _account_in(_host_store(root)) == "OTHERACCT"


@pytest.mark.xfail(strict=True, reason=(
    "A5 (DN-006 §9): history is per-profile today, so a quota-driven "
    "account switch loses the conversation. Remove when settings move to "
    "state/<uuid>/data/<agent>/settings/."))
def test_quota_driven_account_switch_keeps_the_conversation(tmp_path):
    """SEQUENCE: work on a task under `work` -> quota runs out -> `personal`.

    The case that decides the layout. You switch accounts to KEEP WORKING, so
    the history must survive the switch. Keyed on profile it does not.
    """
    root = tmp_path / "state-root"
    _write(_host_store(root), _cred("WORK", _now_ms() + HOUR_MS))
    _run(SHARED_HOOK, root, "proj1", "work")

    settings = (root / "state" / "proj1" / "data" / "agent-claude"
                / "profiles" / "work")
    (settings / "history.jsonl").write_text('{"turn": 1}\n')

    _run(SHARED_HOOK, root, "proj1", "personal")
    after = (root / "state" / "proj1" / "data" / "agent-claude"
             / "profiles" / "personal" / "history.jsonl")
    assert after.exists(), "the conversation did not survive the account switch"


# ── --auth-profile, end to end through composition ──

def _init_composable(proj: Path) -> None:
    """A project compose_session will accept: mock runtime + an image override,
    so the test exercises the profile path rather than the image-resolution
    refusal."""
    from botainer.cli.init import do_init
    do_init(proj, agent="claude", runtime="mock", quiet=True)
    cfg = proj / ".botainer" / "config.yaml"
    cfg.write_text(cfg.read_text() + "\nimage: ubuntu:24.04\n")


def test_auth_profile_override_reaches_the_real_hooks(tmp_path, monkeypatch):
    """The observation that decides it: does a profile dir actually appear?

    Driven through compose_session + run_pre_session_hooks — the real path — not
    by invoking the hook with an env var, which is exactly the mistake that hid
    this bug for months.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    _init_composable(proj)
    _write(tmp_path / "state" / "shared-auth" / "agent-claude" / ".credentials.json",
           _cred("A", _now_ms() + HOUR_MS))

    from botainer.core import composition
    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=True,
        auth_profile_override="work")
    assert spec.profile == "work"
    composition.run_pre_session_hooks(spec)

    base = Path(spec.state_dir) / "data" / "agent-claude" / "profiles"
    assert base.is_dir() and "work" in {p.name for p in base.iterdir()}, (
        "--auth-profile did not reach the hooks; they used `default`"
    )


def test_no_auth_profile_still_means_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    _init_composable(proj)
    from botainer.core import composition
    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=True)
    assert spec.profile == "default"


def test_the_override_never_touches_disk(tmp_path, monkeypatch):
    """One-shot means one-shot.

    The auth-MODE override learned this the hard way: eager config mutation plus
    an atexit restore cannot survive SIGKILL/OOM/exec, and leaves the project
    silently switched. A profile switched on disk would be worse — it points at
    another ACCOUNT.
    """
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    _init_composable(proj)
    cfg = proj / ".botainer" / "config.yaml"
    before = cfg.read_text()

    from botainer.core import composition
    composition.compose_session(proj, runtime_choice="mock",
                                identity_accept=True, auth_profile_override="work")
    assert cfg.read_text() == before, "the override was written to config.yaml"


def test_a_path_hostile_profile_is_refused(tmp_path, monkeypatch):
    """The profile becomes a directory component and an apptainer bind source."""
    monkeypatch.setenv("MY_BOTAINER", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    _init_composable(proj)
    from botainer.core import composition
    for bad in ("../escape", "a/b", "", "WORK", "x" * 64):
        # match= on purpose: a bare pytest.raises(Exception) stays green if the
        # code fails for an UNRELATED reason (a missing image, a bad fixture),
        # which is how a validation test comes to assert nothing at all.
        with pytest.raises(ValueError, match=r"must match \^\[a-z\]"):
            composition.compose_session(
                proj, runtime_choice="mock", identity_accept=True,
                auth_profile_override=bad)

    # And the counterpart: a legitimate name is NOT refused, so the check above
    # is not passing because everything is rejected.
    spec = composition.compose_session(
        proj, runtime_choice="mock", identity_accept=True,
        auth_profile_override="work-2")
    assert spec.profile == "work-2"
