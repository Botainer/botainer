"""`setup --allow-tier` is hidden, and cannot brick an install if used anyway.

Found by audit sweep C,, by running it. `--allow-tier` was a
documented three-value option, TWO of whose values destroyed a fresh install:

    $ botainer setup --allow-tier third-party
    ... raw Python traceback ...
    RuntimeError: refused: site policy plugins.allowed_tiers=[] excludes
                  first-party; bundled plugin 'agent-claude' not installed.

setup WROTE the value into policy.yaml, and only THEN intersected it with the
site default ["first-party"] to get the empty set. So nothing installed, the bad
value was persisted, and every later plain `botainer setup` crashed identically.
The escape was --force, which nothing mentioned; the message blamed "site
policy" while the offending value sat in the USER policy setup had just written.

TWO SEPARATE DEFECTS, so two separate guards here:

1. THE OPTION SHOULD NOT BE OFFERED. There are no third-party plugins. A control
   for something that does not exist can only hurt the person who finds it.
   Suppression beats repair — building the tier knob properly is work for the
   day a non-first-party plugin is real. (The user's call, and the right one:
   "there's no third party anything, so this whole thing should be suppressed".)

2. HIDDEN IS NOT REMOVED. Anyone reading the source, or an old script, can still
   pass it. A hidden footgun that still fires is not suppressed — so the value
   is now refused BEFORE anything is written. That ordering is the whole fix:
   validate-then-write cannot strand you, write-then-validate did.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _run(args, env_extra=None):
    import os
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    env.update(env_extra or {})
    # `sys.exit(main())`, exactly as the console-script entry point does.
    # botainer's main() CATCHES click's SystemExit and RETURNS the code so its
    # atexit hooks run; calling main() and discarding the return value silently
    # turns every refusal into exit 0. Two earlier versions of this helper got
    # that wrong and would have made this whole file assert the wrong thing.
    code = ("import sys; sys.argv=['botainer']+%r\n"
            "from botainer.cli.main import main\n"
            "sys.exit(main())\n") % (args,)
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=120)


def test_the_option_is_not_offered_in_help() -> None:
    """A control with no reachable meaning must not be advertised."""
    out = _run(["setup", "--help"])
    assert "--allow-tier" not in out.stdout, (
        "--allow-tier is back in `setup --help`. There are still no "
        "third-party plugins; if that changed, un-hide it AND fix the "
        "intersect-before-write ordering at the same time.")


def test_a_tier_set_without_first_party_refuses_and_writes_NOTHING(
        tmp_path) -> None:
    """THE regression guard, and the property that matters is the SECOND
    assertion: no state dir, no policy.yaml, nothing to un-stick."""
    root = tmp_path / "state-root"
    out = _run(["setup", "--allow-tier", "third-party"],
               {"MY_BOTAINER": str(root)})

    assert out.returncode == 2, (
        f"expected a clean refusal (exit 2), got {out.returncode}. "
        f"stderr:\n{out.stderr[-800:]}")
    assert "Traceback" not in out.stderr, (
        "a raw Python traceback is what the user saw before this fix")
    assert not root.exists(), (
        f"{root} was created despite the refusal — if a policy.yaml lands "
        f"there holding the bad value, every later `botainer setup` fails the "
        f"same way and only --force escapes. That was the bug.")


def test_the_refusal_explains_the_consequence_and_the_way_out(
        tmp_path) -> None:
    """The old message blamed 'site policy' — sending the user to their cluster
    admin for a problem botainer created from their own flag."""
    out = _run(["setup", "--allow-tier", "third-party"],
               {"MY_BOTAINER": str(tmp_path / "r")})
    err = out.stderr
    assert "nothing has been written" in err.lower(), (
        "must say the install is untouched, or the user assumes it is broken")
    assert "first-party" in err, "must name the tier that has to be included"
    assert "site policy" not in err.lower(), (
        "do not blame site policy for a value the user passed on the CLI")
