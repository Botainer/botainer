"""botainer — a launcher for AI coding agents in inspectable containers.

Five trust principals (USER, HOST, CONTAINER, PLUGIN-SIDECAR, EXTERNAL)
connected by four edge classes. Every grant of power from a higher-trust
principal to a lower one is a named, structured capability. The launcher
composes config -> effective policy -> SessionSpec -> adapter argv -> runtime
readback verification.
"""

from __future__ import annotations

from pathlib import Path

# THE VERSION IS DERIVED, NEVER DECLARED HERE.
#
# This line used to read `__version__ = "0.1.0a1"`, and `botainer --version`
# printed it, while pyproject.toml said 0.1.0a4. Three copies existed; a test
# pinned two of them; the third was the only one a USER ever sees. Found by
# installing the built wheel into a clean venv and running the CLI — no amount
# of reading finds this, because each copy is locally correct and only the
# disagreement between files is wrong.
#
# TWO SOURCES, IN THIS ORDER, AND THE ORDER IS THE WHOLE POINT.
#
#   1. An ADJACENT pyproject.toml. Only a source tree has one next to the
#      package, so this arm is the developer's. It is tried FIRST because an
#      editable install's metadata is frozen at install time: `pip install -e`
#      records the version it saw, and editing pyproject afterwards does not
#      update it. Metadata-first would therefore print a stale number to the
#      one person who just changed it. (Observed: the dev container reported
#      0.1.0a0 from an install predating two version bumps.)
#
#   2. The INSTALLED PACKAGE METADATA. A wheel in site-packages has no
#      pyproject beside it, so this is the user's arm — and pip wrote that
#      metadata from pyproject at build time, so it cannot disagree.
#
# Either way there is one number with one origin. That makes the drift
# unrepresentable rather than merely detectable.
def _derive_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            # Confirm it is OURS. A pyproject that happens to sit beside an
            # installed package belongs to something else, and taking its
            # version would be worse than having none.
            if data.get("project", {}).get("name") == "botainer":
                return str(data["project"]["version"])
        except Exception:
            pass  # fall through to metadata rather than fail an import

    try:
        from importlib.metadata import version as _installed_version

        return _installed_version("botainer")
    except Exception:
        # Neither source available: on sys.path directly, no install, no
        # adjacent pyproject. Say so rather than invent a number — a wrong
        # version is worse than an absent one.
        return "unknown (not installed)"


__version__ = _derive_version()
__all__ = ["__version__"]
