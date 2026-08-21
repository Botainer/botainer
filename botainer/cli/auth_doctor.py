"""`botainer auth doctor` — why is one project logged in and another not?

WHY THIS EXISTS. On the user hit "a project I started yesterday
works, a new one says login expired". I produced three confident explanations
from reading code, and all three were wrong; one `ls -la` from the user settled
it in seconds. This command is that `ls -la`, productised — it reports the
OBSERVED state of every credential holder on the host instead of inviting
another theory.

There are two DIFFERENT failure modes behind the same symptom, and the whole
point of this command is that they need different fixes:

  1. SYMLINK BROKEN (confirmed, observed on disk). Shared mode gives each
     project a `.credentials.json` SYMLINK into the host-wide store, so that
     writes land in one shared place. But Claude Code saves credentials with an
     atomic `rename()`, and rename() replaces the SYMLINK ITSELF with a regular
     file. After a project's first token refresh it silently stops sharing: it
     holds a private copy, and the shared store keeps whatever it had. Nothing
     announces this. A new project reads the stale shared store.

  2. TOKEN ROTATION — SETTLED (EF-1): refreshing mints a new
     refresh token AND invalidates the old one (HTTP 400). This command
     reports what is on disk; `auth rotation-probe` re-measures the
     server behaviour.
     If the OAuth server issues a NEW refresh token on each refresh and
     invalidates the old one, then multiple holders are unsound in principle:
     whoever refreshes first silently logs everyone else out, and no amount of
     syncing fixes it. `broker/oauth_refresh.py` handles rotation defensively
     (`if resp.get("refresh_token")`), which proves someone CONSIDERED it — not
     that it happens. Treating that defensive branch as evidence is what led me
     to declare shared mode architecturally doomed before checking.

The distinction is decidable from data already on this disk: if several holders
have DIVERGED (mode 1) but all still carry the SAME refresh token, rotation is
not happening and shared mode is fixable. If their refresh tokens DIFFER,
rotation is real and only a single-holder design (the broker) can work.

SECRET HANDLING. No token material and no hash of any token is ever printed.
Holders are compared to each other and reported as opaque group labels ("group
A", "group B"). Two holders in the same group have byte-identical tokens; that
is the entire fact needed, and it discloses nothing about the token itself.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.state import dir as state_dir


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _oauth_block(data: dict | None, oauth_key: str = "claudeAiOauth") -> dict:
    """The nested block holding the tokens, per agent.

    Claude Code uses `claudeAiOauth`; codex uses `tokens`. Hardcoding Claude's
    was half of why `--agent codex` reported nothing logged in — the other half
    was the filename (see _AGENT_CREDENTIALS).
    """
    if not isinstance(data, dict):
        return {}
    blk = data.get(oauth_key)
    return blk if isinstance(blk, dict) else {}


def _is_empty_stub(blk: dict) -> bool:
    """A credential file that parses but carries no usable token.

    Observed on the cluster: two projects held `.credentials.json`
    with `expiresAt: 0` and no refreshToken, written minutes apart. botainer
    writes no such file (the only writer of a claudeAiOauth block is the
    broker), so these are almost certainly Claude Code's own startup stub from
    a session where no login ever completed.

    They matter because the file EXISTS. Any check that asks "is there a
    credential here?" says yes, and the project is logged out anyway. That is a
    third state, distinct from both "healthy" and "expired", and it needs its
    own words.
    """
    return not (blk.get("refreshToken") or blk.get("refresh_token"))


def _expiry_note(blk: dict) -> str:
    if _is_empty_stub(blk):
        return "EMPTY — file exists but holds no token (this project is logged out)"
    ms = blk.get("expiresAt")
    if not isinstance(ms, (int, float)):
        return "no expiry recorded"
    when = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    delta = (when - datetime.now(tz=timezone.utc)).total_seconds()
    stamp = when.strftime("%Y-%m-%d %H:%M UTC")
    if delta < 0:
        return f"access token EXPIRED {stamp} ({int(-delta // 3600)}h ago)"
    return f"access token valid until {stamp} ({int(delta // 60)} min)"


# ── Per-agent credential shapes ──────────────────────────────────────────────
#
# `--agent` accepted any value and defaulted to agent-claude, but the FILENAME
# and the OAuth block key were hardcoded to Claude's. Codex stores auth.json
# with a `tokens` object, so with two real codex credentials on disk the command
# printed:
#
#     No agent-codex credentials found anywhere under this root.
#     Nothing is logged in. `botainer auth login` to start.
#
# A confident false negative in the one command whose docstring says it exists
# to "report the OBSERVED state instead of another theory". The user hit it
#. Meanwhile `auth status`, in the same command group, found both
# files — because IT resolves the filename per family. This copies that model.
#
# A second bug in the same flag: the directory component was built as
# agent-<name>, so a bare `--agent codex` could not match either. Both spellings
# are normalised now.
_AGENT_CREDENTIALS = {
    "agent-claude": {
        "filenames": (".credentials.json",),
        "oauth_key": "claudeAiOauth",
    },
    "agent-codex": {
        # auth.json for OAuth; api_key is the isolated-mode raw key file, which
        # holds no token to compare but DOES mean "logged in" — reporting
        # "nothing is logged in" at a project that has one is the same lie in a
        # smaller size.
        "filenames": ("auth.json", "api_key"),
        "oauth_key": "tokens",
    },
}


def _normalise_agent(agent: str) -> str:
    """`codex` and `agent-codex` must both work; only one used to."""
    return agent if agent.startswith("agent-") else f"agent-{agent}"


def _cred_shape(agent: str) -> dict:
    """Filenames + OAuth block for an agent. Unknown agents are NOT guessed."""
    return _AGENT_CREDENTIALS.get(_normalise_agent(agent),
                                  _AGENT_CREDENTIALS["agent-claude"])


def _refuse_unknown_agent(agent: str) -> None:
    """`--agent frobnicator` must not report "nothing is logged in".

    It did, which is the same confident false negative the codex fix removed:
    an agent that does not exist looked identical to an agent with no
    credentials. The docstring here even CLAIMED the caller distinguished them.
    It did not. `auth login` already refuses unknown agents with the installed
    list; do the same rather than answer a question about a thing that is not
    there.
    """
    if _normalise_agent(agent) in _AGENT_CREDENTIALS:
        return
    known = ", ".join(sorted(a[len("agent-"):] for a in _AGENT_CREDENTIALS))
    click.secho(
        f"refused: unknown agent {agent!r}. This command reports credential "
        f"state for a known agent family; it cannot say anything about one it "
        f"does not recognise.\n  Known: {known}",
        fg="red", err=True)
    raise SystemExit(2)


#: Where the shared-auth dir is bound INSIDE the container. The per-project
#: symlink is deliberately created with this container-absolute target
#: (plugins/agent-claude-shared/hooks/pre_session.py:143) because that is the
#: path the agent resolves it through.
_CONTAINER_SHARED_PREFIX = "/shared-auth/"


class _Holder:
    """One credential file found on disk.

    RUNS ON THE HOST, points at paths written for the CONTAINER. The first
    version of this class missed that and produced an inverted report on real
    data: every intact symlink was labelled "DANGLING /
    unreadable" in red, because `/shared-auth/agent-claude/...` does not exist
    on the host — while the projects that had genuinely broken away, and were
    holding stale private copies, were listed as unremarkable "regular file".

    The user would have concluded the two healthy projects were broken and the
    four broken ones were fine. A diagnostic that inverts its own signal is
    worse than no diagnostic, so the container path is now translated to its
    host equivalent before anything is judged.
    """

    def __init__(self, label: str, path: Path, state_root: Path,
                 oauth_key: str = "claudeAiOauth") -> None:
        self.oauth_key = oauth_key
        self.label = label
        self.path = path
        self.is_symlink = path.is_symlink()
        self.link_target = os.readlink(path) if self.is_symlink else ""
        #: The symlink names a container path; resolve it against the state
        #: root to get the file this actually points at from out here.
        self.host_target: Path | None = None
        if self.link_target.startswith(_CONTAINER_SHARED_PREFIX):
            rel = self.link_target[1:]          # strip the leading slash
            self.host_target = state_root / rel

        if self.host_target is not None:
            self.exists = self.host_target.exists()
            read_from = self.host_target
        else:
            self.exists = path.exists()         # follows symlinks
            read_from = path
        data = _read_json(read_from) if self.exists else None
        self.blk = _oauth_block(data, self.oauth_key)
        self.parsed = bool(self.blk)
        self.refresh = self.blk.get("refreshToken") or self.blk.get("refresh_token") or ""
        #: Expiry in epoch-ms, or 0 when unknown. Used ONLY to decide whether a
        #: broken-away project copy is newer than the shared store — i.e.
        #: whether a human needs to act, or the next start repairs it. Unknown
        #: (0) deliberately sorts as "older", so an agent whose expiry we cannot
        #: read is reported as the harmless case rather than alarmed about.
        # int() on a float infinity raises OverflowError, and this file is
        # bound RW INTO THE CONTAINER — a caged agent can write
        # `"expiresAt": 1e999` and crash the diagnostic with a raw traceback
        # before it prints anything. Clamp instead of trusting the type check;
        # isinstance(1e999, float) is True.
        _exp = self.blk.get("expiresAt")
        try:
            self.expires_ms = int(_exp) if isinstance(_exp, (int, float)) else 0
        except (OverflowError, ValueError):
            self.expires_ms = 0
        try:
            self.mtime = path.stat().st_mtime if self.exists else 0.0
        except OSError:
            self.mtime = 0.0


def _group_labels(holders: list[_Holder]) -> dict[int, str]:
    """Map holder index -> 'A'/'B'/... by refresh-token EQUALITY.

    Deliberately not a hash: nothing derived from the secret is displayed, and
    grouping is all the diagnosis needs. Holders with no readable token get no
    group.
    """
    groups: dict[str, str] = {}
    out: dict[int, str] = {}
    for i, h in enumerate(holders):
        if not h.refresh:
            continue
        if h.refresh not in groups:
            groups[h.refresh] = chr(ord("A") + len(groups))
        out[i] = groups[h.refresh]
    return out


def _collect(root: Path, agent: str) -> list[_Holder]:
    agent = _normalise_agent(agent)
    names = _cred_shape(agent)["filenames"]
    holders: list[_Holder] = []
    for fname in names:
        shared = root / "shared-auth" / agent / fname
        if shared.is_symlink() or shared.exists():
            holders.append(_Holder("shared store (host-wide)", shared, root,
                                   _cred_shape(agent)["oauth_key"]))

    state = root / "state"
    if state.is_dir():
        for proj in sorted(state.iterdir()):
            if not proj.is_dir() or proj.is_symlink() or proj.name == "by-name":
                continue
            base = proj / "data" / agent / "profiles"
            if not base.is_dir():
                continue
            for prof in sorted(base.iterdir()):
                for fname in names:
                    cred = prof / fname
                    if cred.is_symlink() or cred.exists():
                        name = _project_label(root, proj.name)
                        holders.append(
                            _Holder(f"project {name} [{prof.name}]", cred, root,
                                    _cred_shape(agent)["oauth_key"]))
    return holders


def _project_label(root: Path, uuid: str) -> str:
    """Human name via the by-name symlinks, falling back to a short uuid."""
    byname = root / "state" / "by-name"
    if byname.is_dir():
        for link in byname.iterdir():
            try:
                if link.is_symlink() and Path(os.readlink(link)).name == uuid:
                    return link.name
            except OSError:
                continue
    return uuid[:8]


@click.command("doctor")
@click.option("--agent", default="agent-claude", show_default=True,
              help="Which agent's credentials to inspect.")
@handle_refusals
def auth_doctor(agent: str) -> None:
    """Show every credential holder on this host and whether they agree.

    Read-only. Prints no token material and no hash of one — holders are
    compared to each other and reported as opaque group labels.
    """
    _refuse_unknown_agent(agent)
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    root = paths.root
    click.secho(f"State root: {root}", bold=True)

    holders = _collect(root, agent)
    if not holders:
        click.echo(f"\nNo {agent} credentials found anywhere under this root.")
        click.echo("  Nothing is logged in. `botainer auth login` to start.")
        return

    groups = _group_labels(holders)
    click.secho(f"\nCredential holders ({len(holders)})", fg="cyan", bold=True)
    broken_links: list[_Holder] = []
    for i, h in enumerate(holders):
        click.echo(f"\n  {h.label}")
        click.echo(f"    {h.path}")
        if h.is_symlink:
            if h.host_target is not None:
                # A CONTAINER path. Resolves fine inside the cage; it only
                # looks broken from out here. Say which is which.
                state = ("OK — still sharing" if h.exists
                         else "shared store MISSING on the host")
                click.echo(f"    kind: SYMLINK -> {h.link_target}   ({state})")
                click.echo(f"          (container path; on this host = "
                           f"{h.host_target})")
            else:
                state = "OK" if h.exists else "DANGLING"
                click.echo(f"    kind: SYMLINK -> {h.link_target}   ({state})")
        else:
            click.echo("    kind: regular file")
            if "project " in h.label:
                broken_links.append(h)
        if not h.exists:
            click.secho("    unreadable (dangling symlink)", fg="red")
            continue
        if h.path.name == "api_key":
            # Deliberately NOT JSON: plugins/agent-codex/hooks/login.py writes a
            # raw one-line key because the image entrypoint cats it into
            # OPENAI_API_KEY, and JSON would export the whole blob as the key.
            # Reporting it red as "not valid credential JSON" was a false
            # statement about a healthy file — the same class of lie as the
            # "nothing is logged in" this command was fixed to stop telling.
            click.secho("    API key (raw file) — logged in; no token to compare",
                        fg="green")
            continue
        if not h.parsed:
            click.secho("    could not parse — not valid credential JSON", fg="red")
            continue
        click.echo(f"    {_expiry_note(h.blk)}")
        g = groups.get(i)
        click.echo(f"    refresh token: group {g}" if g
                   else "    refresh token: absent from this file")
        if h.mtime:
            when = datetime.fromtimestamp(h.mtime, tz=timezone.utc)
            click.echo(f"    last written: {when.strftime('%Y-%m-%d %H:%M UTC')}")

    _verdict(holders, groups, broken_links)


def _write_spread_days(holders) -> int | None:
    """Days between the oldest and newest holder write, or None."""
    times = [h.mtime for h in holders if h.mtime]
    if len(times) < 2:
        return None
    return int((max(times) - min(times)) / 86400)


def _verdict(holders, groups, broken_links) -> None:
    """State what the data shows — and what it does NOT show."""
    click.secho("\n" + "=" * 60, fg="cyan")
    click.secho("What this means", fg="cyan", bold=True)

    distinct = len(set(groups.values()))
    projects = [h for h in holders if "project " in h.label]
    stubs = [h for h in holders if h.parsed and _is_empty_stub(h.blk)]

    if broken_links:
        # SPLIT BY WHETHER IT MATTERS. Every one of these is a project that
        # broke away from the shared store, but the consequence differs
        # entirely, and the old verdict alarmed identically about both.
        #
        # Reported: three projects flagged in red, all last written
        # WEEKS earlier with long-expired tokens. Nothing was wrong with them —
        # agent-claude-shared/pre_session.py compares expiresAt on the next
        # start, declines to back-fill an older copy, backs it up and re-links.
        # The state repairs itself the moment the project is used again.
        #
        # The case that DOES need a human is the opposite one: a project copy
        # NEWER than the shared store, which holds a refreshed token the shared
        # store has not seen. Re-linking naively would discard it.
        # The comparison is only meaningful when there IS a shared store with a
        # readable expiry to compare against. Audit caught the
        # opposite: with no shared store — the normal state after `auth use
        # isolated` — max(..., default=0) made EVERY project "newer", so the
        #red alarm fired on healthy isolated projects and told the user to run a
        # remediation that does nothing. Same for a shared store that is an
        # empty stub: a 90-day-expired copy was reported as newer than it.
        #
        # No comparable baseline => report nothing rather than everything. A
        # verdict that cannot be computed must not be guessed.
        shared_exp = max((h.expires_ms for h in holders
                          if h.label.startswith("shared") and h.expires_ms),
                         default=0)
        # The comparison is expiresAt-based, which is Claude's shape. Codex
        # auth.json carries no expiresAt, so every codex holder has
        # expires_ms == 0 and lands in `stale` — correct by luck, not by
        # reasoning. Only claim the distinction where it can actually be drawn.
        if shared_exp:
            newer = [h for h in broken_links
                     if h.expires_ms and h.expires_ms > shared_exp]
        else:
            newer = []
        stale = [h for h in broken_links if h not in newer]

        if newer:
            click.secho(
                f"\n✗ {len(newer)} project(s) hold a credential NEWER than the "
                f"shared store.", fg="red", bold=True)
            click.echo(
                "  These broke away and then refreshed. Their token is the most\n"
                "  recent one; the shared store has not seen it. This is the case\n"
                "  worth acting on.")
            for h in newer:
                click.echo(f"    - {h.label}")
            click.echo(
                "\n  Next `botainer start` in one of these back-fills the newer\n"
                "  token into the shared store and re-links. Run those projects\n"
                "  before any other, so the newest token is the one that survives.")
        if stale:
            click.secho(
                f"\n· {len(stale)} project(s) hold a private copy, not a link.",
                fg="cyan")
            click.echo(
                "  They stopped sharing at some point — an agent that saves its\n"
                "  credential with rename() replaces the symlink with a private\n"
                "  file. NO ACTION IS NEEDED: the next `botainer start` in each\n"
                "  reconciles it against the shared store and re-links.")
            for h in stale:
                click.echo(f"    - {h.label}")
    elif projects:
        click.secho("\n✓ Every project still points at the shared store.",
                    fg="green")

    if stubs:
        click.secho(
            f"\n! {len(stubs)} credential file(s) exist but hold NO token.",
            fg="yellow")
        click.echo(
            "  These projects are logged out despite having a credential file,\n"
            "  so any check that only asks 'does the file exist?' will say yes.\n"
            "  botainer does not create these; they look like the agent's own\n"
            "  startup stub from a session where no login completed.")
        for h in stubs:
            click.echo(f"    - {h.label}")

    if distinct <= 1:
        click.secho(
            "\n✓ All readable holders carry the SAME refresh token.", fg="green")
        click.echo(
            "  So nothing has invalidated them out from under each other.\n"
            "  Shared mode is sound in principle here; the symlink problem\n"
            "  above is the thing that actually breaks it.")
    else:
        click.secho(
            f"\n! {distinct} DIFFERENT refresh tokens are in use.",
            fg="yellow", bold=True)
        # DIVERGENCE IS NOT EVIDENCE OF ROTATION. Stated plainly because the
        # first version of this text did not, and real data had
        # five distinct tokens written across SEVEN WEEKS — which separate
        # logins explain completely, with no rotation anywhere. Reporting that
        # as "holders have diverged, rotation invalidates the rest" would have
        # been a confident wrong conclusion from a tool built to stop exactly
        # that.
        spread = _write_spread_days(holders)
        click.echo(
            "  This ALONE does NOT show the server rotates refresh tokens.\n"
            "  Separate `auth login` runs, or `/login` inside different\n"
            "  sessions, mint independent tokens and explain divergence just\n"
            "  as well.")
        if spread is not None and spread > 2:
            click.echo(
                f"  These were written {spread} days apart, which looks much\n"
                f"  more like separate logins over time than like one\n"
                f"  credential rotating.")
        # WHAT ROTATION DOES, now that it is measured. This block used to end
        # by telling the user to go run the R1 experiment and settle it, citing
        # an internal doc the distribution does not contain. The
        # experiment WAS run  (`auth rotation-test` showed a new
        # refresh token; `auth rotation-probe` showed the old one rejected with
        # HTTP 400), so the command was sending people to measure something
        # already measured, via a path they do not have. Recorded as EF-1.
        click.echo(
            "\n  Rotation itself is settled: refreshing MINTS a new refresh\n"
            "  token and INVALIDATES the old one (measured 2026-07-29; the old\n"
            "  token comes back HTTP 400). So two mount-mode sessions running\n"
            "  at once WILL log each other out — whichever refreshes first\n"
            "  kills the other's token. That is a property of the mode, not a\n"
            "  bug in your setup.\n"
            "  Re-measure any time with `botainer auth rotation-probe`.\n"
            "  Broker mode avoids it entirely: the container never holds or\n"
            "  refreshes the credential.")

    click.echo("\nWhat this does NOT tell you")
    click.echo(
        "  Whether a token is accepted by the server. Every check here is\n"
        "  local file state. A token can look perfect and still be revoked.\n"
        "  Only a real request settles that.")


# `botainer auth rotation-test` — the R1 experiment, as a command.
#
# WHY THIS IS A COMMAND AND NOT A SNIPPET. I gave the user a shell one-liner,
# then a heredoc; both failed on paste. There is a memory entry that says
# exactly this — "heredocs/nested-quotes break on paste" — written after the
# same thing happened before, and I handed over a heredoc anyway.
#
# The rule this violates is the project's own: a question the user has to ask,
# or a procedure they have to hand-assemble, is a missing capability. R1 decides
# the whole shared-auth architecture, so it deserves to be a command rather than
# a ritual.
#
# TWO STEPS, NOT A WAIT LOOP. `arm` records and backdates; the user runs a
# session; `check` compares. Deliberately not one blocking command: the session
# has to happen in a different terminal, and a blocking process would be lost to
# a closed window, an ssh drop, or a container restart. The recorded state is a
# file, so the experiment survives all three.
#
# SECRETS: only sha256[:12] digests are stored and printed. Comparing digests
# across time is the entire measurement; the tokens themselves never leave the
# credential file.
# ==========================================================================

_ROTATION_STATE = "rotation-test.json"


def _digest(s: str) -> str:
    import hashlib
    return hashlib.sha256((s or "").encode()).hexdigest()[:12]


def _newest_usable_credential(root: Path, agent: str) -> Path | None:
    """The most recently written credential that actually holds a refresh token.

    Newest matters: a weeks-old file may have a refresh token the server has
    already forgotten, and the experiment would then fail for a reason that has
    nothing to do with rotation — the exact false negative that wastes a trial.
    """
    best: tuple[float, Path] | None = None
    for h in _collect(root, agent):
        if not h.exists or not h.refresh:
            continue
        src = h.host_target or h.path
        try:
            mt = src.stat().st_mtime
        except OSError:
            continue
        if best is None or mt > best[0]:
            best = (mt, src)
    return best[1] if best else None


def _resolve_credential_path(cred: Path, root: Path, agent: str):
    """(file to read, was_relinked). None if nothing readable can be found.

    Handles the three shapes the armed path can have by check time:
      - still a regular file            -> itself
      - a symlink with a CONTAINER path -> the host equivalent under the root
      - vanished, project re-linked     -> the shared store
    """
    if cred.is_symlink():
        target = os.readlink(cred)
        if target.startswith(_CONTAINER_SHARED_PREFIX):
            host = root / target[1:]
            return (host, True) if host.exists() else (None, True)
        return (cred, False) if cred.exists() else (None, False)
    if cred.exists():
        return cred, False
    shared = root / "shared-auth" / agent / ".credentials.json"
    return (shared, True) if shared.exists() else (None, True)


@click.command("rotation-test")
@click.argument("action", type=click.Choice(["arm", "check", "restore"]))
@click.option("--agent", default="agent-claude", show_default=True)
@handle_refusals
def rotation_test(action: str, agent: str) -> None:
    """Settle whether refresh tokens ROTATE (experiment R1).

    \b
      botainer auth rotation-test arm      # record + force the next refresh
      ...start a session, ask the agent anything, exit...
      botainer auth rotation-test check    # compare and report
      botainer auth rotation-test restore  # put the backup back

    Only 12-char digests are stored or shown; no token material is written
    anywhere or printed.
    """
    import shutil
    import time

    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    root = paths.root
    statefile = root / _ROTATION_STATE

    if action == "arm":
        # Prefer the SHARED STORE when it holds a token. In shared mode — the
        # default — `pre_session` re-links every project to it at session
        # start, so the shared file is the one the agent actually refreshes.
        # Arming a project file there measures a credential nothing touched.
        shared = root / "shared-auth" / agent / ".credentials.json"
        cred = None
        if shared.exists() and (_oauth_block(_read_json(shared)).get("refreshToken")):
            cred = shared
        if cred is None:
            cred = _newest_usable_credential(root, agent)
        if cred is None:
            click.secho("refused: no credential file holds a refresh token.",
                        fg="red", err=True)
            click.echo("  Run `botainer auth login` first.")
            raise SystemExit(2)
        blk = _oauth_block(_read_json(cred))
        backup = root / f"rotation-test-backup-{int(time.time())}.json"
        shutil.copy2(cred, backup)

        before = {
            "path": str(cred),
            "backup": str(backup),
            "refresh": _digest(blk.get("refreshToken") or ""),
            "access": _digest(blk.get("accessToken") or ""),
            "armed_at": time.time(),
        }
        statefile.write_text(json.dumps(before, indent=2), encoding="utf-8")

        # Backdate so the next real API call must refresh, instead of waiting
        # out an access token's ~1h life for every trial.
        doc = _read_json(cred) or {}
        doc.setdefault("claudeAiOauth", {})["expiresAt"] = int((time.time() - 600) * 1000)
        cred.write_text(json.dumps(doc), encoding="utf-8")

        click.secho("ARMED.", fg="green", bold=True)
        click.echo(f"  file   : {cred}")
        click.echo(f"  backup : {backup}")
        click.echo(f"  refresh: {before['refresh']}   access: {before['access']}")
        click.echo("\nNow, in a session:")
        click.echo("  1. start botainer in the project that owns that file")
        click.echo("  2. ask the agent anything (forces a real API call)")
        click.echo("  3. exit")
        click.echo("\nThen run:  botainer auth rotation-test check")
        return

    if not statefile.exists():
        click.secho("refused: nothing armed.", fg="red", err=True)
        click.echo("  Run `botainer auth rotation-test arm` first.")
        raise SystemExit(2)
    rec = json.loads(statefile.read_text(encoding="utf-8"))
    cred = Path(rec["path"])

    if action == "restore":
        shutil.copy2(rec["backup"], cred)
        click.secho(f"restored {cred} from {rec['backup']}", fg="green")
        return

    # RESOLVE THE SAME WAY `auth doctor` DOES. The armed file can legitimately
    # change KIND between arm and check: in shared mode, `pre_session`
    # reconciles a project back to a SYMLINK into the shared store, and that
    # symlink carries a CONTAINER path (/shared-auth/...) which does not exist
    # on the host. Reported as "refused: ... is gone" — the same
    # container-path-read-as-host-path mistake this module had already fixed in
    # _Holder, reintroduced here days later. Fixing it in one place and not the
    # other is what made it possible; hence the shared prefix constant.
    read_from, relinked = _resolve_credential_path(cred, root, agent)
    if read_from is None:
        click.secho(f"refused: cannot find the armed credential.", fg="red",
                    err=True)
        click.echo(f"  armed : {cred}")
        click.echo("  It is neither a readable file nor a symlink into the")
        click.echo("  shared store. Re-arm and try again.")
        raise SystemExit(2)
    if relinked and str(read_from) != rec["path"]:
        # REFUSE TO COMPARE ACROSS FILES. The before-digests came from the
        # armed file; this is a different one, with a different token, so any
        # difference is guaranteed and means nothing. Reporting "both changed"
        # here would print ROTATION CONFIRMED off an apples-to-oranges
        # comparison — a fabricated result, and the exact failure mode this
        # command exists to avoid.
        click.secho("\nINCONCLUSIVE — the armed file is no longer the one in "
                    "use.", fg="yellow", bold=True)
        click.echo(f"  armed : {rec['path']}")
        click.echo(f"  now   : {read_from}")
        click.echo(
            "\n  `pre_session` re-linked this project to the shared store when\n"
            "  the session started, so the credential the agent refreshed is\n"
            "  not the one that was armed. Comparing them would compare two\n"
            "  different tokens and always look like rotation.\n"
            "\n  Re-arm now (it will target the shared store) and repeat:\n"
            "      botainer auth rotation-test arm")
        return
    blk = _oauth_block(_read_json(read_from))
    now_refresh = _digest(blk.get("refreshToken") or "")
    now_access = _digest(blk.get("accessToken") or "")

    click.secho("=" * 60, fg="cyan")
    click.echo(f"  before   refresh: {rec['refresh']}   access: {rec['access']}")
    click.echo(f"  after    refresh: {now_refresh}   access: {now_access}")
    click.secho("=" * 60, fg="cyan")

    if now_access == rec["access"]:
        click.secho("\nNO REFRESH HAPPENED — the access token did not change.",
                    fg="yellow", bold=True)
        click.echo(
            "  Inconclusive, not a result. The session made no real API call,\n"
            "  or it used a different credential file than the one armed.\n"
            "  Re-arm and make sure the session belongs to that project.")
        return

    if now_refresh == rec["refresh"]:
        click.secho("\nRESULT: NO ROTATION.", fg="green", bold=True)
        click.echo(
            "  The access token changed; the refresh token did NOT. So a\n"
            "  refresh does not invalidate other holders, and shared mode is\n"
            "  sound in principle — the symlink bug is the whole story.")
    else:
        click.secho("\nRESULT: ROTATION CONFIRMED.", fg="red", bold=True)
        click.echo(
            "  Both changed. Each refresh mints a new refresh token, so only\n"
            "  the most recent holder can be valid and multi-holder mount mode\n"
            "  cannot work. `botainer auth use broker` is the single-holder\n"
            "  design.")
    click.echo("\nRun this a SECOND time before trusting it: one observation")
    click.echo("cannot tell 'never rotates' from 'rotates sometimes', and")
    click.echo("intermittent is the worst case for us.")
    click.echo(f"\nrestore if needed:  botainer auth rotation-test restore")
