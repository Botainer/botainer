"""Shared in-container PREPEND trampoline (Step B; internal design note DN-005, #216).

Both the docker and apptainer adapters share this trampoline so the path-list
PREPEND semantics are identical across runtimes (HPC parity rule, CLAUDE.md).

What it does:

    For each PATH_LIST_VARS name (PATH, LD_LIBRARY_PATH, …), if the
    `_BOTAINER_PREPEND_<NAME>` helper env var is set in the container, PREPEND
    its value (colon-separated) onto the existing `<NAME>`, preserving the
    container's own entries (notably /opt/conda/bin where the agent binary
    lives). Unset the helper, then exec the original entrypoint argv.

Why a shell trampoline (vs `botainer-in-container` apply step):

    Avoids requiring botainer to be installed inside every agent image
    (which would couple the agent image to the launcher's wheel and break
    the layered design). Pure bash; works in any Debian-based image where
    `/bin/bash` is present (true for both agent-claude.def and
    agent-codex.def).

Security notes (matched by hostile tests):

    Values arrive via apptainer/docker `--env _BOTAINER_PREPEND_<NAME>=<val>`,
    which do NOT shell-interpret the value (one argv element each). Inside
    the trampoline, all expansions are fully quoted (`"$var"`), so bash
    treats values as LITERAL STRINGS — no word-split, no glob, no
    command-substitution evaluation. A module value containing
    `$(rm -rf /)` is propagated to PATH as the literal string
    `$(rm -rf /)`, not evaluated. The composition layer also validates
    the values via `_validate_host_env_text` before they reach the
    trampoline (refuses execution-injection vars, credentials, non-
    identifier keys, and newline-forged values).
"""

from __future__ import annotations


def _inner_prepend_trampoline_script() -> str:
    """Return the literal bash script that performs the in-container PREPEND.

    Single source of truth — docker + apptainer adapters both pass this
    via `bash -c '<script>'` so the semantics can't drift. Kept as a
    compile-time constant string with no interpolation (no values from
    config/spec are spliced in; all dynamic data flows through env-var
    space).
    """
    # The list is the SAME as botainer.hpc.module_binds.PATH_LIST_VARS
    # (the sbatch-flow inner PREPEND uses that frozenset directly). The two
    # paths must agree — adding a var here without adding it there (or vice
    # versa) silently asymmetrizes the two runtimes' PREPEND behavior.
    # tests/unit/test_inner_prepend.py::test_trampoline_matches_sbatch_path_list_vars
    # pins this lockstep.
    return (
        'for v in PATH LD_LIBRARY_PATH LIBRARY_PATH CPATH '
        'CMAKE_PREFIX_PATH PKG_CONFIG_PATH MANPATH INFOPATH; do '
        'p="_BOTAINER_PREPEND_$v"; '
        'pv="${!p-}"; '
        'if [ -n "$pv" ]; then '
        'cur="${!v-}"; '
        'if [ -n "$cur" ]; then '
        'export "$v=$pv:$cur"; '
        'else '
        'export "$v=$pv"; '
        'fi; '
        'fi; '
        'unset "$p"; '
        'done; '
        'exec "$@"'
    )
