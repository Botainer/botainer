"""Project identity: UUID + path history + clone/fork handling.

A project has a stable UUID. The UUID is stored in `Main/.botainer/project-id`
(small, git-shareable file). Path history is stored host-only in
`~/.botainer/state/<uuid>/meta.json`.

Same-machine clone/fork (codex HIGH 3):
- If the UUID's `path_history` contains a *different live path*, prompt:
  "moved checkout / new independent copy / abort".
- `--accept-identity-change` accepts non-interactively (moved).
- "new independent copy" mints a new UUID, rewrites `project-id`.
"""

from __future__ import annotations

import datetime
import sys
import unicodedata
import uuid as _uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from botainer.core.refusal import RefusalCategory, Refused
from botainer.state import dir as state_dir

PROJECT_ID_FILENAME = "project-id"
HOST_MANAGED_DIR = ".botainer"  # task #173: NOT configurable at v0.1.0; the
# policy field naming.host_managed_folder exists but ~15 sites hardcode
# ".botainer" directly. v0.2 will route all sites through a single
# resolver; for now this constant is the source of truth and the policy
# field is reserved-but-unused.


class IdentityChangeRefused(Refused):
    """User refused (or non-interactive declined) a same-UUID-different-path change.

    Task #240: was `Exception` — @handle_refusals only catches Refused, so
    every IdentityChangeRefused raised through a CLI command dumped a full
    Python traceback instead of clean 'refused: ...' output. Now inherits
    Refused with category IDENTITY_CHANGE_REFUSED so the decorator catches it.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(RefusalCategory.IDENTITY_CHANGE_REFUSED, message)


@dataclass(frozen=True)
class InitResult:
    project_id: str
    state_dir: Path
    was_existing: bool


def _project_id_path(project_root: Path) -> Path:
    return project_root / HOST_MANAGED_DIR / PROJECT_ID_FILENAME


def read_project_id(project_root: Path) -> str | None:
    p = _project_id_path(project_root)
    if not p.exists():
        return None
    txt = p.read_text(encoding="utf-8").strip()
    return txt or None


def _validate_uuid(s: str) -> str:
    try:
        return str(_uuid.UUID(s))
    except (ValueError, TypeError):
        raise Refused(RefusalCategory.PROJECT_ID_TAMPERED, f"invalid uuid in project-id: {s!r}") from None


def write_project_id(project_root: Path, uuid_str: str, *, overwrite: bool = False) -> None:
    """Write the project-id file.

    Task #113: parallel `botainer init` from two terminals could each
    see no project-id, each generate a UUID, then race on write. The
    loser's UUID gets persisted while the winner's state dir is
    orphaned. Default: O_EXCL so the first writer wins; the loser
    gets a Refused with PROJECT_ID_TAMPERED so the caller knows to
    re-read the file rather than silently overwriting.

    overwrite=True is the explicit fork case (resolve_identity's
    fork branch wants to REPLACE the project-id with a fresh UUID).
    """
    p = _project_id_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    import os as _os
    if overwrite:
        p.write_text(uuid_str + "\n", encoding="utf-8")
        return
    try:
        fd = _os.open(str(p), _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL, 0o644)
    except FileExistsError:
        raise Refused(
            RefusalCategory.PROJECT_ID_TAMPERED,
            f"project-id already exists at {p} (concurrent init race). "
            f"Read the file to get the existing UUID; do not overwrite.",
        ) from None
    try:
        _os.write(fd, (uuid_str + "\n").encode("utf-8"))
    finally:
        _os.close(fd)


def generate_new() -> str:
    return str(_uuid.uuid4())


def now_iso8601_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_project_name(name: str | None) -> str | None:
    """Normalize an optional cosmetic name before any initialization writes.

    Names are stored as data, never paths or commands. The filter covers the
    remaining presentation gap: native list output prints the name in a terminal.
    Keep Unicode text, including ordinary right-to-left letters, but not control
    sequences, directional overrides, or line separators that can spoof rows.
    """
    if name is None:
        return None
    if not isinstance(name, str):
        raise Refused(RefusalCategory.CONFIG_INVALID,
                      "project name must be text")
    forbidden_bidi = {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
    if any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"}
           or unicodedata.bidirectional(char) in forbidden_bidi for char in name):
        raise Refused(
            RefusalCategory.CONFIG_INVALID,
            "project name must be one line without control characters or directional overrides",
        )
    name = name.strip()
    if not name or len(name) > 200:
        raise Refused(RefusalCategory.CONFIG_INVALID,
                      "project name must contain 1 to 200 characters after trimming whitespace")
    return name


def init_project(
    project_root: Path,
    *,
    agent: str,
    force: bool,
    non_interactive: bool,
    name: str | None = None,
) -> InitResult:
    """Initialize or recognize the project's identity.

    `force` does NOT apply to the identity — see the branch below. It records
    that a forced re-init happened; the overwriting it names is the CONFIG's,
    done by the caller.

    `non_interactive` is accepted for signature parity with the CLI and is not
    read: nothing in this function prompts, so the promise it makes ("refuse if
    any prompt would be required") is vacuously kept. Filed as its own queue row
    rather than dropped, because removing it touches every call site.
    """
    name = validate_project_name(name)
    project_root = project_root.resolve()
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    existing = read_project_id(project_root)

    if existing:
        # A FORCED INIT REWRITES THE CONFIG; IT NEVER RE-MINTS THE IDENTITY.
        #
        # (Flag spelling deliberately absent: every other flag this module
        # names is a `botainer start` flag, and a test asserts exactly that —
        # so naming an `init` flag here is the layer breach, not the test being
        # blunt. The CLI owns flag names; this owns the behaviour.)
        #
        # This condition used to be `existing and not force`, so a forced init
        # fell through to the mint path below and called write_project_id()
        # without overwrite=True — hitting the O_EXCL guard on every project
        # that has a project-id, which is every project a forced init is for.
        # Measured through the real CLI: exit 2, "project-id already exists ...
        # (concurrent init race). ... do not overwrite." — accusing the user of
        # a race they had not run, and refusing the one thing they had asked
        # for. The config was never rewritten either, because that refusal
        # happens before write_initial_config() is reached, so the flag's whole
        # documented purpose ("Overwrite existing Main/.botainer/ contents")
        # had never once executed.
        #
        # RE-MINTING IS NOT THE ALTERNATIVE READING. A fresh UUID orphans this
        # project's state dir — sessions/, data/ (which holds the credential in
        # isolated and shared modes) and packages/ — with nothing left pointing
        # at it. Deliberately giving a copy its own identity is the clone/fork
        # path in resolve_identity(), and that one asks first.
        #
        # THE RACE GUARD IS UNAFFECTED: in a genuine parallel init both
        # processes read `existing is None`, so neither takes this branch and
        # the loser still refuses below. Its wording becomes TRUE for the first
        # time — after this change, reaching it really does mean a race.
        uid = _validate_uuid(existing)
        proj_paths = state_dir.ensure_project_dirs(paths, uid)
        meta = state_dir.read_meta(proj_paths)
        if name is not None:
            meta["display_name"] = name
        meta = state_dir.append_path_history(meta, str(project_root))
        meta.setdefault("created_at", now_iso8601_utc())
        meta["updated_at"] = now_iso8601_utc()
        if force:
            # `force` would otherwise be a parameter this function accepts and
            # never reads — the shape that invites a later reader to "fix" the
            # dead argument by wiring it back to the mint path, which is the
            # bug above. It leaves a fact instead: this project's config.yaml
            # was reset, and when. `doctor` and the upgrade question both want
            # to know that a config is not the one the project was born with.
            meta["last_forced_reinit"] = now_iso8601_utc()
        state_dir.write_meta(proj_paths, meta)
        return InitResult(project_id=uid, state_dir=proj_paths.base, was_existing=True)

    new_uid = generate_new()
    write_project_id(project_root, new_uid)
    proj_paths = state_dir.ensure_project_dirs(paths, new_uid)
    meta_new: dict[str, object] = {
        "uuid": new_uid,
        "agent": agent,
        "created_at": now_iso8601_utc(),
        "updated_at": now_iso8601_utc(),
        "path_history": [str(project_root)],
    }
    if name is not None:
        meta_new["display_name"] = name
    meta = meta_new
    state_dir.write_meta(proj_paths, meta)
    return InitResult(project_id=new_uid, state_dir=proj_paths.base, was_existing=False)


def resolve_identity(
    project_root: Path,
    *,
    identity_accept: bool = False,
    fork: bool = False,
    prompt_fn: Callable[[str], str] | None = None,
    record: bool = True,
) -> tuple[str, Path]:
    """Return (uuid, state_dir_path), handling the clone/fork prompt.

    `prompt_fn(prompt_str) -> str` returns one of: "moved", "fork", "abort".
    If `prompt_fn` is None and stdin is not a TTY, refuse unless
    `identity_accept` or `fork` is given.

    `fork=True` is the non-interactive counterpart of answering "fork": mint a
    new UUID for THIS path and leave the original's state dir alone. The
    refusal message has named `--fork` since v0.0.13 but the flag did not
    exist — nobody noticed, because until the path_history reader was fixed
    (#134) the refusal itself was unreachable.
    """
    project_root = project_root.resolve()
    existing = read_project_id(project_root)
    if existing is None:
        raise Refused(
            RefusalCategory.CONFIG_MISSING,
            "no .botainer/project-id; run `botainer init` first",
        )
    uid = _validate_uuid(existing)
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    # Task #182 + #269: BEFORE ensure_project_dirs (which creates +
    # populates packages/sessions/data subdirs), snapshot whether the
    # per-project state base + meta.json existed. ensure_project_dirs
    # creates packages/<lang_subdir> directories that would make a
    # post-hoc 'has content' check unreliable.
    _project_state_base = paths.state_dir / uid
    meta_file_present = (_project_state_base / "meta.json").exists()
    pre_existing_state = False
    if _project_state_base.exists():
        for sub in ("sessions", "data"):
            sub_path = _project_state_base / sub
            if sub_path.exists() and sub_path.is_dir():
                try:
                    if any(sub_path.iterdir()):
                        pre_existing_state = True
                        break
                except OSError:
                    pass
    proj_paths = state_dir.ensure_project_dirs(paths, uid)
    meta = state_dir.read_meta(proj_paths)
    # `path_history` records are dicts ({path, first_seen, last_seen, host}).
    # This used to do `[str(x) for x in raw_hist]`, which stringified each
    # record into `"{'first_seen': ..., 'path': ...}"` — never equal to `here`,
    # never a real path — so every branch below collapsed into "prior path is
    # gone, treat as moved, record silently" and the clone/fork prompt was
    # unreachable. Read through the one shape-aware reader instead.
    hist = state_dir.path_history_records(meta)
    here = str(project_root)

    if not hist:
        # If meta.json was missing AND state dir had prior content (sessions/
        # data/) BEFORE ensure_project_dirs ran, that's tamper-shape. Refuse
        # unless explicitly accepted.
        if not meta_file_present and pre_existing_state and not identity_accept:
            raise IdentityChangeRefused(
                f"project-id {uid} has prior state at {proj_paths.base!s} "
                f"but meta.json is missing — looks like tampering. "
                f"Pass --accept-identity-change to silently re-record."
            )
        # First sighting (or accepted tamper); record.
        meta = state_dir.append_path_history(meta, here)
        meta.setdefault("created_at", now_iso8601_utc())
        meta["updated_at"] = now_iso8601_utc()
        state_dir.write_meta(proj_paths, meta)
        return uid, proj_paths.base

    last_known = state_dir.last_known_path(meta) or hist[-1]["path"]
    if last_known == here:
        # Matches; bump updated_at and proceed.
        meta["updated_at"] = now_iso8601_utc()
        state_dir.write_meta(proj_paths, meta)
        return uid, proj_paths.base

    # The UUID is known but at a different live path. Two subcases:
    # (a) The previously-known path no longer exists → moved checkout.
    # (b) Both paths exist → ambiguous: real choice between move and second copy.
    prior_alive = Path(last_known).exists()
    if not prior_alive:
        # Moved; record silently.
        meta = state_dir.append_path_history(meta, here)
        meta["updated_at"] = now_iso8601_utc()
        state_dir.write_meta(proj_paths, meta)
        return uid, proj_paths.base

    # Ambiguous.

    # A QUERY MUST NOT ANSWER THIS QUESTION. (#231) `status`, `attach`, `stop`
    # and `nudge` need the uuid to FIND things; they are not the moment to
    # decide whether this checkout is a move or a second copy. They used to pass
    # `identity_accept=True` unconditionally — which means "the user passed
    # --accept-identity-change", and none of those four commands HAS that flag.
    # So they took the moved branch without asking, appended this path to
    # path_history, and because `last_known` then equals `here` the guard never
    # fired again FOR ANY COMMAND, including `start`. One `cp -a` plus one
    # `botainer status` and the copy was permanently attached to the original's
    # credentials with the prompt disarmed. §4ck asserts that copies are
    # prompted or refused; that claim was false for those four.
    #
    # Passing `identity_accept=False` instead would be wrong in a different way:
    # the refusal names `--accept-identity-change` and `--fork`, flags these
    # commands do not have (the #130 class — output naming commands that do not
    # exist). So the answer is to ANSWER NOTHING: return the uuid, write no
    # meta, and say plainly what was seen and which command decides.
    if not record:
        sys.stderr.write(
            f"[botainer] note: this directory and {last_known!r} both exist and "
            f"carry the same project id. Showing the project's existing state; "
            f"nothing was recorded.\n"
            f"    If this is a COPY that should be its own project, run "
            f"`botainer start --fork` here.\n"
            f"    If you MOVED it, run `botainer start --accept-identity-change` "
            f"here.\n")
        return uid, proj_paths.base

    if fork and identity_accept:
        raise Refused(
            RefusalCategory.IDENTITY_AMBIGUOUS,
            "--fork and --accept-identity-change say opposite things "
            "(mint a new UUID vs keep this one). Pass one.",
        )
    if fork:
        return _do_fork(project_root, uid, here, paths)
    if identity_accept:
        # Accept as "moved": append new path, keep state.
        meta = state_dir.append_path_history(meta, here)
        meta["updated_at"] = now_iso8601_utc()
        state_dir.write_meta(proj_paths, meta)
        return uid, proj_paths.base

    if prompt_fn is None and not sys.stdin.isatty():
        raise IdentityChangeRefused(
            f"project UUID {uid} is also tracked at {last_known!r} which still exists; "
            f"re-run with --accept-identity-change (moved) or pass --fork to mint a new UUID"
        )

    if prompt_fn is None:
        prompt_fn = _interactive_clone_fork_prompt
    choice = prompt_fn(_clone_fork_prompt_text(uid, last_known, here))
    if choice == "moved":
        meta = state_dir.append_path_history(meta, here)
        meta["updated_at"] = now_iso8601_utc()
        state_dir.write_meta(proj_paths, meta)
        return uid, proj_paths.base
    if choice == "fork":
        return _do_fork(project_root, uid, here, paths)
    raise IdentityChangeRefused("user aborted identity-change prompt")


def _do_fork(project_root: Path, uid: str, here: str, paths) -> tuple[str, Path]:
    """Mint a new UUID for this path; the original keeps its state dir.

    One implementation for both entry points — the interactive "[f] fork"
    answer and the non-interactive `--fork` flag — so they cannot drift.
    The new project starts with an EMPTY /packages and /scratch; it is a
    separate project that happens to share a checkout's contents.
    """
    new_uid = generate_new()
    # Fork explicitly replaces the existing project-id.
    write_project_id(project_root, new_uid, overwrite=True)
    new_paths = state_dir.ensure_project_dirs(paths, new_uid)
    new_meta: dict[str, object] = {
        "uuid": new_uid,
        "forked_from": uid,
        "created_at": now_iso8601_utc(),
        "updated_at": now_iso8601_utc(),
        "path_history": [here],
    }
    state_dir.write_meta(new_paths, new_meta)
    return new_uid, new_paths.base


def _clone_fork_prompt_text(uid: str, prior: str, here: str) -> str:
    return (
        f"This project (UUID {uid}) is also tracked at:\n"
        f"  prior path: {prior}  (still exists)\n"
        f"  here:       {here}\n\n"
        f"Is this:\n"
        f"  [m] a moved checkout (use the same state dir)\n"
        f"  [f] a fork / independent copy (mint a new UUID for this path)\n"
        f"  [a] abort\n"
        f"Choose: "
    )


def _interactive_clone_fork_prompt(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        raw = input().strip().lower()
    except EOFError:
        return "abort"
    if raw in ("m", "moved", "move"):
        return "moved"
    if raw in ("f", "fork", "new"):
        return "fork"
    return "abort"
