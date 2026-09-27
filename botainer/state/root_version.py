"""Which botainer wrote this state root — the precondition for every upgrade check.

WHY THIS EXISTS (#202). `session_record.py` accepts schema N or N-1, so a user who
skips one release gets records that fail to load and history that quietly
disappears — the same class of loss as #122/#159. But nothing could even DETECT
that, because nothing recorded which botainer had written a given state root. So
the migration command tracked as #27 stayed unbuildable for the most basic reason
available: there was nothing to migrate FROM. (Named that way rather than as a
command line on purpose — it does not exist yet, and
tests/integration/test_shipped_strings_name_real_commands.py is right to refuse
shipped text that names a command a user cannot run.)

This is the "few lines" that unblocks the rest. It is deliberately not a
migration engine — it is the fact a migration engine would need, written down
before it is needed rather than after, because a version can only be recorded
going forward. A root that was never stamped cannot be retroactively stamped
with the truth.

TWO NUMBERS, AND THE DISTINCTION IS THE POINT.

  `layout_version`   an INTEGER we control, bumped only when the ON-DISK layout
                     changes. This is what code branches on.
  `botainer_version` the human display string. Never branched on: it comes from
                     `botainer.__version__`, which derives it from
                     pyproject/metadata and can legitimately read
                     "unknown (not installed)". A label for a person, not an
                     input to logic.

Branching on the display string is how you end up regex-parsing an alpha suffix
at 2am. The integer exists so that never has to happen. No example version is
written out here: tests/unit/test_version_is_single_sourced.py counts a
version-shaped literal anywhere under botainer/ as a fourth copy of the version,
and it is right to — a docstring example is exactly how such a copy starts.

THE OLDER BUILD MUST NOT WRITE OVER THE NEWER RECORD. If a root says layout 3 and
this build knows layout 2, the honest state is "a botainer newer than me has been
here" — and stamping it back down to 2 would destroy exactly the evidence that
says so, converting a detectable situation into an undetectable one. So
`record()` refuses to lower the number, and reports the skew instead. Downgrades
can occur when multiple installations are pointed at the same state root.

THIS IS A FILTER, NOT A PROPERTY, and calling it "the rule with teeth" (as an
earlier draft did) oversold it. Two things defeat it, and both are worth knowing
before anyone relies on it:

  * DELETE THE FILE and the refusal has nothing to refuse — absence is the
    benign case by design, so an old build re-stamps a fresh layout-1 record and
    the skew is simply gone. `_NOTE` says so now rather than calling deletion
    unqualifiedly "safe".
  * TWO PROCESSES RACING. The write is atomic (`os.replace`) so there is no torn
    file, but the read-then-decide is not locked: an old build can read
    `previous`, a new build can replace the file, and the old build then writes.
    Sub-millisecond window and both processes have to be starting at once. Not
    fixed, deliberately — `broker/refresh_lock.py` has the right primitive if it
    ever matters, and a lock on the busiest path in the launcher is a worse trade
    today than a documented race.

What it DOES buy is the accidental case, which is the common one: the same user
alternating between two installs over days. That is worth having. It is not a
guarantee against a determined or unlucky sequence, and documenting it as one
would be the "filter documented as a guarantee" error this project keeps
recording.

ABSENCE IS BENIGN BUT NOT SILENT-EQUIVALENT. A missing file means one of two
genuinely different things, and we can tell them apart cheaply by asking whether
the root already holds projects:

  empty root      → we are creating it now, so `created_by` is TRUE.
  populated root  → it predates this feature. `created_by` is UNKNOWN, and
                    saying so is the whole value. Writing today's version into
                    `created_by` would be a fabricated provenance record — worse
                    than none, because later code would trust it.

WRITE-ON-CHANGE ONLY. Recording unchanged values would add unnecessary write
traffic and an inode-quota failure point to nearly every command. With the same
build and record, the steady state requires no write.

NEVER RAISES, and the code keeps that rather than the caller. `read()` catches
broadly and bounds the file size first, because `(OSError, ValueError)` missed
`RecursionError` from `json.loads` on nested input and `MemoryError` on a huge
file — which meant a corrupt `root.json` produced a traceback from `doctor`, the
command you would run to diagnose a corrupt state root. A root that cannot record
its version is a root that cannot be migrated later; that is bad, and still not a
reason to take out `botainer --help` on a full disk. Failures surface through the
return value so `doctor` can report them.

WHERE THIS FILE SITS, AND WHAT THAT IS WORTH. It lives at the state ROOT, a
sibling of `state/`, and every bind botainer composes is a per-component path
UNDER `state/<uuid>/` — verified by rendering a real mount plan, whose eight bind
sources were all below that level. So the default composition cannot hand this
file to a container, on either runtime.

That is a strong default, not an invariant, and the difference matters: a user's
`mounts.extra` may name `~/.botainer` as a source. `mount_plan/validation.py`
denies `$HOME` and a list of sensitive subpaths, but `$HOME/.botainer` is on
neither list. So an explicit, deliberate config CAN expose this file — along with
`shared-auth/`, which is the bigger prize. Adding the resolved state root to the
bind-source denylist is tracked separately; until then, "the container cannot see
this" is true of every plan botainer builds on its own and NOT a property of the
system, and it must not be written down as one.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from botainer.state.secure_write import write_secure

FILENAME = "root.json"

# THE ON-DISK LAYOUT CONTRACT. Bump ONLY when an older build would misread a
# newer root — not for adding a file an old build simply ignores, and not for
# changes inside a per-project dir (those have their own versions). Every bump
# needs a note in the table below, because "why is this 3?" is the first
# question anyone writing a migration will ask.
#
#   1  2026-09-01  first recorded layout: <root>/{state,plugins,logs,images}/,
#                  policy.yaml, state/<uuid>/ with meta.json + sessions/.
LAYOUT_VERSION = 1

# ASCII ONLY, deliberately. json.dumps escapes non-ASCII by default, so an em
# dash here reaches the user as a literal "—" in the file they are meant to
# READ. Observed on the first real run. Restricting the sentence to ASCII fixes
# it at the source rather than by remembering ensure_ascii=False at each writer.
#
# AND IT SAYS WHAT DELETION ACTUALLY COSTS. The first draft said deleting this
# is "safe", which is true of the user's data and false of the property the file
# exists for: delete it, run an older botainer, and the newer-build record is
# gone, so the skew this file is supposed to make visible becomes invisible
# instead. Saying "safe" full stop would be the comfortable half of the truth.
_NOTE = ("botainer writes this to know which version last used this directory, "
         "so a future upgrade can migrate it. Do not edit it. Deleting it does "
         "not harm your projects or logins, but botainer then cannot tell "
         "whether a newer version has used this directory, which is the one "
         "thing this file is for.")

_UNKNOWN_ORIGIN = "unknown (root predates version recording)"

#: A real record is ~300 bytes. Bounded before `read_text` so a damaged or
#: hostile file cannot turn a diagnostic into a MemoryError.
_MAX_RECORD_BYTES = 64 * 1024

#: Layout versions are hand-assigned and increment by one. This is not a
#: prediction about how many there will be; it is a ceiling that keeps an
#: absurd on-disk value from being accepted, permanently refusing every future
#: write, and being interpolated whole into a terminal.
_MAX_LAYOUT_VERSION = 10_000

#: How much of a version label we are willing to show. Long enough for any real
#: version string plus the "unknown (…)" sentinel.
_MAX_LABEL_CHARS = 64


def _display_safe(value: object) -> str:
    """A version label that is safe to put in a terminal, or the unknown sentinel.

    These two fields come off disk and go straight into a `Finding` that
    `doctor` prints with `click.secho`, which filters nothing. A hand-edited
    `root.json` could therefore carry C0/ANSI bytes into the user's terminal.
    `mount_plan/validation.py` rejects every C0 character on attacker-adjacent
    input for exactly this reason; this reader had no equivalent.

    Structure over filtering where it is cheap: rather than trying to enumerate
    dangerous sequences, keep only printable non-control characters and bound
    the length. Anything else is not a version string.
    """
    if not isinstance(value, str) or not value:
        return _UNKNOWN_ORIGIN
    cleaned = "".join(c for c in value[:_MAX_LABEL_CHARS] if c.isprintable())
    return cleaned or _UNKNOWN_ORIGIN


@dataclass(frozen=True)
class RootVersion:
    """What the root says about itself. Frozen: a record of the past is not editable."""

    layout_version: int
    created_by: str
    last_used_by: str

    @property
    def origin_is_known(self) -> bool:
        return self.created_by != _UNKNOWN_ORIGIN


@dataclass(frozen=True)
class RecordResult:
    """What `record()` observed. Every field is a thing `doctor` may want to say."""

    #: What was on disk before this call. None = nothing recorded yet.
    previous: RootVersion | None
    #: True when the root was stamped by a build with a HIGHER layout than ours.
    #: The one condition here that is genuinely a problem for the user.
    root_is_newer: bool
    #: True when we wrote. False means "nothing changed" OR "the write failed" —
    #: deliberately not distinguished by this flag; see `write_failed`.
    wrote: bool
    #: True only when a write was attempted and raised.
    write_failed: bool = False

    @property
    def recorded_layout(self) -> int | None:
        return self.previous.layout_version if self.previous else None


def path_for(root: Path) -> Path:
    return Path(root) / FILENAME


def read(root: Path) -> RootVersion | None:
    """The recorded version, or None when there is nothing usable.

    None for: no file, unreadable, unparseable, wrong shape, or a
    `layout_version` that is not an int. Every one of those means the same thing
    to a caller — "this root does not tell me what wrote it" — and the safe
    response to all of them is identical. A corrupt file must never be more
    dangerous than a missing one.

    Note what is NOT rejected here: a layout_version HIGHER than
    `LAYOUT_VERSION`. That is the single most important value this function can
    return, so refusing to parse it would blind the exact check it exists to
    feed.

    The except is deliberately BROAD, and the size guard is not decoration.
    `(OSError, ValueError)` was not enough: `json.loads` on deeply nested input
    raises `RecursionError`, which is a `RuntimeError`, and a large file raises
    `MemoryError` — so a corrupt `root.json` produced a raw traceback from
    `doctor`, the command you would run to diagnose a corrupt state root. The
    docstring above promised the opposite. Caught by review; the promise is now
    kept by the code rather than by the caller's `contextlib.suppress`.
    """
    p = path_for(root)
    try:
        # Bounded before reading. A real record is ~300 bytes; anything past
        # this is damaged or hostile, and neither deserves a MemoryError.
        if p.stat().st_size > _MAX_RECORD_BYTES:
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:            # noqa: BLE001 — see above
        return None
    if not isinstance(raw, dict):
        return None
    layout = raw.get("layout_version")
    if not isinstance(layout, int) or isinstance(layout, bool):
        return None
    # Bound it. An absurd value (10**5000) would otherwise be accepted, refuse
    # every future write, and be interpolated whole into a terminal Finding.
    if not 0 < layout <= _MAX_LAYOUT_VERSION:
        return None
    return RootVersion(
        layout_version=layout,
        created_by=_display_safe(raw.get("created_by")),
        last_used_by=_display_safe(raw.get("last_used_by")),
    )


def record(root: Path, botainer_version: str, *, created_now: bool) -> RecordResult:
    """Stamp this build onto the root. Never raises. Never lowers the layout.

    Called from `ensure_user_state_dir`, i.e. on essentially every command, so
    the no-change path must be cheap: one read, one comparison, no write.

    `created_now` says whether THIS call is what brought the root into
    existence, and it is a REQUIRED keyword — omitting it is a `TypeError`, not
    a silent guess. That matters because the first version of this guessed, by
    asking whether `state/` held anything but `by-name/`, and the guess was
    wrong on a perfectly ordinary sequence:

        botainer setup       # pre-#202 build: creates root, policy.yaml,
                             # plugins/, shared-auth/ — but state/ stays EMPTY
        botainer auth login
        # ... upgrade ...
        botainer start       # state/ still empty -> "I created this root"

    `created_by` would then name a build that did not create the root, and it is
    preserved verbatim forever after. The module docstring calls a fabricated
    provenance record worse than none precisely because #27's migration engine
    is the code that would trust it.

    `ensure_user_state_dir` KNOWS the answer — it can test `root.exists()`
    immediately before `root.mkdir()` — so the fix is to pass the fact in rather
    than infer it downstream. The enumeration is gone, not merely widened: a
    `state/` containing something unanticipated (`locks/` already does, created
    by the codex login hook) is now INERT here rather than classified.
    """
    try:
        root = Path(root)
        previous = read(root)
    except Exception:            # noqa: BLE001 — "never raises" means it
        # `Path()` on a bad type and `read()` were both OUTSIDE any try, so the
        # no-raise promise was actually being kept by the CALLER's
        # contextlib.suppress in ensure_user_state_dir. A guarantee that lives
        # in the caller is not a guarantee of this function.
        return RecordResult(previous=None, root_is_newer=False, wrote=False,
                            write_failed=True)

    # THE REFUSAL THAT MATTERS. A newer build has been here; leave its record
    # intact. Writing ours would erase the only evidence of the skew.
    if previous is not None and previous.layout_version > LAYOUT_VERSION:
        return RecordResult(previous=previous, root_is_newer=True, wrote=False)

    if previous is None:
        created_by = botainer_version if created_now else _UNKNOWN_ORIGIN
    else:
        created_by = previous.created_by

    desired = RootVersion(
        layout_version=LAYOUT_VERSION,
        created_by=created_by,
        # Same treatment as values read off disk. `botainer.__version__` is
        # derived from pyproject/metadata, so it is not attacker input — but it
        # lands in the same terminal string, and a rule that applies to one
        # source of a field and not the other is a rule someone will get wrong.
        last_used_by=_display_safe(botainer_version),
    )
    if previous == desired:
        return RecordResult(previous=previous, root_is_newer=False, wrote=False)

    payload = {
        "_": _NOTE,
        "layout_version": desired.layout_version,
        "created_by": desired.created_by,
        "last_used_by": desired.last_used_by,
    }
    try:
        write_secure(path_for(root),
                     json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except Exception:
        return RecordResult(previous=previous, root_is_newer=False,
                            wrote=False, write_failed=True)
    return RecordResult(previous=previous, root_is_newer=False, wrote=True)
