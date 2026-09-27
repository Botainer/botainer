"""Who else can refresh this credential right now? (#215)

Concurrent holders of a rotating refresh token can invalidate one another's
credentials if the provider rejects reuse. A resulting `invalid_grant` may not
explain the competing refresh. This is an observed Claude concern; Codex refresh
concurrency is not established by this detector.

botainer has three auth modes and they do NOT all refresh the same way:

  broker     the daemon refreshes HOST-side and takes botainer/broker/
             refresh_lock, so N brokers on one store serialise correctly.
  shared     Claude Code / codex refreshes INSIDE the container, against a
             per-project COPY that back-fills to a host-wide master, taking no
             lock and knowing nothing about the host's.
  isolated   the container refreshes its per-project/profile copy. Other
             projects use separate stores; sessions in one profile share it.

Two shared sessions on one store, or a shared session alongside a broker, can
refresh independently. This detector identifies competing holders. It does not
coordinate refreshes or establish a provider's token-invalidation behavior.

THREE STATES, NOT TWO. A live session whose record predates `plugins_enabled`
(or whose spec cannot be read) is UNKNOWN, never SAFE. Reporting "no conflict"
because evidence was unreadable would turn an absent measurement into a false
assurance. Preserve uncertainty rather than reporting an unobserved all-clear.

MODES ARE READ FROM THE PLUGIN MANIFESTS, not from a table here. Every agent
plugin already declares `auth_family` and `auth_mode`; a second list in this
module would be a thing to forget when a fourth mode arrives.
"""
from __future__ import annotations

from dataclasses import dataclass

# How a mode redeems the refresh token. This is the whole basis of the rule, so
# it is named rather than left implicit in a boolean.
HOST_SERIALIZED = "host-serialized"        # broker: takes refresh_lock
CONTAINER_UNSERIALIZED = "container-unserialized"   # shared: no lock, in-cage
CONTAINER_PRIVATE = "container-private"    # isolated: its own store
UNKNOWN = "unknown"

_REFRESH_KIND = {
    "broker": HOST_SERIALIZED,
    "shared": CONTAINER_UNSERIALIZED,
    "isolated": CONTAINER_PRIVATE,
    # `proxy` was removed in #59 and cannot be composed; if a record from
    # before that is still live, it is not something this rule can reason
    # about, so it reads UNKNOWN rather than being silently dropped.
}


@dataclass(frozen=True)
class Holder:
    """One live session that holds, or brokers, a credential."""
    session_id: str
    project_uuid: str
    project_label: str
    family: str            # "anthropic" | "openai" | ""
    mode: str              # "broker" | "shared" | "isolated" | ""
    scope: str             # "shared" | "isolated" | ""
    refresh_kind: str
    why_unknown: str = ""

    @property
    def is_unknown(self) -> bool:
        return self.refresh_kind == UNKNOWN


def _mode_index() -> dict[str, tuple[str, str]]:
    """plugin name -> (auth_family, auth_mode), from the installed manifests.

    Loaded the same way composition._drop_unselected_agent_plugins loads it:
    InstalledPlugin carries only name/version/tier/source/plugin_dir, so the
    manifest has to be read off disk. An earlier draft did
    `getattr(inst, "auth_family", "")` — always "", so the index came back
    EMPTY and the collision check silently never fired. A detector that cannot
    fire is the same defect as the missing warning it was written to add.
    """
    from botainer.plugins.lifecycle import list_installed
    from botainer.plugins.manifest import load_manifest
    out: dict[str, tuple[str, str]] = {}
    for inst in list_installed():
        try:
            man = load_manifest(inst.plugin_dir)
        except Exception:
            continue
        family = getattr(man, "auth_family", "") or ""
        mode = getattr(man, "auth_mode", "") or ""
        if family and mode:
            out[inst.name] = (family, mode)
    return out


def holder_for_plugins(
    plugins_enabled, *, session_id: str, project_uuid: str,
    project_label: str, broker_handle: dict | None = None,
    index: dict[str, tuple[str, str]] | None = None,
) -> Holder | None:
    """The credential holder implied by a session's enabled plugins.

    None when the session has no auth plugin at all — a `botainer shell`, or a
    project that never logged in. That is genuinely "not a holder", as distinct
    from "we could not tell", which returns a Holder with refresh_kind UNKNOWN.
    """
    idx = _mode_index() if index is None else index
    if plugins_enabled is None:
        return Holder(session_id, project_uuid, project_label, "", "", "",
                      UNKNOWN, why_unknown="the record has no plugins_enabled "
                      "(written by an older botainer)")
    matches = [(n, *idx[n]) for n in plugins_enabled if n in idx]
    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(m[0] for m in matches)
        return Holder(session_id, project_uuid, project_label, "", "", "",
                      UNKNOWN,
                      why_unknown=f"more than one auth plugin enabled ({names})")
    _name, family, mode = matches[0]
    kind = _REFRESH_KIND.get(mode, UNKNOWN)
    if mode == "broker":
        # The broker records which store it opened. Default to `shared` — the
        # conservative reading, because that is the one that can collide.
        scope = str((broker_handle or {}).get("credential_scope") or "shared")
    elif mode == "shared":
        scope = "shared"
    else:
        scope = "isolated"
    why = "" if kind != UNKNOWN else f"auth_mode {mode!r} has no known refresh kind"
    return Holder(session_id, project_uuid, project_label, family, mode, scope,
                  kind, why_unknown=why)


def live_holders(paths, *, exclude_session_id: str = "") -> list[Holder]:
    """Every live session on this host, as a credential holder."""
    from botainer.state import dir as state_dir
    from botainer.state import liveness, session_record

    index = _mode_index()
    out: list[Holder] = []
    for entry in state_dir.list_projects():
        proj = paths.for_project(entry.uuid)
        try:
            records = session_record.list_sessions(proj.sessions_dir)
        except OSError:
            continue
        label = entry.display_name or entry.last_path or entry.uuid[:8]
        for rec in records:
            if rec.session_id == exclude_session_id or rec.ended_at:
                continue
            if not liveness.is_session_alive(rec):
                continue
            spec = rec.spec if isinstance(rec.spec, dict) else {}
            handle = rec.extra_runtime_handle.get("broker")
            holder = holder_for_plugins(
                spec.get("plugins_enabled"),
                session_id=rec.session_id, project_uuid=entry.uuid,
                project_label=label,
                broker_handle=handle if isinstance(handle, dict) else None,
                index=index)
            if holder is not None:
                out.append(holder)
    return out


def collisions(candidate: Holder, others) -> tuple[list[Holder], list[Holder]]:
    """(will collide with candidate, cannot be judged).

    THE RULE, stated once: two holders collide when they hold the SAME TOKEN
    LINEAGE and at least one of them redeems it without taking the host lock.

      same lineage       same auth family AND both scope == "shared"
      unserialised       refresh_kind == CONTAINER_UNSERIALIZED

    "Lineage", not "file", and the distinction is not pedantry. Shared mode
    does not bind one file to every project: each project gets a working COPY
    and back-fills refreshes up to a master store. So two shared sessions read
    two different paths — and still collide, because both copies descend from
    one authorization and rotation invalidates the lineage, not the path.
    A rule written as "same path" would compare the two copies, find them
    different, and report SAFE for the exact case that broke six projects.

    So broker+broker is fine (both serialise through refresh_lock), anything
    +isolated is fine (a different store), and shared+shared or shared+broker
    collide. An UNKNOWN holder is returned separately and never counted as
    safe.
    """
    if candidate.is_unknown:
        return [], list(others)
    hits: list[Holder] = []
    unknown: list[Holder] = []
    for other in others:
        if other.is_unknown:
            unknown.append(other)
            continue
        if other.family != candidate.family:
            continue
        if candidate.scope != "shared" or other.scope != "shared":
            continue
        if CONTAINER_UNSERIALIZED in (candidate.refresh_kind,
                                      other.refresh_kind):
            hits.append(other)
    return hits, unknown


def holder_for_spec(spec) -> Holder | None:
    """The holder a session ABOUT TO START will become.

    The scope comes from the VALIDATED config (ProjectConfig.plugins), not from
    raw YAML and not from a guess. Guessing "shared" would be the conservative
    direction — it can only over-warn — but a warning that fires when it should
    not is the noise problem this repo keeps having to undo, and the real value
    is one attribute away.
    """
    idx = _mode_index()
    holder = holder_for_plugins(
        list(spec.plugins_enabled or []), session_id=spec.session_id,
        project_uuid=spec.project_uuid, project_label=str(spec.project_root),
        index=idx)
    if holder is None or holder.mode != "broker":
        return holder
    from pathlib import Path

    from botainer.core.config import load_config
    try:
        cfg = load_config(Path(spec.project_root))
        plugin_name = next(n for n in spec.plugins_enabled
                           if idx.get(n, ("", ""))[1] == "broker")
        scope = str((cfg.plugins.get(plugin_name) or {}).get(
            "credential_scope", "shared"))
    except Exception:
        scope = "shared"
    return Holder(holder.session_id, holder.project_uuid, holder.project_label,
                  holder.family, holder.mode, scope, holder.refresh_kind)


def warning_lines(candidate: Holder | None, hits, unknown) -> list[str]:
    """The block a human sees. Empty when there is nothing to say.

    STATES A FACT AND POINTS; it does not render a safety verdict. It also does
    not claim to have fixed anything — concurrent shared sessions still do not
    work, and saying "we now warn about it" as though that were a resolution is
    the shape this project keeps having to correct.
    """
    if candidate is None or (not hits and not unknown):
        return []
    out: list[str] = [""]
    if hits:
        out.append(
            "⚠ ANOTHER SESSION CAN ALREADY REFRESH THIS LOGIN — one of you will "
            "be logged out.")
        out.append(
            f"    This session: {candidate.family} in {candidate.mode} mode.")
        for h in hits:
            out.append(f"    Also running:  {h.project_label}  "
                       f"({h.mode} mode, session {h.session_id[:12]})")
        out.append(
            "    A refresh token can only be redeemed ONCE: whichever side "
            "refreshes second gets")
        out.append(
            "    `invalid_grant`, and the message it prints does not say why. "
            "Shared mode refreshes")
        out.append(
            "    inside the container and takes no host lock, so it cannot "
            "coordinate with the others.")
        out.append(
            "    What you can do NOW: stop the other session, or put both "
            "projects on broker mode —")
        out.append(
            "    brokers refresh host-side under one lock and are safe to run "
            "together.")
        out.append(
            "    Not fixed, only detected: see `botainer auth status`.")
    for u in unknown:
        out.append(
            f"?   Could not tell what {u.project_label} "
            f"(session {u.session_id[:12]}) is doing: {u.why_unknown}.")
        out.append(
            "    Treat it as a possible second holder rather than as absent.")
    return out


def warning_lines_for_spec(spec) -> list[str]:
    """One call for the launcher: candidate, census, verdict, prose.

    NEVER RAISES, and never goes SILENT either. A detector that can abort a
    launch is worse than the failure it detects — but `except: return []` would
    make a broken detector indistinguishable from "no other holders", which is
    the same absent-answer-read-as-reassuring defect this module exists to
    avoid, committed by the module itself. So a failure says so, in one line,
    and the launch continues.
    """
    try:
        from botainer.state import dir as state_dir
        candidate = holder_for_spec(spec)
        if candidate is None:
            return []
        paths = state_dir.ensure_user_state_dir(create_if_missing=False)
        others = live_holders(paths, exclude_session_id=spec.session_id)
        return warning_lines(candidate, *collisions(candidate, others))
    except Exception as exc:                      # noqa: BLE001 - see above
        return ["",
                f"?   Could not check whether another session already holds "
                f"this login: {type(exc).__name__}: {exc}",
                "    This is NOT an all-clear — the check failed, it did not "
                "come back empty."]
