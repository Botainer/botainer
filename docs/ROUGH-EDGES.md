# Rough edges

Things botainer does not handle well yet, written down because a known limit you
can plan around beats an unknown one you discover at 2am.

Each entry says what happens, whether botainer tells you, and what you can do
today. Where there is no fix, it says so rather than suggesting one that does
not work.

This is not the bug list. It is the set of places where the honest answer is
"this is a real limitation, here is its shape."

---

## Filenames differing only in case are not distinct on macOS

**What happens.** macOS formats disks case-insensitively by default, and a
container bind inherits whatever the host directory does. So inside a session on
a Mac, `/workspace` and `/packages` treat `Model.py` and `model.py` as the same
file.

Writing both leaves ONE file, holding the SECOND write. **There is no error.**
Measured:

    write Foo.py "first"   then   foo.py "second"
    -> one file, named Foo.py, containing "second"

The first file's content is simply gone. What you see later is a broken import,
a missing attribute, or a file `git status` reports as modified forever — with
nothing pointing back at the cause.

**How often.** Rare. Most packages never ship two names differing only in case.
But it does happen, and when it does the symptom looks like anything except what
it is.

**Does botainer tell you.** Yes, now:

- `botainer doctor` reports it for the state root.
- The agent is told directly in its session notes, including what the symptom
  looks like and how to confirm it — because an agent that knows the rule
  diagnoses in one step instead of inventing a workaround nobody can follow.

Both are PROBED — botainer writes `Foo` and `foo` and looks — not inferred from
the platform, because an APFS volume can be formatted either way and a guess
here is a guess about the thing a wrong guess corrupts.

**What you can do today.** Nothing that is not worse than the problem, which is
why this is on this list. The options were weighed and each fails on something:

| | why it is not the default |
|---|---|
| case-sensitive disk image for `/packages` | attach/detach lifecycle, plus compaction that Docker blocks at the end of a session — and the sessions that most need reclaiming are the ones that died badly |
| Docker named volume | works, but you can no longer see, size or back up your packages as a folder |
| case-sensitive APFS volume | mechanically the cleanest, but asks you to run `diskutil` on your disk before you can try the tool |

If you hit an actual collision, say so — that is the signal that makes one of
these worth offering as an opt-in for your project.

**Where it does and does not bite.** In practice this is a macOS problem,
because macOS formats disks case-insensitively by default and Linux and HPC
hosts normally do not. But botainer does not assume that — `doctor` and the
agent's session notes both PROBE the actual path, so a Linux or HPC user whose
storage happens to fold case (a CIFS/SMB share, an exFAT or NTFS volume, an
ext4 filesystem with casefold enabled) gets the same line, correctly. If you
see it on Linux, it is not a bug in the check; it is telling you something
about that mount.

---

## codex may not work where botainer keeps its state

**What happens.** codex stores its history and session state in SQLite
databases using write-ahead logging, and re-enables WAL every time it opens
them — forcing the simpler journal mode from outside does not stick, measured.
WAL needs its `-shm` file to be shared *memory*, which some network filesystems
do not provide.

Where that support is missing, you get one of two behaviours, and they are very
different:

- **No shared-memory support at all** (some parallel filesystems): SQLite fails
  when it opens the database. Loud, immediate, at startup. Annoying, not
  damaging.
- **Weak locking** (some NFS setups): it appears to work and corrupts
  occasionally. This is the bad one.

**This is not about your auth mode.** Isolated, shared and broker all keep
codex's state under the botainer state root, so they succeed or fail together.
Switching modes will not rescue it.

**Does botainer tell you.** Yes. `botainer doctor` opens a database on the state
root's filesystem, turns on WAL, checks the mode was actually adopted, commits a
write and confirms the shared-memory segment appeared:

    state.sqlite_wal    supported here (ext4 (local)) — codex databases will work

Run it on the machine you intend to use, before relying on codex there. It needs
no codex install, no login and no session.

**What you can do today.**

- Point `MY_BOTAINER` at local disk, if the machine has any you can use.
- Use Claude on that machine. It does not depend on this and is unaffected.

**Not affected:** any machine where the check above says supported — which
includes every ordinary laptop and workstation.

---

## Sessions that die badly leave things behind

**What happens.** A session killed hard — SIGKILL, a Mac reboot, Docker Desktop
quitting, power loss — does not get to clean up. What it leaves depends on what
it was doing, and botainer's recovery is best-effort rather than guaranteed.

The general shape: anything botainer records at the END of a session cannot be
relied on to exist, so nothing important is designed to depend on it.

**Does botainer tell you.** Partly. `botainer status` and `botainer doctor`
report what they can see. A session directory with no record in it is a launch
that was refused or interrupted; those accumulate and nothing removes them yet.

**What you can do today.** Nothing needed in the normal case. If `botainer
status` shows a session you know is gone, it is safe to ignore.

---

## A project's own config can bind its `.botainer/` directory, or its parent

**What happens.** `mounts.extra` in `.botainer/config.yaml` may name the
project's own `.botainer/` directory — or the directory containing the project —
as a bind source, and the session composes with no refusal.

That matters because `/workspace` is read-write inside the session, so the agent
you are running can edit that config file. A later session then starts with
whatever the previous one wrote there.

**What botainer does tell you.** The bind appears in `botainer inspect` and
`botainer dry-run` with its source, target and mode, and the capability summary
before launch lists the mounts. Nothing calls it out as unusual.

**What you can do today.** Read the mount list before you answer `Launch this
session? [y/N]` — an unexpected source under your own project is the signal. If
you are running an agent against material you do not trust, `botainer inspect`
before and after a session shows whether `mounts.extra` changed.

**Scope, so this is not read as bigger than it is.** A config-supplied source
may NOT name botainer's state root, anything under it, an ancestor of it, or a
symlink to any of those — that is refused, and it is the case that reaches every
other project's credentials. This entry is the narrower one: a project reaching
its own directory and its parent.

**Fix.** Deferred to a6 by maintainer decision on 2026-09-16, with the shape
above understood. It is a real limitation, not an oversight.
