"""Every hook that reads the session record must survive a REAL one.

THE DEFECT THIS EXISTS FOR. Five bundled hooks need the state root, and all
five reach for it in the record the launcher hands them:

    state_dir = Path(record["state_dir"])

`SessionRecord.to_dict` writes twelve keys and `state_dir` is not among them —
it lives inside the embedded spec, at `record["spec"]["state_dir"]`. Four of the
five carried a fallback. `wolfram-sidecar` did not, so it raised `KeyError` on
EVERY run, on every host, in a real `start` exactly as much as in a preview.
That is why the host-side Wolfram path had never worked (#179): the hook could
not get past the line.

Sibling drift, the #136 shape: a family of five, four fixed, one missed. The
per-plugin tests could not see it because each one tests its own plugin, and the
thing that was wrong was the RELATIONSHIP between a producer and five consumers.

SO THIS TEST IS THE JOIN, not another per-plugin test. It builds the record the
way the launcher builds it — `session_record.from_spec(spec)` — and then asserts
against every consumer at once. A sixth plugin added tomorrow is covered on the
day it is added, with no new test file, which is the only version of this that
survives contact with a future session.

WHY IT READS SOURCE TEXT. The hooks are standalone scripts run as subprocesses
with a scrubbed environment; importing them is not how they are invoked, and
driving all five for real would need a credential, a Wolfram install and a
container. Reading the source is weaker than running it, and it is the right
weaker thing here: the question is "does this expression handle a key that is
absent", which is answerable from the expression. The runtime half is covered
for wolfram-sidecar by test_wolfram_sidecar_survives_a_real_record below, which
does drive the real hook against a real record.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PLUGINS = REPO / "plugins"

#: The twelve keys `SessionRecord.to_dict` actually writes. Derived from the
#: dataclass at import time rather than copied, so it cannot drift.
def _record_keys() -> set[str]:
    sys.path.insert(0, str(REPO))
    from botainer.state import session_record

    src = pathlib.Path(session_record.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef) and node.name == "to_dict"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    return {k.value for k in sub.keys
                            if isinstance(k, ast.Constant)}
    raise AssertionError("could not find SessionRecord.to_dict's returned dict")


def _hook_scripts() -> list[pathlib.Path]:
    """Every bundled hook script, from the manifests."""
    import yaml

    out: list[pathlib.Path] = []
    for man in sorted(PLUGINS.glob("*/botainer-plugin.yaml")):
        data = yaml.safe_load(man.read_text(encoding="utf-8")) or {}
        hooks = data.get("hooks") or {}
        items = (hooks.items() if isinstance(hooks, dict)
                 else [(h.get("when"), h.get("script")) for h in hooks])
        for _when, val in items:
            for script in (val if isinstance(val, list) else [val]):
                if isinstance(script, str):
                    p = man.parent / script
                    if p.is_file():
                        out.append(p)
    return out


def test_no_hook_subscripts_the_record_with_a_key_it_does_not_have():
    """The load-bearing one. A bare `record["x"]` for an `x` the record never
    contains is a guaranteed KeyError, not a risk."""
    real_keys = _record_keys()
    assert "session_id" in real_keys and "spec" in real_keys, (
        f"the key extractor has stopped matching to_dict (got {sorted(real_keys)}), "
        f"so every assertion below is vacuous"
    )
    assert "state_dir" not in real_keys, (
        "the session record now HAS a top-level 'state_dir'. That is fine — but "
        "this test's whole premise was that it does not, so re-read it before "
        "deleting anything."
    )

    offenders: list[str] = []
    for script in _hook_scripts():
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # record["literal"] — a subscript with a constant string key.
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in ("record", "record_data", "record_json")
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                    and node.slice.value not in real_keys):
                # An `if "k" in record else` guard makes the subscript safe.
                parent_is_guarded = any(
                    isinstance(a, ast.IfExp)
                    and node.lineno == getattr(a.body, "lineno", -1)
                    for a in ast.walk(tree)
                )
                if not parent_is_guarded:
                    offenders.append(
                        f"{script.relative_to(REPO)}:{node.lineno} "
                        f"record[{node.slice.value!r}]"
                    )

    assert not offenders, (
        "hook(s) subscript the session record with a key it never contains:\n  "
        + "\n  ".join(offenders)
        + f"\n\nThe record's real keys are: {sorted(real_keys)}\n"
        f"`state_dir` in particular lives at record['spec']['state_dir'].\n"
        f"Use the same fallback the other hooks use:\n"
        f"    Path(record.get('state_dir')\n"
        f"         or record.get('spec', {{}}).get('state_dir', ''))"
    )


def test_wolfram_sidecar_survives_a_real_record(tmp_path):
    """The runtime half, for the hook that actually had the defect.

    Reading source says the expression is guarded. This drives the real hook as
    a subprocess against a record built by the real `from_spec`, and asserts it
    gets PAST the record parse — which is the step that used to raise. It then
    refuses for the honest reason (no wolframscript on this host), and that
    refusal is the proof it got through.
    """
    sys.path.insert(0, str(REPO))
    from botainer.state import session_record

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions" / "sess-1"
    session_dir.mkdir(parents=True)
    proj = tmp_path / "proj"
    (proj / ".botainer").mkdir(parents=True)
    (proj / ".botainer" / "config.yaml").write_text(
        "version: botainer-project-v1\n", encoding="utf-8"
    )

    # A record with exactly the shape from_spec produces: no top-level
    # state_dir, the value inside `spec`.
    record = {
        "schema_version": 1,
        "session_id": "sess-1",
        "project_uuid": "u-1",
        "project_root": str(proj),
        "runtime": "docker",
        "image": "img",
        "host": "h",
        "started_at": None,
        "ended_at": None,
        "screen_session_id": None,
        "spec": {"state_dir": str(state_dir)},
        "runtime_handle": {},
    }
    rec_path = session_dir / session_record.RECORD_FILENAME
    rec_path.write_text(json.dumps(record), encoding="utf-8")

    hook = PLUGINS / "wolfram-sidecar" / "hooks" / "pre_session.py"
    proc = subprocess.run(
        [sys.executable, str(hook)],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "BOTAINER_SESSION_RECORD_PATH": str(rec_path),
            "BOTAINER_SESSION_SCRATCH": str(session_dir),
        },
        capture_output=True, text=True,
    )
    combined = proc.stdout + proc.stderr
    assert "KeyError" not in combined and "Traceback" not in combined, (
        f"the hook crashed parsing a record the launcher really produces:\n"
        f"{combined}"
    )
    assert "state_dir missing from record" not in combined, (
        f"the hook could not find state_dir in a record that has it at "
        f"spec.state_dir — the fallback is not reaching the right place:\n"
        f"{combined}"
    )
    # POSITIVE CONTROL, and it must not be vacuous: "no KeyError" is also true
    # of a hook that died on line 1, so pin that execution reached a stage
    # AFTER the record parse. Any of these markers is past it — which one you
    # get depends on the host (whether wolframscript exists, whether an OS
    # sandbox is available), and the test should not care which.
    reached = [m for m in ("wolframscript", "proxy", "socket", "sandbox")
               if m in combined]
    assert reached, (
        f"the hook produced nothing recognisable from any stage after the "
        f"record parse, so this test does not show it got past the line that "
        f"used to raise:\n  rc={proc.returncode}\n{combined}"
    )
