"""Inspect shared and per-project credential state using local evidence.

Atomic replacement can replace a credential symlink with a per-project regular
file, leaving the shared store stale. Separate holders can also contain different
tokens. These are distinct observations and do not, by themselves, establish
server-side rotation or whether a token will be accepted.

`auth doctor` distinguishes missing files, broken links, empty credentials,
known expiry and token divergence. It compares holders using opaque group labels
without printing token values or fingerprints. Direct refresh diagnostics are
separate commands with their own explicit consent and output contracts.
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

    A credential file may parse while containing no usable refresh token. File
    presence alone does not establish authentication; report this state
    separately from a credential with a usable token or a known expiry.
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
# Resolve the credential filename and OAuth block key per agent family.
# Codex uses auth.json with a tokens object; looking only for Claude's shape
# would incorrectly report that no Codex credentials were present.
#
# A second bug in the same flag: the directory component was built as
# agent-<name>, so a bare `--agent codex` could not match either. Both spellings
# are normalised now.
#
# THE ENDPOINT LIVES HERE TOO, and that placement is the whole fix for the
# cross-provider hazard. `rotation-probe` used to hold its own module-level
# `_TOKEN_ENDPOINT = https://api.anthropic.com/…` while `--agent` accepted any
# name. Measured with a `.credentials.json` planted under `agent-codex`: the
# token in it was handed to the ANTHROPIC token endpoint. Nothing checked the
# family — the only reason that had never happened in practice is that codex's
# real file is called `auth.json`, so the lookup missed. A coincidence of
# spelling is not a security boundary.
#
# Now the filename and the endpoint come out of the SAME entry, so they cannot
# disagree: there is no way to obtain an endpoint except by naming a family, and
# naming a family also fixes which file is read.
_AGENT_CREDENTIALS = {
    "agent-claude": {
        "filenames": (".credentials.json",),
        "oauth_key": "claudeAiOauth",
        #: Field carrying the access-token expiry, in epoch MILLISECONDS.
        #: `rotation-test arm` backdates it to force the next call to refresh.
        "expiry_field": "expiresAt",
        #: PINNED, never read from config: the durable refresh token is sent
        #: here, so a hostile `.botainer/config.yaml` must not redirect it.
        #: Same values the broker plugin pins.
        "token_endpoint": "https://api.anthropic.com/v1/oauth/token",
        "oauth_client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    },
    "agent-codex": {
        # auth.json for OAuth; api_key is the isolated-mode raw key file, which
        # holds no token to compare but DOES mean "logged in" — reporting
        # "nothing is logged in" at a project that has one is the same lie in a
        # smaller size.
        "filenames": ("auth.json", "api_key"),
        "oauth_key": "tokens",
        #: None means WE DO NOT KNOW, and it is load-bearing. Codex auth.json
        #: carries no expiresAt (the same gap `auth doctor` documents where it
        #: declines to compare codex holders by expiry). `arm` therefore
        #: REFUSES for codex instead of inventing a field: writing a guess
        #: corrupts the very credential the experiment is measuring, which is
        #: exactly what it used to do.
        "expiry_field": None,
        #: None means WE HAVE NO OAUTH REFRESH ROUTE FOR THIS FAMILY, and it is
        #: as load-bearing as `expiry_field` above. It is NOT a placeholder to
        #: be filled in with whatever endpoint is to hand: a caller that finds
        #: None must refuse BY NAME, so the user is told codex is unsupported
        #: rather than being sent to log in again for a file that is already
        #: there. The alternative — falling back to claude's endpoint — is the
        #: cross-provider leak this field exists to make unrepresentable.
        "token_endpoint": None,
        "oauth_client_id": None,
    },
}


def _refresh_of(blk: dict) -> str:
    """The refresh token, under either spelling. Claude camelCases; codex not.

    One place, because reading it in two places with one spelling is how
    `rotation-test` came to digest the empty string and print it as a result.
    """
    return blk.get("refreshToken") or blk.get("refresh_token") or ""


def _access_of(blk: dict) -> str:
    """The access token, under either spelling. See `_refresh_of`."""
    return blk.get("accessToken") or blk.get("access_token") or ""


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
                 oauth_key: str = "claudeAiOauth",
                 in_broker_state: bool = False) -> None:
        self.oauth_key = oauth_key
        #: A credential under `broker-state/` is POLLUTION, not a login, and
        #: the difference changes the ADVICE. Carried as a flag rather than
        #: sniffed out of `label`, because the first version of the broker-state
        #: walk did the latter: the label contained "project ", so the holder
        #: landed in `broken_links` and the verdict told the user to launch the
        #: polluted project first "so the newest token survives" — the one
        #: action that hands the credential to the agent. A reviewer measured
        #: that. Advice derived from display text will drift the moment the
        #: display text changes.
        self.in_broker_state = in_broker_state
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
        self.refresh = _refresh_of(self.blk)
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
            # Search both profiles and broker-state. Credentials in either
            # directory are part of the project data tree and must appear in
            # the report.
            for kind in ("profiles", "broker-state"):
                base = proj / "data" / agent / kind
                if not base.is_dir():
                    continue
                # RECURSIVE, and it accepts the `.pre-shared` backup
                # spelling. A reviewer found both gaps: the profile directory
                # is bound whole, so `…/<profile>/sub/.credentials.json` is
                # delivered exactly like a top-level one, and this module's own
                # docs already say the `.pre-shared` backups ARE credentials.
                #
                # `credential_files_under` is shared with `auth status` rather
                # than reimplemented, because two copies of a scan drift — the
                # defect this whole change exists to fix was one surface
                # looking in a place another surface did not. Its result is
                # intersected with THIS agent's filenames so `--agent` stays
                # meaningful: a codex `auth.json` is not an agent-claude
                # holder.
                from botainer.core.history_carry import credential_files_under
                wanted = set(names) | {f"{n}.pre-shared" for n in names}
                for cred in credential_files_under(base):
                    if cred.name not in wanted:
                        continue
                    prof_name = (cred.relative_to(base).parts[0]
                                 if cred.relative_to(base).parts else "?")
                    name = _project_label(root, proj.name)
                    where = (f"project {name} [{prof_name}]"
                             if kind == "profiles"
                             else f"project {name} [{prof_name}] "
                                  f"** in broker-state, which is not "
                                  f"supposed to hold one **")
                    holders.append(
                        _Holder(where, cred, root,
                                _cred_shape(agent)["oauth_key"],
                                in_broker_state=(kind == "broker-state")))
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
    stray_broker: list[_Holder] = []
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
            if h.in_broker_state:
                # NOT a broken mount-mode link. `broken_links` drives advice
                # about reconciling per-project copies against the shared
                # store; nothing reconciles broker-state, and the remedy that
                # branch prints is actively wrong here.
                stray_broker.append(h)
            elif "project " in h.label:
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

    _verdict(holders, groups, broken_links, stray_broker)


def _write_spread_days(holders) -> int | None:
    """Days between the oldest and newest holder write, or None."""
    times = [h.mtime for h in holders if h.mtime]
    if len(times) < 2:
        return None
    return int((max(times) - min(times)) / 86400)


def _verdict(holders, groups, broken_links, stray_broker=()) -> None:
    """State what the data shows — and what it does NOT show."""
    click.secho("\n" + "=" * 60, fg="cyan")
    click.secho("What this means", fg="cyan", bold=True)

    if stray_broker:
        # ITS OWN PARAGRAPH, because the advice is the opposite of every other
        # branch here. The rest of this verdict is about reconciling copies
        # against the shared store, and its remedies say "run those projects".
        # Running a polluted broker project is what DELIVERS the credential.
        click.secho(
            f"\n✗ {len(stray_broker)} credential file(s) sit in a "
            f"`broker-state/` directory.", fg="red", bold=True)
        for h in stray_broker:
            click.echo(f"    - {h.path}")
        click.echo(
            "  In broker mode botainer hands the container a sentinel and "
            "keeps the real token host-side. This directory is bound rw into "
            "the container, so while a broker session for that project runs, "
            "the agent can read what is here.")
        click.echo(
            "  botainer does not put credentials here and has no command to "
            "clear them. Look at each file and decide: it may be a leftover "
            "from an older layout or a restored backup, and it may also be "
            "the only copy of a login you still want. Deleting the last copy "
            "logs that profile out.")

    distinct = len(set(groups.values()))
    projects = [h for h in holders if "project " in h.label]
    #: A project whose link DANGLES points at nothing. It cannot reach
    #: `broken_links` (that list is built in the regular-file branch, and a
    #: symlink `continue`s before it), so the "every project still points at
    #: the shared store" all-clear below fired over exactly the case that
    #: disproves it — observed on an isolated-mode project holding a leftover
    #: shared-mode link.
    dangling = [h for h in projects if h.is_symlink and not h.exists]
    stubs = [h for h in holders if h.parsed and _is_empty_stub(h.blk)]

    if broken_links:
        # SPLIT BY WHETHER IT MATTERS. Every one of these is a project that
        # broke away from the shared store, but the consequence differs
        # entirely, and the old verdict alarmed identically about both.
        #
        # An older project copy can be reconciled at the next shared-mode
        # start. A newer copy may hold a refreshed token missing from the
        # shared store; replacing it blindly would discard that token.
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
    elif dangling:
        click.secho(
            f"\n! {len(dangling)} project(s) link to a shared store that is "
            f"not on this host.", fg="yellow", bold=True)
        click.echo(
            "  The link is a leftover: the project points into a container\n"
            "  path that only exists inside a running shared-mode session, so\n"
            "  nothing here can read it and a login cannot write through it.\n"
            "  This is not 'still sharing' — it reaches nothing.")
        for h in dangling:
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

    if distinct == 0:
        click.secho(
            "\n? No holder here has a readable token, so nothing was compared.",
            fg="yellow")
        click.echo(
            "  This is NOT an all-clear. Agreement over an empty set is\n"
            "  vacuous, and the previous wording printed a green tick here.")
    elif distinct <= 1:
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
        # Different refresh tokens alone do not establish server-side
        # rotation: separate login events can produce independent tokens. Keep
        # any direct endpoint probe distinct from local-state inspection.
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
# Provide this diagnostic as a command with persistent state instead of
# requiring users to assemble shell snippets across terminals.
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
    for fname in _cred_shape(agent)["filenames"]:
        shared = root / "shared-auth" / _normalise_agent(agent) / fname
        if shared.exists():
            return shared, True
    return None, True


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

    # NORMALISE AND VALIDATE ONCE, HERE. `--agent claude` and `--agent
    # agent-claude` used to arm DIFFERENT FILES: the shared-store lookup below
    # interpolated the raw spelling, so `claude` built a path
    # (`shared-auth/claude/...`) that never exists, silently fell through, and
    # armed whichever PROJECT copy was newest — a credential the docstring
    # itself says "measures a credential nothing touched". Observed by running
    # both spellings against one state root. A single chokepoint makes the two
    # spellings the same value everywhere downstream rather than something each
    # future call site has to remember.
    _refuse_unknown_agent(agent)
    agent = _normalise_agent(agent)
    shape = _cred_shape(agent)
    oauth_key = shape["oauth_key"]

    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    root = paths.root
    statefile = root / _ROTATION_STATE

    if action == "arm":
        # REFUSE BEFORE WRITING ANYTHING when this family's expiry field is
        # unknown. Arming works by backdating that field; with no field to
        # backdate the previous code did `setdefault("claudeAiOauth", {})` and
        # wrote CLAUDE's block into codex's auth.json — observed by running it:
        #
        #   {"tokens": {...}, "claudeAiOauth": {"expiresAt": 1789065788978}}
        #
        # It then reported `refresh: e3b0c44298fc access: e3b0c44298fc`, which
        # is sha256("") twice, and called that ARMED. A corrupted credential
        # and a meaningless measurement, presented as a success.
        if shape["expiry_field"] is None:
            click.secho(
                f"refused: rotation-test cannot arm {agent}.", fg="red",
                err=True)
            click.echo(
                "  Arming backdates the credential's expiry so the next API\n"
                "  call is forced to refresh. This agent has no such field to\n"
                "  backdate: codex takes its refresh timing from the `exp`\n"
                "  claim inside the access token, falling back to\n"
                "  `last_refresh` + 8 days — an RFC3339 STRING, not the\n"
                "  epoch-ms field this command writes. Inventing one corrupts\n"
                "  the file the experiment is meant to measure.\n"
                "\n  Claude is supported:\n"
                "      botainer auth rotation-test arm --agent claude")
            raise SystemExit(2)

        # Prefer the SHARED STORE when it holds a token. In shared mode — the
        # default — `pre_session` re-links every project to it at session
        # start, so the shared file is the one the agent actually refreshes.
        # Arming a project file there measures a credential nothing touched.
        cred = None
        for fname in shape["filenames"]:
            shared = root / "shared-auth" / agent / fname
            if shared.exists() and _refresh_of(
                    _oauth_block(_read_json(shared), oauth_key)):
                cred = shared
                break
        if cred is None:
            cred = _newest_usable_credential(root, agent)
        if cred is None:
            click.secho("refused: no credential file holds a refresh token.",
                        fg="red", err=True)
            click.echo("  Run `botainer auth login` first.")
            raise SystemExit(2)
        # Refuse to arm through a symlink. Backdating its target would modify
        # a file not named by the experiment record and make restoration
        # ambiguous.
        if cred.is_symlink():
            click.secho("refused: that credential is a symlink; arming would "
                        "backdate a file this record cannot name.",
                        fg="red", err=True)
            click.echo(f"  path  : {cred}")
            click.echo(f"  target: {os.readlink(cred)}")
            click.echo("  Point --agent at the store that really holds the "
                       "token, or resolve the link first.")
            raise SystemExit(2)
        blk = _oauth_block(_read_json(cred), oauth_key)
        # BACKS UP NO STRUCTURAL GAP — both discovery paths above already
        # require a refresh token, so this cannot fire today. It is here
        # because the failure it catches printed sha256("") twice and looked
        # like a result; a measurement of nothing must never reach the screen
        # wearing a digest.
        if not _refresh_of(blk):
            click.secho("refused: the chosen credential holds no refresh "
                        "token, so there is nothing to measure.",
                        fg="red", err=True)
            click.echo(f"  file: {cred}")
            raise SystemExit(2)
        backup = root / f"rotation-test-backup-{int(time.time())}.json"
        shutil.copy2(cred, backup)

        before = {
            "path": str(cred),
            "backup": str(backup),
            "refresh": _digest(_refresh_of(blk)),
            "access": _digest(_access_of(blk)),
            "armed_at": time.time(),
            #: Recorded so `check` reads the same block it armed, even if the
            #: default --agent differs between the two invocations.
            "agent": agent,
        }
        statefile.write_text(json.dumps(before, indent=2), encoding="utf-8")

        # Backdate so the next real API call must refresh, instead of waiting
        # out an access token's ~1h life for every trial.
        doc = _read_json(cred) or {}
        doc.setdefault(oauth_key, {})[shape["expiry_field"]] = int(
            (time.time() - 600) * 1000)
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
        # The documented recovery step, and the one that died with a raw
        # FileNotFoundError traceback. Observed: arm a project copy, let
        # `pre_session` re-link that project to the shared store (which
        # replaces the file with a symlink whose target is the CONTAINER path
        # /shared-auth/...), then restore.
        #
        # Following that symlink would be worse than the crash. The backup
        # holds the PROJECT's old credential; the symlink points at the SHARED
        # store, so copying through it would overwrite the live shared
        # credential with a stale copy of a different one — silent damage in
        # the command whose whole job is undoing damage.
        #
        # There is also nothing left to undo: the backdated file arm wrote no
        # longer exists. So say that, name the backup, and write nothing.
        if cred.is_symlink() or not cred.exists():
            # STATE WHAT WAS SEEN, DO NOT DIAGNOSE. The first version of this
            # asserted `pre_session` had re-linked the project. That is one
            # cause among several, and in the case where the store was ALREADY
            # a symlink every sentence of it was false. A refusal that explains
            # a cause it did not observe is a confident wrong answer.
            click.secho("refused: the file that was armed is not a regular "
                        "file now, so it is not the file that was backed up.",
                        fg="yellow", err=True)
            click.echo(f"  armed  : {cred}")
            if cred.is_symlink():
                click.echo(f"  now    : a symlink -> {os.readlink(cred)}")
                click.echo("  Writing through it would modify whatever it "
                           "points at, which is not what was backed up.")
            else:
                click.echo("  now    : missing")
            click.echo(f"\n  Your backup is kept at:\n      {rec['backup']}")
            click.echo("  Nothing was written.")
            raise SystemExit(2)

        # NEVER WRITE A TOKEN BACK. `restore` used to copy2 the whole backup
        # over the live file — refresh token included. EF-1 measured that a
        # rotated-away refresh token is DEAD (HTTP 400), so that made restore
        # SAFE exactly when the experiment failed and DESTRUCTIVE exactly when
        # it succeeded. `check` printed "restore if needed" under ROTATION
        # CONFIRMED, i.e. the product invited the user to destroy their own
        # login at the one moment it was fatal. In broker mode the file is the
        # host-wide shared store, so the blast radius is every project.
        #
        # arm changes exactly one field, so undoing it needs exactly one field.
        current = _read_json(cred) or {}
        cur_blk = _oauth_block(current, oauth_key)
        cur_refresh = _refresh_of(cur_blk)

        # AN EMPTY STUB IS NOT A ROTATION, AND SAYING SO IS THE WHOLE POINT.
        # Without this, a credential that has become `{"claudeAiOauth":
        # {"expiresAt": 0}}` — the state `_is_empty_stub` documents as OBSERVED
        # ON THE CLUSTER, and the one the open logged-out diagnosis is chasing —
        # digests to sha256(""), fails the equality below, and takes the
        # rotation branch. `restore` then said "A refresh happened and the
        # server issued a new token" and "Your live credential is fine". Both
        # false, on exactly the state where they are most harmful, while
        # `check` on the identical file said "EMPTY — this project is logged
        # out". Two surfaces of one command contradicting each other.
        #
        # `arm` and `check` gained a digest-of-nothing guard; this path did
        # not. Found by the loop tzar reviewing that very commit.
        #
        # It still writes nothing. `restore`'s contract is "put the expiry
        # back, never a token", and there is no expiry here to put back — the
        # file has no block at all. What the user needs is the truth plus the
        # two things that can actually help.
        if not cur_refresh:
            click.secho("refused: the credential is EMPTY — this is not a "
                        "rotation, and there is no expiry to put back.",
                        fg="yellow", err=True)
            click.echo(f"  file : {cred}")
            click.echo(f"  state: {_expiry_note(cur_blk)}")
            click.echo(
                "\n  The file exists but holds no token, so the experiment did\n"
                "  not measure anything and this command has nothing to undo.")
            click.echo(f"\n  Your pre-arm backup is at:\n      {rec['backup']}")
            click.echo(
                "  It holds a token from before arming. Whether that token is\n"
                "  still valid cannot be determined from here, so it is not\n"
                "  written back automatically.\n"
                "\n  To see what state this credential is really in:\n"
                "      botainer auth doctor\n"
                "  To get a working login regardless:\n"
                "      botainer auth login")
            raise SystemExit(2)

        if _digest(cur_refresh) != rec["refresh"]:
            click.secho("refused: the refresh token has CHANGED since arm, so "
                        "there is nothing here to undo.", fg="yellow", err=True)
            click.echo(
                "  A refresh happened and the server issued a new token. The\n"
                "  backup holds the PREVIOUS one, which rotation has already\n"
                "  invalidated — writing it back would log this credential\n"
                "  out, not restore it.\n"
                "  Your live credential is fine. The backdated expiry it was\n"
                "  armed with has already been replaced by the refresh.")
            click.echo(f"\n  The backup is still at:\n      {rec['backup']}")
            raise SystemExit(2)

        backup_blk = _oauth_block(_read_json(Path(rec["backup"])), oauth_key)
        field = shape["expiry_field"]
        current.setdefault(oauth_key, {})[field] = backup_blk.get(field)
        cred.write_text(json.dumps(current), encoding="utf-8")
        click.secho(f"restored {field} in {cred}", fg="green")
        click.echo("  Only the expiry was put back — no token was written.")
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
        click.secho("refused: cannot find the armed credential.", fg="red",
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
    # The block key comes from the family recorded AT ARM TIME. Re-deriving it
    # from this invocation's --agent would compare a claude block against a
    # codex one whenever the two spellings differed.
    _armed_shape = _cred_shape(rec.get("agent") or agent)
    blk = _oauth_block(_read_json(read_from), _armed_shape["oauth_key"])
    now_refresh = _digest(_refresh_of(blk))
    now_access = _digest(_access_of(blk))

    # A MEASUREMENT OF NOTHING MUST NOT REACH THE SCREEN AS A RESULT. `arm`
    # got this guard; `check` did not, so an EMPTY STUB — the state
    # `_is_empty_stub` documents as OBSERVED ON THE CLUSTER — read as
    # `after refresh: e3b0c44298fc` and printed ROTATION CONFIRMED with no
    # session ever having been run. That is the exact confusion this command
    # exists to prevent: "rotated" and "your login was destroyed" look
    # identical, and the second is what the open logout diagnosis is chasing.
    _nothing = _digest("")
    if rec["refresh"] == _nothing or now_refresh == _nothing:
        click.secho("\nINCONCLUSIVE — no refresh token to compare.",
                    fg="yellow", bold=True)
        click.echo(f"  file: {read_from}")
        if now_refresh == _nothing:
            click.echo(f"  now  : {_expiry_note(blk)}")
        if rec["refresh"] == _nothing:
            click.echo("  armed: the record holds no token digest either, so "
                       "it was armed before this was checked for.")
        click.echo(
            "\n  This is NOT 'no rotation'. Comparing an absent token to an\n"
            "  absent token would print a verdict about nothing. Run\n"
            "  `botainer auth doctor` to see what state this credential is\n"
            "  actually in, then re-arm.")
        raise SystemExit(2)

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
        click.echo("\n  undo the backdated expiry:  "
                   "botainer auth rotation-test restore")
        return

    if now_refresh == rec["refresh"]:
        click.secho("\nRESULT: NO ROTATION.", fg="green", bold=True)
        click.echo(
            "  The access token changed; the refresh token did NOT. So a\n"
            "  refresh does not invalidate other holders, and shared mode is\n"
            "  sound in principle — the symlink bug is the whole story.")
        # Safe here, and ONLY here: the refresh token is unchanged, so putting
        # the old expiry back cannot cost anything.
        click.echo("\n  undo the backdated expiry:  "
                   "botainer auth rotation-test restore")
    else:
        click.secho("\nRESULT: ROTATION CONFIRMED.", fg="red", bold=True)
        click.echo(
            "  Both changed. Each refresh mints a new refresh token, so only\n"
            "  the most recent holder can be valid and multi-holder mount mode\n"
            "  cannot work. `botainer auth use broker` is the single-holder\n"
            "  design.")
        # NO RESTORE OFFER HERE. This is the branch where the backup's token is
        # already dead, and it is the branch that used to carry the invitation.
        click.secho("\n  Do NOT restore. ", fg="red", bold=True, nl=False)
        click.echo("The token in the backup is the one rotation just\n"
                   "  replaced, so the server no longer accepts it. Your live\n"
                   "  credential is the good one; leave it alone.")
    click.echo("\nRun this a SECOND time before trusting it: one observation")
    click.echo("cannot tell 'never rotates' from 'rotates sometimes', and")
    click.echo("intermittent is the worst case for us.")
