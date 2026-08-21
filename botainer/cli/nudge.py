"""`botainer nudge` — inject text into a running agent's prompt.

Usage:

    botainer nudge "continue"               # default: most-recent session in current project
    botainer nudge --session <sid> "..."    # specific session
    botainer nudge --in 30m "..."           # send after a delay (native; no at(1))
    botainer nudge --keys "C-c"             # send a special key (tmux key syntax)
    botainer nudge --dry-run "..."          # show what would be done

The CLI:
1. Locates the target session (project state + sessions/ list, runtime
   handle in spec.json).
2. Verifies the session is alive (Docker: container running; HPC: jobid
   in squeue).
3. Constructs the delivery argv (docker exec / srun --overlap).
4. Executes (or prints, with --dry-run).
5. Optionally appends to nudges-sent.jsonl audit log.

Refusals are typed and have remediation hints.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from botainer.cli import _common
from botainer.cli._refusal_handler import handle_refusals
from botainer.core.identity import resolve_identity
from botainer.core.refusal import RefusalCategory, Refused
from botainer.state import dir as state_dir
from botainer.state import session_record

# Per-call `screen -X stuff` text limit. screen's `stuff` reads the whole
# payload into a single allocation; very-long sends can fail or be silently
# truncated. Chunking is safer. 8 KB chunks tested OK on screen 4.x and 5.x.
SCREEN_STUFF_CHUNK_BYTES = 8000


@click.command("nudge")
@click.argument("text", nargs=-1)
@click.option(
    "--session",
    "session_id",
    default=None,
    help="Session ID to nudge. Defaults to most-recent running session.",
)
@click.option(
    "--in",
    "schedule_in",
    default=None,
    help="Send after a delay instead of now, e.g. '30s', '5m', '2h', '1d'. "
    "Runs in the background; no `at`/cron needed.",
)
@click.option(
    "--keys",
    "keys",
    default=None,
    help=(
        "Send a screen/tmux special key sequence instead of text. "
        "Accepted: C-a..C-z, M-a..M-z, F1..F12, Up, Down, Left, Right, "
        "Home, End, PageUp, PageDown, Tab, Escape, Enter, Space, BSpace, "
        "Delete, Insert. Mutually exclusive with TEXT argument. "
        "Anything else is REFUSED (task #170)."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be sent without executing.",
)
@click.option(
    "--no-enter",
    is_flag=True,
    default=False,
    help="Do not append Enter after the text. Default: send TEXT then Enter.",
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress informational output.",
)
@click.pass_context
@handle_refusals
def nudge(
    ctx: click.Context,
    text: str | None,
    session_id: str | None,
    schedule_in: str | None,
    keys: str | None,
    dry_run: bool,
    no_enter: bool,
    quiet: bool,
) -> None:
    """Type into a running agent's prompt from OUTSIDE the container.

    \b
    Send now:        botainer nudge continue
    Send later:      botainer nudge --in 30m "please wrap up"
    Special keys:    botainer nudge --keys C-c      (e.g. interrupt)
    A specific one:  botainer nudge --session <id> "..."

    RUN IT FROM YOUR PROJECT DIRECTORY. nudge finds the project by the
    .botainer/project-id in the folder you are standing in, and refuses
    outside one. `--session` does NOT change that — it disambiguates BETWEEN
    that project's sessions, it is not an alternative to being in the project.

    It targets that project's running session. If more than one is running
    (e.g. claude and codex side by side) it refuses and lists them rather than
    guessing, because a nudge sent to the wrong agent is not recoverable.

    REQUIRES nudge to have been enabled for the session at launch (add `nudge`
    to plugins_enabled in .botainer/config.yaml and restart). If it wasn't, you
    get a clear "not enabled" message, not a screen error.
    """
    # F6: accept multi-word text WITHOUT requiring quotes — `nudge are you
    # connected` joins to "are you connected". `text` arrives as a tuple
    # (nargs=-1); empty tuple → None so the no-text/--keys checks below fire.
    text = " ".join(text) if text else None
    # 1. Argument validation.
    if text is None and keys is None:
        _common.refuse(
            "nudge",
            "no text or --keys to send",
            "provide text to send (e.g. `botainer nudge \"continue\"`) "
            "or use --keys for special keys (e.g. `botainer nudge --keys C-c`)",
            exit_code=2,
        )
    if text is not None and keys is not None:
        _common.refuse(
            "nudge",
            "TEXT and --keys are mutually exclusive",
            "use one or the other, not both",
            exit_code=2,
        )
    # Task #170: --keys was passed through to screen/tmux verbatim. A
    # user typing `--keys "kill -9 \$\$"` would get screen interpreting
    # it as keystrokes including special meta sequences. Allowlist the
    # known good key names instead.
    if keys is not None:
        import re as _re
        _ALLOWED = _re.compile(
            r"^(C-[a-z]|M-[a-z]|F([1-9]|1[0-2])|"
            r"Up|Down|Left|Right|Home|End|PageUp|PageDown|"
            r"Tab|Escape|Enter|Space|BSpace|Delete|Insert)$"
        )
        if not _ALLOWED.fullmatch(keys):
            _common.refuse(
                "nudge",
                f"--keys value {keys!r} not in allowlist",
                "use one of: C-a..C-z, M-a..M-z, F1..F12, Up/Down/Left/Right, "
                "Home/End/PageUp/PageDown, Tab/Escape/Enter/Space/BSpace/Delete/Insert. "
                "Use TEXT (not --keys) for arbitrary characters.",
                exit_code=2,
            )
    if text == "":
        _common.refuse(
            "nudge",
            "empty text",
            "provide a non-empty string to send",
            exit_code=2,
        )

    # 2. Locate target session.
    project_root = _common.find_project_root()
    if project_root is None:
        _common.refuse(
            "nudge",
            "not inside a botainer project (no .botainer/project-id found)",
            "cd into the project directory whose agent you want to nudge. "
            "There is no flag for this — nudge is deliberately scoped to the "
            "project you are standing in.",
        )

    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    # Simplify review: the try/except TypeError was guarding against a
    # `non_interactive` kwarg that resolve_identity has never accepted.
    # Dead branch; removed.
    uid, _project_paths_state = resolve_identity(
        project_root,
        identity_accept=True,
    )
    proj_paths = state_dir.ensure_project_dirs(paths, uid)

    record = _resolve_target_session(proj_paths.sessions_dir, session_id)
    if record is None:
        if session_id:
            _common.refuse(
            "nudge",
                f"no session found with ID {session_id}",
                f"check `ls {proj_paths.sessions_dir}` for available sessions, "
                "or omit --session to use the most-recent running one",
                exit_code=2,
            )
        else:
            _common.refuse(
            "nudge",
                "no running session found in this project",
                "start one with `botainer start`",
                exit_code=2,
            )

    # 3. Host check (HPC: nudge from same host as where the session was launched).
    current_host = socket.gethostname()
    # HPC: login nodes typically share hostname prefix with compute
    # nodes (e.g. `grace-login.example.edu` and `c1n1`). Refuse only
    # if the prefix mismatches.
    if (
        record.host
        and record.host != current_host
        and not _hosts_likely_compatible(record.host, current_host)
    ):
        _common.refuse(
            "nudge",
            f"session was launched from host {record.host!r}; "
            f"current host is {current_host!r}",
            "run `botainer nudge` from the same host (or login node) "
            "where you started the session",
            exit_code=2,
        )

    # 4. Verify session alive.
    if not _is_session_alive(record):
        _common.refuse(
            "nudge",
            f"session {record.session_id} is not running "
            f"(runtime={record.runtime})",
            "list running sessions with `botainer status` (`botainer list` shows "
            "PROJECTS, not sessions); if you expected this one to be running, "
            "the container may have exited",
            exit_code=2,
        )

    # 5. Build delivery command(s).
    chunks = _prepare_chunks(text, keys, no_enter)
    if not chunks:
        _common.refuse(
            "nudge",
            "nothing to send after preparation",
            "internal: text/keys produced no screen-stuff invocation",
            exit_code=2,
        )

    # 6. Delayed send if --in was given — a detached background sleep+redeliver,
    #    so it needs no at(1)/cron (which are often absent). The scheduled copy
    #    re-invokes us with --session so it targets the same session, and runs in
    #    the project dir so it resolves identity the same way.
    if schedule_in is not None:
        secs = _parse_delay_seconds(schedule_in)
        if secs is None:
            _common.refuse(
                "nudge",
                f"--in value {schedule_in!r} could not be parsed",
                "use forms like '30s', '5m', '2h', '1d'",
                exit_code=2,
            )
        my_argv = _self_invocation_argv(text, keys, record.session_id, no_enter)
        sched_log = (
            proj_paths.sessions_dir / record.session_id / "scheduled-nudge.log"
        )
        _schedule_native(my_argv, secs, cwd=project_root, log_path=sched_log)
        if not quiet:
            click.echo(
                f"scheduled nudge in {schedule_in} (background) into session "
                f"{record.session_id}.\n"
                f"  if it doesn't arrive, check: {sched_log}"
            )
        return

    # 7. Execute (or dry-run).
    delivery_argv_list = [
        _build_delivery_argv(record, chunk) for chunk in chunks
    ]
    if dry_run:
        click.echo("dry-run: would execute:")
        for cmd in delivery_argv_list:
            click.echo("  " + " ".join(_quote_for_display(arg) for arg in cmd))
        return

    for cmd in delivery_argv_list:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            _common.refuse(
                "nudge",
                "delivery timed out after 30s",
                "the session may be unresponsive; check `botainer status` or "
                "for HPC try `squeue -j <jobid>`; for Docker try `docker ps`",
                exit_code=1,
            )
        except FileNotFoundError:
            # `screen` (or `srun`) isn't on PATH — nudge's delivery channel is
            # host-side GNU screen. This is a host-tooling gap, not a bug.
            _common.refuse(
                "nudge",
                "`screen` is not installed on this host",
                "nudge delivers keystrokes through a host-side `screen` session; "
                "install it (apt install screen / brew install screen) on the "
                "machine where you launched the session",
                exit_code=1,
            )
        if result.returncode != 0:
            err = (result.stderr.strip() or result.stdout.strip())
            # screen prints "No screen session found matching botainer-<sid>"
            # when the session was NOT launched with nudge enabled (no screen
            # wrapper) — the common case. Give the enablement fix, not the raw
            # screen error the user actually saw ("-X: stuff: ...").
            if "no screen session" in err.lower():
                _common.refuse(
                    "nudge",
                    f"nudge is not enabled for session {record.session_id} "
                    f"(no `screen` wrapper around it)",
                    "nudge only works if the session was STARTED with nudge "
                    "enabled. Add `nudge` to `plugins_enabled` (or a `plugins: "
                    "{nudge: {}}` block) in .botainer/config.yaml, then restart "
                    "the session. (Config alone isn't enough — the wrapper is set "
                    "up at launch, so a running session can't be nudge-enabled "
                    "retroactively.)",
                    exit_code=1,
                )
            _common.refuse(
                "nudge",
                f"delivery failed (exit {result.returncode}): {err}",
                "verify the session is alive (`botainer status`); "
                "for HPC, verify your Slurm credentials (`squeue`); "
                "for Docker, verify the container is running (`docker ps`)",
                exit_code=1,
            )

    # 8. Append to audit log.
    if not quiet:
        what = f"text={text!r}" if text is not None else f"keys={keys!r}"
        click.echo(
            f"nudged session {record.session_id} ({record.runtime}): {what}"
        )
    # Task #71: honor plugins.nudge.log_sent_nudges (declared in manifest
    # config_schema but previously never read). Default True for backward
    # compatibility with the prior unconditional-log behavior.
    log_enabled = True
    try:
        from botainer.core import config as _config
        cfg = _config.load_project_config(project_root)
        nudge_cfg = (cfg.plugins or {}).get("nudge", {})
        log_enabled = bool(nudge_cfg.get("log_sent_nudges", True))
    except Exception:
        # Config load failure during nudge shouldn't break the nudge
        # itself; default behavior was "always log" so fall back to that.
        log_enabled = True
    if log_enabled:
        _append_audit(proj_paths.sessions_dir / record.session_id, text, keys)


# ────────── helpers ──────────
# (_find_project_root was previously duplicated here; consolidated into
# botainer.cli._common. _refuse_with_remediation was the previous local
# refuse helper; new code uses _common.refuse and the old helper has
# been removed (no monkeypatching test references it any more).)


def _resolve_target_session(
    sessions_dir: Path, session_id: str | None
) -> session_record.SessionRecord | None:
    """Find the most-recent session matching the optional ID."""
    records = session_record.list_sessions(sessions_dir)
    if not records:
        return None
    if session_id:
        matches = [r for r in records if r.session_id.startswith(session_id)]
        if not matches:
            return None
        if len(matches) > 1:
            # Ambiguous prefix; refuse with hint.
            ids = ", ".join(r.session_id for r in matches[:5])
            _common.refuse(
            "nudge",
                f"session ID {session_id!r} is an ambiguous prefix",
                f"matches: {ids}. Use a longer prefix or the full ID.",
                exit_code=2,
            )
        return matches[0]
    # No ID: the project's running session — and REFUSE if there is more than
    # one. This file already refuses an ambiguous --session PREFIX three lines
    # up ("use a longer prefix"); silently guessing when the ambiguity is
    # implicit is the same question answered two different ways. A project can
    # legitimately have two live sessions (`--agent claude` and `--agent
    # codex`, #112), and text delivered to the wrong agent cannot be recalled.
    alive = [r for r in records if _is_session_alive(r)]
    if len(alive) > 1:
        listed = "\n".join(
            f"    --session {r.session_id}   ({r.runtime}, started "
            f"{r.started_at or 'unknown'})" for r in alive[:6])
        _common.refuse(
            "nudge",
            f"{len(alive)} sessions are running in this project — "
            f"refusing to guess which one you meant",
            f"name one:\n{listed}",
            exit_code=2,
        )
    if alive:
        return alive[0]
    return records[0]  # most recent record; step 4 refuses it as not running


def _hosts_likely_compatible(record_host: str, current_host: str) -> bool:
    """Loose heuristic for HPC: hostnames sharing a 4+ char prefix are
    'compatible' (login + compute nodes typically do)."""
    if record_host == current_host:
        return True
    common = 0
    for a, b in zip(record_host, current_host, strict=False):
        if a != b:
            break
        common += 1
    return common >= 4


def _is_session_alive(record: session_record.SessionRecord) -> bool:
    """Best-effort liveness check.

    Delegates to botainer.state.liveness — kept as a module-local symbol
    so existing monkeypatching in tests still works. Direct callers
    should use `liveness.is_session_alive` instead.
    """
    from botainer.state.liveness import is_session_alive as _delegate
    return _delegate(record)


def _screen_key_seq(key: str) -> str:
    """Translate a tmux-style key name (the --keys allowlist) into the raw byte
    sequence GNU screen's `stuff` actually sends — screen has no named keys, so
    `stuff Enter` used to stuff the letters "Enter". C-x → its control
    byte, M-x → ESC+x, and the named/arrow/function keys → their terminal escape
    sequences."""
    named = {
        "Enter": "\r", "Tab": "\t", "Escape": "\x1b", "Space": " ",
        "BSpace": "\x7f", "Delete": "\x1b[3~", "Insert": "\x1b[2~",
        "Up": "\x1b[A", "Down": "\x1b[B", "Right": "\x1b[C", "Left": "\x1b[D",
        "Home": "\x1b[H", "End": "\x1b[F",
        "PageUp": "\x1b[5~", "PageDown": "\x1b[6~",
    }
    if key in named:
        return named[key]
    if len(key) == 3 and key.startswith("C-") and key[2].isalpha():
        return chr(ord(key[2].upper()) - 64)   # C-c → \x03
    if len(key) == 3 and key.startswith("M-") and key[2].isalpha():
        return "\x1b" + key[2]                  # M-x → ESC x
    if key and key[0] == "F" and key[1:].isdigit():
        n = int(key[1:])                        # F1..F12
        f = {1: "\x1bOP", 2: "\x1bOQ", 3: "\x1bOR", 4: "\x1bOS", 5: "\x1b[15~",
             6: "\x1b[17~", 7: "\x1b[18~", 8: "\x1b[19~", 9: "\x1b[20~",
             10: "\x1b[21~", 11: "\x1b[23~", 12: "\x1b[24~"}
        if n in f:
            return f[n]
    return key  # already validated by the --keys allowlist; fall back to literal


def _prepare_chunks(text: str | None, keys: str | None, no_enter: bool) -> list[list[str]]:
    """Build the per-call screen `-X stuff` payload lists.

    Each returned list is the trailing args appended after `screen -S <sid>
    -X stuff --` in `_build_delivery_argv`. §A19: screen runs OUTSIDE the
    container; tmux is no longer involved.

    Long text is split into ≤SCREEN_STUFF_CHUNK_BYTES chunks to avoid
    screen's `stuff` single-allocation buffer limits. Enter is only sent
    at the end (so chunks form a single line).
    """
    # Enter is a real carriage-return BYTE appended to the text — NOT the tmux key
    # name "Enter" (screen has no named keys; `stuff "text" "Enter"` stuffed the
    # letters "Enter" / errored —). `\r` is what the Enter key sends in
    # a terminal, so the agent's TUI submits.
    _CR = "\r"
    chunks: list[list[str]] = []
    if keys is not None:
        chunks.append([_screen_key_seq(keys)])
        return chunks
    assert text is not None
    encoded = text.encode("utf-8")
    if len(encoded) <= SCREEN_STUFF_CHUNK_BYTES:
        chunks.append([text if no_enter else text + _CR])
        return chunks
    # Need to split. Be careful at UTF-8 boundaries.
    parts = _split_utf8_safe(encoded, SCREEN_STUFF_CHUNK_BYTES)
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        s = part.decode("utf-8")
        chunks.append([s + _CR if (is_last and not no_enter) else s])
    return chunks


def _split_utf8_safe(data: bytes, chunk_size: int) -> list[bytes]:
    """Split bytes into chunks of ≤chunk_size without breaking UTF-8 sequences."""
    parts: list[bytes] = []
    i = 0
    n = len(data)
    while i < n:
        end = min(i + chunk_size, n)
        # Walk back end to a safe UTF-8 boundary (start byte is 0xxxxxxx
        # or 11xxxxxx; continuation byte is 10xxxxxx).
        while end > i and end < n and (data[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(data[i:end])
        i = end
    return parts


def _build_delivery_argv(
    record: session_record.SessionRecord, stuff_args: list[str]
) -> list[str]:
    """Construct the full delivery argv for this runtime + this screen-stuff call.

    FEATURE-PARTITION-LOCKED.md §A19: screen runs OUTSIDE the container.
    Delivery target is the HOST-side `screen -S <screen_session_id>` that
    `botainer start` created when nudge was enabled.

    For Docker:
      Host runs the screen wrap directly:
        screen -S botainer-<sid> -X stuff "<text>\\n"
      No `docker exec`; the screen pty is the docker container's stdin.

    For Apptainer + Slurm:
      Screen runs on the COMPUTE NODE (created inside the sbatch
      script around the apptainer exec). Reach it via
      `srun --overlap --jobid X` and run the same screen -X command
      on the compute node. No apptainer exec into the running step.

    For Apptainer foreground (no Slurm):
      Screen ran in the user's local shell on the same host; reach
      it directly with `screen -X stuff`.

    Insecure-defaults review (HIGH 3): `--` separator between -S target
    and user-controlled `stuff` payload. `screen -S foo -X stuff -- "..."`
    avoids screen treating the leading `-` of a hostile payload as a flag.
    """
    sid = record.screen_session_id
    if not sid:
        raise Refused(
            RefusalCategory.RUNTIME_NOT_AVAILABLE,
            "no screen_session_id recorded for session — nudge plugin was "
            "not enabled at `botainer start` time, so no host-side screen "
            "wrap exists. Enable `nudge` in plugins_enabled and restart.",
        )
    # stuff_args is the payload to `screen -X stuff`. screen's `stuff` takes ONE
    # argument: the literal text (a trailing \r submits). NO `--` separator —
    # GNU screen's stuff command does not accept it (it errored with
    # "-X: stuff: ...";). A leading-`-` payload is safe without it: the
    # payload is the `stuff` command's operand, already past screen's own option
    # parsing (screen -S <sid> -X stuff consumed the options), so it is never
    # re-parsed as a screen flag.
    inner = ["screen", "-S", sid, "-X", "stuff", *stuff_args]
    if record.runtime == "docker":
        # Screen lives on the launching host. No docker exec.
        return inner
    if record.runtime == "apptainer":
        if record.apptainer is None:
            raise Refused(
                RefusalCategory.RUNTIME_NOT_AVAILABLE,
                "no apptainer handle recorded for session",
            )
        if record.apptainer.slurm_jobid:
            # HPC path: screen lives on the compute node. Reach via srun --overlap.
            # HPC review F7: --overcommit + --cpus-per-task=1 so the overlap step
            # doesn't block waiting for the parent step's resources.
            return [
                "srun",
                "--overlap",
                "--overcommit",
                "--cpus-per-task=1",
                "--mem=0",  # share parent step's memory
                "--jobid",
                record.apptainer.slurm_jobid,
                *inner,
            ]
        # No Slurm; screen ran on this host. Same as docker path.
        return inner
    if record.runtime == "mock":
        # Mock runtime: just print what would be done; never actually delivered.
        return ["echo", "[mock]", *inner]
    raise Refused(
        RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
        f"nudge: unsupported runtime {record.runtime!r}",
    )


def _quote_for_display(s: str) -> str:
    """Lightweight shell-quote for human-readable dry-run output. Not
    for execution — we always exec via argv tuple."""
    if not s or any(c in s for c in " \t\n\"'\\$;`&|<>()*?[]{}~#!"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def _parse_delay_seconds(s: str) -> int | None:
    """Parse '30s' / '5m' / '2h' / '1d' into a number of seconds. None if bad."""
    m = re.fullmatch(r"(\d+)([smhd])", s.strip().lower())
    if not m:
        return None
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _self_invocation_argv(
    text: str | None, keys: str | None, sid: str, no_enter: bool
) -> list[str]:
    # Re-invoke via the RUNNING interpreter + module, NOT a bare "botainer":
    #  - a detached `sh -c` doesn't see the user's shell aliases/wrappers, so a
    #    literal "botainer" resolved to nothing and `nudge --in` failed SILENTLY
    #;
    #  - the user may run MULTIPLE botainer versions — `sys.executable` pins the
    #    scheduled send to the SAME version that scheduled it (its own venv),
    #    not whatever `botainer` is active when the timer fires.
    # `python -m botainer.cli.main` is the same entrypoint the pool worker uses.
    argv = [sys.executable, "-m", "botainer.cli.main",
            "nudge", "--session", sid, "--quiet"]
    if no_enter:
        argv.append("--no-enter")
    if keys is not None:
        argv += ["--keys", keys]
    elif text is not None:
        argv.append(text)
    return argv


def _schedule_native(argv: list[str], seconds: int, *, cwd: Path,
                     log_path: Path | None = None) -> None:
    """Deliver `argv` (a `botainer nudge …` self-invocation) after `seconds`, via
    a DETACHED background `sh -c 'sleep N; <cmd>'` — no at(1)/cron dependency.

    The command string is a shell boundary, so every token is shlex.quote'd
    (stdlib, audited) exactly like the old at(1) path. `start_new_session=True`
    detaches it from this shell/terminal so it survives after the CLI returns;
    it runs in the project dir so the re-invocation resolves the same identity.

    Output (incl. any error from the re-invocation) is APPENDED to `log_path` when
    given — it used to go to /dev/null, so a failed scheduled send (e.g. an
    unresolved command name) was completely INVISIBLE. Markers
    bracket the sleep so the log proves the timer fired even on a quiet success.
    """
    import shlex as _shlex
    cmd_str = " ".join(_shlex.quote(a) for a in argv)
    script = (
        f'echo "[nudge] scheduled $(date); firing in {int(seconds)}s"; '
        f'sleep {int(seconds)}; '
        f'echo "[nudge] delivering $(date)"; {cmd_str}; '
        f'echo "[nudge] done (rc=$?) $(date)"'
    )
    logf = None
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logf = open(log_path, "a", encoding="utf-8")  # child dups the fd
        except OSError:
            logf = None
    stdout = logf if logf is not None else subprocess.DEVNULL
    stderr = subprocess.STDOUT if logf is not None else subprocess.DEVNULL
    try:
        subprocess.Popen(
            ["sh", "-c", script],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    except OSError as exc:
        raise Refused(
            RefusalCategory.RUNTIME_LAUNCH_FAILED,
            f"could not schedule background nudge: {exc}",
        ) from None
    finally:
        if logf is not None:
            logf.close()


def _append_audit(session_dir: Path, text: str | None, keys: str | None) -> None:
    """Append to nudges-sent.jsonl with mode 0600.

    Insecure-defaults review (HIGH 2): previously used the bare
    `open(..., "a")` which honors the process umask (typically 022 →
    0644 = world-readable). The audit log is in a 0700 session dir
    which mostly mitigates, but a tarball / shared-FS spread can leak
    the contents. Create with explicit 0600 mode.
    """
    try:
        log_path = session_dir / "nudges-sent.jsonl"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "by": os.environ.get("USER") or "unknown",
            "host": socket.gethostname(),
            # Task #301: users paste credentials into nudge text.
            # Record length + first-line + sha256-prefix instead of
            # verbatim. Stops obvious key formats but doesn't disclose
            # accidental long secrets.
            "text_len": len(text) if text else 0,
            "text_head": (text or "")[:40],
            "text_sha8": (
                __import__("hashlib").sha256(text.encode("utf-8")).hexdigest()[:8]
                if text else None
            ),
            "keys": keys,
        }
        # Open with explicit 0600 via os.open; honors mode arg even if
        # the file doesn't exist yet (unlike open() which uses umask).
        fd = os.open(
            str(log_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            mode=0o600,
        )
        try:
            os.write(fd, (json.dumps(entry) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


