"""Hook invocation.

Plugin hooks (pre_session, post_session, host_pre_launch) run on the
*host* as the user (codex HIGH 4: full host trust). They're invoked
via subprocess with a timeout and structured env. Their stdout becomes
a PluginContribution JSON (parsed; envelope-checked elsewhere).

#215: pre_request / post_request hooks were declared as
valid `when:` values in the manifest schema but had no dispatcher in
composition. They depend on the credential-proxy infrastructure that
§B1 of internal design note DN-041 defers to v0.2. Removed; will be
re-added with a real dispatcher when proxy lands.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from botainer.core import exec_bit
from botainer.core.refusal import RefusalCategory, Refused

# Allowlist of host env vars the hook may see (task #284 / codex P0-1).
# Everything else is stripped. Add a var only when you verify it carries
# no credential / no path-confusion risk.
_HOOK_ENV_ALLOWLIST = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TMPDIR",
    "TEMP",
    "TMP",
    # Per-user runtime dir (/run/user/<uid>, 0700) — the preferred short, private
    # location for the credential-broker's unix socket when the state-root path
    # overflows sun_path (agent-claude-broker start_broker._resolve_socket_path).
    # It is a path already owned 0700 by the user; no credential surface.
    "XDG_RUNTIME_DIR",
    "SHELL",
    "TERM",
    "TZ",
    "PWD",
    # SLURM env propagated to host_pre_launch / pre_session hooks (nudge
    # prepare_socket needs SLURM_TMPDIR for node-local socket placement).
    # These are HPC-only and contain no credential surface — they're job
    # ids + node names + paths already known to the scheduler.
    "SLURM_TMPDIR",
    "SLURM_JOB_ID",
    "SLURM_JOBID",
    "SLURM_STEP_ID",
    "SLURMD_NODENAME",
    "SLURM_NTASKS",
    "SLURM_NNODES",
    "SLURM_CPUS_PER_TASK",
    # Lmod bootstrap discovery (hpc-modules)
    "LMOD_PKG",
    "LMOD_CMD",
    # Operator override for the Lmod bootstrap path (hpc-modules). Sourced on
    # the node, so it is deliberately operator/host-controlled (set in the host
    # env or by the launcher from the cluster profile) and NEVER read from the
    # git-shareable project config — see AUDIT C2.
    "BOTAINER_LMOD_BOOTSTRAP",
})


def _scrubbed_host_env() -> dict[str, str]:
    """Return host env limited to _HOOK_ENV_ALLOWLIST (task #284).

    Cluster-ease C2: when the operator has NOT set
    `BOTAINER_LMOD_BOOTSTRAP` explicitly but the active cluster profile
    (`~/.botainer/cluster.yaml`, host-private — NOT the git-shareable
    project config) carries `lmod.bootstrap`, propagate that value into the
    hook env. The env var still wins on explicit set (operator override
    pattern); the YAML field becomes load-bearing instead of decorative
    when the user sets it.

    Trust note: AUDIT C2 forbids reading the bootstrap path
    from the git-shareable project config. `~/.botainer/cluster.yaml` is
    user-owned + host-private (default 0700 state dir), so it satisfies
    the operator/host-source rule. We do NOT consult `.botainer/cluster.yaml`
    (project-local), only the user-state-rooted profile.
    """
    env = {k: v for k, v in os.environ.items() if k in _HOOK_ENV_ALLOWLIST}
    if not env.get("BOTAINER_LMOD_BOOTSTRAP"):
        try:
            from botainer.state.cluster_profile import active_profile
            prof = active_profile()
        except Exception:
            prof = None
        if prof and prof.lmod_bootstrap:
            env["BOTAINER_LMOD_BOOTSTRAP"] = prof.lmod_bootstrap
    return env


@dataclass(frozen=True)
class HookResult:
    rc: int
    stdout: str
    stderr: str
    parsed_contribution: dict | None = None


def refuse_agent_writable_hook(script_path: Path,
                               agent_writable_roots: Sequence[Path],
                               *, plugin_name: str) -> None:
    """Refuse to execute a hook that lives where the CAGED AGENT can write.

    THE INVARIANT: code the HOST executes must not live anywhere the agent can
    write. This is structural, not a filter — it does not try to detect a
    malicious script, it makes the host refuse to run any script from a location
    the untrusted side controls.

    Why it exists (security audit, S3): in an EDITABLE install,
    `list_installed()` overlays the clone's `plugins/` and source WINS
    (`plugins/lifecycle.py:52-106`). When the project root IS the botainer clone
    — the documented develop-botainer-inside-botainer workflow — that directory
    is bound **rw** at /workspace. A prompt-injected agent could overwrite
    `plugins/<x>/hooks/pre_session.py`, and the launcher would execute it ON THE
    HOST, as the user, at the next `botainer start`. Same class as the
    `.git/hooks` escape the git plugin exists to prevent, with no guard at all.

    Ordinary user projects are unaffected: `~/.botainer/plugins/` is not inside
    any bind source. So this refusal only ever fires on the dev-in-clone setup —
    which is precisely the case that was exploitable.
    """
    try:
        script_real = script_path.resolve()
    except OSError:                      # unresolvable → fail closed
        raise Refused(
            RefusalCategory.PLUGIN_HOOK_FAILED,
            f"hook script path for {plugin_name!r} cannot be resolved: "
            f"{script_path}",
        ) from None
    for root in agent_writable_roots:
        try:
            root_real = root.resolve()
        except OSError:
            continue
        if script_real == root_real or root_real in script_real.parents:
            raise Refused(
                RefusalCategory.PLUGIN_HOOK_FAILED,
                f"REFUSING to run hook {plugin_name!r} from {script_real}: it "
                f"is inside {root_real}, which is bound WRITABLE into the "
                f"container. The caged agent could rewrite this script and the "
                f"host would execute it as you.\n"
                f"  This happens when botainer is EDITABLE-installed from the "
                f"same clone you are running as the project (the source "
                f"plugins/ tree overrides the installed one).\n"
                f"  Fix: run the session from a different project directory, or "
                f"install the plugins non-editably "
                f"(`botainer plugin add <path>`) so hooks load from "
                f"~/.botainer/plugins/.",
            )


def _hook_stderr_excerpt(stderr: str, *, limit: int = 1200) -> str:
    """Excerpt hook stderr so the ACTIONABLE line survives truncation.

    UX audit (M6): the old `stderr[:300]` kept the FIRST 300 chars.
    Python puts the real error on the LAST line of a traceback, so the only line
    worth reading was reliably the part discarded. Lead with the last non-empty
    line, then include the tail for context.

 — THAT RULE WAS RIGHT FOR ONE INPUT SHAPE AND WRONG FOR THE
    OTHER, and the other is the common one. A hook that fails deliberately
    writes like a human: problem first, fix second, alternative last. Promoting
    its LAST line made the headline of the default path's first failure a
    dangling subordinate clause:

        refused: plugin-hook-failed: hook agent-claude-shared.pre_session
        exited 2: (Or switch this project back to isolated mode: …)

    while "no shared credential at …" and "Run: botainer auth login …" sat
    below a separator. Preserve the primary diagnosis in the headline.

    Only a TRACEBACK buries its error at the end, so only a traceback gets the
    last-line treatment. Everything else leads with its first line, which is
    where an author who meant to be understood put the point.

    This is a shape FILTER, not a property, and it says so: it
    backs up the absence of any structured error channel between a hook and the
    launcher. A hook returns bytes on stderr, so the launcher can only guess at
    their shape. The structural fix is a hook protocol with a declared headline
    field; until then, guess from the one marker that is unambiguous.
    """
    text = (stderr or "").strip()
    if not text:
        return "(no stderr)"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    _is_traceback = "Traceback (most recent call last)" in text
    last = (lines[-1] if _is_traceback else lines[0]).strip() if lines else ""
    if len(text) <= limit:
        body = text
    else:
        body = "…" + text[-limit:]
    if last and not body.strip().endswith(last):
        return f"{last}\n  --- hook stderr (tail) ---\n{body}"
    return f"{last}\n  --- hook stderr (tail) ---\n{body}" if len(lines) > 1 else last


def surface_hook_stderr(result, plugin_name: str, hook_when: str) -> None:
    """Show the user what a SUCCESSFUL hook said on stderr.

    #138: `run_hook` has always captured stderr into `HookResult.stderr`, and
    no caller has ever read it when the hook exited 0. So the shared-auth leak
    canary, the anti-poisoning refusal, and the "back-filled shared credential"
    notice — which announces a HOST-WIDE credential change — have never once
    reached a user. Every warning any of this machinery emits was written to a
    void.

    Not noise, and that is load-bearing: these hooks are silent on the happy
    path. Verified by running agent-claude-shared's pre_session against a
    healthy state root — zero bytes on stderr. So anything appearing here is by
    construction an event a hook author chose to report. A channel that fires
    on every run trains everyone to ignore it, and then the one real warning is
    missed too; if a hook ever starts chattering on success, fix the hook rather
    than muting this.

    Failure-path stderr is already surfaced by the Refused path; this is only
    the rc==0 case.
    """
    import sys

    text = (getattr(result, "stderr", "") or "").strip()
    if not text:
        return
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # Hooks already prefix their own messages with `[plugin-name]`; don't
        # double it up into an unreadable stutter.
        if line.startswith("["):
            sys.stderr.write(f"{line}\n")
        else:
            sys.stderr.write(f"[{plugin_name} {hook_when}] {line}\n")


def _runs_under_our_interpreter(script_path) -> bool:
    """Will this hook be handed to `sys.executable` rather than exec'd?

    ONE PREDICATE, ASKED TWICE, AND IT USED TO BE TWO COPIES. The executability
    exemption and the interpreter choice are the same question — a script we run
    as `[sys.executable, path]` is never exec'd, so its mode is irrelevant — and
    both sites spelled it `str(path).endswith(".py")` independently. A refuting
    review pointed out that the test meant to pin the pairing did not: swapping
    ONE of them to `Path(path).suffix == ".py"` left all 14 tests green, because
    the two spellings agree on every filename the tests used.

    They do not agree on every filename. `Path(".py").suffix` is `""` — a
    leading dot makes it a hidden file with no suffix — while `".py".endswith(
    ".py")` is True. With the two copies, that file would have been exempted from
    the bit check and then exec'd, or refused and then handed to python,
    depending on which copy drifted. One function makes the pair impossible to
    break rather than merely tested.

    `endswith` is the kept spelling because it is what `cli/auth.py` and
    `plugins/manifest.py` use for the same dispatch; changing three sites to
    match a fourth is not a bug fix.
    """
    return str(script_path).endswith(".py")


def run_hook(
    *,
    plugin_name: str,
    hook_when: str,
    script_path: Path,
    env: dict[str, str],
    agent_writable_roots: Sequence[Path],
    timeout_seconds: int = 30,
) -> HookResult:
    """Invoke a hook script. Returns parsed contribution if stdout is JSON.

    Host env is scrubbed to _HOOK_ENV_ALLOWLIST (task #284). Credentials
    like ANTHROPIC_API_KEY, AWS_*, GITHUB_TOKEN, SSH_AUTH_SOCK do NOT
    reach the hook subprocess.

    `agent_writable_roots` is REQUIRED (not defaulted) so a future call site
    cannot silently opt out of the containment check below — the whole point is
    that it is not forgettable. Pass the session's writable bind sources; pass
    an empty sequence only where no container exists yet.
    """
    refuse_agent_writable_hook(script_path, agent_writable_roots,
                               plugin_name=plugin_name)
    if not script_path.exists():
        raise Refused(
            RefusalCategory.PLUGIN_HOOK_FAILED,
            f"hook script not found: {script_path}",
        )
    # ONLY FOR NON-.py HOOKS, and that exemption is load-bearing: a few lines
    # below, a `.py` hook is run as `[sys.executable, script]` — through
    # botainer's own interpreter, never through its shebang — so it does NOT
    # need the execute bit and a 0o644 `.py` hook works fine. Requiring it here
    # REFUSED a shape that previously ran; caught by the loop tzar after I
    # shipped it. `cli/auth.py` already carried this exemption (`if not is_py
    # and not ...`) and `plugins/manifest.py` dispatches the same way; this is
    # the sibling that had drifted.
    #
    # is_executable, NOT os.access, for the hooks that DO need it: os.access
    # returns True for a 0o644 file on a filesystem that does not enforce the
    # bit, so this refusal never fired for the case it exists to catch — a hook
    # committed without +x (task #118). See botainer/core/exec_bit.py.
    if not _runs_under_our_interpreter(script_path) and not exec_bit.is_executable(script_path):
        raise Refused(
            RefusalCategory.PLUGIN_HOOK_FAILED,
            f"hook script not executable: {script_path} — "
            f"{exec_bit.why_not_executable(script_path)}",
        )
    full_env = {
        **_scrubbed_host_env(),  # was **os.environ — task #284 / codex P0-1
        **env,
        "BOTAINER_HOOK_WHEN": hook_when,
        "BOTAINER_PLUGIN": plugin_name,
    }
    # Run PYTHON hooks with botainer's OWN interpreter (sys.executable), NOT via
    # their `#!/usr/bin/env python3` shebang. On HPC login nodes the system
    # `python3` is a different interpreter that lacks botainer's deps — the
    # bundled hooks `import yaml` (pyyaml is a botainer dependency), so the
    # shebang path can fail with ModuleNotFoundError. sys.executable is the
    # venv/interpreter running
    # botainer, which has every dep botainer ships. Non-.py hooks (none bundled
    # today) still run via their own shebang.
    if _runs_under_our_interpreter(script_path):
        _cmd = [sys.executable, str(script_path)]
    else:
        _cmd = [str(script_path)]
    try:
        proc = subprocess.run(
            _cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=full_env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Refused(
            RefusalCategory.PLUGIN_HOOK_FAILED,
            f"hook {plugin_name}.{hook_when} timed out after {timeout_seconds}s",
        ) from None
    # Task #218: was 'if stdout.startswith("{")' → any preamble (banner,
    # debug print, BOM) made the hook contribution silently invisible.
    # Now: scan from the last JSON-looking line backward. If we find a
    # JSON object, parse it. If stdout starts with non-whitespace
    # non-{ AND contains json-like content later, log a WARNING to
    # stderr so the user knows we tried but couldn't parse.
    parsed: dict | None = None
    out = proc.stdout
    # Find the last line that starts with '{' on its first non-space col.
    candidate_json: str | None = None
    for ln in reversed(out.splitlines()):
        if ln.lstrip().startswith("{"):
            candidate_json = ln
            break
    # If no per-line {, also try the whole stdout in case it's pretty-printed.
    blob_candidates: list[str] = []
    if candidate_json:
        blob_candidates.append(candidate_json)
    stripped = out.strip()
    if stripped.startswith("{") and stripped not in blob_candidates:
        blob_candidates.append(stripped)
    for blob in blob_candidates:
        try:
            parsed = json.loads(blob)
            break
        except json.JSONDecodeError:
            continue
    if parsed is None and out.strip() and "{" in out:
        # AUDIT (MEDIUM): this previously WARNED and DROPPED the
        # contribution (fail-open), then proceeded with rc=0 — so a contribution
        # corrupted by a BOM/stray-print/truncation silently vanished and the
        # session launched WITHOUT it. That is unsafe: the dropped contribution
        # may be a security control (e.g. the git guarded-mode ro overlay, or a
        # credential bind) the user believes is active. DN-027 §5 mandates
        # refusal on non-empty-but-unparseable contribution JSON. Fail CLOSED.
        raise Refused(
            RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
            f"hook {plugin_name}.{hook_when} produced stdout that looks like a "
            f"contribution (contains '{{') but did not parse as JSON. Refusing "
            f"rather than silently dropping it — a dropped contribution may be a "
            f"security control. The hook must print ONLY its contribution JSON "
            f"on stdout (diagnostics → stderr). stdout was:\n{out[:300]}",
        )
    if proc.returncode != 0:
        raise Refused(
            RefusalCategory.PLUGIN_HOOK_FAILED,
            # UX audit (M6): this was `proc.stderr[:300]`, i.e. the
            # FIRST 300 chars. A Python traceback puts the actual error on its
            # LAST line, so the one actionable line was ALWAYS the part cut off —
            # users saw "Traceback (most recent call last): File ... line ..." and
            # nothing else. Hooks are the primary failure surface (credentials,
            # git overlay, broker, hpc-modules), so this is the message that
            # matters most. Keep the TAIL, and lead with the last non-empty line.
            f"hook {plugin_name}.{hook_when} exited {proc.returncode}: "
            f"{_hook_stderr_excerpt(proc.stderr)}",
        )
    return HookResult(
        rc=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, parsed_contribution=parsed
    )
