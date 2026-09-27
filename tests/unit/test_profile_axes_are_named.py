"""#174/#160: "profile" is three unrelated things, so no flag may just say it.

botainer uses the word for three axes that have nothing to do with each other:

    auth profile     a credential slot            which of my logins?
    cluster profile  declared facts about a host  what machine am I on?
    job profile      a resource-request shape     how big a job?

They arrived from three features at three times and nobody reconciled the
vocabulary, so `--profile` selected DIFFERENT OBJECTS depending on the command
— the auth axis on `auth login`, the cluster axis on `hpc setup`. `start` had
already been forced to invent `--auth-profile` because the plain name was
taken, which is the workaround #160 names.

WHY A TEST ON NAMES. Normally keying a test to an identifier is the defect
#199 warned about — it teaches renaming-to-evade rather than fixing anything.
Here the name IS the behaviour under test: the ambiguity is not a proxy for a
bug, it is the bug. A `--profile` that reads correctly on one command and
misleads on another cannot be caught by exercising either one.

Both spellings keep working. The point is which one `--help` shows and which
one ends up in somebody's script.
"""
from __future__ import annotations

import pytest

from botainer.cli.main import cli

# (command path, the axis it selects, the canonical flag it must offer)
AXIS_FLAGS = [
    pytest.param(("auth", "login"), "auth", "--auth-profile", id="auth-login"),
    pytest.param(("start",), "auth", "--auth-profile", id="start"),
    pytest.param(("inspect",), "auth", "--auth-profile", id="inspect"),
    pytest.param(("dry-run",), "auth", "--auth-profile", id="dry-run"),
    pytest.param(("hpc", "setup"), "cluster", "--cluster", id="hpc-setup"),
]


def _command(path):
    cmd = cli
    for part in path:
        cmd = cmd.commands[part]
    return cmd


def _profile_opts(cmd):
    """Every option on cmd whose spellings mention 'profile' or 'cluster'."""
    out = []
    for param in cmd.params:
        opts = list(getattr(param, "opts", []) or [])
        if any("profile" in o or o == "--cluster" for o in opts):
            out.append(opts)
    return out


@pytest.mark.parametrize("path,axis,canonical", AXIS_FLAGS)
def test_the_command_offers_the_axis_qualified_name(path, axis, canonical) -> None:
    cmd = _command(path)
    spellings = [o for opts in _profile_opts(cmd) for o in opts]
    assert canonical in spellings, (
        f"`{' '.join(path)}` selects the {axis} axis but does not offer "
        f"{canonical}; it has {spellings}")


@pytest.mark.parametrize("path,axis,canonical", AXIS_FLAGS)
def test_a_bare_profile_is_never_the_PRIMARY_spelling(path, axis, canonical) -> None:
    """Click shows the FIRST declared spelling first in `--help`, and that is
    the one a reader copies. `--profile` may remain as an alias — removing it
    breaks scripts for no safety gain — but it must not be what the command
    presents itself as."""
    cmd = _command(path)
    for opts in _profile_opts(cmd):
        longs = [o for o in opts if o.startswith("--")]
        assert longs[0] != "--profile", (
            f"`{' '.join(path)}` leads with a bare --profile, which does not "
            f"say which of the three axes it means (this one is {axis}). "
            f"Declare {canonical} first; keep --profile after it.")


def test_the_two_commands_that_collide_still_disagree_on_purpose() -> None:
    """The residual ambiguity, pinned so it stays deliberate rather than
    forgotten: `--profile` really does mean different things on these two, and
    both keep accepting it. If someone ever removes one alias, this fails and
    the removal has to be a decision instead of a drift."""
    login = [o for opts in _profile_opts(_command(("auth", "login"))) for o in opts]
    setup = [o for opts in _profile_opts(_command(("hpc", "setup"))) for o in opts]
    assert "--profile" in login and "--profile" in setup
    assert "--auth-profile" in login and "--auth-profile" not in setup
    assert "--cluster" in setup and "--cluster" not in login


def test_hpc_setup_and_jobs_explain_no_longer_share_an_identifier() -> None:
    """The collision was not only in the flags. `hpc.py` used the identifier
    `profile_name` for the cluster axis in `setup` and the job axis in
    `jobs_explain` — one name, two unrelated things, in one file."""
    import inspect as _i

    from botainer.cli import hpc
    # BOTH the click parameter name and the Python signature. Mutation showed
    # the signature alone is not enough: changing only the decorator
    # (`@click.argument("profile_name")`) leaves the function parameter
    # qualified, so a signature-only check stays green on a command that no
    # longer wires up. Click's name is the one that has to match.
    for cmd, want in ((hpc.setup, "cluster_profile_name"),
                      (hpc.jobs_explain, "job_profile_name")):
        click_names = {pm.name for pm in cmd.params}
        sig_names = set(_i.signature(cmd.callback).parameters)
        assert want in click_names, f"{cmd.name}: click has {click_names}"
        assert want in sig_names, f"{cmd.name}: signature has {sig_names}"
        assert "profile_name" not in click_names | sig_names, (
            f"{cmd.name} went back to the unqualified name")
        assert click_names >= sig_names - {"kwargs"}, (
            f"{cmd.name}: click params {click_names} do not cover the "
            f"callback's {sig_names} — the command would not wire up")


# ── What the axis flags SAY they do ───────────────────────────────────────────
#
# Naming the axis was half of it. The other half is that an override selects the
# agent's WHOLE config directory — transcripts, todos, settings, MCP entries —
# and not just the credential, because the axis is a path component. Five
# shipped help strings described `--auth-profile`; two said "credential +
# history directory", one said only "Reads credentials from …", and two said
# nothing. Which fact a user got depended on which command they typed.

_AXIS_FLAG_COMMANDS = [
    ("start", "--auth-profile"),
    ("start", "--auth-mode"),
    ("inspect", "--auth-profile"),
    ("dry-run", "--auth-profile"),
    ("hpc.submit", "--auth-profile"),
]


def _help_for(path: str, flag: str) -> str:
    cmd = cli
    for part in path.split("."):
        cmd = cmd.get_command(None, part)  # type: ignore[union-attr]
        assert cmd is not None, f"no such command: {path}"
    for p in cmd.params:
        if flag in getattr(p, "opts", []):
            return p.help or ""
    raise AssertionError(f"{path} has no {flag}")


@pytest.mark.parametrize("path,flag", _AXIS_FLAG_COMMANDS)
def test_an_axis_override_says_it_moves_the_HISTORY_not_just_the_credential(
        path: str, flag: str) -> None:
    text = _help_for(path, flag).lower()
    assert "transcript" in text or "history" in text, (
        f"`botainer {path.replace('.', ' ')} {flag}` describes only the "
        f"credential. The mode and profile are path components of the agent's "
        f"whole config directory, so an override also changes which "
        f"transcripts, todos, settings and MCP entries the session sees:\n"
        f"  {_help_for(path, flag)}")
    assert "nothing is carried" in text, (
        f"`{flag}` on {path} does not say that NOTHING is carried into the "
        f"directory it selects — the fact that surprises people, and the one "
        f"that makes the next run look like lost history")


@pytest.mark.parametrize("path,flag", _AXIS_FLAG_COMMANDS)
def test_an_axis_override_points_at_the_persistent_way_to_do_it(
        path: str, flag: str) -> None:
    text = _help_for(path, flag)
    assert "auth use" in text or "config.yaml" in text, (
        f"`{flag}` on {path} tells the user it is one-shot without naming the "
        f"persistent alternative; a one-shot flag is where someone lands when "
        f"they wanted to switch for good:\n  {text}")


def test_the_STANDALONE_hpc_launcher_says_it_too() -> None:
    """The launcher argparse cannot import this text — it must still carry it.

    `plugins/hpc-launcher/host_helper/submit.py` runs on login nodes where
    `botainer` may not be importable, which is why it is a standalone script
    with its own argparse. That is a real constraint and also exactly how a
    surface drifts: the four botainer-side commands share one sentence and this
    one had none at all.

    It runs the REAL parser and reads the REAL `--help`, per flag. My first
    version sliced 1200 characters of source starting at `--auth-mode` and
    looked for "history" anywhere in it — so reverting `--auth-mode` to its old
    one-liner still passed, because `--auth-profile` three lines below carried
    the word. A block assertion is not a per-flag assertion.
    """
    import contextlib
    import importlib.util
    import io
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    helper = root / "plugins" / "hpc-launcher" / "host_helper"
    spec = importlib.util.spec_from_file_location(
        "_hpc_submit_under_test", helper / "submit.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(helper))
    try:
        spec.loader.exec_module(module)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
            module.parse_args(["--help"])
    finally:
        sys.path.remove(str(helper))

    rendered = " ".join(buf.getvalue().split())   # argparse wraps; unwrap
    assert "--auth-mode" in rendered, "the launcher no longer offers --auth-mode"
    # Skip the usage line: it repeats every flag with no help beside it, and
    # slicing from the first match landed there.
    opts_at = rendered.find("options:")
    assert opts_at != -1, f"no options section in argparse --help:\n{rendered[:200]}"
    rendered = rendered[opts_at:]
    for flag in ("--auth-mode", "--auth-profile"):
        # argparse lists each option then its help until the next option.
        at = rendered.index(flag + " ")
        # This flag's help ONLY: argparse prints `--flag VALUE help…` and the
        # next option starts at the next `  --`. Slicing a fixed window is what
        # let the neighbouring flag's help satisfy this assertion.
        end = rendered.find(" --", at + len(flag) + 1)
        segment = rendered[at:end if end != -1 else len(rendered)]
        assert "history" in segment, (
            f"the launcher's {flag} help does not say the override selects the "
            f"agent's history directory:\n  {segment[:200]}")


def test_no_shipped_remedy_tells_you_to_run_a_flag_that_is_not_THERE() -> None:
    """A remedy naming a bare flag is the dead-flag class, fourth instance.

    `auth login`'s proxy warning said "you must run the session in --shared or
    --isolated mode". Those are flags of `auth login` ITSELF, not of `botainer
    start`, which takes `--auth-mode`. The flags exist, so "is this flag real?"
    answers yes and misses it — exactly how the proxy hook's `--mode=shared`
    survived for months.

    WHAT THIS PINS, and what it does not: the shipped STRING, extracted from the
    source, not the code path — reaching that branch needs a proxy-configured
    project and a real login attempt. Weaker than driving the caller, and said
    so rather than dressed up. The value is that the sentence names commands
    that exist; the branch itself is covered by the login tests.
    """
    import ast
    import pathlib

    from botainer.cli.main import cli

    src = pathlib.Path(__file__).resolve().parents[2] / "botainer/cli/auth.py"
    tree = ast.parse(src.read_text())
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value
        if "run the session in" not in text and "run the session with" not in text:
            continue
        # The sentence is about SESSIONS, so any flag it names must be one
        # `botainer start` actually has.
        start = cli.get_command(None, "start")
        opts = {o for p in start.params
                for o in getattr(p, "opts", []) + getattr(p, "secondary_opts", [])}
        for word in text.replace("\n", " ").split():
            flag = word.strip("`,.()").split("=")[0]
            if flag.startswith("--") and flag not in opts:
                offenders.append(f"auth.py:{node.lineno}: {flag!r} in a "
                                 f"session remedy, but `start` has no such option")
    assert not offenders, (
        "a shipped remedy tells the user to run a session with a flag `start` "
        "does not have:\n  " + "\n  ".join(offenders))


def test_the_MODE_axis_only_moves_the_history_for_broker() -> None:
    """The axes are not symmetric, and the help text said they were.

    Mode and profile are both path components, so I wrote one sentence covering
    both: an override "runs against that directory as it stands (often empty),
    nothing is carried into it". Measured against the resolver, that is FALSE
    for the commonest mode override — `isolated` and `shared` resolve to the
    SAME `profiles/<profile>` directory, and only `broker` has its own. So
    `start --auth-mode shared` from an isolated project keeps every transcript,
    and the help was telling the user it would not.

    An over-warning, not an under-warning, which is why nothing failed: the user
    is told to expect a loss that does not happen, and learns the warnings are
    approximate. Found by the checkpoint-11 loop tzar against `history_dir_for`.

    Pins the FACT, not the wording, so the sentence stays checkable if the
    layout changes.
    """
    from pathlib import Path

    from botainer.auth_modes import ONE_SHOT_AXIS_HELP
    from botainer.cli._history_prompt import history_dir_for

    root = Path("/nonexistent-root")
    same = {m: history_dir_for(root, "UID", "agent-claude", m, "default")
            for m in ("isolated", "shared", "broker")}
    assert same["isolated"] == same["shared"], (
        f"isolated and shared no longer share a history directory "
        f"({same['isolated']} vs {same['shared']}) — the help text now says "
        f"they do, and must be corrected with this test")
    assert same["broker"] != same["isolated"], (
        "broker no longer has its own directory; the help says it does")

    # And the help must not flatten the two axes back together.
    text = ONE_SHOT_AXIS_HELP.lower()
    assert "isolated and shared share" in text, (
        f"the one-shot help no longer says isolated and shared share a "
        f"directory, so it is back to warning about a loss that does not "
        f"happen on the commonest override:\n  {ONE_SHOT_AXIS_HELP}")
