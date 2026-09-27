"""Shared helpers for CLI commands.

Consolidates code duplicated across `nudge`, `status`, `stop`, `attach`:
- find_project_root: walks up looking for .botainer/project-id
- refuse: print typed refusal to stderr + remediation hint + exit
- find_session_by_prefix: prefix-match against session records;
  refuse on ambiguity, return None on no match

Per the simplify review: previously these were 3-4 verbatim copies.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

import click

from botainer.state import session_record


def _outer_has_ever_run_a_session(outer: Path) -> bool | None:
    """Has a session ever LAUNCHED for the project at `outer`? None = cannot tell.

    This is the ONE fact that decides whether a nested inner project could have
    been agent-authored: an agent can only plant a `.botainer/` in a tree that
    was bound as its workspace, and that only happens when a session runs.

    A SESSION DIRECTORY IS NOT A SESSION. Measured on a fresh install: `init`
    leaves `sessions/` empty, and `botainer inspect` — which launches nothing —
    creates a directory there with a `spec.json` whose `started_at` is null. So
    the discriminator is a RECORD WITH A START TIME, not a directory entry. A
    refuting review caught the directory version asserting that an agent had
    held the tree read-write after nothing but an `inspect`.

    THREE WAYS TO ANSWER "CANNOT TELL", and each one is a case where a `False`
    would be a lie:
      * the state root is not there at all — `ensure_user_state_dir(
        create_if_missing=False)` still hands back paths for a root that does
        not exist;
      * the root is there but holds NO state for this project — the project may
        well have run under a DIFFERENT `MY_BOTAINER`, which this root cannot
        see, and hand-deleting a state directory is currently the only way to
        remove a project at all, so the documented remedy manufactures exactly
        this state;
      * a record exists but cannot be read, and none of the readable ones
        started — an unreadable record is not an absent launch.
    """
    try:
        import json

        from botainer.state import dir as state_dir
        from botainer.state import session_record

        uuid = (outer / ".botainer" / "project-id").read_text(
            encoding="utf-8").strip()
        if not uuid:
            return None
        paths = state_dir.ensure_user_state_dir(create_if_missing=False)
        # ONE CHECK COVERS BOTH "no state root" and "no state for this project":
        # if the root is absent then this path is absent too. An explicit
        # root-exists check sat here until a mutation showed it could be deleted
        # with no test noticing — which is the demonstration this project
        # requires before removing a redundant guard, rather than a reason to
        # add a test for a branch that cannot be distinguished.
        project_state = paths.state_dir / uuid
        if not project_state.is_dir():
            return None
        sessions = project_state / "sessions"
        if not sessions.is_dir():
            return False
        unreadable = False
        for entry in sessions.iterdir():
            record = entry / session_record.RECORD_FILENAME
            try:
                data = json.loads(record.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue                      # composed, never recorded
            except Exception:
                unreadable = True             # do NOT let this read as "never"
                continue
            if data.get("started_at"):
                return True
        return None if unreadable else False
    except Exception:
        return None


#: Roots already warned about in this process. `hpc submit` resolves the project
#: THREE times, so without this a nested project printed the same four lines
#: three times over. A set beats a `warn=False` parameter: a parameter is an
#: off-switch for the only control left here, and a caller who wants quiet is
#: indistinguishable from a caller who wants the hazard hidden.
_WARNED_NESTED: set[Path] = set()


def _warn_nested_project(inner: Path, outers: list[Path]) -> None:
    """Say what is true about this shape, name every root, and point.

    Not a refusal, and not a safety verdict either — per the short-surface
    rule, this states FACTS and POINTS. "No session has run" is a measurement;
    it is deliberately NOT phrased as "so this is safe", because a session is
    only the route this code can see.

    EVERY ENCLOSING PROJECT IS CONSULTED, not just the nearest. A refuting
    review built `work/foo/bar` where `work` had run a session and `work/foo`
    had not, and the nearest-only version printed "No session has ever run" —
    a false all-clear, produced by the cheapest action the threat model already
    grants the agent (one `mkdir`), about a tree that HAD been bound. The
    precedence is therefore worst-known-wins: HAS RUN beats CANNOT TELL beats
    NEVER RAN, and the root named is the outermost one in that state.
    """
    if inner in _WARNED_NESTED:
        return
    _WARNED_NESTED.add(inner)

    measured = [(o, _outer_has_ever_run_a_session(o)) for o in outers]
    ran = [o for o, v in measured if v is True]
    unknown = [o for o, v in measured if v is None]
    # NO INDEX. Picking one root out of several is a choice, and the first
    # version picked the nearest — which a refuting review defeated with one
    # `mkdir`. Naming every root in the reported state removes the choice
    # instead of getting it right: there is no `[0]` left to be wrong.
    if ran:
        subjects, state = ran, True
    elif unknown:
        subjects, state = unknown, None
    else:
        subjects, state = outers, False
    named = ", ".join(str(o) for o in subjects)

    click.secho(
        f"botainer: NESTED PROJECT. Using {inner}, which is inside "
        + (f"another botainer project at {outers[0]}."
           if len(outers) == 1 else
           f"{len(outers)} other botainer projects: "
           + ", ".join(str(o) for o in outers) + "."),
        fg="yellow", err=True)
    if state is True:
        click.secho(
            f"  A session HAS run for {named}, so an agent has had that whole "
            f"tree bound read-write as its workspace — and an agent can create "
            f"a `.botainer/` anywhere under it. A project's config controls "
            f"plugins, mounts, network and agent permissions, so check that "
            f"{inner}/.botainer/config.yaml is the one you wrote.",
            fg="yellow", err=True)
    elif state is False:
        click.secho(
            f"  No session has ever run for {named}, so nothing has had this "
            f"tree bound as a workspace by that route.",
            fg="yellow", err=True)
    else:
        click.secho(
            f"  Could not tell whether a session has run for {named} — so "
            f"this is NOT a statement that none has.",
            fg="yellow", err=True)
    click.secho(
        f"  If an enclosing project was a mistake — "
        + ", ".join(str(o) for o in outers)
        + " — stop its sessions, then remove that project's `.botainer/` (see "
          "`botainer where` for the state it also holds). Background: "
          "docs/CAPABILITY-SURFACE.md.",
        fg="yellow", err=True)


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) looking for .botainer/project-id.

    Returns the NEAREST project root, or None if not in a botainer project.

    Project discovery returns the nearest project and warns about enclosing
    projects. Refusing during discovery would strand commands that have no
    explicit project-path option. The warning must still explain the risk of
    agent-writable nested project configuration.

    The hazard this guard was written for (threat-model audit, BS-5) is real and
    unchanged: `/workspace` is rw, and the null-bind mask that makes the project
    config trusted input covers exactly ONE path (`_MASK_REQUIRED_UNDER_RW` =
    {"/workspace": ("/workspace/.botainer",)}). Nothing masks
    `/workspace/anysubdir/.botainer/`, so a caged agent can write a project-id +
    config.yaml there, and a user who later runs a botainer command from that
    subdirectory ON THE HOST would get an AGENT-AUTHORED config driving
    plugins_enabled, mounts.extra, network.mode, agent_permissions and image.

    What was wrong was treating two shapes as one. The commands that reach here
    resolve a project by WALKING UP and take no project path of their own, so a
    refusal left a user with a stray enclosing project no way to work at all —
    and the old message's remedy, "run the command with an explicit project
    path", named something those commands do not have. (Nine `botainer hpc`
    subcommands DO take `--project`; they pass a path directly and never reach
    this function, so they were never affected either way. An earlier version of
    this comment said no command took one, which was false.)

    So: return the nearest, which is what a user standing in their own project
    means, and warn — consulting EVERY enclosing project, because the nearest
    one alone can be an agent's own `mkdir`.
    """
    p = Path(start or Path.cwd()).resolve()
    found: Path | None = None
    outers: list[Path] = []
    while True:
        if (p / ".botainer" / "project-id").exists():
            if found is None:
                found = p                      # nearest — the normal answer
            else:
                outers.append(p)
        if p.parent == p:
            break
        p = p.parent
    if found is not None and outers:
        _warn_nested_project(found, outers)
    return found


def refuse(
    cmd: str,
    msg: str,
    hint: str,
    *,
    exit_code: int = 2,
) -> NoReturn:
    """Print a typed refusal + remediation hint to stderr; sys.exit."""
    click.secho(f"botainer {cmd}: {msg}", fg="red", err=True)
    click.secho(f"    hint: {hint}", fg="cyan", err=True)
    sys.exit(exit_code)


def find_session_by_prefix(
    records: list[session_record.SessionRecord],
    prefix: str | None,
    cmd: str,
) -> session_record.SessionRecord | None:
    """Resolve a session prefix to a single record.

    Returns:
        - None if no records OR prefix is None (caller should default)
        - the matching record if prefix is unambiguous
        - calls refuse() and exits if prefix is ambiguous (>1 match)
    """
    if not records:
        return None
    if not prefix:
        return None
    matches = [r for r in records if r.session_id.startswith(prefix)]
    if not matches:
        return None
    if len(matches) > 1:
        ids = ", ".join(r.session_id[:12] for r in matches[:5])
        refuse(
            cmd,
            f"session ID {prefix!r} is an ambiguous prefix",
            f"matches: {ids}. Use a longer prefix.",
        )
    return matches[0]


def apptainer_missing_advice(*, build_cmd: str = "") -> str:
    """What to tell a user who has no apptainer/singularity on PATH.

    ONE TEXT, THREE CALLERS. Until the same failure produced three
    different answers, and the loudest was the wrong one:

      cli/hpc.py      "refused: neither `apptainer` nor `singularity` on PATH"
                      — and nothing else. No fix named at all.
      cli/image.py    "On a Slurm cluster, try `module load apptainer`."
      cli/doctor.py   "Many clusters install apptainer ONLY on compute nodes;
                       `module load apptainer` on the login node won't help."

    doctor was right and the other two were the ones a user actually hits
    first. The laptop equivalent (adapters/docker.py) has done this properly
    for a long time: it distinguishes daemon-down from image-absent, names the
    fix for each, AND refutes the wrong fix by name. This brings the platform
    that IS the product up to what the dev scaffold already had.

    Note the shape of the advice, not just its content: it says WHERE to run
    the commands. "salloc" on the login node, the build on the compute node —
    those are two different places, and an instruction that does not say which
    is a defect in this project by rule.
    """
    build = build_cmd or "botainer image build agent-claude --runtime apptainer"
    return (
        "Apptainer is usually installed on COMPUTE nodes only, not on the\n"
        "login node — so `module load apptainer` here typically does NOT help.\n"
        "Get an interactive allocation first, then build from there:\n"
        "    salloc -t 60 -c 4\n"
        "        ^ run on the LOGIN node; waits for a slot, then drops you\n"
        "          onto a compute node\n"
        f"    {build}\n"
        "        ^ run there\n"
        "    exit\n"
        "        ^ back to the login node\n"
        "On a laptop instead? Pass `--runtime docker`.\n"
        "`botainer doctor` reports which runtimes this host actually has."
    )


def confirm_no_other_shared_session(project_root, *, agent_family: str,
                                    as_json: bool = False,
                                    assume_yes: bool = False) -> None:
    """Stop before a second concurrent shared-auth session kills the first.

    THE ONE IMPLEMENTATION. `botainer start` and `botainer hpc submit` both
    launch a shared-auth session and both must ask. It was written in start.py
    first and not here, which put the guard on the laptop path and left the
    CLUSTER path — the product's main path — unprotected. That is the
    sibling-drift class this project keeps repeating; a third entry point must
    call this too rather than grow a third copy.

    Shared mode holds ONE credential. Refreshing mints a new refresh token and
    REVOKES the old one (measured), so two live sessions log each
    other out — and the loser finds out hours later as an "expired" token that
    reads like a server fault.

    ADVISORY, NOT A BOUNDARY, and deliberately so: any failure reading the
    bookkeeping returns silently and the launch proceeds. A missed warning
    costs a re-login; a launch refused because a record would not parse costs
    the user their working session for no reason. Nothing should ever describe
    this as protection.
    """
    import sys

    import click

    from botainer.core import identity as _identity
    from botainer.state import credential_events as _ce
    from botainer.state import dir as _sd

    try:
        uid = _identity.read_project_id(project_root) or ""
        paths = _sd.ensure_user_state_dir(create_if_missing=True)
        holders = _ce.live_shared_holders(paths, exclude_uuid=uid,
                                          agent_family=agent_family)
    except Exception:                                           # noqa: BLE001
        return
    if not holders:
        return

    lines = [
        f"⛔ Another {agent_family} session is already using the shared "
        f"{agent_family} credential.",
        "",
        "   Shared mode holds ONE login PER AGENT. Starting here will log that",
        "   session",
        "   out — refreshing revokes the other token, and it will fail with",
        '   an "expired" error that looks like a server problem.',
        "",
    ]
    for h in holders:
        where = f" on {h['host']}" if h.get("host") else ""
        lines.append(f"   • {h['project_root']}{where}")
        if h.get("started_at"):
            lines.append(f"     started {h['started_at']}")
    lines += [
        "",
        "   To run both at once, give this project its own login:",
        "       botainer auth use isolated",
    ]
    msg = "\n".join(lines)

    if as_json or assume_yes or not sys.stdin.isatty():
        click.secho(msg, fg="yellow", err=True)
        click.secho("   (continuing — non-interactive or --yes)",
                    fg="yellow", err=True)
        return
    click.secho(msg, fg="red", bold=True, err=True)
    click.echo("", err=True)
    if not click.confirm("   Start anyway and log the other session out?",
                         default=False):
        raise SystemExit(
            "Not started. The other session keeps the credential.")
