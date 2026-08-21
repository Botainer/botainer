# nudge — inject text into a running agent's prompt

Inject keystrokes into a running agent's prompt from a different
shell. Useful when:

- The agent hit a rate limit and is idle at the prompt; you want to
  send "continue" when the limit clears.
- You're stepping away from the keyboard and want to schedule a prompt
  for later via `at(1)`.
- You're driving the agent from a wrapper script.

## How it works

The agent runs inside a `screen` session **on the host** (outside the
container — per the project rule "screen runs outside container, only if
nudge enabled"). `botainer nudge "<text>"` runs
`screen -S botainer -X stuff "<text>\n"`. screen's `stuff` is the
cleanest way to inject keystrokes that look identical to user typing.

For HPC (Apptainer + Slurm), nudge uses `srun --overlap --jobid=<jid>`
to reach the compute node, then `screen -X stuff` against the session
running there.

screen is preferred over tmux because (a) it ships built-in on macOS
and (b) it's already standard on most HPC login nodes. On Linux
laptops, install via `apt install screen` (per task #169 — nudge
must work on Linux).

## Tradeoff

screen changes your terminal's copy/paste behavior — mouse selection
acts inside the screen pane. Use `shift+mouse` for terminal-native
copy, or screen's prefix `Ctrl-A [` for screen's own copy mode. This
is the cost of having `stuff` available.

## Setup

Add to `.botainer/config.yaml`:

```yaml
plugins_enabled:
  - agent-claude
  - git
  - nudge

plugins:
  nudge:
    show_status_line: true
    log_sent_nudges: true     # writes to ~/.botainer/state/<uuid>/sessions/<sid>/nudges-sent.jsonl
```

## Usage

```sh
# Start a session in the background.
botainer start --detach

# In another shell:
botainer nudge "continue from where you left off"
botainer nudge --keys C-c               # send Ctrl-C
botainer nudge --in 30m "..."           # schedule via at(1)
botainer nudge --session <sid> "..."    # specific session
botainer nudge --dry-run "..."          # preview
```

## Security notes

- The screen socket is at `$XDG_RUNTIME_DIR/screen/S-$USER/<sid>.botainer`
  (laptop) or `$SLURM_TMPDIR/screen-<sid>` (HPC). Both are mode 0700
  on the parent dir and visible only to the user.
- screen runs OUTSIDE the container; nothing inside the container
  can reach the screen socket directly.
- ANYONE with shell access to the host (under your UID) can `botainer
  nudge` into your sessions. The capability summary warns about this
  at every launch.
- Nudges are logged to `nudges-sent.jsonl` for audit.

See DN-037 for the full design.
