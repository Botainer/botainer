#!/usr/bin/env python3
"""agent-codex-shared pre_session: selective-bind contributor.

Same architecture as agent-claude-shared (see that file's docstring
for the dir-bind + symlink rationale). Substitutes:
  ~/.botainer/shared-auth/agent-codex/auth.json (host)
  /home/agent/.codex (container per-project dir)
  /shared-auth/agent-codex (container shared bind)
"""
from __future__ import annotations

import json
import contextlib
import os
import sys
from pathlib import Path


# The per-project configuration bind and CODEX_HOME must agree. Without the
# variable, Codex searches the ordinary HOME for .codex and can miss the
# credential correctly mounted at /home/agent/.codex.
#
# Its sibling agent-claude-shared has always contributed CLAUDE_CONFIG_DIR, and
# agent-codex-broker has always contributed CODEX_HOME; only this plugin — the
# DEFAULT codex auth mode — contributed "env": {}. A bind is delivery, not
# discovery. See tests/unit/test_agent_config_env_contract.py, which runs every
# agent plugin's hook and asserts the pair agrees.
_CONTAINER_CODEX_HOME = "/home/agent/.codex"


def _last_refresh(data: object) -> str:
    """Codex's own record of when this credential was last refreshed, or "".

    UNVERIFIED AGAINST A REAL FILE. `last_refresh` is present in the codex
    auth.json shapes we have seen, but the format is undocumented and this was
    written without one to hand. So it is read defensively and its ABSENCE is
    not treated as an error — only as "no ordering signal available".

    ISO-8601 strings compare correctly with `<` when they share a format, which
    is why no parsing is attempted: parsing would introduce a failure mode
    (a malformed date) worse than the ambiguity it resolves.
    """
    if not isinstance(data, dict):
        return ""
    v = data.get("last_refresh")
    return v if isinstance(v, str) else ""


def _is_newer(per_project: object, shared: object) -> bool:
    """May the per-project credential replace the shared one?

    TRUE only when we can SEE that it is newer, or when neither side carries an
    ordering signal (which is the pre-existing behaviour and the common case).
    FALSE when the shared copy is demonstrably newer — the case that used to
    silently roll a good token backwards.

    Deliberately permissive when BOTH lack `last_refresh`: refusing there would
    break every back-fill on a codex build that omits the field, turning a
    "might overwrite a newer token" bug into a "never propagates a refresh"
    bug, which is the failure this whole mechanism exists to prevent.
    """
    mine, theirs = _last_refresh(per_project), _last_refresh(shared)
    if not mine or not theirs:
        return True          # no signal on one side: unchanged behaviour
    return mine > theirs


def _oauth_identity(data: object) -> tuple[str, str, str] | None:
    """Return (access_token, refresh_token, account_id) iff `data` is a FULL
    codex ChatGPT-OAuth bundle — all three fields present and non-trivial — else
    None.

    This gates refresh-back-fill (overwriting the host-wide SHARED credential
    from the per-project file). Codex Priority-A MEDIUM: the earlier recognizer
    accepted ANY dict with `OPENAI_API_KEY` OR `tokens.access_token` of length
    >= 20, so a caged agent (which can write the per-project `auth.json`) could
    forge one and poison the shared credential. Two tightenings:
      1. API-key shape is NOT back-fillable at all — an API key is never
         refreshed inside the container, so there is no legitimate reason to
         overwrite the shared credential from one. (It still works for the live
         session via the bind; it just can't propagate back.)
      2. OAuth requires the full {access_token, refresh_token, account_id}
         shape, and the CALLER additionally requires account_id to MATCH the
         existing shared credential before overwriting (a refresh of the SAME
         account, not a swap to an attacker's). If identity can't be verified,
         the caller backs up rather than back-fills.
    Codex Pro/Max store real access/refresh tokens (JWT/opaque, hundreds of
    chars); the >= 20 floor rejects trivial placeholders without asserting an
    exact format. Residual: a same-account garbage write is a self-inflicted DoS
    recoverable by re-login (accepted per the finding)."""
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    at = tokens.get("access_token")
    rt = tokens.get("refresh_token")
    acct = tokens.get("account_id")
    if (isinstance(at, str) and len(at) >= 20
            and isinstance(rt, str) and len(rt) >= 20
            and isinstance(acct, str) and acct):
        return (at, rt, acct)
    return None


def main() -> int:
    uid = os.environ.get("BOTAINER_PROJECT_UUID", "")
    state_root = Path(os.environ.get("BOTAINER_STATE_ROOT", str(Path.home() / ".botainer")))
    profile = os.environ.get("BOTAINER_PROFILE", "default")
    if not uid:
        sys.stderr.write("agent-codex-shared: no BOTAINER_PROJECT_UUID\n")
        return 1

    per_project_dir = (
        state_root / "state" / uid / "data" / "agent-codex" / "profiles" / profile
    )
    per_project_dir.mkdir(parents=True, exist_ok=True)

    shared_dir = state_root / "shared-auth" / "agent-codex"
    shared_file = shared_dir / "auth.json"

    if not shared_file.exists():
        sys.stderr.write(
            f"agent-codex-shared: no shared credential at {shared_file}\n"
            f"Run: botainer auth login --shared --agent codex\n"
            f"(Or switch to isolated mode: botainer auth use isolated --family openai)\n"
        )
        return 2

    st = shared_file.stat()
    if st.st_uid != os.getuid():
        sys.stderr.write(
            f"agent-codex-shared: refusing to mount shared credential "
            f"owned by uid={st.st_uid} (expected {os.getuid()})\n"
        )
        return 3
    if (st.st_mode & 0o077) != 0:
        sys.stderr.write(
            f"agent-codex-shared: refusing to mount shared credential "
            f"with mode {oct(st.st_mode & 0o777)} (expected 0600). "
            f"Fix: chmod 600 {shared_file}\n"
        )
        return 3

    rc = reconcile_shared_credential(per_project_dir, shared_dir)
    if rc != 0:
        return rc

    # F6: explanatory README so the host-side dangling symlink doesn't
    # confuse audit / investigation tooling.
    return _finish(per_project_dir, shared_dir)


def reconcile_shared_credential(per_project_dir: Path, shared_dir: Path) -> int:
    """Point this project's auth.json at the shared store, back-filling first
    if the project holds a NEWER same-account credential.

    Extracted so `post_session` can run the SAME logic at session
    EXIT. Codex had no exit reconcile at all, so a token refreshed inside the
    container sat in that project until its NEXT start — and a different
    project starting in between inherited the stale one and reported a login
    failure. claude got the exit hook on; this is the counterpart
    that was never written.

    Returns a process exit code: 0 on success, non-zero on the conditions the
    caller must surface. post_session maps every non-zero to 0, because a
    session that already finished must not be reported as failed.
    """
    shared_file = shared_dir / "auth.json"
    creds_symlink = per_project_dir / "auth.json"
    expected_target = "/shared-auth/agent-codex/auth.json"
    needs_update = False
    if not creds_symlink.is_symlink():
        if creds_symlink.exists():
            # Refresh-back-fill heuristic (mirrors agent-claude-shared):
            # if per-project file is newer than shared, copy its content
            # back to shared before backup (preserves refresh tokens
            # that Codex inside container may have written via atomic-
            # replace which destroyed the symlink).
            # Sharp-edges F4: content-validation back-fill instead of
            # mtime (NFS clock skew + attacker `touch` poisoning).
            # P3a + Codex Priority-A MEDIUM: only back-fill a VERIFIABLE
            # same-account OAuth refresh. API-key shape is never back-fillable,
            # and an OAuth file whose account_id doesn't match the existing
            # shared credential is a possible swap attack — back up, don't
            # overwrite the host-wide shared credential.
            import fcntl as _fcntl
            try:
                per_project_data = json.loads(creds_symlink.read_text())
                shared_data = json.loads(shared_file.read_text())
            except (OSError, json.JSONDecodeError):
                per_project_data = None
                shared_data = None
            per_id = _oauth_identity(per_project_data)
            shared_id = _oauth_identity(shared_data)
            backfillable = (
                per_id is not None
                and shared_id is not None
                and per_id[2] == shared_id[2]  # same account_id = a refresh, not a swap
            )
            if not backfillable:
                sys.stderr.write(
                    "[agent-codex-shared] per-project credential is not a "
                    "verifiable same-account OAuth refresh (API-key shape, "
                    "malformed, or a different account_id); backing up rather "
                    "than overwriting the shared credential.\n"
                )
                backup = per_project_dir / "auth.json.pre-shared"
                try:
                    creds_symlink.rename(backup)
                except OSError as exc:
                    sys.stderr.write(f"could not back up: {exc}\n")
                    return 4
            elif per_project_data != shared_data and _is_newer(
                    per_project_data, shared_data):
                # Codex auth.json format isn't documented; content inequality
                # is the "shared is stale" signal, now GATED by _is_newer so an
                # older token cannot overwrite a newer one (claude has required
                # a strictly-later expiry since it was written; codex never
                # gained the equivalent).
                import shutil as _shutil
                # Task #267: PID-named tmp leaks on crash. Use random hex
                # so there's no PID-reuse collision; same dir means rename
                # is atomic; chmod 0o600 still applies.
                tmp = shared_file.with_suffix(
                    ".json.tmp." + os.urandom(8).hex()
                )
                _shutil.copyfile(creds_symlink, tmp)
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                try:
                    with open(shared_file, "rb") as lock_handle:
                        _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_EX)
                        tmp.rename(shared_file)
                except OSError as exc:
                    # FAIL CLOSED (#266 parity). This used to fall back to an
                    # UNLOCKED rename — doing the dangerous thing precisely
                    # when the safety mechanism is unavailable. Two codex
                    # sessions back-filling at once would then both spend the
                    # rotating refresh token, which is what trips the
                    # provider's reuse lockout and can lock the account.
                    # claude has refused this since; codex kept the
                    # fallback for 69 days because the fix was never mirrored.
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                    sys.stderr.write(
                        f"[agent-codex-shared] Could not safely update your "
                        f"shared login: this filesystem does not support file "
                        f"locking ({exc}).\n"
                        f"  Your existing login is untouched. This project's "
                        f"newer token was NOT copied to it.\n"
                        f"  Fix: put your botainer state on a local disk. On a "
                        f"cluster that usually means setting MY_BOTAINER to a "
                        f"local path rather than an NFS home.\n"
                    )
                    return 4
                sys.stderr.write(
                    "[agent-codex-shared] back-filled shared credential "
                    "(per-project content differs)\n"
                )
                creds_symlink.unlink()
            else:
                # Content matches shared; treat as Mode-A leftover and
                # back it up so the symlink can be created cleanly.
                backup = per_project_dir / "auth.json.pre-shared"
                try:
                    creds_symlink.rename(backup)
                    sys.stderr.write(
                        f"[agent-codex-shared] backed up per-project credential "
                        f"to {backup}\n"
                    )
                except OSError as exc:
                    sys.stderr.write(f"could not back up: {exc}\n")
                    return 4
        needs_update = True
    else:
        try:
            current = os.readlink(creds_symlink)
            if current != expected_target:
                needs_update = True
        except OSError:
            needs_update = True

    if needs_update:
        try:
            if creds_symlink.exists() or creds_symlink.is_symlink():
                creds_symlink.unlink()
            os.symlink(expected_target, creds_symlink)
        except OSError as exc:
            sys.stderr.write(
                f"agent-codex-shared: symlink {creds_symlink} → {expected_target}: {exc}\n"
            )
            return 5
    return 0


def _finish(per_project_dir: Path, shared_dir: Path) -> int:
    """Emit the README + the bind contribution. pre_session only."""
    readme_path = per_project_dir / "README.shared-mode.txt"
    _readme_text = (
        "This project uses shared-mode OpenAI credentials.\n"
        "\n"
        "auth.json here is a SYMLINK pointing at\n"
        "/shared-auth/agent-codex/auth.json — an IN-CONTAINER path.\n"
        "\n"
        "The symlink is DANGLING when viewed from the host filesystem,\n"
        "by design. To inspect credentials on host:\n"
        "  ls -la ~/.botainer/shared-auth/agent-codex/auth.json\n"
        "\n"
        "To switch this project to per-project credentials:\n"
        "  botainer auth use isolated --family openai\n"
    )
    # T1-6: create atomically with O_EXCL|O_NOFOLLOW. per_project_dir is bound RW
    # INTO the container, so a prior caged session could plant a dangling symlink
    # named README.shared-mode.txt to redirect this UNCAGED host write (`if not
    # exists(): write_text()` follows a dangling symlink). O_EXCL refuses an
    # existing file; O_NOFOLLOW refuses a symlink.
    try:
        _fd = os.open(
            str(readme_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600,
        )
    except (FileExistsError, OSError):
        pass
    else:
        with os.fdopen(_fd, "w") as _f:
            _f.write(_readme_text)

    contribution = {
        "version": "plugin-contribution-v1",
        "binds": [
            {
                "source": str(per_project_dir),
                "target": _CONTAINER_CODEX_HOME,
                "mode": "rw",
                "provenance_detail": (
                    "agent-codex-shared (Mode B): per-project state. "
                    "Contains symlink auth.json → /shared-auth/agent-codex/."
                ),
                "self_test": "SELFTEST_EXTRA_BIND",
            },
            {
                "source": str(shared_dir),
                "target": "/shared-auth/agent-codex",
                "mode": "rw",
                "provenance_detail": (
                    "agent-codex-shared (Mode B): host-wide shared OpenAI "
                    "credential. CROSS-PROJECT visibility."
                ),
                "self_test": "SELFTEST_EXTRA_BIND",
            },
        ],
        # Delivery is not discovery. The bind above puts the credential in the
        # container; this is what makes codex LOOK there instead of resolving
        # ~/.codex against the session HOME, which is a different directory and
        # is empty.
        "env": {
            "CODEX_HOME": _CONTAINER_CODEX_HOME,
        },
    }
    sys.stdout.write(json.dumps(contribution))
    return 0


if __name__ == "__main__":
    sys.exit(main())
