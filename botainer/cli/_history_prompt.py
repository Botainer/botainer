"""Ask the user what to do with their history when a switch would relocate it.

The interactive half of :mod:`botainer.core.history_carry`. It lives here, in
its own module, for a reason worth stating: the two commands that need it —
``botainer auth use`` and ``botainer config set`` — are both on the
security-surface review list, and everything written here would otherwise be
written twice inside them. Keeping it out means the review-gated diff in each is
a single call, and the branching a reviewer actually has to think about is in
one place with tests on it.

WHY THE OFFER HAPPENS BEFORE THE SWITCH
---------------------------------------
Both endpoints are only known while the old value is still in the config. Do the
carry afterwards and the source directory must be reconstructed from previous
configuration. Offer the choice while both paths are known, and allow declining
without losing either directory.

WHAT IT WILL NOT DECIDE FOR YOU
-------------------------------
When both directories hold history, there is no safe automatic answer: either
could be the one the user wants, and picking wrong loses work. The prompt shows
both — size, when each was last touched, and the opening prompt of the newest
transcript in each, because that last one is what lets someone RECOGNISE which
pile is theirs — and it defaults to the least destructive option. Under
``--yes`` it takes that default and says so loudly rather than guessing.
"""

from __future__ import annotations

from pathlib import Path

import click
import yaml

from botainer.core.history_carry import (
    SET_ASIDE_NUDGE_AT,
    archive_dir,
    describe_dir,
    execute_carry,
    has_history,
    plan_carry,
    set_aside_siblings,
    supersede_carried,
)

_PROMPT_EXCERPT = 72


def mode_for_agent(enabled: "list[object]", agent: str) -> str:
    """Is THIS agent in broker mode? Scoped to one family, deliberately. (#223)

    The two callers of this used to write, identically:

        mode = "broker" if any("broker" in str(p) for p in enabled) else "shared"

    a substring scan over EVERY enabled plugin, with no family filter. So an
    enabled `agent-codex-broker` made the ANTHROPIC history logic believe claude
    was in broker mode; it then looked for claude's history under
    `broker-state/`, found nothing there, and stayed silent — an unrelated
    OpenAI plugin silencing a Claude history warning. The inverse also held: it
    would offer to move claude's history INTO broker-state/ while claude was
    still shared.

    Same cross-family read as #220, in the data half rather than the auth half,
    and duplicated in two files — so it lives here once now, and both call it.

    Only broker-vs-not matters: `history_dir_for` selects `broker-state` or
    `profiles` and nothing else, so collapsing shared/isolated is exact for the
    path (which is all this answer is used for).
    """
    prefix = agent if str(agent).startswith("agent-") else f"agent-{agent}"
    for p in enabled:
        name = str(p)
        if (name == prefix or name.startswith(prefix + "-")) and name.endswith("-broker"):
            return "broker"
    return "shared"


def history_dir_for(state_root: Path, uid: str, family_plugin: str, mode: str,
                    profile: str = "default") -> Path:
    """Where THIS mode+profile keeps the agent's config dir, and so its history.

    Every mode binds the same container path (``/home/agent/.claude``,
    ``/home/agent/.codex``) from a DIFFERENT host directory. Broker deliberately
    uses ``broker-state/`` because that dir must hold NO credential — that
    separation is the whole point of broker mode and must not be "tidied" away.
    The side effect is the one this module exists to soften: switching to or
    from broker moves the agent's history with it, and so does changing the
    profile, which is a path component in every mode.
    """
    agent = (family_plugin.replace("-shared", "")
                          .replace("-broker", "")
                          .replace("-proxy", ""))
    sub = "broker-state" if mode == "broker" else "profiles"
    return state_root / "state" / uid / "data" / agent / sub / profile


def live_sessions_for(project_root: Path) -> list[str]:
    """Session ids currently RUNNING for this project. Empty means safe to move.

    A switch relocates the very directory a running session has bound at
    ``/home/agent/.claude``. Renaming a live agent's transcripts and todos out
    from under it is not something to warn about and proceed with — the agent
    keeps writing to files that are no longer the ones the next session reads.

    This got worse when the carry became a move: a copy at least left the
    running session's files where they were.

    Resolution failures return [] rather than raising. That is the honest
    default for a project that has never been started (no state dir, no session
    records) — but it does mean this is a best-effort signal, not a lock, and
    the refusal it feeds says so rather than claiming certainty.
    """
    try:
        from botainer.core import identity
        from botainer.state import dir as _state_dir
        from botainer.state import liveness, session_record

        uid, _ = identity.resolve_identity(project_root, identity_accept=False)
        paths = _state_dir.ensure_user_state_dir(create_if_missing=False)
        sessions_dir = paths.for_project(uid).sessions_dir
        return [
            rec.session_id
            for rec in session_record.list_sessions(sessions_dir)
            if liveness.is_session_alive(rec)
        ]
    except Exception:
        return []


def refuse_if_a_session_is_live(project_root: Path) -> bool:
    """Print the refusal and return True when the caller must stop.

    A refusal rather than a warning, deliberately. There is no version of
    "rename this running agent's files anyway" that ends well, and a prompt
    offering it would be offering a mistake.
    """
    live = live_sessions_for(project_root)
    if not live:
        return False
    click.secho(
        f"\nrefused: {len(live)} session(s) for this project are still "
        f"running.\n  Changing this would move the agent's config directory "
        f"out from under them\n  while they are writing to it.",
        fg="red", err=True)
    for session_id in live[:5]:
        click.secho(f"      {session_id}", fg="cyan", err=True)
    click.secho(
        "  Close them (or `botainer stop`) and run this again. Nothing has "
        "been changed.", fg="cyan", err=True)
    return True


#: `what_changed` is substituted into two sentences, and BOTH must read as
#: English with it in place:
#:
#:      "Changing the {what_changed} changes which directory holds…"
#:      "…do NOT travel: each {what_changed} keeps its own…"
#:
#: So it is a NOUN PHRASE, not a config key. `auth use` passes "auth mode" and
#: reads correctly; `config set` passed the raw key and produced "Changing the
#: plugins_enabled" and "each plugins_enabled keeps its own" — observed by
#: running it. This translates the keys that can move history, and its fallback
#: stays grammatical in both frames for any key it does not know.
_AXIS_NOUN = {
    # plugins_enabled IS how the auth mode is expressed, so this is not a
    # euphemism for the key — it is what the user actually changed.
    "plugins_enabled": "auth mode",
    "agent": "agent",
    "auth_profile": "auth profile",
}


def axis_noun_for_config_key(key: str) -> str:
    return _AXIS_NOUN.get(key, f"`{key}` setting")


def _n_files(n: int) -> str:
    """"1 file", not "1 files". Small, and it is the first thing a reader sees
    on the carry line — a count that cannot agree with its own noun reads as
    machine output, in a paragraph whose whole job is to be read."""
    return f"{n} file" if n == 1 else f"{n} files"


def warn_history_will_move(source: Path, destination: Path, *,
                           what_changed: str) -> bool:
    """State the consequence BEFORE the command's own confirm. Returns whether
    anything was said.

    Split from the offer on purpose, because the two belong at different points
    in every command that uses them:

      - the WARNING has to come before the "apply these changes?" confirm, or
        the user is told about the move only after agreeing to it — which is
        the original defect wearing a different hat;
      - the COPY has to come after the change is applied, so it lands in the
        directory the next session will actually read, and so that aborting at
        the confirm leaves no half-carried files behind to look like a
        both-sides-have-history conflict the next time round.

    Says nothing when there is nothing to move. A notice that fires on every
    switch regardless of whether it applies is the "warn that fires on every
    commit" failure in another place: people learn to skip the channel.
    """
    if source == destination or not has_history(source):
        return False
    click.secho(
        f"\n! Changing the {what_changed} changes which directory holds your "
        f"agent's\n  session history — transcripts, todos, your own subagents "
        f"and settings.", fg="yellow", err=True)
    click.secho(f"      now:  {source}", fg="cyan", err=True)
    click.secho(f"      next: {destination}", fg="cyan", err=True)
    if has_history(destination):
        click.secho(
            "  BOTH already have history. Nothing is deleted or merged — you "
            "will be\n  asked which to keep once the change is applied.",
            fg="yellow", err=True)
    else:
        click.secho(
            "  botainer will offer to move it across once the change is "
            "applied.\n  Nothing is deleted either way.", err=True)
    return True


def offer_carry_for_switch(state_root: Path, uid: str, agent_plugin: str, *,
                           from_mode: str, to_mode: str,
                           from_profile: str = "default",
                           to_profile: str = "default",
                           what_changed: str,
                           assume_yes: bool = False) -> bool:
    """The entry point the CLIs call. Both paths, ONE agent.

    Taking a single `agent_plugin` and deriving both ends from it is the point:
    a carry between two DIFFERENT agents must never happen, and this way it
    cannot be expressed. Claude's directory holds `claude.json`, `projects/`
    and `todos/`; codex's holds `auth.json`, `config.toml`, its own global
    instructions file and a SQLite state DB. Copying either into the other would not be history — it
    would be junk in a directory the tool then has to survive reading. The
    formats have nothing in common and neither tool ignores what it does not
    recognise reliably enough to bet a user's session on.

    So switching AGENT is a genuine fresh start, and the caller says that in
    words instead of offering a copy that would be wrong.
    """
    return offer_carry(
        history_dir_for(state_root, uid, agent_plugin, from_mode, from_profile),
        history_dir_for(state_root, uid, agent_plugin, to_mode, to_profile),
        what_changed=what_changed,
        assume_yes=assume_yes,
    )


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _summarise(label: str, directory: Path) -> None:
    """Print one side of a two-sided choice. Facts only, no recommendation."""
    info = describe_dir(directory)
    click.secho(f"  {label}", fg="white", bold=True, err=True)
    click.secho(f"      {directory}", fg="cyan", err=True)
    if not info["exists"]:
        click.secho("      (nothing there)", err=True)
        return
    click.secho(
        f"      {_n_files(int(info['files']))}, {_human_bytes(int(info['bytes']))}"
        f", last written {info['modified'] or 'unknown'}", err=True)
    prompt = info["last_prompt"]
    if isinstance(prompt, str) and prompt:
        excerpt = prompt[:_PROMPT_EXCERPT]
        if len(prompt) > _PROMPT_EXCERPT:
            excerpt += "…"
        click.secho(f'      most recent session began: "{excerpt}"', err=True)


def offer_carry(source: Path, destination: Path, *,
                what_changed: str,
                assume_yes: bool = False) -> bool:
    """Offer to carry history from `source` to `destination`.

    `what_changed` is a short human phrase for the thing being switched — "auth
    mode", "profile", "agent" — used in the sentences so the message names the
    user's actual action rather than a generic one.

    Always returns True. The bool exists so callers read as a decision point
    and so a future option that genuinely aborts has somewhere to live — the
    chance to abort is given earlier, by :func:`warn_history_will_move`, before
    the command's own confirm.
    """
    if source == destination:
        return True

    plan = plan_carry(source, destination)

    if plan.blocked and has_history(destination) and has_history(source):
        return _resolve_two_populated_dirs(
            plan, what_changed=what_changed, assume_yes=assume_yes)

    if plan.blocked and plan.blocked_reason and has_history(source):
        # A blocked plan carries nothing, so without this it falls into the
        # is_empty branch below and says NOTHING — the user's history quietly
        # does not move and no reason is given. Which is the exact silent
        # failure this whole feature exists to end. Observed by running the
        # symlinked-destination case through the real CLI and watching it
        # print a clean switch and no explanation.
        click.secho(
            f"\n! Your history was NOT moved: {plan.blocked_reason}.",
            fg="yellow", err=True)
        click.secho(
            f"  It is still at {source}, untouched. The {what_changed} change "
            f"itself was applied.", fg="cyan", err=True)
        return True

    if plan.is_empty:
        # Nothing to carry: a first switch, or a source that only ever held a
        # credential. Say nothing — a notice about zero files is noise, and the
        # switch itself is already reported by the caller.
        return True

    # Deliberately shorter than warn_history_will_move, which has just run in
    # every wired caller and named the consequence. Repeating it here would be
    # the same paragraph twice on one screen; what this adds is the SIZE of
    # what would move and where it would land.
    click.secho(
        f"\n  {_n_files(plan.file_count)}, {_human_bytes(plan.bytes_to_carry)} — "
        f"transcripts, todos, your own\n  subagents, settings and this "
        f"project's MCP servers.", fg="yellow", err=True)
    click.secho(f"      from  {source}", fg="cyan", err=True)
    click.secho(f"      to    {destination}", fg="cyan", err=True)
    click.secho(
        "  This MOVES them: the old copies are renamed aside, so there is only\n"
        "  ever one live copy and never a question of which is current.",
        err=True)
    _say_what_stays_behind(plan)
    # Says what does NOT travel and why, in the user's terms. Two earlier
    # versions of this line were wrong: one said "a mode switch is a credential
    # change" on the PROFILE path (wrong noun, found by running it), and both
    # implied only the credential stays behind — the account details stay too.
    click.secho(
        f"  Your login and account details do NOT travel: each {what_changed} "
        f"keeps its own,\n  so you will sign in once on the other side.",
        fg="yellow", err=True)

    if assume_yes:
        click.secho("  --yes given: moving.", fg="green", err=True)
        carry = True
    else:
        carry = click.confirm("\n  Move your history across?", default=True,
                              err=True)

    if not carry:
        click.secho(
            f"  Left in place. It is still at {source} — nothing was deleted.",
            fg="cyan", err=True)
        return True

    _run_and_report(plan)
    return True


def _say_what_stays_behind(plan) -> None:
    """Name the files deliberately withheld from a history carry, and explain why.

    For Codex, config.toml is withheld because it can contain an agent-controlled
    base_url; carrying it into a credential-bearing profile could redirect requests.
    The display must distinguish those withheld settings from transferred history.
    The plan already records each path and reason. Group and cap the display so
    large groups of existing destination files do not obscure security exclusions."""
    withheld = list(getattr(plan, "withheld", ()) or ())
    if not withheld:
        return
    by_reason: dict[str, list[str]] = {}
    for w in withheld:
        by_reason.setdefault(getattr(w, "reason", "?"), []).append(
            str(getattr(w, "path", w)))
    click.secho("  NOT moved, deliberately:", fg="yellow", err=True)
    _CAP = 4
    for reason, names in sorted(by_reason.items()):
        shown = ", ".join(sorted(names)[:_CAP])
        more = len(names) - _CAP
        click.secho(f"      {shown}{f' (+{more} more)' if more > 0 else ''}"
                    f"\n        — {reason}", fg="yellow", err=True)


def _run_and_report(plan) -> None:
    report = execute_carry(plan)
    click.secho(
        # `_n_files` exists ten lines up and says why; this line was the one
        # place that did not use it, so the SAME switch printed "3 files" in
        # the plan and "Moved 1 files" in the result.
        f"  Moved {_n_files(report.file_count)} "
        f"({_human_bytes(report.bytes_copied)}).", fg="green", err=True)
    if report.reduced:
        click.secho(
            "  Your project settings came across without the account details "
            "attached to them.", err=True)
    # Copy first, verify, THEN move aside — so a failure leaves the user with
    # two copies rather than none. See supersede_carried() for why this is a
    # move at all: with a copy, "which of these two is current?" becomes a
    # question the user has to answer from timestamps.
    superseded = supersede_carried(plan, report)
    if superseded is not None:
        click.secho(
            f"  The old copies were set aside at\n      {superseded}\n"
            f"  (renamed, not deleted). Your sign-in stayed where it was.",
            fg="cyan", err=True)
        _nudge_if_set_asides_are_piling_up(plan.source)
    if report.failed:
        click.secho(
            f"  {len(report.failed)} could not be copied and were left behind:",
            fg="yellow", err=True)
        for item in report.failed[:5]:
            click.secho(f"      {item.path} — {item.reason}", fg="yellow",
                        err=True)
        if len(report.failed) > 5:
            click.secho(f"      … and {len(report.failed) - 5} more", err=True)
        click.secho(
            "  Nothing was overwritten. Re-running the switch will retry them.",
            err=True)


def _nudge_if_set_asides_are_piling_up(source: Path) -> None:
    """Say something once the set-aside copies of one profile reach the nudge
    threshold. Nothing is deleted or refused — this is the only moment botainer
    ever mentions them unprompted.

    WHY THIS EXISTS. Every switch that carries anything leaves a timestamped
    copy behind, and nothing ever removes one: not the next switch, not
    `botainer where` (read-only by design), not any command that exists. So
    switching back and forth — which is the normal way to use two profiles —
    grows the state root monotonically, and the only surface that shows it is a
    command the user has to already suspect they need. Warn when this profile
    reaches the warning threshold and point to the inspection command. Do not
    delete copies automatically.

    NOT a count of every set-aside copy in the state root. Only the copies of
    THIS profile, because that is the pile the user just added to and the one
    the printed command acts on. A global count would nag about directories
    belonging to a project they are not in.

    It points at `botainer where` rather than printing an `rm -rf` here, and
    that is deliberate in both directions: `where` prints the delete command AND
    the restore commands, next to each copy's size and age. A bare `rm -rf` at
    the end of a successful switch is a one-keystroke path to deleting the only
    remaining copy of a transcript, offered at the moment the user is least
    likely to be reading carefully.
    """
    kept = set_aside_siblings(source)
    if len(kept) < SET_ASIDE_NUDGE_AT:
        return
    click.secho(
        f"\n  Note: this profile now has {len(kept)} set-aside history copies. "
        f"botainer never\n  removes them, and each switch adds one.",
        fg="yellow", err=True)
    click.secho(
        "  To see their sizes, and the exact command to delete or restore one, "
        "run\n  in your HOST shell (not inside a session):", err=True)
    click.secho("      botainer where", fg="cyan", err=True)


def _resolve_two_populated_dirs(plan, *, what_changed: str,
                                assume_yes: bool) -> bool:
    """Both sides hold history. Show both; let the user pick. Never merge.

    Merging would interleave two separate sets of transcripts, and there is no
    later operation that can pull them back apart. Every option offered here
    keeps every byte: the destructive-looking one is a rename.
    """
    click.secho(
        f"\n! Both {what_changed}s already have session history, and botainer "
        f"will not\n  merge them — mixing two sets of transcripts cannot be "
        f"undone.", fg="yellow", err=True)
    _summarise("the one you are leaving:", plan.source)
    _summarise("the one you are switching to:", plan.destination)

    if assume_yes:
        click.secho(
            "\n  --yes given, so botainer is taking the option that moves "
            "nothing:\n  the destination keeps its own history and the other "
            "stays where it is.\n  Re-run without --yes to choose.",
            fg="yellow", err=True)
        return True

    choice = click.prompt(
        "\n  [k] keep the destination's history (nothing moves)\n"
        "  [a] archive the destination's history, then move this one across\n"
        "  Choose",
        type=click.Choice(["k", "a"]), default="k", err=True)

    if choice == "k":
        click.secho(
            f"  Keeping it. The other history stays at {plan.source}.",
            fg="cyan", err=True)
        return True

    archived = archive_dir(plan.destination)
    click.secho(f"  Archived to {archived} (renamed, not deleted).",
                fg="green", err=True)
    plan.destination.mkdir(parents=True, exist_ok=True)
    _run_and_report(plan_carry(plan.source, plan.destination))
    return True


def warn_override_moves_history(project_root: Path, *,
                                auth_mode_override: str | None,
                                auth_profile_override: str | None,
                                agent_override: str | None = None,
                                err: bool = True) -> None:
    """Disclose the actual history selected by one-session overrides.

    Temporary mode, profile and agent flags can select a different configuration
    directory. They do not persist a configuration change and must not trigger a
    history carry: moving history for a temporary flag would make the next ordinary
    start look like a switch back. Show the selected and configured paths instead.
    Persistent changes are handled by offer_carry_for_declared_change."""
    # A FAST PATH, NOT A GUARD, and the distinction is mutation-proven: with
    # no override the effective agent/mode/profile equal the declared ones, so the
    # `was == now` comparison below returns on its own and deleting this line
    # changes no behaviour. It stays because `start` is hot and this avoids
    # reading config.yaml + resolving the state root on every launch — but a
    # reader must not mistake it for the thing that decides whether to warn.
    if not agent_override and not auth_mode_override and not auth_profile_override:
        return
    try:
        import io
        from contextlib import redirect_stderr

        from botainer.core import composition
        from botainer.core import config as config_module

        cfg = config_module.load_config(project_root)
        # Selection is install-aware: an explicitly enabled target-family
        # variant beats inferred mode carry, and an unavailable auth variant
        # can leave the existing mode selected. Do not reconstruct those rules
        # from flag strings. Composition later prints its own selection/refusal
        # diagnostics; this read-only advisory must not print them twice.
        with redirect_stderr(io.StringIO()):
            effective = composition.apply_plugin_overrides(
                cfg, agent_override=agent_override,
                auth_mode_override=auth_mode_override)
        declared_mode = mode_for_agent(cfg.plugins_enabled, cfg.agent)
        effective_mode = mode_for_agent(effective.plugins_enabled, effective.agent)
        effective_profile = (auth_profile_override if auth_profile_override is not None
                             else effective.profile)
        # NO SEPARATE "did the mode/profile change?" CHECK. I wrote one, and
        # mutation testing showed it was redundant with the `was == now`
        # comparison below — deleting either left the behaviour correct, which
        # means neither was carrying the property on its own. The PATH is the
        # thing that matters (it is what gets bound), so the path comparison is
        # the single owner and a value comparison that agrees with it is dead
        # weight that a future reader would have to reason about.

        from botainer.core import identity
        from botainer.state import dir as _state_dir

        uid = identity.read_project_id(project_root) or ""
        if not uid:
            return
        root = _state_dir.ensure_user_state_dir(create_if_missing=False).root
        was = history_dir_for(root, uid, f"agent-{cfg.agent}", declared_mode,
                              cfg.profile)
        now = history_dir_for(root, uid, f"agent-{effective.agent}", effective_mode,
                              effective_profile)
        if was == now:
            return

        click.secho(
            f"\n⚠ This session reads a DIFFERENT history directory than your "
            f"config declares.", fg="yellow", err=err)
        click.secho(
            f"    You passed a one-session override, and the agent/mode/profile is "
            f"part of the path\n"
            f"    where {effective.agent} keeps its transcripts and notes. Nothing is "
            f"moved or lost:", fg="yellow", err=err)
        click.secho(f"      this session reads:  {now}", fg="yellow", err=err)
        click.secho(f"      your config's is:    {was}", fg="yellow", err=err)
        if effective.agent != cfg.agent:
            click.secho(
                "    These agents keep separate histories. This does not move or "
                "convert\n    history between agents; switching back keeps the "
                "saved agent's history.", fg="cyan", err=err)
            return
        try:
            new_history_is_empty = (
                not has_history(now, strict=True) and has_history(was, strict=True))
        except OSError:
            click.secho(
                "    History availability is unknown: a history location could "
                "not be fully inspected.\n    This does not establish that it is empty.",
                fg="yellow", err=err)
        else:
            if new_history_is_empty:
                click.secho(
                    "    The one this session reads is EMPTY, so the agent starts "
                    "with no history.", fg="red", err=err)
        click.secho(
            "    To make the change permanent AND bring the history across, "
            "use\n"
            "    `botainer auth use <mode>` or `botainer config set`, which "
            "offer to carry it.", fg="cyan", err=err)
    except Exception:  # noqa: BLE001 — never fail a launch over a disclosure
        return


def offer_carry_for_declared_change(project_root: Path, *,
                                    assume_yes: bool = False,
                                    can_prompt: bool = True) -> None:
    """#192: act on a HAND-EDIT of config.yaml, at `botainer start`.

    THE HALF THAT WAS MISSING. Three routes move the agent's history, because
    the mode and profile are PATH COMPONENTS of its config dir:

        data/<agent>/profiles/<profile>/       isolated, shared
        data/<agent>/broker-state/<profile>/   broker

    `auth use` and `config set` both warn and offer to carry. Editing
    config.yaml by hand and running `start` carried NOTHING — and that is the
    route botainer's own `--auth-profile` help text recommends for a persistent
    change. The user's agent looks like it lost its history; it is sitting in
    the old directory.

    `state/declared.py` built the detection on 2026-09-01 and was DELIBERATELY
    record-only, with the prompting policy split out to land separately. It
    never did: until this function, `declared` was imported by nothing but its
    own test. Recording a finding is not resolving it, and neither is building
    half the mechanism.

    WHY IT READS THE DECLARED VALUE AND NOT THE SESSION SPEC — this is the
    subtle part, and `declared.py`'s docstring is the authority: the effective
    spec has `--auth-profile` / `--auth-mode` / `--agent` already folded in,
    with nothing marking a field as flag-derived. Comparing specs would make
    one temporary `start --auth-profile work` look like a switch, and the next
    plain `start` like a switch back — two carries from one flag, history
    bounced between directories. Reading only what the FILE said cannot see a
    flag: not "detects and skips it", which every future caller would have to
    remember, but structurally unable to reach it.

    NON-INTERACTIVE IS A WARNING, NEVER A PROMPT. `start` runs from scripts, in
    CI, and under `hpc submit`; `click.confirm` on a closed stdin raises Abort
    and would turn a config edit into a failed launch. So without a tty this
    states what moved, names the directory the history is in, and gives the
    command to carry it — then proceeds. A missed carry costs a `--yes` re-run;
    a refused launch costs the session.

    Never raises: a failure to notice is not a reason to fail a start.
    """
    from botainer.state import declared

    try:
        cfg_path = Path(project_root) / ".botainer" / "config.yaml"
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return
        enabled = raw.get("plugins_enabled") or []
        # Same derivation config_cmd._resolve_history_context uses. Only
        # broker-vs-not selects the subdirectory (history_dir_for), so this is
        # exact for the path even though it collapses shared/isolated.
        _agent = str(raw.get("agent", "claude"))
        current = {
            "agent": _agent,
            "profile": str(raw.get("profile", "default")),
            "auth_mode": mode_for_agent(enabled, _agent),
        }

        from botainer.core import identity
        from botainer.state import dir as _state_dir
        uid = identity.read_project_id(project_root) or ""
        if not uid:
            return
        paths = _state_dir.ensure_user_state_dir(create_if_missing=False)
        proj = paths.for_project(uid)

        # `.base`, not `.root` — ProjectPaths has no `root` attribute (it is
        # base/data_dir/home_dir/locks_dir/meta_path/packages_dir/scratch_dir/
        # sessions_dir). The AttributeError was swallowed by the bare
        # `except Exception: return` below, so THIS FEATURE HAS NEVER RUN ONCE:
        # no declared.json is ever written and no carry is ever offered on a
        # hand-edited config. #192 shipped as dead code; #224 is that. The
        # module's own docstring warns about "a bare except swallowing a NAME
        # error" — reintroduced two functions later.
        previous = declared.read(proj.base)
        moved = previous.differs_from(current) if previous else {}
    except Exception:                                            # noqa: BLE001
        return

    try:
        if moved:
            _report_declared_change(
                paths.root, uid, current, moved,
                assume_yes=assume_yes, can_prompt=can_prompt)
    finally:
        # RECORD EVEN IF THE OFFER FAILED. Otherwise the same change is
        # re-reported on every subsequent start, which is the "warn that fires
        # every time becomes scenery" failure — and the user would be asked
        # about a switch they already answered.
        declared.write_if_changed(proj.base, current)  # #224: see above


def _report_declared_change(state_root: Path, uid: str, current: dict,
                            moved: dict, *, assume_yes: bool,
                            can_prompt: bool) -> None:
    agent_plugin = f"agent-{current['agent']}"

    if "agent" in moved:
        was, now = moved["agent"]
        # A cross-agent carry is not expressible (offer_carry_for_switch derives
        # both ends from one agent) and would be wrong anyway: the two config
        # dirs have no format in common. Say it is a fresh start, and say the
        # old history is not gone.
        click.secho(
            f"\n! config.yaml now says agent: {now} (was {was}). That is a "
            f"FRESH history.\n"
            f"  Each agent keeps its own transcripts, todos and MCP config in "
            f"its own format,\n  so there is nothing meaningful to copy. "
            f"Nothing is deleted — {was}'s history\n  stays where it is and "
            f"comes back if you switch back.",
            fg="yellow", err=True)
        return

    what = " and ".join(sorted(moved))
    was_profile, now_profile = moved.get(
        "profile", (current["profile"], current["profile"]))
    was_mode, now_mode = moved.get(
        "auth_mode", (current["auth_mode"], current["auth_mode"]))
    source = history_dir_for(state_root, uid, agent_plugin, was_mode, was_profile)
    destination = history_dir_for(state_root, uid, agent_plugin, now_mode,
                                  now_profile)
    if source == destination or not has_history(source):
        return

    click.secho(
        f"\n! You changed {what} in .botainer/config.yaml, which points this "
        f"session at a\n  DIFFERENT agent config dir — so its history, todos "
        f"and MCP config look gone.\n  They are not: they are still in the old "
        f"directory.",
        fg="yellow", err=True)

    if not can_prompt:
        click.secho(
            f"      old: {source}\n"
            f"      new: {destination}\n"
            f"  Not asking (no terminal). To move it across, run:\n"
            f"      botainer config set {sorted(moved)[0]} "
            f"{now_profile if 'profile' in moved else now_mode} --yes",
            fg="cyan", err=True)
        return

    offer_carry(source, destination, what_changed=what, assume_yes=assume_yes)
