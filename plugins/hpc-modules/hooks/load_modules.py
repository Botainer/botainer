#!/usr/bin/env python3
"""hpc-modules host_pre_launch hook.

Runs on the host as the user (HIGH 4 trust). Reads the project's
`plugins.hpc-modules.modules:` list, sources the cluster's Lmod
bootstrap, executes `module load` for each requested module, captures
the resulting env, filters out Lmod internals + denylisted-but-not-in-
override vars, writes the filtered env to a file, and emits a
contribution JSON on stdout describing what it did.

Exit codes:
  0 — success (env-file written; contribution emitted on stdout)
  0 — no modules requested (no-op; emit empty contribution)
  1 — bootstrap not found / module subsystem unavailable
  2 — module load failed; modules missing with fail_on_missing=true
  3 — config corrupt or unreadable

Contract:
  env vars passed in by the launcher:
    BOTAINER_PROJECT_ROOT     — project root (for reading config.yaml)
    BOTAINER_SESSION_SCRATCH  — writable dir for env-file output
    BOTAINER_SESSION_ID       — session identifier
    BOTAINER_HOOK_WHEN        — "host_pre_launch"
    BOTAINER_PLUGIN           — "hpc-modules"

  stdout: a single line of JSON like:
    {"version":"plugin-contribution-v1",
     "kind":"host_pre_launch",
     "env_file":"/state/.../module-env.env",
     "module_list":[{"name":"python","version":"3.11"}, ...]}
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import yaml

# Filtered out always (Lmod internals; cluster-specific noise).
_LMOD_NOISE = re.compile(
    r"^("
    r"BASH_ENV|"
    r"LMOD_.*|"
    r"_LMFILES_.*|"
    r"MODULEPATH|MODULESHOME|"
    r"__LMOD_.*|"
    r"FPATH|"
    r"__EGL.*|"
    r"EBDEVEL.*|EBROOT.*|EBVERSION.*|"
    r"SHLVL|"
    r"_$"
    r")$"
)


# Default trusted module-env vars. The cluster admin curates modulefiles;
# these variables, when set by a module load, are allowed to override the
# launcher's env_var_denylist. User can extend via allowed_env_overrides.
_MODULE_TRUSTED_DEFAULTS = frozenset({
    "PATH",
    "LD_LIBRARY_PATH",
    "LIBRARY_PATH",
    "CPATH",
    "CMAKE_PREFIX_PATH",
    "PKG_CONFIG_PATH",
    "MANPATH",
    "INFOPATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "JULIA_LOAD_PATH",
    "JULIA_DEPOT_PATH",
    "R_LIBS",
    "R_LIBS_USER",
    "R_HOME",
    "CUDA_HOME",
    "CUDA_PATH",
    "CUDA_VISIBLE_DEVICES",
    "MPI_HOME",
    "OMPI_DIR",
    "JAVA_HOME",
})


# #160: location env vars whose `module load` additions name real software
# dirs the container may need bound. This MUST stay a SUPERSET of
# botainer.hpc.module_binds.PATH_VARS — the trusted derivation in composition
# only diffs THAT set, but it can only see vars this standalone hook actually
# emits in `software_root_env`. A drift test
# (tests/unit/test_hpc_modules.py::test_hook_pathvars_superset_of_module_binds)
# asserts the superset relation so a new PATH_VAR in module_binds can't silently
# become unreachable here.
# This hook is stdlib-only / standalone (it may run under a bare /usr/bin/env
# python3 on an HPC login node) so it CANNOT import module_binds — hence the
# mirrored literal + the drift test (the meta-pattern: a standalone HPC mirror
# must have a regression test pinning it to the canonical definition).
_SOFTWARE_ROOT_PATH_VARS = frozenset({
    "PATH",
    "LD_LIBRARY_PATH",
    "LIBRARY_PATH",
    "CPATH",
    "CMAKE_PREFIX_PATH",
    "PKG_CONFIG_PATH",
    "MANPATH",
    "INFOPATH",
    "CUDA_HOME",
    "CUDA_PATH",
    "MPI_HOME",
    "OMPI_DIR",
    "JAVA_HOME",
    "R_HOME",
})


# Botainer-managed env vars: never override these from module env.
# The agent-claude Dockerfile sets PIP_TARGET=/packages/pip,
# PYTHONPATH=/packages/pip, NODE_PATH=/packages/node_modules, etc. so
# package installs persist per-project. A module that sets these would
# break the agent's package routing — pip-installed libs would be
# invisible to `import`, etc.
#
# If the user wants a module's Python to see its OWN libraries, they
# should invoke that Python directly (PATH contains the module's bin
# dir; the python binary has its own built-in sys.path). PYTHONPATH
# stays botainer-managed.
_BOTAINER_MANAGED = frozenset({
    "PIP_TARGET",
    "PYTHONPATH",
    "NODE_PATH",
    "JULIA_DEPOT_PATH",
    "R_LIBS_USER",
    "CONDA_ENVS_PATH",
    "CARGO_HOME",
    "GOPATH",
    "CLAUDE_CONFIG_DIR",
    # Counterpart for codex; see composition._BOTAINER_MANAGED_ROUTES. A drift
    # test pins these two sets together.
    "CODEX_HOME",
})


_LMOD_BOOTSTRAP_CANDIDATES = [
    # Generic / package-manager installs (covers most clusters).
    "/etc/profile.d/lmod.sh",
    "/usr/share/lmod/lmod/init/bash",
    "/usr/local/lmod/lmod/init/bash",
    "/opt/lmod/lmod/init/bash",
    "/cm/local/apps/lmod/lmod/init/bash",
    # EasyBuild-style versioned installs.
    "/usr/share/lmod/8.7.32/init/bash",
    "/usr/share/lmod/8.7/init/bash",
    # Site-specific paths are intentionally NOT hardcoded here per
    # CLAUDE.md (no personal/site info in distributable plugins).
    # Sites with custom Lmod install paths should rely on env-var
    # fallback (LMOD_PKG) below — it covers any cluster where Lmod
    # is initialized at user login.
]


def _reject_unsafe_bootstrap(path: str) -> str:
    """Return a human reason string if `path` is unsafe to `source`, else "".

    AUDIT (CRITICAL C2 hardening): the bootstrap is executed via
    `source` on the node. Even though it now comes only from operator/host
    sources (never project config), validate it defensively: absolute,
    existing, a regular file (after following symlinks), and not writable by
    'other'. A world-writable bootstrap means any local user can replace it
    with code that runs as us — exactly the multi-tenant HPC threat model.
    """
    if not os.path.isabs(path):
        return f"{path!r} is not an absolute path"
    try:
        # stat() follows symlinks → checks the real target's mode/type.
        st = os.stat(path)
    except OSError as exc:
        return f"{path!r} cannot be stat'd ({exc.strerror})"
    if not stat.S_ISREG(st.st_mode):
        return f"{path!r} is not a regular file"
    if st.st_mode & stat.S_IWOTH:
        return (
            f"{path!r} is world-writable (mode {oct(st.st_mode & 0o777)}); any "
            f"local user could swap in code that runs as you"
        )
    return ""


def _detect_bootstrap() -> str:
    """Find Lmod's bash init script.

    Strategy:
    1. Check the literal-path candidates (covers most clusters via
       generic install locations only — no site-specific paths).
    2. Fall back to `$LMOD_PKG/init/bash` if Lmod has already been
       partially initialized in the environment (covers any cluster
       where the path is set at user login, including sites with
       custom Lmod install paths under e.g. /vast or /apps).
    3. Final: `command -v ml` followed by detection of the parent
       distribution.

    Returns "" if none found.
    """
    for p in _LMOD_BOOTSTRAP_CANDIDATES:
        if os.path.exists(p):
            return p
    lmod_pkg = os.environ.get("LMOD_PKG")
    if lmod_pkg:
        candidate = os.path.join(lmod_pkg, "init", "bash")
        if os.path.exists(candidate):
            return candidate
    lmod_cmd = os.environ.get("LMOD_CMD")
    if lmod_cmd:
        # Some clusters set LMOD_CMD=/path/to/libexec/lmod; derive
        # /path/to/init/bash.
        import re as _re
        m = _re.match(r"^(.*)/libexec/lmod$", lmod_cmd)
        if m:
            candidate = os.path.join(m.group(1), "init", "bash")
            if os.path.exists(candidate):
                return candidate
    return ""


def main() -> int:
    try:
        project_root = Path(os.environ["BOTAINER_PROJECT_ROOT"])
        session_scratch = Path(os.environ["BOTAINER_SESSION_SCRATCH"])
    except KeyError as exc:
        print(
            f"hpc-modules: missing required env {exc}",
            file=sys.stderr,
        )
        return 3

    cfg = _read_plugin_config(project_root)
    modules: list[str] = list(cfg.get("modules", []) or [])

    # Task #166: honor always_load + denylist from plugin config.
    # always_load: list of module names to prepend (cluster admins like
    # to insist on, e.g., compiler defaults). Dedup with `modules`.
    # denylist: list of module names to refuse even if explicitly listed.
    always_load: list[str] = list(cfg.get("always_load", []) or [])
    denylist: set[str] = set(cfg.get("denylist", []) or [])
    # Order: always_load FIRST so user `modules` can override versions
    # via load-after-load semantics (Lmod resolves last-wins on family).
    combined = list(always_load) + [m for m in modules if m not in always_load]
    if denylist:
        refused = [m for m in combined if any(m == d or m.startswith(d + "/") for d in denylist)]
        if refused:
            print(
                f"hpc-modules: refusing denylisted modules: {refused}",
                file=sys.stderr,
            )
            return 2
    modules = combined

    if not modules:
        # Nothing to do; emit empty contribution.
        _emit_contribution(env_file=None, module_list=[])
        return 0

    # AUDIT (CRITICAL C2): the bootstrap path is `source`d in a
    # bash shell on the node, so it MUST come from an operator/host source,
    # NEVER the git-shareable .botainer/config.yaml — a `bootstrap_script:`
    # there let a cloned repo run arbitrary code as the user. We resolve from
    # operator/host sources only:
    #   1. BOTAINER_LMOD_BOOTSTRAP — operator override env, allowlisted in
    #      botainer.plugins.hooks so it survives the hook env scrub. The
    #      operator can export it OR set `lmod.bootstrap` in the host-private
    #      cluster profile at ~/.botainer/cluster.yaml — the launcher
    #      auto-populates this env var from the active profile when the
    #      operator didn't export one explicitly (cluster-ease C2,
    #; see botainer/plugins/hooks.py::_scrubbed_host_env).
    #      Operator-exported env wins on conflict.
    #   2. _detect_bootstrap() — host login env (LMOD_PKG/LMOD_CMD + canonical
    #      install paths), which covers real Lmod clusters on the direct
    #      apptainer path (where this hook runs on the host before exec).
    # The project config is no longer consulted for this path.
    bootstrap = os.environ.get("BOTAINER_LMOD_BOOTSTRAP") or _detect_bootstrap()
    if not bootstrap:
        print(
            "hpc-modules: no Lmod bootstrap found. Auto-detection (LMOD_PKG / "
            "LMOD_CMD / canonical paths) found nothing; the operator can export "
            "BOTAINER_LMOD_BOOTSTRAP=<path>. It is sourced on the node and is "
            "deliberately NOT read from project config (that was an RCE vector).",
            file=sys.stderr,
        )
        return 1
    # Defense-in-depth before sourcing: the path must be an absolute, existing
    # regular file that is not world-writable (a writable bootstrap would let
    # any local user swap in code that runs as us). Fail loud — a misconfigured
    # operator path is abnormal and silently degrading would hide it.
    bad = _reject_unsafe_bootstrap(bootstrap)
    if bad:
        print(f"hpc-modules: refusing to source bootstrap: {bad}", file=sys.stderr)
        return 1

    purge_first = bool(cfg.get("purge_first", True))
    # #160 (adversarial-review B3): when the launcher runs this hook purely to
    # DISCOVER software-root bind dirs (BOTAINER_MODBINDS_DERIVE_ONLY=1, set by
    # hpc-launcher's plan-time derivation), a module that is invisible on THIS
    # node (e.g. login-only) must NOT abort — we load what resolves and bind
    # those roots. The genuine fail-on-missing belongs to the real module load
    # on the compute node, not to this best-effort discovery pass.
    fail_on_missing = (
        bool(cfg.get("fail_on_missing", True))
        and os.environ.get("BOTAINER_MODBINDS_DERIVE_ONLY") != "1"
    )
    extra_overrides = set(cfg.get("allowed_env_overrides", []) or [])

    # Build a bash script that:
    # 1. Sources the bootstrap
    # 2. Optionally purges
    # 3. Loads each module (failures noted on stderr, not fatal here)
    # 4. Lists what loaded (terse format on stderr — separated channel)
    # 5. Prints the resulting env on stdout
    quoted_bootstrap = shlex.quote(bootstrap)
    purge_line = (
        "module purge 2>/dev/null || true" if purge_first else ": # no purge"
    )
    # Task #286: was `echo 'MODULE_MISSING: {m}' >&2` with {m} interpolated
    # raw. A module name containing a single quote would close the string
    # and run arbitrary bash. Even though module names are normally
    # well-formed, defense-in-depth: shlex.quote(m) on BOTH the `module
    # load` arg AND the echo.
    load_lines = "\n".join(
        f"module load {shlex.quote(m)} 2>>/tmp/.modload-err-$$ || "
        f"echo MODULE_MISSING: {shlex.quote(m)} >&2"
        for m in modules
    )
    # Module-list raw output goes to a file under session_scratch so it
    # doesn't contaminate the env capture on stdout.
    module_list_raw = session_scratch / "module-list-raw.txt"
    quoted_ml = shlex.quote(str(module_list_raw))
    list_modules_line = f"module --terse list 2>{quoted_ml}"
    # NUL-separated env so multi-line values (notably bash-exported
    # function definitions in BASH_FUNC_*) don't break parsing. We use
    # `\x1f` (ASCII Unit Separator) as the on-the-wire delimiter since
    # \0 doesn't survive text-mode stdout. Unit Separator is reserved
    # for exactly this purpose and won't appear in any sane env value.
    capture_env_line = (
        "/usr/bin/env -0 | tr '\\0' '\\037'"  # 037 = octal 31 = US
    )
    # #160: capture env TWICE — the post-purge BASELINE (before any load) and
    # the post-load env — separated by an ASCII Record Separator (\x1e / octal
    # 036). The trusted derivation in composition diffs the two PATH-var
    # subsets so only dirs `module load` ADDED become bind candidates; a
    # pre-existing (possibly poisoned) PATH entry is in the baseline and is
    # never bound. The separator can't appear in env values (reserved control
    # char), mirroring the \x1f Unit-Separator choice above.
    record_sep_line = "printf '\\036'"

    bash_script = (
        f"set +e\n"
        f"source {quoted_bootstrap} || exit 9\n"
        f"{purge_line}\n"
        f"{capture_env_line}\n"     # BASELINE: post-purge, pre-load
        f"{record_sep_line}\n"
        f"{load_lines}\n"
        f"{list_modules_line}\n"
        f"{capture_env_line}\n"     # LOADED: post-load
    )

    try:
        proc = subprocess.run(
            ["/bin/bash", "-c", bash_script],
            capture_output=True,
            text=True,
            timeout=60,
            env={
                # Minimal env. /bin/bash needs PATH so it can find the
                # `module` shell function's helper binaries.
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", "/tmp"),
                # Cluster-specific: Lmod sometimes needs USER
                "USER": os.environ.get("USER", ""),
            },
        )
    except subprocess.TimeoutExpired:
        print(
            "hpc-modules: bash module-load timed out after 60s "
            "(slow Lmod initialization? check `time module avail` interactively)",
            file=sys.stderr,
        )
        return 1
    if proc.returncode == 9:
        print(
            f"hpc-modules: bootstrap script failed to source: {bootstrap}",
            file=sys.stderr,
        )
        return 1
    if proc.returncode != 0:
        print(
            f"hpc-modules: bash exited {proc.returncode}: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    missing = re.findall(r"MODULE_MISSING:\s+(\S+)", proc.stderr)
    if missing and fail_on_missing:
        print(
            f"hpc-modules: refused; modules not found: {sorted(set(missing))}",
            file=sys.stderr,
        )
        return 2

    # #160: stdout is now TWO env blocks (post-purge baseline, post-load)
    # separated by the Record Separator (\x1e). Within each block, entries are
    # Unit-Separator (\x1f) delimited (so multi-line values like bash-exported
    # functions don't break splitting). A run without the second block (very
    # old bootstrap, or a crash mid-script) degrades safely: loaded_raw == "".
    blocks = proc.stdout.split("\x1e")
    baseline_raw = blocks[0] if len(blocks) >= 1 else ""
    loaded_raw = blocks[1] if len(blocks) >= 2 else ""
    baseline_all = _parse_env_block(baseline_raw)
    loaded_all = _parse_env_block(loaded_raw)

    # env-file: the FILTERED post-load env (Lmod noise / botainer-managed /
    # denylist removed) — unchanged behavior, now sourced from loaded_all.
    new_env: dict[str, str] = {}
    for k, v in loaded_all.items():
        if _LMOD_NOISE.match(k):
            continue
        # Skip bash-exported function vars (BASH_FUNC_<name>%%) — bash's
        # internal scheme for forwarding shell functions; not needed in the
        # container shell.
        if k.startswith("BASH_FUNC_"):
            continue
        if k in _BOTAINER_MANAGED:
            # Don't let module env clobber botainer-managed routes.
            continue
        if not _is_allowed(k, extra_overrides):
            continue
        # #160 (adversarial-review T2): drop values carrying a line-boundary
        # char — written into the line-oriented env-file they would forge an
        # extra KEY=VALUE entry. (The launcher's per-line validator catches
        # harmful forges regardless; this keeps the on-disk file well-formed.)
        if any(c in v for c in ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85")):
            continue
        new_env[k] = v

    # #160: RAW PATH-var subsets for the trusted bind derivation in
    # composition. Emitted unfiltered (the derivation does its own
    # baseline-diff + allowlist + ceiling check); restricted to the location
    # vars so we never ship the whole env (and never credentials) over stdout.
    baseline_pv = {
        k: v for k, v in baseline_all.items() if k in _SOFTWARE_ROOT_PATH_VARS
    }
    loaded_pv = {
        k: v for k, v in loaded_all.items() if k in _SOFTWARE_ROOT_PATH_VARS
    }

    # Capture which modules ended up loaded (terse output went to its
    # own file under session_scratch).
    try:
        ml_raw = module_list_raw.read_text(encoding="utf-8", errors="replace")
    except OSError:
        ml_raw = ""
    loaded_list = _parse_module_list(ml_raw, modules)

    # Write env-file under the session scratch (already 0700-mode dir).
    env_file = session_scratch / "module-env.env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in sorted(new_env.items())) + "\n",
        encoding="utf-8",
    )
    # Tighten file mode.
    try:
        env_file.chmod(0o600)
    except OSError:
        pass

    _emit_contribution(
        env_file=str(env_file),
        module_list=loaded_list,
        software_root_env={"baseline": baseline_pv, "loaded": loaded_pv},
    )
    return 0


def _read_plugin_config(project_root: Path) -> dict:
    cfg_path = project_root / ".botainer" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        print(f"hpc-modules: config.yaml parse error: {exc}", file=sys.stderr)
        return {}
    plugins = data.get("plugins") or {}
    return (plugins.get("hpc-modules") or {})  # type: ignore[no-any-return]


def _is_allowed(var: str, extra_overrides: set[str]) -> bool:
    """Is `var` allowed through the launcher's env_var_denylist?

    Allowed if either:
    - In our built-in MODULE_TRUSTED_DEFAULTS (the canonical module-env vars), or
    - In the project's allowed_env_overrides (user-extended), or
    - Not on the launcher's denylist (the common case for harmless vars
      like JULIA_VERSION, GCC_VERSION, etc.)
    """
    if var in _MODULE_TRUSTED_DEFAULTS or var in extra_overrides:
        return True
    # If we don't know about the launcher's denylist, allow it; the
    # adapter will catch any final-line refusals.
    denylist = set(
        v for v in os.environ.get("BOTAINER_ENV_DENYLIST", "").split(",") if v
    )
    return var not in denylist


def _parse_module_list(
    stderr: str, requested: list[str]
) -> list[dict[str, str]]:
    """Parse `module --terse list` output into [{name, version}] entries.

    Lmod's --terse output is one module per line in `name/version` form.
    Cluster-specific noise (warnings, headers like `Currently Loaded
    Modulefiles:`) is skipped.
    """
    out: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith(":"):  # header line
            continue
        if line.startswith(("Lmod", "MODULE_MISSING")):
            continue
        # Heuristic: looks like "name/version" or just "name"
        if "/" in line:
            name, _, version = line.partition("/")
            entry = {"name": name, "version": version}
        else:
            entry = {"name": line, "version": ""}
        if entry["name"] not in seen_names:
            seen_names.add(entry["name"])
            out.append(entry)
    # If we couldn't parse anything but requested some, emit the request
    # as a best-effort module list for audit.
    if not out:
        for m in requested:
            if "/" in m:
                name, _, version = m.partition("/")
                out.append({"name": name, "version": version})
            else:
                out.append({"name": m, "version": ""})
    return out


def _parse_env_block(raw: str) -> dict[str, str]:
    """Parse one US-(\\x1f)-separated env block into a dict.

    Entries are `KEY=VALUE`; the leading `\\n` bash may emit before an entry is
    stripped. Non-`=` fragments (e.g. stray `module list` stdout) are skipped.
    No filtering here — callers filter (env-file) or restrict to path vars
    (software_root_env) as appropriate.
    """
    out: dict[str, str] = {}
    for entry in raw.split("\x1f"):
        if not entry:
            continue
        entry = entry.lstrip("\n")
        if "=" not in entry:
            continue
        k, _, v = entry.partition("=")
        if not k:
            continue
        out[k] = v
    return out


def _emit_contribution(
    *,
    env_file: str | None,
    module_list: list[dict[str, str]],
    software_root_env: dict[str, dict[str, str]] | None = None,
) -> None:
    payload = {
        "version": "plugin-contribution-v1",
        "kind": "host_pre_launch",
        "env_file": env_file,
        "module_list": module_list,
    }
    # #160: only include software_root_env when we actually loaded modules and
    # captured both env subsets. Composition treats absence as "no software
    # roots to derive" (feature degrades to env-only, the pre-#160 behavior).
    if software_root_env is not None:
        payload["software_root_env"] = software_root_env
    print(json.dumps(payload))


if __name__ == "__main__":
    sys.exit(main())
