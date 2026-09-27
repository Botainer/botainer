#!/usr/bin/env python3
"""agent-claude-shared pre_session hook.

Mode B (shared) selective-bind contributor.

Per internal design note DN-040 §3:
- bind per-project state-claude dir at /home/agent/.claude (rw)
- ALSO bind shared-auth dir at /shared-auth/agent-claude (rw)
- Per-project state dir contains a SYMLINK at .credentials.json that
  points at /shared-auth/agent-claude/.credentials.json
- Inside container, the symlink resolves to the shared file via the
  second bind. Atomic-replace of the shared file (by Claude refresh)
  works because we re-resolve through the symlink each time.

Why not a file-bind overlay? Bind-mounting a single file pins to the
SOURCE inode at bind-create time. If Claude inside the container does
a tmp-write + rename for atomic refresh, the bind becomes invalid
(the new file has a new inode; the bind still points at the orphaned
old one). Container and host see DIFFERENT files. Dir-bind + symlink
avoids this because directory operations (including rename) survive
the bind.

The symlink is created at `botainer init` for shared mode (when
init.py writes the per-project state dir, it adds the symlink).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# internal design note DN-040 §4 (agent_home_allowlist): the shared-auth dir
# should hold ONLY the credential file. Anything else is either a
# `claude /login` residue (login.py runs in a tempdir now, so this
# should be rare) or future state from `claude` that we haven't
# accounted for. We warn on unknown entries so a silent leak is
# caught the first session after it appears.
_SHARED_ALLOWLIST = {
    ".credentials.json",
    # BOTAINER'S OWN FILES. Every entry below is created by botainer itself, so
    # warning about them is warning the user about us.
    #
    # `.claude.json` — written into this dir by the login flow and symlinked
    # into each project by THIS HOOK (see the `claude_json_link` block). It
    # carries Claude Code's account state, without which a session sees the
    # token but no account and prompts for login.
    ".claude.json",
    # the backup this hook makes when it finds a stale regular `.claude.json`.
    ".claude.json.pre-shared",
    # the backup reconcile_shared_credential makes when a per-project
    # credential fails the anti-poisoning checks.
    ".credentials.json.pre-shared",
    # `.login-residue/` is the legacy quarantine dir from the old
    # login.py. It pre-dates the tempdir refactor; left in the
    # allowlist so existing installs don't bombard the user with
    # warnings if they have one.
    ".login-residue",
}

# The broker's OAuth refresh mutex, `refresh_lock._lockfile_path`:
#   cred_path.with_name(f".{cred_path.name}.refresh.lock")
# i.e. `..credentials.json.refresh.lock` here. Matched by SUFFIX rather than
# listed literally, because the name is derived from the credential filename
# and would drift the moment that changed — the two live in different packages
# and nothing else ties them together.
_SHARED_ALLOWED_SUFFIXES = (".refresh.lock",)

# Per-project allowlist: anything `claude` writes inside the container
# at /home/agent/.claude/ is fine. Listed for completeness; we only
# warn on things outside this set. Generated from a survey of a recent
# Claude Code install (per internal design note DN-040 §3 table).
_PER_PROJECT_ALLOWLIST = {
    ".credentials.json",   # the symlink we manage
    ".credentials.json.pre-shared",  # back-up from mode-A → mode-B migration
    ".claude.json",   # symlink to shared (Claude v2.x account state)
    ".claude.json.pre-shared",  # back-up if a stale regular file was there
    "README.shared-mode.txt",  # documentation we drop
    "history.jsonl",
    "sessions",
    "projects",
    "file-history",
    "paste-cache",
    "plugins",
    "settings.json",
    "mcp-needs-auth-cache.json",
    "shell-snapshots",
    "tasks",
    "stats-cache.json",
    "telemetry",
    "backups",
    "cache",
    "session-env",
    ".last-cleanup",
}


def _warn_unknown_in_dir(
    dir_path: Path, allowlist: set[str], label: str,
    allowed_suffixes: tuple[str, ...] = (),
) -> None:
    """Warn on dir entries not in the allowlist. Best-effort; never fails.

    THIS HAS BEEN FIRING ON EVERY SHARED SESSION and nobody saw it, because
    hook stderr was captured and discarded (#138). Surfacing that stderr
    revealed the allowlist was missing `.claude.json` — a file THIS HOOK
    symlinks into the shared dir itself — and the broker's refresh lockfile.
    So the first thing the new warning channel did was warn about botainer.

    That is the point of a warning channel and also its danger: a warn that
    fires every time is a bug or a lie, never scenery, because it trains
    everyone to ignore the channel and the next real finding goes with it.
    If this starts firing on every run again, the allowlist is stale — fix
    the allowlist, do not mute the warning.
    """
    try:
        entries = sorted(p.name for p in dir_path.iterdir())
    except OSError:
        return
    unknown = [
        n for n in entries
        if n not in allowlist and not n.endswith(allowed_suffixes or ("\0",))
    ]
    if unknown:
        sys.stderr.write(
            f"[agent-claude-shared] WARN: unexpected file(s) in {label} "
            f"({dir_path}): {', '.join(unknown)}\n"
            f"  If Claude Code added new state we're not aware of, ensure "
            f"it doesn't contain cross-project sensitive data. File an issue "
            f"so we can update the allowlist.\n"
        )




class _ReconcileError(RuntimeError):
    """Reconcile failed. `code` is the exit status pre_session used to use."""

    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code


def reconcile_shared_credential(per_project_dir: Path, shared_dir: Path) -> str:
    """Make the per-project `.credentials.json` a symlink into the shared store,
    back-filling first if the local file holds a NEWER token.

    Extracted from `main` so it can also run at session EXIT.

    WHY IT HAS TO RUN TWICE. Claude Code refreshes the token with a temp file
    plus `rename()`, and `rename()` REPLACES a symlink with a regular file. So
    the first in-container refresh silently detaches the project from the shared
    store: that project keeps the fresh token locally, `/shared-auth/` keeps the
    stale one. Running only at START meant the repair happened the next time you
    started THAT SAME project — so a NEW project inherited the stale token and
    reported "login expired" while the old one kept working. Two projects,
    identical config, opposite behavior.

    In-place writes preserve a symlink; atomic replacement leaves a regular
    file. Reconciliation must handle both shapes without relying on how a
    previous client version wrote the credential.

    Returns "backfilled" | "relinked" | "ok". Raises _ReconcileError on the two
    conditions that used to be bare `return 4` / `return 5`, so each caller can
    choose: pre_session aborts the launch, post_session must not (the session
    already ran; failing there would report a successful session as failed).

    NO NEW TRUST SURFACE: this is the existing, already-reviewed logic verbatim,
    including the #158 anti-poisoning checks. Only WHEN it runs changed.
    """
    shared_file = shared_dir / ".credentials.json"
    # Ensure the per-project symlink exists. CRITICAL: if a previous
    # session's Claude did atomic-replace from inside container, the
    # symlink got destroyed and replaced with a regular file holding
    # the NEW refreshed credentials. The shared file would still have
    # the OLD content. We MUST copy that newer content back to shared
    # before recreating the symlink, or we lose the refresh.
    creds_symlink = per_project_dir / ".credentials.json"
    expected_target = "/shared-auth/agent-claude/.credentials.json"
    needs_update = False
    if not creds_symlink.is_symlink():
        if creds_symlink.exists():
            # There's a regular file. Two cases:
            #   1. Mode-A → Mode-B migration: per-project file is the
            #      user's last-known-good before switching modes.
            #   2. Previous session atomic-replace: this file is NEWER
            #      than the shared file.
            # Heuristic: compare mtimes. If per-project is newer, copy
            # its content into the shared file (refresh-back-fill); if
            # not, treat as Mode-A migration and back up.
            # Sharp-edges F4 + insecure-defaults M7: don't trust mtime
            # alone (FS granularity, NFS clock skew, attacker `touch`).
            # Validate CONTENT looks like a real Claude credential AND
            # has a strictly later expiresAt before back-filling.
            # Also use O_NOFOLLOW + flock to avoid TOCTOU + races.
            import fcntl as _fcntl
            try:
                per_project_data = json.loads(creds_symlink.read_text())
                shared_data = json.loads(shared_file.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                sys.stderr.write(
                    f"[agent-claude-shared] Could not read one of the login "
                    f"files ({exc}).\n"
                    f"  Your other projects keep the login they already had. "
                    f"This project's copy has been set aside rather than "
                    f"shared, in case it was damaged.\n"
                    f"  If this project asks you to log in, that is why.\n"
                )
                per_project_data = None
                shared_data = None
            per_oauth = (per_project_data or {}).get("claudeAiOauth", {}) \
                if isinstance(per_project_data, dict) else {}
            shared_oauth = (shared_data or {}).get("claudeAiOauth", {}) \
                if isinstance(shared_data, dict) else {}
            per_exp = per_oauth.get("expiresAt", 0) if isinstance(per_oauth, dict) else 0
            shared_exp = shared_oauth.get("expiresAt", 0) if isinstance(shared_oauth, dict) else 0
            # Refresh-back-fill ONLY if per-project credential has the
            # expected shape AND a strictly-later expiresAt. mtime
            # comparison removed (untrustworthy on NFS / attacker-tamper).
            # Task #158: harden against per-project → host-wide credential
            # poisoning. A hostile in-container agent can write a forged
            # credential file with shape-conformant accessToken /
            # refreshToken / large expiresAt; without further checks, the
            # next pre_session would back-fill the forgery into the shared
            # store and every OTHER project using shared-mode would inherit
            # it. Tighten validation:
            #   1. accessToken must match Claude OAuth prefix (sk-ant-oat*)
            #   2. refreshToken must match (sk-ant-ort*)
            #   3. Both tokens must be >= 64 chars (forgeries with random
            #      short strings are caught)
            #   4. expiresAt delta over shared must be <= 60 days (refusing
            #      'expires in 100 years' style poisoning)
            _at = per_oauth.get("accessToken") if isinstance(per_oauth, dict) else ""
            _rt = per_oauth.get("refreshToken") if isinstance(per_oauth, dict) else ""
            _MAX_REFRESH_DELTA_MS = 60 * 24 * 60 * 60 * 1000
            looks_real = (
                isinstance(per_oauth, dict)
                and isinstance(_at, str) and len(_at) >= 64
                and _at.startswith("sk-ant-oat")
                and isinstance(_rt, str) and len(_rt) >= 64
                and _rt.startswith("sk-ant-ort")
                and isinstance(per_exp, int) and per_exp > 0
                and (per_exp - max(shared_exp, 0)) <= _MAX_REFRESH_DELTA_MS
            )
            if not looks_real:
                # Fall through to the else-branch (backup as Mode-A leftover).
                sys.stderr.write(
                    "[agent-claude-shared] This project's login file is not "
                    "in the shape a Claude login normally has.\n"
                    "  It has NOT been copied to your shared login, so your "
                    "other projects are unaffected.\n"
                    "  The file was set aside as .credentials.json.pre-shared. "
                    "If this project asks you to log in, that is why.\n"
                )
                backup = per_project_dir / ".credentials.json.pre-shared"
                try:
                    creds_symlink.rename(backup)
                except OSError as exc:
                    sys.stderr.write(f"could not back up: {exc}\n")
                    raise _ReconcileError(4, "could not back up the pre-shared credential")
            elif per_exp > shared_exp:
                # Task #267: was \`.tmp.<pid>\` which leaks on crash (PID
                # reuse + persistent files). Use tempfile.NamedTemporaryFile
                # with delete-on-close so a crash mid-swap doesn't strand
                # the credential content on disk.
                import shutil as _shutil
                import tempfile as _tempfile
                with _tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=shared_file.parent,
                    prefix=".credentials.",
                    suffix=".tmp",
                    delete=False,
                ) as _tf:
                    tmp = Path(_tf.name)
                _shutil.copyfile(creds_symlink, tmp)
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                # Task #266: previously this fell through to an unlocked
                # rename on OSError. flock unsupported by the filesystem
                # (NFS without lockd, some FUSE setups) defeated the
                # ENTIRE concurrency control. A second process racing on
                # the same shared cred would silently clobber the rename.
                # Now: refuse the back-fill if the lock can't be acquired
                # so the user knows the operation was unsafe.
                try:
                    with open(shared_file, "rb") as lock_handle:
                        _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_EX)
                        tmp.rename(shared_file)
                except OSError as exc:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    sys.stderr.write(
                        f"[agent-claude-shared] Could not safely update your "
                        f"shared login: this filesystem does not support file "
                        f"locking on "
                        f"{shared_file} failed ({exc}).\n"
                        f"  Locking is required here because two sessions "
                        f"updating the login at once can invalidate it for "
                        f"both.\n"
                        f"  Fix: put your botainer state on a local disk. On a "
                        f"cluster that usually means setting MY_BOTAINER to a "
                        f"local path rather than an NFS home.\n"
                    )
                    raise _ReconcileError(4, "could not back up the pre-shared credential")
                sys.stderr.write(
                    f"[agent-claude-shared] This session's agent refreshed "
                    f"its login token, and the new token has been copied to "
                    f"your SHARED login.\n"
                    f"  Why: refreshing replaces the old token and makes it "
                    f"invalid. Without this copy your other projects would "
                    f"keep the old one and be logged out.\n"
                    f"  Affects: every project using shared mode on this "
                    f"machine.\n"
                    f"  No action needed — this is botainer keeping your "
                    f"projects in sync.\n"
                )
                creds_symlink.unlink()
            else:
                # Older or equal: probably a Mode-A leftover. Back it up.
                backup = per_project_dir / ".credentials.json.pre-shared"
                try:
                    creds_symlink.rename(backup)
                    sys.stderr.write(
                        f"[agent-claude-shared] This project had its own "
                        f"login file, older than your shared one.\n"
                        f"  It was set aside and the project now uses the "
                        f"shared login.\n"
                        f"  No action needed.\n"
                        f"  Set aside at: {backup}\n"
                    )
                except OSError as exc:
                    sys.stderr.write(f"could not back up: {exc}\n")
                    raise _ReconcileError(4, "could not back up the pre-shared credential")
        needs_update = True
    else:
        # Symlink exists; verify target.
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
                f"agent-claude-shared: could not create symlink "
                f"{creds_symlink} -> {expected_target}: {exc}\n"
            )
            raise _ReconcileError(5, "could not create the credential symlink")

    return "relinked" if needs_update else "ok"

def main() -> int:
    uid = os.environ.get("BOTAINER_PROJECT_UUID", "")
    state_root = Path(os.environ.get("BOTAINER_STATE_ROOT", str(Path.home() / ".botainer")))
    profile = os.environ.get("BOTAINER_PROFILE", "default")
    if not uid:
        sys.stderr.write("agent-claude-shared pre_session: no BOTAINER_PROJECT_UUID in env\n")
        return 1

    # Per-project state dir (history, sessions, claude.json, etc.).
    per_project_dir = (
        state_root / "state" / uid / "data" / "agent-claude" / "profiles" / profile
    )
    per_project_dir.mkdir(parents=True, exist_ok=True)

    # Shared auth dir on this host.
    shared_dir = state_root / "shared-auth" / "agent-claude"
    shared_file = shared_dir / ".credentials.json"

    # internal design note DN-040 §4: warn on unknown files in either dir.
    # Cheap; runs once per session start. Caught now → not a silent
    # cross-project leak next release.
    _warn_unknown_in_dir(shared_dir, _SHARED_ALLOWLIST, "shared-auth dir",
                         _SHARED_ALLOWED_SUFFIXES)
    _warn_unknown_in_dir(per_project_dir, _PER_PROJECT_ALLOWLIST, "per-project state dir")

    if not shared_file.exists():
        # User hasn't logged in shared-mode yet. Refuse with a clear
        # message + a hint pointing at the login command.
        sys.stderr.write(
            f"agent-claude-shared: no shared credential at {shared_file}\n"
            f"Run: botainer auth login --shared --agent claude\n"
            f"(Or switch this project back to isolated mode: "
            f"botainer auth use isolated)\n"
        )
        return 2

    # Validate mode/ownership of the shared file (the proxy does
    # similar validation; we mirror it here for mount-mode security).
    st = shared_file.stat()
    if st.st_uid != os.getuid():
        sys.stderr.write(
            f"agent-claude-shared: refusing to mount shared credential "
            f"file owned by uid={st.st_uid} (expected {os.getuid()})\n"
        )
        return 3
    if (st.st_mode & 0o077) != 0:
        sys.stderr.write(
            f"agent-claude-shared: refusing to mount shared credential "
            f"file with mode {oct(st.st_mode & 0o777)} (expected 0600 or stricter). "
            f"Fix: chmod 600 {shared_file}\n"
        )
        return 3

    # Reconcile the shared credential (symlink + back-fill). See
    # reconcile_shared_credential for why this must also run at session exit.
    try:
        reconcile_shared_credential(per_project_dir, shared_dir)
    except _ReconcileError as exc:
        sys.stderr.write(f"agent-claude-shared: {exc}\n")
        return exc.code

    # Also symlink `.claude.json`. Claude Code v2.x stores authentication
    # STATE (user email, subscription tier, "logged in as X" flag) in
    # `.claude.json` — distinct from the OAuth token in `.credentials.json`.
    # Without the shared .claude.json, a session container sees the OAuth
    # token but no account state and re-prompts for login.
    #
    # The shared .claude.json is written by login.py's docker session
    # (because we point CLAUDE_CONFIG_DIR at the shared dir during login).
    # Sharing this file across projects means user-level Claude settings
    # are host-wide; per-project conversation state (sessions/, history.jsonl,
    # projects/) stays isolated under the per-project dir.
    #
    # Refresh logic is simpler than for .credentials.json: Claude doesn't
    # atomic-replace .claude.json mid-session in a way we need to back-fill.
    # If a per-project regular file exists (probably written by a session
    # container before this fix shipped), back it up so the symlink can be
    # created cleanly.
    claude_json_link = per_project_dir / ".claude.json"
    claude_json_target = "/shared-auth/agent-claude/.claude.json"
    shared_claude_json = shared_dir / ".claude.json"
    if shared_claude_json.exists():
        needs_link = False
        if claude_json_link.is_symlink():
            try:
                if os.readlink(claude_json_link) != claude_json_target:
                    needs_link = True
            except OSError:
                needs_link = True
        elif claude_json_link.exists():
            backup = per_project_dir / ".claude.json.pre-shared"
            try:
                claude_json_link.rename(backup)
                needs_link = True
            except OSError as exc:
                sys.stderr.write(
                    f"[agent-claude-shared] could not back up stale "
                    f".claude.json: {exc}\n"
                )
                return 6
        else:
            needs_link = True
        if needs_link:
            try:
                if claude_json_link.is_symlink():
                    claude_json_link.unlink()
                os.symlink(claude_json_target, claude_json_link)
            except OSError as exc:
                sys.stderr.write(
                    f"agent-claude-shared: could not create symlink "
                    f"{claude_json_link} -> {claude_json_target}: {exc}\n"
                )
                return 5

    # Sharp-edges F6: the symlink target is an IN-CONTAINER path
    # (/shared-auth/agent-claude/.credentials.json), which doesn't
    # exist on the HOST. `ls -L` / `find -L` from the host see it
    # as dangling. Drop a README explaining this so audit / inventory
    # tools don't lie about credentials being absent.
    readme_path = per_project_dir / "README.shared-mode.txt"
    readme_text = (
        "This project uses shared-mode Claude credentials.\n"
        "\n"
        "Two files here are SYMLINKS to /shared-auth/agent-claude/ —\n"
        "an IN-CONTAINER path that only resolves when the agent's\n"
        "container is running with both the per-project bind (this dir)\n"
        "AND the shared-auth bind (~/.botainer/shared-auth/agent-claude/):\n"
        "\n"
        "  .credentials.json  → OAuth token (refreshed in-container)\n"
        "  .claude.json       → account state (user, subscription)\n"
        "\n"
        "Both symlinks are DANGLING when viewed from the host filesystem,\n"
        "by design. Don't 'fix' them.\n"
        "\n"
        "To inspect credentials on host:\n"
        "  ls -la ~/.botainer/shared-auth/agent-claude/.credentials.json\n"
        "\n"
        "To switch this project to per-project credentials:\n"
        "  botainer auth use isolated --family anthropic\n"
        "\n"
        "Run `botainer auth doctor` to see every credential holder.\n"
    )
    # T1-6: create the README atomically with O_EXCL|O_NOFOLLOW. per_project_dir
    # is bound RW INTO the container, so a prior caged session could plant a
    # (dangling) symlink named README.shared-mode.txt to redirect this UNCAGED
    # host-side write to an arbitrary path — `if not exists(): write_text()`
    # follows a dangling symlink (exists() is False for it). O_EXCL refuses an
    # existing file; O_NOFOLLOW refuses a symlink. This is the one session-launch
    # host write that lacked the O_NOFOLLOW guard used everywhere else.
    try:
        _fd = os.open(
            str(readme_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600,
        )
    except (FileExistsError, OSError):
        pass  # already present, or path is a symlink (refused) — best-effort
    else:
        with os.fdopen(_fd, "w") as _f:
            _f.write(readme_text)

    # Contribute two binds: per-project state dir, shared-auth dir.
    contribution = {
        "version": "plugin-contribution-v1",
        "binds": [
            {
                "source": str(per_project_dir),
                "target": "/home/agent/.claude",
                "mode": "rw",
                "provenance_detail": (
                    "agent-claude-shared (Mode B): per-project state dir "
                    "(history, sessions, claude.json). Contains a symlink "
                    ".credentials.json → /shared-auth/agent-claude/."
                ),
                "self_test": "SELFTEST_EXTRA_BIND",
            },
            {
                "source": str(shared_dir),
                "target": "/shared-auth/agent-claude",
                "mode": "rw",
                "provenance_detail": (
                    "agent-claude-shared (Mode B): host-wide shared Anthropic "
                    "credentials. Symlink target for /home/agent/.claude/"
                    ".credentials.json. CROSS-PROJECT and READ-WRITE: a "
                    "compromised agent in ANY shared-mode project can read AND "
                    "OVERWRITE this, which would change the account every "
                    "shared-mode project uses. rw is required because the "
                    "agent writes its refreshed token back here."
                ),
                "self_test": "SELFTEST_EXTRA_BIND",
            },
        ],
        "env": {
            "CLAUDE_CONFIG_DIR": "/home/agent/.claude",
        },
    }
    sys.stdout.write(json.dumps(contribution))
    return 0


if __name__ == "__main__":
    sys.exit(main())
