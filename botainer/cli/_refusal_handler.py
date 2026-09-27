"""Shared Refused-exception handling for CLI subcommands.

Per host-smoke observation: dry-run/inspect/access dumped full
Python tracebacks when composition refused, while `start` handled them
cleanly. This module centralizes the handling so every command exits with
a typed error code + a single readable line.

Use as a decorator:

    @click.command(...)
    @handle_refusals
    def my_command():
        composition.compose_session(...)
"""
from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from typing import Any

import click

from botainer.core import identity
from botainer.core.refusal import Refused

# AUDIT (LOW): exit codes now follow DN-028 §9 instead of exiting
# 4 for every refusal (which clashed with both §9 and the exit-2 already used by
# click usage errors + config/nudge refusals). 2 = refused (capability/policy/
# config/mount/env/identity); 3 = runtime/adapter cannot enforce; 5 = plugin
# failure. (1 = usage error is click's; 4 = self-test failure is emitted by the
# `botainer selftest` runner — botainer/preflight/runner.py — NOT by this
# refusal decorator, which never returns 4.)
_EXIT_REFUSED = 2
_EXIT_RUNTIME = 3
_EXIT_PLUGIN = 5


def _exit_code_for(category) -> int:
    name = category.value
    if name == "runtime-cannot-enforce":
        return _EXIT_RUNTIME
    if name.startswith(("plugin-", "sidecar-", "host-helper-", "cross-plugin-")):
        return _EXIT_PLUGIN
    return _EXIT_REFUSED


def handle_refusals(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: convert Refused / IdentityChangeRefused into clean error
    output + a DN-028 §9 exit code (no Python traceback)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except identity.IdentityChangeRefused as exc:
            click.secho(f"refused: {exc}", fg="red", err=True)
            sys.exit(_EXIT_REFUSED)  # an identity change is a refusal (2)
        except Refused as exc:
            # Task #279: exc.args may be empty if Refused was constructed as
            # Refused(category) without a message. Fall back to '' so the
            # decorator doesn't IndexError and silently re-raise.
            detail = exc.args[0] if exc.args else ""
            line = f"refused: {exc.category.value}" + (f": {detail}" if detail else "")
            click.secho(line, fg="red", err=True)
            sys.exit(_exit_code_for(exc.category))

    return wrapper


def render_refusal(exc: Exception) -> int:
    """Print a refusal the same way the decorator does; return its exit code.

    THE DECORATOR IS A RULE AND THIS IS THE BACKUP FOR IT. Every command is
    supposed to carry `@handle_refusals`, and two did not: `botainer list` and
    `botainer image list` printed a 43- and a 47-line Python traceback where the
    `state-root-not-allowed` refusal already existed, with `doctor` on the same
    state root printing it cleanly. Measured with
    `MY_BOTAINER=/project/<grp>/<user>/botainer`.

    Decorating those two would fix those two. `main()` calling this means a
    command that forgets the decorator still cannot show a traceback — the
    structural half, with the decorator kept because it converts the refusal
    NEAR the call and is what most commands already carry.

    Returns the exit code rather than calling sys.exit so the caller stays in
    charge of its own exit path.
    """
    if isinstance(exc, identity.IdentityChangeRefused):
        click.secho(f"refused: {exc}", fg="red", err=True)
        return _EXIT_REFUSED
    if isinstance(exc, Refused):
        detail = exc.args[0] if exc.args else ""
        line = f"refused: {exc.category.value}" + (f": {detail}" if detail else "")
        click.secho(line, fg="red", err=True)
        return _exit_code_for(exc.category)
    raise exc
