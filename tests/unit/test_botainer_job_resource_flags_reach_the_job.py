"""`botainer-job submit --cpus/--mem/...` must reach the request.

The parser can be exercised without Slurm or a dispatcher:

    $ botainer-job submit big --cpus 8 --mem 64G python train.py
        cpus=None  mem=None
        command=['--cpus', '8', '--mem', '64G', 'python', 'train.py']

Every resource flag the in-container AGENT_HINTS teach was swallowed into the
command argv, so the job ran with profile defaults and nothing said so. Adding
`--` did not help: REMAINDER kept the `--` in the command too.

WHY: `command` was `nargs=argparse.REMAINDER`. REMAINDER starts collecting at
the first token after the preceding positional and takes options with it, so
`--cpus` was never offered to the parser at all.

These tests load and run the helper parser directly. A failed import must
fail the test rather than skip the only path that exercises the behavior.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SRC = (pathlib.Path(__file__).resolve().parents[2]
        / "plugins" / "hpc-launcher" / "agent_helper" / "botainer-job")


def _load():
    """Load the extensionless helper as a module.

    NO try/except-and-skip. If this cannot load, every test below is
    meaningless and must say so loudly rather than report green.
    """
    assert _SRC.is_file(), f"botainer-job not found at {_SRC}"
    spec = importlib.util.spec_from_loader("botainer_job_under_test", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(_SRC)
    exec(compile(_SRC.read_text(), str(_SRC), "exec"), mod.__dict__)
    return mod


def _parse(argv):
    """Run the REAL main() and capture what the parser handed cmd_submit."""
    mod = _load()
    captured = {}

    def _spy(args):
        captured.update(vars(args))
        return 0

    mod.cmd_submit = _spy
    mod.main(argv)
    assert captured, "cmd_submit was never reached; the parse failed silently"
    return captured


def test_resource_flags_are_parsed_not_swallowed() -> None:
    """THE DEFECT. These landed in `command` and the job used profile defaults."""
    ns = _parse(["submit", "big", "--cpus", "8", "--mem", "64G",
                 "python", "train.py"])

    assert ns["cpus"] == 8, ns
    assert ns["mem"] == "64G", ns


def test_the_command_survives_intact_alongside_them() -> None:
    """Parsing the flags must not eat the command, which is the other half."""
    ns = _parse(["submit", "big", "--cpus", "8", "--mem", "64G",
                 "python", "train.py"])

    assert ns["command"] == ["python", "train.py"], ns


def test_the_documented_dash_dash_form_works() -> None:
    """The help text has always offered `--`; REMAINDER kept the `--` itself."""
    ns = _parse(["submit", "big", "--gpus", "2", "--", "python", "train.py"])

    assert ns["gpus"] == 2, ns
    assert ns["command"] == ["python", "train.py"], ns


def test_after_dash_dash_the_users_own_flags_stay_the_users() -> None:
    """`--` means "stop parsing" — a `--cpus` past it belongs to the command.

    Without this the fix would have traded one swallowing bug for its mirror.
    """
    ns = _parse(["submit", "big", "--", "python", "train.py", "--cpus", "99"])

    assert ns["cpus"] is None, ns
    assert ns["command"] == ["python", "train.py", "--cpus", "99"], ns


def test_a_plain_command_with_no_flags_still_works() -> None:
    ns = _parse(["submit", "big", "python", "train.py"])
    assert ns["command"] == ["python", "train.py"], ns


def test_a_store_true_flag_is_parsed_too() -> None:
    """`--hot` is the flag whose loss is hardest to notice: the job just queues."""
    ns = _parse(["submit", "big", "--hot", "--", "python", "t.py"])
    assert ns["hot"] is True, ns
    assert ns["command"] == ["python", "t.py"], ns


def test_the_parsed_flags_reach_the_REQUEST_not_just_the_namespace(
        tmp_path, monkeypatch) -> None:
    """The namespace is not the artefact — the JSON the dispatcher reads is.

    Driven through the real `cmd_submit` writing to a real inbox, because a
    correctly-parsed value that never reaches the request would still leave the
    job running on profile defaults, which is the defect.
    """
    monkeypatch.setenv("BOTAINER_JOBS_IN", str(tmp_path / "in"))
    monkeypatch.setenv("BOTAINER_JOBS_OUT", str(tmp_path / "out"))
    mod = _load()          # re-load so the env is read at import time
    mod.main(["submit", "big", "--cpus", "8", "--mem", "64G",
              "--", "python", "train.py"])

    import json
    written = list((tmp_path / "in").glob("*.json"))
    assert len(written) == 1, f"expected one request, got {written}"
    req = json.loads(written[0].read_text())
    assert req["resources"] == {"cpus": 8, "memory": "64G"}, req
    assert req["command"] == ["python", "train.py"], req
