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

from botainer.core.refusal import Refused, RefusalCategory
from botainer.state import session_record


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) looking for .botainer/project-id.

    Returns the project root path or None if not in a botainer project.
    """
    p = Path(start or Path.cwd()).resolve()
    found: Path | None = None
    while True:
        if (p / ".botainer" / "project-id").exists():
            if found is None:
                found = p                      # nearest — the normal answer
            else:
                # SECURITY (threat-model audit, BS-5): a botainer
                # project NESTED inside another botainer project is the exact
                # shape a caged agent can manufacture. `/workspace` is rw, and
                # the null-bind mask that makes the project config trusted input
                # covers exactly ONE path (`_MASK_REQUIRED_UNDER_RW` =
                # {"/workspace": ("/workspace/.botainer",)}). Nothing masks
                # `/workspace/anysubdir/.botainer/`, so the agent can write a
                # project-id + config.yaml there; a user who later runs any
                # botainer command from that subdirectory ON THE HOST would get
                # an AGENT-AUTHORED config driving plugins_enabled, mounts.extra,
                # network.mode, agent_permissions and image.
                #
                # The mask is structural over a domain strictly smaller than its
                # consumer's — so close the gap on the consumer side: refuse the
                # ambiguous shape rather than silently preferring the inner one.
                # Legitimate nesting is rare; pass an explicit project path.
                raise Refused(
                    RefusalCategory.IDENTITY_AMBIGUOUS,
                    f"refusing to use the botainer project at {found} because it "
                    f"is NESTED inside another botainer project at {p}.\n"
                    f"  A caged agent has read-write access to its workspace and "
                    f"can create a `.botainer/` directory there, so a nested "
                    f"project may be agent-authored — and its config controls "
                    f"plugins, mounts, network and permissions.\n"
                    f"  If {found} is genuinely yours, run the command from that "
                    f"directory with an explicit project path, or remove the "
                    f"outer/inner `.botainer/` you did not intend.",
                )
        if p.parent == p:
            return found
        p = p.parent


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


def confirm_no_other_shared_session(project_root, *, as_json: bool = False,
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
        holders = _ce.live_shared_holders(paths, exclude_uuid=uid)
    except Exception:                                           # noqa: BLE001
        return
    if not holders:
        return

    lines = [
        "⛔ Another session is already using the shared credential.",
        "",
        "   Shared mode holds ONE login. Starting here will log that session",
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
