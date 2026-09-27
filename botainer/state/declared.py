"""What `.botainer/config.yaml` DECLARED at the last start — botainer's own note.

WHY THIS EXISTS (#192). Three commands change which directory holds the agent's
history: `auth use`, `config set`, and a HAND-EDIT of config.yaml. The first two
warn and offer to carry it. The third carries nothing — and it is the route
botainer's own `--auth-profile` help text recommends for a persistent change.

The obvious fix is "at start, compare this session to the last one" and it is
WRONG. `SessionRecord.spec` holds the EFFECTIVE spec, after `--auth-profile` /
`--auth-mode` / `--agent` have been folded in by composition, with nothing
marking a field as flag-derived. So `start --auth-profile work` would look like
a switch (carry #1), and the NEXT plain `start` would look like a switch back
(carry #2) — one temporary flag, two carries, history bounced between two
directories and ending where it began.

So this records what the CONFIG FILE SAID, never what the session ran with.
One-shot flags never touch config.yaml — that is their contract, hardened by
#149 when they used to set os.environ and leaked into child processes. A
comparison reading only the declared value therefore CANNOT SEE a flag: not
"detects and skips it", which is a rule every future call site has to remember,
but structurally unable to reach it. A fourth one-shot override added later
inherits the property for free.

WHY NOT meta.json. Its job is identity, and its ABSENCE is a tampering signal
(`identity.py` refuses when meta.json is missing but the state dir has content).
Putting a value that changes with every config edit into a file whose stability
carries security meaning mixes two jobs and two failure modes.

WRITE-ON-CHANGE ONLY. Temporary state files require free inodes even when
little disk space is used. A state root at its inode quota may reject writes;
avoiding unchanged writes reduces that failure surface on routine starts.

ABSENCE IS BENIGN, DELIBERATELY. No file means "no previous start recorded", and
the caller's correct response is to say nothing and record. That is what makes
the "safe to delete" note in the file honest rather than a threat: a user who
deletes it loses one carry OFFER, never data. A "do not touch" warning on a file
whose deletion breaks things is a rule; on a file whose deletion is harmless it
is courtesy.

HOST-PRIVATE. Never bound into any container. Same reasoning as the warm-pool
sbatch `--output` placement: if the agent can write this, it can lie to the next
session about what the config said, and the carry machinery would act on the lie.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from botainer.state.secure_write import write_secure

FILENAME = "declared.json"

SCHEMA_VERSION = 1

# First key in the file, so it is the first thing a curious user sees. The
# second sentence is load-bearing and TRUE BY CONSTRUCTION — see the module
# docstring on why absence is benign.
_NOTE = ("botainer writes this to notice when you change .botainer/config.yaml. "
         "Do not edit it. Deleting it is SAFE: botainer treats a missing file "
         "as a first start and simply records again.")

# The fields whose change RELOCATES something the user cares about. Adding a
# field here is what makes a new axis participate; nothing else needs to change.
TRACKED = ("agent", "profile", "auth_mode")


@dataclass(frozen=True)
class Declared:
    """What config.yaml said. Frozen: a record of the past is not editable."""

    values: dict[str, str] = field(default_factory=dict)

    def differs_from(self, current: dict[str, str]) -> dict[str, tuple[str, str]]:
        """{field: (was, now)} for every TRACKED field that moved.

        Only compares fields present in BOTH. A field absent from the record —
        because it was added to TRACKED after this file was written — is not a
        change, it is an unknown, and reporting it as a change would fire a
        carry offer on every project the first time a new axis ships.
        """
        moved: dict[str, tuple[str, str]] = {}
        for key in TRACKED:
            if key not in self.values or key not in current:
                continue
            if self.values[key] != current[key]:
                moved[key] = (self.values[key], current[key])
        return moved


def path_for(state_dir: Path) -> Path:
    return Path(state_dir) / FILENAME


def read(state_dir: Path) -> Declared | None:
    """The recorded values, or None when there is nothing usable.

    None for: no file, unreadable, unparseable, wrong shape, or a schema version
    this build does not understand. Every one of those means the same thing to a
    caller — "I do not know what the config said last time" — and the safe
    response to all of them is identical, so they are deliberately not
    distinguished. A corrupt file must never be more dangerous than a missing
    one.
    """
    p = path_for(state_dir)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != SCHEMA_VERSION:
        return None
    vals = raw.get("declared")
    if not isinstance(vals, dict):
        return None
    return Declared({k: v for k, v in vals.items() if isinstance(v, str)})


def write_if_changed(state_dir: Path, current: dict[str, str]) -> bool:
    """Record `current` if it differs from what is on disk. Returns wrote?

    NEVER RAISES. A failure to record is not a reason to fail a session: the
    consequence is one missed carry offer, while raising would take out `start`
    entirely on a full disk or an exhausted inode quota — which is precisely the
    failure mode this file's write-on-change design exists to avoid. Silence on
    failure is the correct trade here and it is the only place in this module
    where an exception is swallowed.
    """
    tracked = {k: v for k, v in current.items() if k in TRACKED and isinstance(v, str)}
    existing = read(state_dir)
    if existing is not None and existing.values == tracked:
        return False
    payload = {
        "_": _NOTE,
        "version": SCHEMA_VERSION,
        "declared": tracked,
    }
    try:
        write_secure(path_for(state_dir),
                     json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except Exception:
        return False
    return True
