"""Session runtime record persistence.

After `compose_session` produces a SessionSpec and before/after the runtime
launches the container, the launcher writes a JSON record to:

    ${state_dir}/sessions/<sid>/spec.json

This record captures both the SessionSpec (immutable composition) AND the
runtime handle info (container_id for Docker, jobid + node + tmux socket
path for Slurm/Apptainer). This is what `botainer nudge`, `botainer
attach`, `botainer status` etc. read to find a running session.

Why a separate file from the SessionSpec? The SessionSpec is immutable and
known at compose time. The runtime info (container_id, jobid) only exists
after launch. We write the spec at compose, then update with runtime info
after launch. Keeping it in one file keeps the on-disk representation
simple.

Format (v1):

    {
      "schema_version": 1,
      "session_id": "...",
      "project_uuid": "...",
      "project_root": "...",
      "runtime": "docker" | "apptainer" | "mock",
      "image": "...",
      "started_at": "ISO-8601" | null,    # set on launch; null at compose
      "ended_at": "ISO-8601" | null,
      "host": "...",                       # socket.gethostname()
      "spec": {<full SessionSpec dict>},
      "runtime_handle": {
        "docker": { "container_id": "..." } | null,
        "apptainer": {
          "instance_name": "..." | null,
          "slurm_jobid": "..." | null,
          "slurm_step_id": "..." | null,
          "node": "..." | null
        } | null
      },
      "screen_session_id": "botainer-<sid>" | null  # set when nudge enabled
    }

Per internal design note DN-041 §A19, the nudge integration point is
top-level `screen_session_id` — a HOST-side screen session that wraps
the docker/apptainer runtime call. `botainer nudge` uses
`screen -S <screen_session_id> -X stuff "..."` to inject text.
The pre-§A19 fields (tmux_socket_path, tmux_session_name) are gone
because the screen session lives on the HOST, not inside the container.
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RECORD_FILENAME = "spec.json"

# Validation patterns: defense-in-depth against argv-injection or
# path-traversal via a tampered spec.json. Production session IDs are
# UUID4 hex (16 hex chars from `_new_session_id`); production container
# IDs are Docker's 64-char hex (12-char short form also accepted).
#
# We accept a slightly broader pattern so legacy test fixtures and other
# safe IDs (e.g., 8-char short forms, lowercase alnum) still load. Key
# safety properties enforced:
#   - First character is alphanumeric (refuses leading `-` which argv
#     would interpret as a flag)
#   - Only [A-Za-z0-9._-] thereafter (refuses path separators like `/`,
#     shell-special chars like `$`/`` ` ``/`;`, control chars, and
#     traversal `..`)
#   - Length 8-64 chars
# Container IDs are stricter (Docker only emits hex) but the same
# safety properties matter.
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
_CONTAINER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{11,63}$")


def is_valid_container_id(s: str) -> bool:
    """True if `s` is a safe container_id value (alnum-leading, no path sep)."""
    return bool(s and _CONTAINER_ID_PATTERN.match(s))


def is_valid_session_id(s: str) -> bool:
    """True if `s` is a safe session_id value (alnum-leading, no path sep)."""
    return bool(s and _SESSION_ID_PATTERN.match(s))


@dataclass
class DockerHandle:
    container_id: str | None = None


@dataclass
class ApptainerHandle:
    instance_name: str | None = None
    slurm_jobid: str | None = None
    slurm_step_id: str | None = None
    node: str | None = None


@dataclass
class ProxyHandle:
    """Recorded by the credential-proxy plugin's start hook.

    Implementation-review CRITICAL 1: this used to be written to
    runtime_handle.proxy as raw JSON by the start hook, then silently
    dropped by from_dict (which only knew about docker/apptainer keys).
    Adding it as a first-class handle preserves it round-trip.
    """
    pid: int | None = None
    socket_path: str | None = None
    audit_log: str | None = None
    started_at: str | None = None


@dataclass
class SessionRecord:
    session_id: str
    project_uuid: str
    project_root: str
    runtime: str
    image: str
    host: str
    spec: dict[str, Any]
    started_at: str | None = None
    ended_at: str | None = None
    docker: DockerHandle | None = None
    apptainer: ApptainerHandle | None = None
    proxy: ProxyHandle | None = None
    schema_version: int = SCHEMA_VERSION
    # Per internal design note DN-041 §A19.4: the host-side `screen`
    # session name that wraps this session (Docker: on the launching host;
    # Apptainer: on the compute node). Used by `botainer nudge` to
    # address the correct `screen -S <id> -X stuff` target without any
    # docker exec / srun --overlap roundtrip when the host is reachable.
    # Set by composition.launch() when nudge is enabled; None otherwise.
    screen_session_id: str | None = None
    # Forward-compat: any keys under runtime_handle that we don't know
    # about get preserved here so re-writes don't clobber them. Plugins
    # can record their own handle subfields without modifying this file.
    extra_runtime_handle: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        runtime_handle: dict[str, Any] = dict(self.extra_runtime_handle)
        runtime_handle["docker"] = asdict(self.docker) if self.docker else None
        runtime_handle["apptainer"] = asdict(self.apptainer) if self.apptainer else None
        runtime_handle["proxy"] = asdict(self.proxy) if self.proxy else None
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "project_uuid": self.project_uuid,
            "project_root": self.project_root,
            "runtime": self.runtime,
            "image": self.image,
            "host": self.host,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "screen_session_id": self.screen_session_id,
            "spec": self.spec,
            "runtime_handle": runtime_handle,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SessionRecord:
        # Task #211: was bare equality; older or newer records were
        # silently dropped by list_sessions (which catches ValueError).
        # Now: accept SCHEMA_VERSION exactly OR a single version older
        # (forward-compat path during in-progress migration). Anything
        # else still refuses, but at least surfaces the version delta
        # via a distinct error class so list_sessions can log it.
        record_v = d.get("schema_version")
        if record_v != SCHEMA_VERSION:
            if isinstance(record_v, int) and record_v == SCHEMA_VERSION - 1:
                # One version older — treat as forward-compat. If a future
                # cls had a v->v migration shim, call it here.
                pass  # accept as-is for v0.1.0
            else:
                raise ValueError(
                    f"unsupported session record schema_version "
                    f"{record_v!r}; expected {SCHEMA_VERSION}"
                )
        # Defense in depth: validate shape of high-risk fields before they
        # become path segments or argv positional args.
        # session_id flows into Path(state_dir) / sessions / <session_id>
        # container_id flows into ["docker", "exec", <container_id>]
        # Tampered specs (a buggy hook or out-of-band edit) shouldn't be
        # able to inject `..` or leading dashes here.
        sid = d.get("session_id", "")
        if not is_valid_session_id(sid):
            raise ValueError(
                f"session_id {sid!r} fails shape check (expected hex 8-64)"
            )
        rh = d.get("runtime_handle") or {}
        docker_d = rh.get("docker")
        if docker_d:
            cid = docker_d.get("container_id")
            if cid is not None and cid != "" and not is_valid_container_id(cid):
                raise ValueError(
                    f"docker.container_id {cid!r} fails shape check "
                    f"(expected hex 12-64)"
                )
        apptainer_d = rh.get("apptainer")
        if apptainer_d:
            # Post-§A19: tmux_socket_path / tmux_session_name moved off
            # ApptainerHandle (the screen session lives on the compute
            # node, not as an apptainer-handle field). Old spec.json
            # files may still have these keys — strip silently.
            apptainer_d = {
                k: v for k, v in apptainer_d.items()
                if k not in {"tmux_socket_path", "tmux_session_name"}
            }
        proxy_d = rh.get("proxy")
        # Capture any other handle keys for forward-compat (CRITICAL 1
        # was caused by silently dropping unknown handle subfields).
        known = {"docker", "apptainer", "proxy"}
        extra_handle = {k: v for k, v in rh.items() if k not in known}
        return cls(
            schema_version=d["schema_version"],
            session_id=d["session_id"],
            project_uuid=d["project_uuid"],
            project_root=d["project_root"],
            runtime=d["runtime"],
            image=d["image"],
            host=d["host"],
            spec=d.get("spec", {}),
            started_at=d.get("started_at"),
            ended_at=d.get("ended_at"),
            screen_session_id=d.get("screen_session_id"),
            docker=DockerHandle(**docker_d) if docker_d else None,
            apptainer=ApptainerHandle(**apptainer_d) if apptainer_d else None,
            proxy=ProxyHandle(**proxy_d) if proxy_d else None,
            extra_runtime_handle=extra_handle,
        )


def _record_path(session_dir: Path) -> Path:
    return session_dir / RECORD_FILENAME


def write(session_dir: Path, record: SessionRecord) -> Path:
    """Atomically write the session record to spec.json.

    Atomic: writes to spec.json.tmp and renames. Mode 0600 (owner read/write).
    """
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _record_path(session_dir)
    tmp_path = path.with_suffix(".json.tmp")
    payload = json.dumps(record.to_dict(), indent=2, sort_keys=True)
    # Open with restrictive umask before write.
    fd = os.open(
        str(tmp_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        mode=0o600,
    )
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)
    return path


def read(session_dir: Path) -> SessionRecord:
    """Read the session record from spec.json.

    Raises FileNotFoundError if the record is missing.
    """
    path = _record_path(session_dir)
    raw = path.read_text(encoding="utf-8")
    return SessionRecord.from_dict(json.loads(raw))


#: Refuse to read a spec.json larger than this when all we want is one integer.
#: The census runs on every `doctor` and every `setup`, over a directory that
#: grows monotonically (nothing GCs session records), on $HOME which on the
#: target platform is NFS or GPFS. A record is a few KB; anything past this is
#: either damaged or hostile, and counting it as unknown is both cheaper and
#: more honest than parsing it.
_CENSUS_MAX_RECORD_BYTES = 512 * 1024


def schema_version_census(root: Path) -> dict[int | None, int]:
    """Histogram of `schema_version` across every session record under a root.

    Takes the STATE ROOT (`<root>`), not `<root>/state` and not
    `<root>/state/<uuid>` — both of which are also called "state_dir" elsewhere
    in this package (`StatePaths.state_dir`, `BOTAINER_STATE_DIR`). The
    parameter is named `root` because when it was named `state_dir` either of
    those two was the obvious thing to pass, and passing one returned `{}` —
    indistinguishable from a healthy empty root, so `doctor` would have printed
    nothing at all. Named for what it is, the mistake is visible.

    WHY (#202). `from_dict` accepts exactly SCHEMA_VERSION or SCHEMA_VERSION-1
    and raises for anything else; `list_sessions` catches that and drops the
    record from its result. So a user who skips a release gets a shorter
    history than they had.

    It is NOT silent — #211 already writes one stderr line per skipped record.
    But that line appears only for a project you happen to be listing, at the
    moment you run some unrelated command, one record at a time. This sweeps
    EVERY project and answers a different question: how much is affected, asked
    before you go looking for a session that is missing rather than after.

    A MISSING spec.json is NOT counted. A session dir with no record is an
    aborted launch — `composition.py` creates the directory long before it
    writes the record, so every refusal and every Ctrl-C leaves one, and nothing
    removes them. `list_sessions` skips that case silently and says why. This
    counted it as unreadable, which made `doctor` warn — and `doctor --strict`
    exit 1 — on a completely healthy install, while blaming a downgrade that
    never happened. Caught by review before it shipped; the shared
    `candidate_session_dirs` exists so the two cannot drift apart again.

    Reads `schema_version` and nothing else — deliberately NOT via `from_dict`,
    which is exactly the function that refuses the records we are trying to
    count. A record that EXISTS but cannot be parsed is counted under None,
    because "I could not tell" and "it is version 7" are different facts and
    collapsing them would overstate what is known.

    Never raises, and means it: the except is deliberately broad. `json.loads`
    on pathological input raises RecursionError, which is not an OSError or a
    ValueError, and a census is a diagnostic — one that can take down the
    command diagnosing with it is worse than no census.
    """
    census: dict[int | None, int] = {}
    try:
        project_dirs = [p for p in (Path(root) / "state").iterdir() if p.is_dir()]
    except OSError:
        return census
    for project in project_dirs:
        for child in candidate_session_dirs(project / "sessions"):
            record = child / RECORD_FILENAME
            key: int | None
            try:
                if record.stat().st_size > _CENSUS_MAX_RECORD_BYTES:
                    key = None
                else:
                    raw = json.loads(record.read_text(encoding="utf-8"))
                    got = raw.get("schema_version")
                    key = (got if isinstance(got, int) and not isinstance(got, bool)
                           else None)
            except FileNotFoundError:
                continue          # aborted launch; not a record. See above.
            except Exception:     # noqa: BLE001 — see the docstring
                key = None
            census[key] = census.get(key, 0) + 1
    return census


def unreadable_by_this_build(census: dict[int | None, int]) -> int:
    """How many records in a census this build's `from_dict` would refuse.

    Split from the scan so the threshold logic is testable without a filesystem,
    and so the accepted range is stated ONCE next to the constant it derives
    from rather than duplicated into the caller.
    """
    return sum(n for v, n in census.items()
               if v not in (SCHEMA_VERSION, SCHEMA_VERSION - 1))


_RECOGNIZED_UPDATE_FIELDS = frozenset({
    "started_at", "ended_at", "screen_session_id",
    "container_id",  # docker handle
    "instance_name", "slurm_jobid", "slurm_step_id", "node",
    "proxy_pid", "proxy_socket_path", "proxy_audit_log", "proxy_started_at",
})


def update_runtime(session_dir: Path, **fields: Any) -> SessionRecord:
    """Read, mutate selected fields on the runtime handle / timestamps, and
    re-write atomically.

    Recognized field names (caller will get a clear error if a typo'd key is
    passed — implementation-review Medium #10):
      started_at, ended_at, screen_session_id (host-side screen wrap name).
      For docker handle: container_id.
      For apptainer handle: instance_name, slurm_jobid, slurm_step_id, node.
      For proxy handle: proxy_pid, proxy_socket_path, proxy_audit_log,
                        proxy_started_at.

    Forward-compat: any keys under runtime_handle that this version doesn't
    know about (e.g. a third-party plugin recording its own subhandle) are
    preserved round-trip via the `extra_runtime_handle` field.
    """
    unknown = set(fields) - _RECOGNIZED_UPDATE_FIELDS
    if unknown:
        raise TypeError(
            f"update_runtime: unrecognized field(s) {sorted(unknown)!r}. "
            f"Known: {sorted(_RECOGNIZED_UPDATE_FIELDS)!r}"
        )
    rec = read(session_dir)
    if "started_at" in fields:
        rec.started_at = fields["started_at"]
    if "ended_at" in fields:
        rec.ended_at = fields["ended_at"]
    if "screen_session_id" in fields:
        rec.screen_session_id = fields["screen_session_id"]
    if rec.runtime == "docker":
        rec.docker = rec.docker or DockerHandle()
        if "container_id" in fields:
            rec.docker.container_id = fields["container_id"]
    elif rec.runtime == "apptainer":
        rec.apptainer = rec.apptainer or ApptainerHandle()
        for k in (
            "instance_name",
            "slurm_jobid",
            "slurm_step_id",
            "node",
        ):
            if k in fields:
                setattr(rec.apptainer, k, fields[k])
    # Proxy handle fields (any runtime — proxy is cross-runtime).
    proxy_field_map = {
        "proxy_pid": "pid",
        "proxy_socket_path": "socket_path",
        "proxy_audit_log": "audit_log",
        "proxy_started_at": "started_at",
    }
    if any(k in fields for k in proxy_field_map):
        rec.proxy = rec.proxy or ProxyHandle()
        for in_key, attr in proxy_field_map.items():
            if in_key in fields:
                setattr(rec.proxy, attr, fields[in_key])
    write(session_dir, rec)
    return rec


def reserialize_spec(session_dir: Path, spec_obj: Any) -> SessionRecord:
    """Re-write the spec portion of an existing session record from a (post-hook)
    SessionSpec, PRESERVING the runtime handle + timestamps already recorded.

    Re-audit round 3 (#12/#16): the record is first written at compose time
    (before host_pre_launch/pre_session hooks add the credential bind, the git
    overlay, and the #160 module software-root binds), so the persisted
    provenance UNDERSTATED what the container actually ran with. The launcher
    calls this from `composition.render_agent_files` AFTER the hooks so spec.json
    reflects the real bind/env set. Runtime fields (slurm_jobid, node,
    container_id, timestamps, proxy handle) set by the adapter / the
    --in-container recorder are NOT clobbered. Falls back to a fresh from_spec
    write if no record exists yet (the dry-run / no-prior-write path)."""
    try:
        rec = read(session_dir)
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        rec = from_spec(spec_obj)
        write(session_dir, rec)
        return rec
    # Refresh only the spec-derived view; keep runtime handle + timestamps.
    rec.spec = spec_obj.model_dump(mode="json")
    rec.image = spec_obj.image
    write(session_dir, rec)
    return rec


def candidate_session_dirs(sessions_root: Path) -> list[Path]:
    """Directories under `sessions/` that could hold a session record.

    ONE definition of "what counts as a session record", because there are two
    callers and they used to encode it separately — which is how the #202 census
    came to disagree with `list_sessions` about the aborted-launch case and warn
    at users about a healthy install (found by review before it shipped).

    Excludes launcher-internal dirs (`_submit-scripts`, `_module-env`, …): they
    are not session records and have no spec.json. Real session ids are hex, so
    never start with `_` or `.`, and this cannot hide a genuine record.

    Says nothing about whether a record is PRESENT or readable — that is the
    caller's business, and the two callers legitimately differ there.
    """
    try:
        children = list(sessions_root.iterdir())
    except OSError:
        return []
    return [c for c in children
            if not c.name.startswith(("_", ".")) and c.is_dir()]


def list_sessions(sessions_root: Path) -> list[SessionRecord]:
    """Enumerate all session records under a project's sessions/ dir.

    Returns records sorted by started_at descending (newest first). Records
    that fail to parse are skipped (logged via stderr).
    """
    if not sessions_root.exists():
        return []
    out: list[SessionRecord] = []
    skipped: list[tuple[Path, str]] = []
    for child in candidate_session_dirs(sessions_root):
        try:
            out.append(read(child))
        except FileNotFoundError:
            # No spec.json at all = an INCOMPLETE/aborted session: the launcher
            # created the dir but crashed/failed before writing the record (e.g. a
            # launch that hit a missing image or was Ctrl-C'd at the confirm
            # prompt). Common + harmless — skip SILENTLY rather than spam a scary
            # "FileNotFoundError … spec.json" line on every status/watch/nudge.
            # MALFORMED records (below) are still surfaced (Task #211).
            continue
        except (ValueError, KeyError) as exc:
            skipped.append((child, f"{type(exc).__name__}: {exc}"))
            continue
        except json.JSONDecodeError as exc:
            skipped.append((child, f"JSONDecodeError: {exc}"))
            continue
    # Task #211: surface silent orphans so users see them in stderr
    # instead of just disappearing from `botainer status`.
    if skipped:
        import sys as _sys
        for path, reason in skipped:
            _sys.stderr.write(
                f"[botainer] skipped session record {path.name}: {reason}\n"
            )
    out.sort(key=lambda r: r.started_at or r.session_id, reverse=True)
    return out


def from_spec(spec_obj: Any) -> SessionRecord:
    """Build an initial SessionRecord from a SessionSpec (no runtime handle yet).

    The spec is dumped via its model_dump (pydantic). Host is captured from
    socket.gethostname().
    """
    spec_dict = spec_obj.model_dump(mode="json")
    return SessionRecord(
        session_id=spec_obj.session_id,
        project_uuid=spec_obj.project_uuid,
        project_root=spec_obj.project_root,
        runtime=spec_obj.runtime,
        image=spec_obj.image,
        host=socket.gethostname(),
        spec=spec_dict,
    )
