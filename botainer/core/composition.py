"""Top-level orchestrator: turn (config + policy + plugins + identity) into a SessionSpec.

`compose_session(project_root, runtime_choice, identity_accept)` is the single
entry point. It:
1. Resolves project identity (UUID + state-dir + clone/fork prompt).
2. Loads project config (Main/.botainer/config.yaml).
3. Loads user policy.yaml.
4. Loads installed-and-enabled plugins.
5. For each enabled plugin, runs its `pre_session` hook (if any) to produce a
   PluginContribution. Validates against envelope.
6. Builds the base MountPlan + applies plugin contributions + user extras.
7. Validates the MountPlan against the effective policy.
8. Composes the final SessionSpec.
"""

from __future__ import annotations

import re
import shutil
import sys
import uuid as _uuid
from pathlib import Path

from botainer.adapters.apptainer import ApptainerAdapter
from botainer.adapters.base import Adapter, RuntimeHandle
from botainer.adapters.docker import DockerAdapter
from botainer.adapters.mock import MockAdapter
from botainer.core import config as config_module
from botainer.core import identity
from botainer.core import policy as policy_module
from botainer.core.refusal import RefusalCategory, Refused
from botainer.core.spec import (
    AgentRendering,
    Bind,
    BindMode,
    CapabilityGrant,
    EnvSpec,
    HookSpec,
    KernelCapsSpec,
    NetworkMode,
    NetworkSpec,
    PortForward,
    Provenance,
    ResourceSpec,
    SessionSpec,
    SidecarSpec,
    validate_image_reference,
)
from botainer.inspect import access as access_renderer
from botainer.inspect import agent_hints as agent_hints_renderer
from botainer.mount_plan.composition import add_extras, add_plugin_contribution, core_base
from botainer.mount_plan.validation import validate as validate_mount_plan
from botainer.plugins.lifecycle import list_installed
from botainer.state import dir as state_dir
from botainer.state import session_record

_LAYER_ORDER = {"outer": 0, "default": 1, "inner": 2}


def sort_entrypoint_wraps(
    wraps_by_plugin: dict[str, tuple[str, tuple[str, ...]]],
) -> tuple[tuple[str, ...], ...]:
    """Layer-sort entrypoint wraps by (layer, plugin-name).

    The resulting tuple ordering is outermost-first: wraps[0] runs
    first (e.g. a host-side wrapper), wraps[-1] is innermost (e.g.
    agent-claude's prompt-prime wrap, closest to the agent binary).
    Adapters render `wraps[0] args ... wraps[-1] args entrypoint`.

    Implementation-review MEDIUM 13 + §A19 regression: alphabetical
    alone broke when an outer-layer wrap and an inner-layer wrap were
    both enabled — the outer wrap got buried inside the inner wrap
    and was not the actual session leader.
    """
    return tuple(
        wrap_cmd
        for _layer, _name, wrap_cmd in sorted(
            (
                (_LAYER_ORDER[layer], name, cmd)
                for name, (layer, cmd) in wraps_by_plugin.items()
            ),
            key=lambda triple: (triple[0], triple[1]),
        )
    )


# The real container runtimes (a plugin declares a subset of these). `mock` is
# the test runtime and is never declared by a plugin, so it is exempt from the
# per-plugin runtimes enforcement (AUDIT).
_REAL_RUNTIMES = frozenset({"docker", "apptainer"})


def compose_session(
    project_root: Path,
    *,
    runtime_choice: str,
    identity_accept: bool,
    fork: bool = False,
    auth_mode_override: str | None = None,
    auth_profile_override: str | None = None,
    agent_override: str | None = None,
    image_override: str | None = None,
) -> SessionSpec:
    """Build a SessionSpec for `botainer start`/`inspect`/`dry-run`.

    auth_mode_override: per AUTH-PRODUCT-PLAN.md, one-shot in-memory
    swap of which auth-family plugin variant is enabled for THIS
    composition. None = use whatever the config says. Otherwise:
    isolated/shared/proxy — we substitute the matching plugin in each
    installed family's place. NO DISK MUTATION. (Insecure-defaults
    H3: the previous atexit-restore design was unreliable; SIGKILL
    or OOM would leak a config edit.)

    image_override: highest-precedence image selection (above cfg.image),
    used by the HPC compose-at-submit path (compose_agent_exec_for_hpc)
    to force the resolved .sif the hpc-launcher picked. Validated through
    the SAME chokepoint as cfg.image (validate_image_reference +, for
    apptainer, an existing-file/dir check) — NOT model_copy'd in, which
    would skip the field validators.
    """
    project_root = Path(project_root).resolve()
    # 1. Identity.
    uid, project_state = identity.resolve_identity(
        project_root,
        identity_accept=identity_accept,
        fork=fork,
    )
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    proj_paths = state_dir.ensure_project_dirs(
        paths, uid, project_name=project_root.name
    )

    # 2. Config + policy.
    cfg = config_module.load_config(project_root)
    user_policy = policy_module.load_user_policy()
    site_policy = policy_module.load_site_policy()
    effective_policy = policy_module.intersect(site_policy, user_policy)

    # Apply in-memory auth-mode override (insecure-defaults H3).
    # Agent first: it decides WHICH family is in play, and the auth-mode swap
    # below operates on whatever ends up enabled.
    if agent_override is not None:
        cfg = _apply_agent_override_in_memory(cfg, agent_override)
    if auth_mode_override is not None:
        cfg = _apply_auth_mode_override_in_memory(cfg, auth_mode_override)
    if auth_profile_override is not None:
        # Same discipline as the auth-mode override: IN MEMORY ONLY. Writing it
        # to disk and restoring at exit cannot survive SIGKILL/OOM, and would
        # leave the project silently switched to another account's profile.
        from botainer.core.spec import validate_profile_name
        cfg = cfg.model_copy(
            update={"profile": validate_profile_name(auth_profile_override)})

    # Exactly one agent plugin is activated, whatever plugins_enabled lists.
    # Runs AFTER both overrides so it filters against the final agent choice.
    cfg = _activate_only_the_selected_agent(cfg)

    # 3. Runtime choice.
    runtime = _resolve_runtime(cfg.runtime, runtime_choice)

    # 4. Image resolution (Phase 0 of v0.1.0 plan):
    #    Resolution order:
    #      a. cfg.image (per-project override) — honored if set; warned via inspect.
    #      b. Agent plugin's recorded image in installed.lock.
    #      c. Agent plugin's manifest image.tag.
    #      d. None → refuse with CONFIG_MISSING.
    #
    # For apptainer runtime: the image must be an absolute path to a .sif
    # file. A bare docker tag like `botainer/agent-claude:0.1` would be
    # interpreted by apptainer as a path relative to CWD and the launch
    # would fail looking for `<project_root>/botainer/agent-claude:0.1`
    # (real-host Grace bug). Resolve to the .sif here.
    image = _resolve_session_image(cfg, runtime=runtime, image_override=image_override)

    # 5. Build AGENT_ACCESS.txt + AGENT_HINTS.md to a session scratch path.
    session_id = _new_session_id()
    session_scratch = proj_paths.sessions_dir / session_id
    session_scratch.mkdir(parents=True, exist_ok=True)
    aas_path = session_scratch / "AGENT_ACCESS.txt"
    hints_path = session_scratch / "AGENT_HINTS.md"

    # Empty null-bind anchor. Name must match `mount_plan/composition.py`
    # (the Bind constructed there references this exact path); a mismatch
    # makes Docker refuse with "bind source path does not exist" because
    # we'd create one name and ask Docker to mount the other.
    null_anchor = proj_paths.data_dir / "null-bind-anchor"
    null_anchor.mkdir(parents=True, exist_ok=True)
    # Task #200 + Grace host-test: the anchor is an EMPTY masking dir
    # (bound over /workspace/.botainer/ so the agent can't see the host's). Its
    # invariant is "empty" — but a previous session's writes to the masked
    # /workspace/.botainer/ (AGENT_ACCESS.txt, plugin state, …) land here and
    # PERSIST (data_dir is on $HOME). The old behavior REFUSED and told the user
    # to `rm -rf` an internal path they should never even know exists — a
    # terrible experience that blocks every normal user after their first crash.
    # botainer OWNS this dir, so RESET it to the required-empty invariant itself:
    # the leftovers are always ephemeral mask-writes (the real agent/plugin data
    # lives in data_dir/<name>/, a SIBLING of the anchor, never inside it). This
    # also NEUTRALIZES the original threat model (an attacker-dropped file is
    # deleted before launch instead of merely alarmed about). Fail closed only if
    # the reset itself fails.
    try:
        leftover = list(null_anchor.iterdir())
    except OSError:
        leftover = []
    for _p in leftover:
        try:
            if _p.is_dir() and not _p.is_symlink():
                shutil.rmtree(_p)
            else:
                _p.unlink()
        except OSError as exc:
            raise Refused(
                RefusalCategory.MOUNT_PATH_NULL_BIND_VIOLATED,
                f"could not reset botainer's internal masking dir at {null_anchor} "
                f"(leftover from a prior session): {exc}",
            ) from None
    if leftover:
        sys.stderr.write(
            f"[botainer] reset session masking dir ({len(leftover)} leftover "
            f"item(s) from a prior session cleared)\n"
        )

    # 6. Mount plan: core base + user extras + plugin contributions.
    plan = core_base(
        project_root,
        agent_access_source=aas_path,
        state_data_root=proj_paths.data_dir,
    )
    # Phase 2 additions: /packages (persistent package installs) + /scratch
    # (ephemeral). Env-var routing (PIP_TARGET, JULIA_DEPOT_PATH, etc.) is
    # baked into the agent image via Dockerfile.
    # Task #239: defend against state-tree symlink escape. If anyone
    # replaces .botainer/state/<uuid>/scratch (or packages) with a
    # symlink to /etc/shadow / some other host path, .resolve() would
    # silently return that target and we'd bind /etc into /scratch.
    # Verify both bind sources resolve under paths.state_dir (the
    # canonical per-user state root); refuse if they escape.
    _state_root_resolved = paths.state_dir.resolve()
    _pkg_resolved = proj_paths.packages_dir.resolve()
    _scratch_resolved = proj_paths.scratch_dir.resolve()
    # Writable HOME for the run-as uid (see ProjectPaths.home_dir): the container
    # runs as the host uid, which has no home in the image, so without this HOME=/
    # and every home-dependent tool (npm/npx, pip, Claude Code's runtime dir)
    # breaks or splatters into /tmp. Create it so the bind source exists (docker
    # on macOS virtiofs requires the mountpoint to pre-exist).
    proj_paths.home_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _home_resolved = proj_paths.home_dir.resolve()
    # SCRATCH MAY LEGITIMATELY LIVE OUTSIDE THE STATE ROOT — the others may not.
    #
    # state (credentials, uuid, plugins) and home must stay under the state root:
    # they are small, must persist, and an escape there is the umbrella-bind
    # class this guard exists for. Scratch is different in kind — bulk,
    # disposable, and on HPC it belongs on the cluster's scratch filesystem
    # precisely BECAUSE that filesystem is purged. Forcing it under the state
    # root made the product advise a two-tier layout it could not implement
    # (internal design note DN-008, and `hpc setup`'s own storage section).
    #
    # The permission is NOT open-ended. `scratch_root` reaches here only from
    # host-side trusted config resolved by the launcher; the agent has no path
    # to set it, and it is checked below against the SAME resolve-and-contain
    # discipline, just against its own declared root instead of the state root.
    #: packages and home became relocatable too, for the INODE half
    # of the problem — a cluster $HOME with a 500,000-file cap is exhausted by
    # `packages/` (conda/pip: byte-light, file-heavy) while `scratch`, the
    # byte-heavy one, was already elsewhere. Same discipline, same shape: each
    # source is contained by ITS OWN declared root.
    #
    # NOTE the failure mode this replaces. Every source used to be checked
    # against the state root, so the first component to move made the guard
    # refuse a perfectly legitimate bind — the dispatcher failed EVERY cycle
    # with [mount-source-denied] on the maintainer's cluster (d734529). Adding
    # a component here without its root does that again, silently, at launch.
    def _own_root(root):
        return root.resolve() if root is not None else _state_root_resolved

    _containment = [
        ("packages", _pkg_resolved, _own_root(proj_paths.packages_root)),
        ("home", _home_resolved, _own_root(proj_paths.home_root)),
        ("scratch", _scratch_resolved, _own_root(proj_paths.scratch_root)),
    ]
    for label, resolved, _root in _containment:
        try:
            resolved.relative_to(_root)
        except ValueError:
            raise Refused(
                RefusalCategory.MOUNT_SOURCE_DENIED,
                f"{label} bind source {resolved!s} resolves outside its "
                f"declared root {_root!s}; symlink-escape defense.",
            ) from None
    packages_bind = Bind(
        source=str(_pkg_resolved),
        target="/packages",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="per-project persistent package installs (rw)",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_EXTRA_BIND",
    )
    scratch_bind = Bind(
        source=str(_scratch_resolved),
        target="/scratch",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="per-project ephemeral scratch (rw; user may delete)",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_EXTRA_BIND",
    )
    # Writable HOME (see ProjectPaths.home_dir). Without it the container runs as
    # a uid with no home → HOME=/ → npm/npx/pip/Claude-Code all break or splatter
    # into /tmp. HOME is pointed here via the composed env below.
    home_bind = Bind(
        source=str(_home_resolved),
        target="/home/user",
        mode=BindMode.RW,
        provenance=Provenance.CORE,
        provenance_detail="per-project writable HOME (rw; caches live here)",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_EXTRA_BIND",
    )
    # AGENT_HINTS.md bind: launcher-generated env description, mounted ro at
    # /workspace/.botainer/AGENT_HINTS.md (namespaced; safe per sharp-edges F2).
    hints_bind = Bind(
        source=str(hints_path.resolve()),
        target="/workspace/.botainer/AGENT_HINTS.md",
        mode=BindMode.RO,
        provenance=Provenance.CORE,
        provenance_detail="agent-facing env hints; ro",
        agent_rendering=AgentRendering.SHOWN,
        self_test="SELFTEST_AGENT_ACCESS_RO",
        nested_under="/workspace/.botainer",
    )
    plan = plan.with_many([packages_bind, scratch_bind, home_bind, hints_bind])
    plan = add_extras(plan, cfg)

    # #54: job-dispatcher mailbox + the in-container `botainer-job` CLI. Wired
    # whenever the project declares `job_profiles` (jobs enabled). The agent gets
    # /jobs/in (RW, requests), /jobs/out (RO, results), and
    # /usr/local/bin/botainer-job (RO — bind-mounted from the installed
    # hpc-launcher plugin, so it's rebuild-independent). `run/` stays host-private
    # and unbound. The available profiles are host-written to out/profiles.json so
    # `botainer-job profiles` works (the agent can't read the masked config).
    # This is the in-session mailbox only; the dispatcher daemon that drains
    # it and calls sbatch is a separate host-side process (botainer/hpc/jobs.py).
    # Full invariants (INV-1/INV-2) and why run/ is host-private: DN-001.
    if cfg.job_profiles:
        from botainer.hpc import jobs as _jobs
        _mb = _jobs.ensure_mailbox(paths, uid)
        _job_binds = list(_jobs.mailbox_binds(_mb))
        _hpc = next((i for i in list_installed() if i.name == "hpc-launcher"), None)
        _botjob = (Path(_hpc.plugin_dir) / "agent_helper" / "botainer-job"
                   if _hpc is not None else None)
        if _botjob is not None and _botjob.exists():
            # BIND A PER-SESSION COPY, NOT THE PLUGIN FILE ITSELF.
            #
            # User,, mid-session: "codex also seems to say that
            # botainer-jobs isn't there, something about a failed mount. oh,
            # maybe the bind died when the version updated..." — exactly right.
            #
            # A single-FILE bind pins an INODE at mount time. Updating botainer
            # (git pull, `rsync` without --inplace, an editor writing
            # temp-then-rename) REPLACES that file, so the running container's
            # mount still refers to the old, now-unlinked inode. `botainer-job`
            # vanishes from a session that was working a minute earlier, and
            # nothing explains why.
            #
            # Copying it into the session's own scratch dir makes the failure
            # IMPOSSIBLE rather than merely diagnosable: the session owns the
            # file it runs, so updating the plugin tree cannot reach into a live
            # session. Same shape as the rename()-breaks-a-symlink finding
            # (EXTERNAL-FACTS EF-2) — replace-in-place destroys identity-pinned
            # references, so do not pin an identity you do not control.
            #
            # The copy also fixes the subtler half: before this, a session
            # started BEFORE an update would silently keep running the OLD
            # helper if the inode survived, so two concurrent sessions could
            # disagree about what `botainer-job` does. Now each session is
            # pinned to the version it launched with, deliberately.
            _botjob_session = session_scratch / "botainer-job"
            shutil.copy2(_botjob, _botjob_session)
            _botjob_session.chmod(0o755)
            _job_binds.append(Bind(
                source=str(_botjob_session.resolve()),
                target="/usr/local/bin/botainer-job",
                mode=BindMode.RO,
                provenance=Provenance.CORE,
                provenance_detail=(
                    "in-container job-dispatch CLI (ro; a per-session COPY, so "
                    "updating botainer cannot break a running session)"),
                agent_rendering=AgentRendering.SHOWN,
                self_test="SELFTEST_EXTRA_BIND",
            ))
        else:
            # LOUD diagnostic (readiness): job_profiles is set, but the
            # in-container `botainer-job` CLI can't be provided — so the agent gets
            # NO dispatch tool AND AGENT_HINTS omits the whole "HOW YOU RUN COMPUTE
            # JOBS" section, leaving it with no idea it can run jobs. This used to
            # be a SILENT skip (the config looked complete but jobs were dead). The
            # bind is sourced from the INSTALLED hpc-launcher plugin, so a stale/
            # missing install breaks jobs even when job_profiles is correct.
            import sys as _sys
            _why = ("the hpc-launcher plugin is not installed"
                    if _hpc is None else
                    f"the installed hpc-launcher plugin at {_hpc.plugin_dir} has no "
                    f"agent_helper/botainer-job (it is stale — predates job dispatch)")
            _sys.stderr.write(
                "\n╔═ botainer: JOB DISPATCH IS DISABLED ═══════════════════════\n"
                f"║ {len(cfg.job_profiles)} job_profiles are configured, but the in-container\n"
                "║ `botainer-job` command CANNOT be provided, so the agent will NOT\n"
                "║ be able to run jobs and its hints will omit the jobs section.\n"
                f"║ Cause: {_why}.\n"
                "║ Fix:   refresh the bundled plugins from source (overwrites the\n"
                "║        stale installed copy):\n"
                "║            botainer setup\n"
                "║        (after syncing the repo to this host).\n"
                "╚════════════════════════════════════════════════════════════\n\n")
        plan = plan.with_many(_job_binds)
        _jobs.write_profiles_manifest(_mb, cfg.job_profiles)
    else:
        # job_profiles parsed EMPTY. If the raw config actually has profiles in
        # the WRONG place (nested under `plugins:`, a `profiles:` key, or a
        # mis-indented `job_profiles:`), the user configured jobs but the launcher
        # sees none — and the whole jobs chain is silently off. Warn AT START,
        # pointing at the full diagnosis (readiness).
        try:
            import sys as _sys

            import yaml as _yaml

            from botainer.hpc.diagnose import _find_misplaced_profiles
            _raw = _yaml.safe_load(
                (project_root / ".botainer" / "config.yaml").read_text(
                    encoding="utf-8")) or {}
            _mis = ([m for m in _find_misplaced_profiles(_raw)
                     if "not found under any obvious key" not in m]
                    if isinstance(_raw, dict) else [])
            if _mis:
                _sys.stderr.write(
                    "\n[botainer] job_profiles look MISPLACED — job dispatch is "
                    "OFF for this session:\n")
                for _m in _mis:
                    _sys.stderr.write(f"    - {_m}\n")
                _sys.stderr.write(
                    "    Run `botainer hpc jobs-doctor` for the full diagnosis.\n\n")
        except Exception:
            pass

    config_enabled = set(cfg.plugins_enabled)
    installed_names = {inst.name for inst in list_installed()}
    # Only plugins both in config AND installed are "effective" — the inspect
    # output should reflect this (sharp-edges F6 + simplification feedback).
    effective_enabled = config_enabled & installed_names
    enabled = effective_enabled
    # UX: warn loudly when the user enabled a plugin in config but it
    # isn't installed. Previously this was silent — user edits config to
    # add `nudge`, doesn't `botainer setup`, then `botainer start` runs
    # without nudge with no explanation. Now they get a clear hint.
    missing_plugins = config_enabled - installed_names
    if missing_plugins:
        import sys as _sys
        for name in sorted(missing_plugins):
            _sys.stderr.write(
                f"[botainer] plugin {name!r} is enabled in config but not "
                f"installed. Run `botainer setup` to install bundled plugins "
                f"or `botainer plugin add <path>` for third-party.\n"
            )

    # AUTH-PRODUCT-PLAN.md §1: enforce single-mode-per-family.
    # Sharp-edges F8: use auth_family as primary check (more robust
    # than mutually_exclusive_with which depends on plugin authors
    # to declare reciprocally). If a manifest fails to load, REFUSE
    # rather than skip (was silent-skip; an attacker could ship a
    # malformed manifest to bypass the exclusion).
    from botainer.plugins.manifest import load_manifest
    family_to_enabled: dict[str, list[str]] = {}
    for inst in list_installed():
        if inst.name not in enabled:
            continue
        try:
            man = load_manifest(inst.plugin_dir)
        except Refused as exc:
            raise Refused(
                RefusalCategory.PLUGIN_MANIFEST_INVALID,
                f"enabled plugin {inst.name!r} has invalid manifest: {exc}. "
                f"Cannot proceed without manifest to check exclusion rules.",
            ) from exc
        if man.auth_family:
            family_to_enabled.setdefault(man.auth_family, []).append(inst.name)
        # Also honor mutually_exclusive_with (catches non-auth-family
        # exclusions or third-party-declared conflicts).
        for sibling in man.mutually_exclusive_with:
            if sibling in enabled and sibling != inst.name:
                # Determine the right hint: if both have an auth_family,
                # hint at `auth use`; otherwise generic plugin disable.
                hint = (
                    "Pick one with `botainer auth use <mode>`"
                    if man.auth_family
                    else "Disable one with `botainer plugin disable <name>`"
                )
                raise Refused(
                    RefusalCategory.PLUGIN_HOOK_FAILED,
                    f"plugins {inst.name!r} and {sibling!r} are mutually "
                    f"exclusive. {hint}.",
                )
    # Refuse if any family has >1 enabled member (catches author-
    # oversight cases where mutually_exclusive_with isn't symmetric).
    for fam, plugins in family_to_enabled.items():
        if len(plugins) > 1:
            raise Refused(
                RefusalCategory.PLUGIN_HOOK_FAILED,
                f"{len(plugins)} plugins enabled in auth_family={fam!r}: "
                f"{sorted(plugins)}. Exactly one per family allowed. "
                f"Use `botainer auth use <isolated|shared|proxy> "
                f"--family {fam}` to pick.",
            )
    plugin_hooks: list[HookSpec] = []
    contributed_capabilities: list[CapabilityGrant] = []
    sidecars: list[SidecarSpec] = []
    # Task #126: deleted the `plugin_env: dict[str, str] = {}` accumulator.
    # Plugin env contributions flow via run_pre_session_hooks → merged_env
    # (see line ~720), NOT via this slot. The dead slot + live credential-
    # leak check on it was a trap: future maintainers would wire to the
    # dead path and not realize the real path is in run_pre_session_hooks.
    # Removed to eliminate the confusion source.
    # entrypoint_wrap from each plugin: name → (layer, command).
    # Layered by (layer, name) below — see EntrypointWrapDecl docstring
    # for layer semantics. Implementation-review MEDIUM 13.
    entrypoint_wraps_by_plugin: dict[str, tuple[str, tuple[str, ...]]] = {}
    # Plugin trust check intentionally NOT performed at runtime.
    #
    # The previous design re-hashed each installed plugin's tree on every
    # `botainer start` and compared to `<source>/plugins/trusted_plugins.lock`.
    # In practice this generated chronic false positives in two scenarios:
    #
    # 1. Developers in editable installs: any source edit drifts the hash,
    #    producing a permanent "user-modified" warning until the dev
    #    manually deletes the lock and re-runs setup.
    # 2. End-users installing v0.1.0 from a clone (no shipped wheel yet):
    #    same as above — the prototype "generate-on-first-setup" path
    #    caches at the wrong moment and never refreshes.
    #
    # The check's theoretical value (catching post-install tampering) is
    # limited: an attacker with FS write to `~/.botainer/plugins/<x>/` also
    # has FS write to the lock file. The right integrity boundary is
    # pip-wheel signature verification at install time, not a runtime
    # hash comparison the launcher writes itself.
    #
    # trust.py and the lock infrastructure are kept for now to support
    # whatever shape the install-time verification eventually takes. The
    # spec field `plugin_trust_warnings` is retained (empty tuple) to
    # avoid an API break for inspect/capability_summary consumers.
    plugin_trust_warnings: list[tuple[str, str, str]] = []
    # Governed MCP servers (contributes.mcp_servers): manifests of enabled plugins
    # that give the agent an MCP tool. Collected here, materialized after the loop.
    _mcp_manifests: list = []
    _allowed_tiers = set(effective_policy.plugins.allowed_tiers)
    for inst in list_installed():
        if inst.name not in enabled:
            continue
        # #64: re-enforce the tier ceiling at COMPOSE against the
        # install-time-recorded tier. install.py/builtin.py gate tier at INSTALL
        # time; but an admin who TIGHTENS plugins.allowed_tiers AFTER a plugin is
        # already installed (without uninstalling it) left a gap — compose only
        # intersected config with the installed set (line ~272) and never
        # re-checked the tier, so a tampered config could still enable a
        # now-disallowed plugin. This does NOT re-hash the plugin (respecting the
        # deliberate "no runtime trust-hashing" decision below): `inst.tier` is
        # the tier recorded at INSTALL (lock.tier, or the manifest tier for
        # editable installs — every bundled manifest declares first-party, so no
        # false-refuse under the default allowed_tiers=[first-party]). We simply
        # enforce the CURRENT policy ceiling against that recorded tier.
        if inst.tier not in _allowed_tiers:
            raise Refused(
                RefusalCategory.PLUGIN_TIER_NOT_ALLOWED,
                f"plugin {inst.name!r} has tier {inst.tier!r}, which is not in "
                f"the effective policy plugins.allowed_tiers "
                f"{sorted(_allowed_tiers)}. It was installed while its tier was "
                f"permitted; the policy has since been tightened. Either drop it "
                f"from this project with `botainer plugin disable {inst.name}`, "
                f"or widen allowed_tiers in the root-owned "
                f"/etc/botainer/policy.yaml.",
            )
        plugin_dir = inst.plugin_dir
        plugin_data = proj_paths.plugin_data_dir(inst.name)
        plugin_data.mkdir(parents=True, exist_ok=True)
        from botainer.plugins.manifest import load_manifest

        try:
            man = load_manifest(plugin_dir)
        except Refused:
            import sys as _sys
            _sys.stderr.write(
                f"[botainer] plugin {inst.name}: manifest failed to load; skipping\n"
            )
            continue

        # AUDIT (MEDIUM, HPC parity): enforce the plugin's declared
        # `runtimes`. It was previously display-only, so a docker-only plugin
        # (e.g. web-ports) silently composed under apptainer instead of
        # refusing. Refuse enabling a plugin whose runtimes excludes the session
        # runtime. `mock` is the test runtime (no plugin declares it) and is
        # exempt so the integration suite (all MockAdapter) still composes.
        if runtime in _REAL_RUNTIMES and runtime not in man.runtimes:
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                f"plugin {inst.name!r} declares runtimes {man.runtimes} but the "
                f"session runtime is {runtime!r}. This plugin does not support "
                f"{runtime!r}; disable it for this runtime "
                f"(`botainer plugin disable {inst.name}`) or run on a supported "
                f"runtime.",
            )

        # Task #89: validate user's plugin config against the plugin's
        # declared config_schema. Lightweight check at v0.1: if the
        # schema has "additionalProperties: false" and "properties: {...}",
        # refuse any user key not in the properties set. Full jsonschema
        # validation is v0.2 (no jsonschema dep added at v0.1).
        _user_plugin_cfg = cfg.plugins.get(inst.name, {}) or {}
        if isinstance(_user_plugin_cfg, dict) and man.config_schema:
            _schema = man.config_schema
            if (_schema.get("additionalProperties") is False
                    and isinstance(_schema.get("properties"), dict)):
                _allowed = set(_schema["properties"].keys())
                _unknown = set(_user_plugin_cfg.keys()) - _allowed
                if _unknown:
                    raise Refused(
                        RefusalCategory.CONFIG_INVALID,
                        f"plugins.{inst.name}: unknown config keys "
                        f"{sorted(_unknown)}. Plugin declared "
                        f"additionalProperties: false; allowed keys: "
                        f"{sorted(_allowed)}.",
                    )
        # AUDIT (H9): a Task #100 block here re-introduced a
        # runtime verify_plugin() call that DIRECTLY CONTRADICTED the
        # deliberate "NOT performed at runtime" decision above — and was dead
        # anyway: it warned on tier in {"third-party","unverified","unknown"}
        # but verify_plugin only ever returns {"first-party","user-modified",
        # "untrusted"} (trust.py:113-115), a disjoint set, so a tampered
        # ("user-modified") or unlocked ("untrusted") plugin produced NO
        # warning. Removed: runtime trust-hashing stays intentionally off (the
        # reasoning above). plugin_trust_warnings is left empty — the field +
        # the capability_summary banner are retained for the future
        # install-time integrity path (wheel-signature verification, v0.2),
        # which is the correct boundary, not a launcher-written runtime hash.
        for prefix in man.contributes.mount_target_prefixes:
            if prefix.startswith("/workspace/.botainer/"):
                target = prefix.rstrip("/")
                bind = Bind(
                    source=str(plugin_data),
                    target=target,
                    mode=BindMode.RO,
                    provenance=Provenance.PLUGIN,
                    provenance_detail=f"plugin {inst.name} settings dir (ro)",
                    agent_rendering=AgentRendering.SHOWN,
                    self_test="SELFTEST_EXTRA_BIND",
                    nested_under="/workspace/.botainer",
                )
                plan = add_plugin_contribution(
                    plan,
                    inst.name,
                    [bind],
                    declared_target_prefixes=[prefix],
                )
        # Plugin hooks: register HookSpec entries. Execution timing is
        # set by `when:`; the launcher iterates the right ones at the
        # right phase (run_host_pre_launch_hooks / run_pre_session_hooks /
        # run_post_session_hooks).
        for hook in man.hooks:
            from typing import Literal, cast
            _valid_whens = (
                "pre_session", "post_session", "host_pre_launch",
            )
            _when_str = hook.when if hook.when in _valid_whens else "pre_session"
            _when: Literal[
                "pre_session", "post_session", "host_pre_launch",
            ] = cast(
                Literal[
                    "pre_session", "post_session", "host_pre_launch",
                ],
                _when_str,
            )
            plugin_hooks.append(
                HookSpec(
                    plugin=inst.name,
                    when=_when,
                    script_path=str(plugin_dir / hook.script),
                    timeout_seconds=hook.timeout_seconds,
                )
            )
        # Plugin sidecars
        # Task #86 + #256: accumulate plugin-declared top-level
        # capabilities into contributed_capabilities. Previously this
        # list stayed empty in production; manifest's capabilities:
        # field was unreachable. Each declared capability becomes a
        # CapabilityGrant with provenance=PLUGIN; _validate_capabilities
        # (task #258) then refuses any unknown name.
        for cap_name in man.capabilities:
            contributed_capabilities.append(
                CapabilityGrant(
                    name=cap_name,
                    value=None,
                    provenance=Provenance.PLUGIN,
                    source_plugin=inst.name,
                )
            )
        # Tasks #95, #291: spec.sidecars launcher is NOT IMPLEMENTED in v0.1.
        # The composition data model has SidecarSpec; the SessionSpec has a
        # `sidecars: tuple[...]` field; inspect/tree.py + inspect/access.py
        # render it as "DECLARED but NOT launched". No adapter consumes it.
        # Wolfram, agent-claude-proxy, and any future host_helper plugin
        # spawn their helper via the pre_session HOOK lifecycle (see
        # plugins/wolfram-sidecar/hooks/pre_session.py — Popen the helper,
        # record pid in the session record, SIGTERM it in post_session).
        # Container-runtime sidecars (`runtime: container`) have NO impl
        # site at v0.1 — refuse fail-closed so a plugin can't declare
        # sidecars that silently never launch.
        if man.contributes.sidecars:
            raise Refused(
                RefusalCategory.SIDECAR_REFUSED_NO_DOWNGRADE,
                f"plugin {inst.name!r} declares "
                f"{len(man.contributes.sidecars)} contributes.sidecars "
                f"entry(ies), but the v0.1 launcher does NOT execute "
                f"contributes.sidecars (#95/#291). Use the host_helper "
                f"hook pattern instead: spawn your helper from a "
                f"pre_session hook (subprocess.Popen + start_new_session) "
                f"and SIGTERM it from a post_session hook. See "
                f"plugins/wolfram-sidecar/hooks/pre_session.py for the "
                f"canonical example. The contributes.sidecars data model "
                f"is reserved for a v0.2 sidecar launcher.",
            )
        # Plugin entrypoint_wrap. §A19: with nudge migrated to a
        # host-side screen wrap, no in-tree plugin currently uses
        # template placeholders in its entrypoint_wrap.command.
        # Each command is taken as-is and stored as an immutable tuple.
        if man.contributes.entrypoint_wrap is not None:
            entrypoint_wraps_by_plugin[inst.name] = (
                man.contributes.entrypoint_wrap.layer,
                tuple(man.contributes.entrypoint_wrap.command),
            )
        if man.contributes.mcp_servers:
            _mcp_manifests.append(man)

    # 6b. Governed MCP servers → a launcher-written mcp-servers.json bound RO at
    # /workspace/.botainer/ (same generated-file+bind pattern as AGENT_HINTS.md).
    # The `--mcp-config <path> --strict-mcp-config` launch flag is appended to the
    # innermost agent wrap below (Claude-family only; trusted compose selects it,
    # not a plugin) so the agent gets EXACTLY these governed tools, nothing else.
    _mcp_target: str | None = None
    from botainer.plugins.mcp import render_mcp_servers as _render_mcp_servers
    _mcp_cfg = _render_mcp_servers(_mcp_manifests)
    if _mcp_cfg:
        import json as _json
        _mcp_path = session_scratch / "mcp-servers.json"
        _mcp_path.write_text(_json.dumps(_mcp_cfg, indent=2), encoding="utf-8")
        _mcp_target = "/workspace/.botainer/mcp-servers.json"
        plan = plan.with_many([Bind(
            source=str(_mcp_path.resolve()),
            target=_mcp_target,
            mode=BindMode.RO,
            provenance=Provenance.CORE,
            provenance_detail="governed MCP servers for the agent (ro)",
            agent_rendering=AgentRendering.SHOWN,
            self_test="SELFTEST_EXTRA_BIND",
            nested_under="/workspace/.botainer",
        )])

    # 7. Network spec from config.
    net_mode_str = cfg.network.mode
    if net_mode_str == "api-only":
        net_mode_str = "endpoint-ip-allowlist"
    try:
        net_mode = NetworkMode(net_mode_str)
    except ValueError:
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"network.mode must be one of {[m.value for m in NetworkMode]}; got {net_mode_str!r}",
        ) from None
    # AUDIT (H4): enforce the site-policy network CEILING. policy
    # computes effective_policy.network.default_mode as the most-restrictive
    # mode across site+user policy (none < endpoint-ip-allowlist < internet),
    # but compose previously read cfg.network.mode straight from the
    # git-shareable project config and NEVER consulted the ceiling — so a
    # tampered config requesting `internet` was accepted even when the site
    # policy ceiling was `none` (fail-open egress). Refuse a project mode less
    # restrictive than the ceiling. Project config may only EQUAL or NARROW it.
    _NET_RANK = {"none": 0, "endpoint-ip-allowlist": 1, "internet": 2}
    _ceiling = effective_policy.network.default_mode
    if _NET_RANK.get(net_mode.value, 99) > _NET_RANK.get(_ceiling, -1):
        # Name the ACTUAL binding source (user vs admin/site) instead of reciting
        # both and always mentioning "admin" — the launcher has both policies, so
        # it can tell. A site policy is only binding if it EXISTS and caps the
        # requested mode; otherwise the user's OWN policy is the constraint and
        # they can raise it themselves (no admin involved).
        _req = net_mode.value
        _site_present = policy_module.site_policy_present()
        _site_caps = _site_present and (
            _NET_RANK.get(site_policy.network.default_mode, 2) < _NET_RANK.get(_req, 99)
        )
        _base = (
            f"network.mode={_req!r} is denied: less restrictive than the effective "
            f"ceiling network.default_mode={_ceiling!r}. The project config "
            f"(git-shareable, attacker-influenceable) may only request a mode at "
            f"or below the ceiling. "
        )
        if _site_caps:
            _detail = (
                f"The binding constraint is the root-owned SITE policy "
                f"(/etc/botainer/policy.yaml), which caps network.default_mode at "
                f"{site_policy.network.default_mode!r} — only an admin can raise "
                f"that. "
            )
        else:
            _detail = (
                f"This is YOUR OWN user policy "
                f"(network.default_mode={user_policy.network.default_mode!r}); no "
                f"admin/site policy is forcing it on this machine. Raise it "
                f"yourself:\n    botainer policy set network.default_mode {_req}\n"
                f"If you did not set this deliberately, it is most likely carried "
                f"over from an older botainer version whose default was more "
                f"restrictive — the command above fixes it. "
            )
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            _base + _detail + "Run `botainer policy show` to see the ceiling + its source.",
        )
    if net_mode == NetworkMode.ENDPOINT_IP_ALLOWLIST and not cfg.network.endpoints:
        raise Refused(
            RefusalCategory.API_ONLY_REQUIRES_ENDPOINTS,
            "network.mode=endpoint-ip-allowlist requires endpoints",
        )
    # #176: endpoint-ip-allowlist needed host-side iptables to enforce.
    # That path was never implemented in v0.1 (see DN-024 D4). The
    # docker adapter would silently fall back to a plain bridge,
    # giving the user a false sense of restriction. Refuse the mode
    # outright until the enforcement lands.
    if net_mode == NetworkMode.ENDPOINT_IP_ALLOWLIST:
        raise Refused(
            RefusalCategory.RUNTIME_CANNOT_ENFORCE,
            "network.mode=endpoint-ip-allowlist is NOT enforceable in v0.1 "
            "(iptables path was retired per #176). Set "
            "network.mode=internet and route per-endpoint policy through "
            "external infra (host egress proxy, cluster ACLs), or "
            "network.mode=none for offline work. Re-enabled when the "
            "endpoint-ip-allowlist enforcement lands.",
        )
    # Task #87 + AUDIT (LOW): enforce
    # site_policy.network.allowed_endpoint_groups, FAIL-CLOSED. Previously an
    # EMPTY allowlist meant "unrestricted" (fail-open) — contradicting the
    # project's empty-list=OFF convention. Now: if a project requests
    # endpoints, the policy MUST list them; an empty allowlist permits NONE.
    # (Latent today — endpoint-ip-allowlist mode is refused at compose per
    # #176 — but corrected so it's a closed landmine if that mode re-enables.)
    if cfg.network.endpoints:
        _aeg = effective_policy.network.allowed_endpoint_groups
        if not _aeg:
            raise Refused(
                RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
                f"network.endpoints {list(cfg.network.endpoints)!r} requested but "
                f"site policy allowed_endpoint_groups is empty — no endpoints are "
                f"permitted. An admin must list permitted groups in the policy.",
            )
        _unknown = [e for e in cfg.network.endpoints if e not in _aeg]
        if _unknown:
            raise Refused(
                RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
                f"network.endpoints {_unknown!r} not in site policy "
                f"allowed_endpoint_groups={_aeg}",
            )
    network = NetworkSpec(
        mode=net_mode,
        endpoints=tuple(cfg.network.endpoints),
    )

    # Inbound loopback forwards: web-ports (user grant) + browser viewer noVNC
    # (docker only; `plugins.browser.viewer: true` is the grant). See §4av.
    port_forwards = _resolve_port_forwards(cfg, enabled, runtime)

    # 8. Resources.
    resources = ResourceSpec(
        cpu=cfg.resources.cpu,
        memory_mb=cfg.resources.memory_mb,
    )
    # #175: HPC scheduling fields (time_minutes, gpus, gpu_type,
    # partition, account) live on cfg.resources and are consumed by
    # plugins/hpc-launcher/host_helper/submit.py at sbatch time —
    # NOT via the SessionSpec. They were on ResourceSpec too but no
    # adapter read them.

    # 8b. Agent permission posture (#53 / T0-2). Resolve the project config
    # against the site-policy ceiling, fail-closed. Ordering (most → least
    # restrictive): prompt < bypass. A project may request a posture at or below
    # the ceiling; requesting "bypass" when the site caps at "prompt" is refused
    # (same shape as the network.mode ceiling above). cfg.agent_permissions is
    # already enum-validated at parse (config.py), and the ceiling at policy
    # parse; this is the cross-level intersection.
    _PERM_RANK = {"prompt": 0, "bypass": 1}
    _req_perm = cfg.agent_permissions
    _perm_ceiling = effective_policy.agent.max_permissions
    if _PERM_RANK.get(_req_perm, 99) > _PERM_RANK.get(_perm_ceiling, -1):
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            f"agent_permissions={_req_perm!r} is denied: the effective policy "
            f"caps agent.max_permissions at {_perm_ceiling!r}. A caged agent may "
            f"run at or below the ceiling only. Set the project's "
            f"agent_permissions to {_perm_ceiling!r}, or ask the cluster admin "
            f"to raise agent.max_permissions in the root-owned "
            f"/etc/botainer/policy.yaml. Run `botainer policy show` to see the "
            f"ceiling + its source.",
        )
    resolved_permissions = _req_perm

    # 9. Env values — denylist check + credential-leak check.
    # The config `env:` is UNTRUSTED (git-shareable). It must be gated by the SAME
    # authoritative exec-injection + managed-route sets the pre_session/host-envfile
    # channels use — NOT just the (user/site-overridable) policy denylist. Without
    # this a config could set GCONV_PATH / GLIBC_TUNABLES / HOSTALIASES / LD_PROFILE
    # (force-code-load at container startup, defeating agent_permissions:prompt) or
    # override a managed route like PIP_TARGET/CLAUDE_CONFIG_DIR (sharp-edges HIGH
    #; this is the more-attacker-controlled sibling of the #65 fix).
    # _HOST_ENVFILE_EXEC_INJECTION + _BOTAINER_MANAGED_ROUTES are HARDCODED, so a
    # loosened site policy can't re-open them.
    env_denylist = set(effective_policy.capabilities.env_var_denylist)
    _hard_denied = _HOST_ENVFILE_EXEC_INJECTION | _BOTAINER_MANAGED_ROUTES
    for k in cfg.env:
        if k in env_denylist:
            raise Refused(
                RefusalCategory.ENV_VAR_DENIED,
                f"env var {k!r} is on policy denylist {sorted(env_denylist)}",
            )
        if k in _hard_denied:
            raise Refused(
                RefusalCategory.ENV_VAR_DENIED,
                f"env var {k!r} is a force-code-load / managed-route var and may "
                f"not be set from config `env:` (it would run code at container "
                f"start or redirect a botainer-managed path).",
            )
    # Credential leak check: refuse to compose if config.env carries
    # anything that looks like a credential. The agent can read its
    # own env; that's where credentials shouldn't go.
    from botainer.core import credential_leak_check
    credential_leak_check.check_env_for_leaks(
        cfg.env, source=".botainer/config.yaml `env:`"
    )
    # Task #126: plugin env contributions flow via run_pre_session_hooks
    # (line ~720), NOT via this slot. The dead `plugin_env` accumulator
    # and its credential-leak check have been removed.
    env_values = dict(cfg.env)
    # #53 / T0-2: inject the resolved permission posture so each agent's
    # entrypoint wrapper can map it to its own flags (claude
    # --dangerously-skip-permissions; codex --sandbox danger-full-access
    # --ask-for-approval never). This is a FIRST-PARTY injection by trusted
    # compose code — NOT a plugin `command_append` (which is refused, #290) —
    # and it crosses `apptainer --cleanenv` because the adapter renders every
    # spec.env.values entry as `--env` (adapters/apptainer.py). Added AFTER the
    # cfg.env denylist + credential-leak checks (it's launcher-controlled, not
    # user env, and is not credential-shaped).
    env_values["BOTAINER_AGENT_PERMISSIONS"] = resolved_permissions
    # Root fix (the through-line behind the browser-npx + /tmp-splatter failures):
    # the container runs as the host uid, which has no home in the image, so HOME
    # would be `/` (unwritable). Point it at the writable per-project /home/user
    # bind added above so npm/npx, pip, and Claude Code's home-based paths work
    # instead of failing or dumping into /tmp. The LAUNCHER OWNS HOME: it's in
    # _BOTAINER_MANAGED_ROUTES (an untrusted config `env:` can't set it), and set
    # unconditionally here — a plugin's pre_session env can at most RE-ASSERT the
    # same value (the merge conflict guard refuses a different one). Crosses
    # `apptainer --cleanenv` as `--env` (adapters render spec.env.values).
    # (/tmp stays EPHEMERAL temp — tmpfs on docker, apptainer's private /tmp under
    # --containall — deliberately NOT this persistent home, so temp can't
    # accumulate the way /scratch does.)
    env_values["HOME"] = "/home/user"
    env = EnvSpec(values=env_values)

    # 10. Kernel caps.
    kernel = KernelCapsSpec(keep=tuple(cfg.caps.kernel.keep))
    if kernel.keep and set(kernel.keep) - set(
        effective_policy.capabilities.kernel_caps_keep_allowed
    ):
        raise Refused(
            RefusalCategory.KERNEL_CAP_KEEP_NOT_ALLOWED,
            f"kernel.keep {kernel.keep} not in policy allowlist "
            f"{effective_policy.capabilities.kernel_caps_keep_allowed}",
        )

    # 11. Validate mount plan.
    validate_mount_plan(plan, policy=effective_policy)

    # 12. Render AGENT_ACCESS.txt to scratch path.
    from typing import Literal as _Literal
    from typing import cast as _cast
    _runtime: _Literal["docker", "apptainer", "mock"] = _cast(
        _Literal["docker", "apptainer", "mock"], runtime
    )
    entrypoint_wraps = sort_entrypoint_wraps(entrypoint_wraps_by_plugin)

    # #53 / T0-2: append the resolved in-cage permission-posture flags to the
    # INNERMOST entrypoint wrap (the one that execs the agent CLI). Doing this in
    # first-party compose — rather than inside the image's wrapper reading an env
    # var — means the flag applies REGARDLESS of image version (the wrapper only
    # forwards "$@"), so no `.sif`/docker rebuild is needed to change the posture.
    # It is NOT a plugin `command_append` (which stays refused, #290): trusted
    # compose code selects from a FIXED first-party flag set by the agent family;
    # a plugin cannot inject arbitrary agent flags. `prompt` appends nothing.
    # See CAPABILITY-SURFACE §4ab.
    _AGENT_BYPASS_FLAGS: dict[str, tuple[str, ...]] = {
        "anthropic": ("--dangerously-skip-permissions",),
        "openai": ("--sandbox", "danger-full-access",
                   "--ask-for-approval", "never"),
    }

    def _agent_family(name: str) -> str | None:
        if name.startswith("agent-claude"):
            return "anthropic"
        if name.startswith("agent-codex"):
            return "openai"
        return None

    # Flags appended to the INNERMOST wrap (the one that execs the agent CLI):
    # the in-cage permission posture (#53) + the governed MCP config (6b above).
    # Trusted compose selects both from FIXED sets; NOT a plugin command_append.
    _append_flags: tuple[str, ...] = ()
    # Derived from the PRIMARY agent (`cfg.agent`), never by scanning `enabled`.
    # `enabled` is a set (see effective_enabled above), so a scan returns
    # whichever agent plugin the hash order happened to put first. With one
    # agent plugin that is harmless; with two it is a coin flip, and an audit
    # probe measured exactly that: PYTHONHASHSEED 1-3,9,11,12 -> openai;
    # 4-8,10 -> anthropic. The primary would randomly get the OTHER family's
    # permission flags and the governed MCP config would randomly vanish.
    #
    # _activate_only_the_selected_agent (#120) currently guarantees one agent
    # plugin, so this is masked today rather than live — which is exactly why
    # it is worth fixing now. Companions deliberately re-open that door; a
    # hash-order-dependent flag choice waiting behind it is a trap.
    _fam = _agent_family(f"agent-{cfg.agent}") if cfg.agent else None
    if _fam is None:
        # No `agent:` set, or an agent this function does not know. Fall back to
        # the scan so behaviour is unchanged for those cases — but sort first,
        # so the fallback is at least deterministic.
        _fam = next((f for n in sorted(enabled) if (f := _agent_family(n))), None)
    if resolved_permissions == "bypass":
        _bypass = _AGENT_BYPASS_FLAGS.get(_fam) if _fam else None
        if _bypass:
            _append_flags += _bypass
    # MCP delivery is PER AGENT FAMILY, and only one family has it. Claude Code
    # takes `--mcp-config <path> --strict-mcp-config`; codex reads
    # `[mcp_servers.*]` out of CODEX_HOME/config.toml and would ignore the file
    # entirely. Families with no delivery mechanism are listed nowhere — the set
    # below IS the list, so adding a family without adding its delivery makes
    # this refuse rather than silently drop the servers.
    _MCP_DELIVERY: dict[str, tuple[str, ...]] = {
        "anthropic": ("--mcp-config", "<path>", "--strict-mcp-config"),
    }
    if _mcp_target is not None:
        if _fam in _MCP_DELIVERY:
            _append_flags += ("--mcp-config", _mcp_target, "--strict-mcp-config")
        else:
            # #128: REFUSE, do not no-op. The old code appended the flag only for
            # anthropic and fell through silently otherwise, so enabling `browser`
            # under codex composed a session that looked completely correct — the
            # config file written, bound, valid — and the agent simply had no
            # browser tool. Nothing said why. A plugin the user turned on that
            # does nothing is worse than one that refuses to start.
            _mcp_plugins = ", ".join(sorted(m.name for m in _mcp_manifests))
            _who = (
                f"the selected agent family ({_fam})" if _fam
                else "this session (no agent plugin is enabled)"
            )
            raise Refused(
                RefusalCategory.PLUGIN_DEPENDENCY_UNRESOLVED,
                f"plugin(s) {_mcp_plugins} give the agent MCP tools, but "
                f"{_who} has no way to receive them — the session would start "
                f"with those tools MISSING and nothing would say so. Remove "
                f"{_mcp_plugins} from plugins_enabled, or run this project with "
                f"a Claude agent (`botainer start --agent claude`). Codex MCP "
                f"delivery (CODEX_HOME/config.toml [mcp_servers]) is tracked "
                f"as #128.",
            )
    if _append_flags and entrypoint_wraps:
        entrypoint_wraps = (
            entrypoint_wraps[:-1]
            + (tuple(entrypoint_wraps[-1]) + _append_flags,)
        )

    spec = SessionSpec(
        session_id=session_id,
        project_uuid=uid,
        project_root=str(project_root),
        state_dir=str(project_state),
        runtime=_runtime,
        image=image,
        mount_plan=plan,
        network=network,
        resources=resources,
        kernel_caps=kernel,
        env=env,
        entrypoint_wraps=entrypoint_wraps,
        port_forwards=port_forwards,
        sidecars=tuple(sidecars),
        hooks=tuple(plugin_hooks),
        # Task #68 + #258: consult the capability registry. Any
        # contributed capability whose name isn't in CAPABILITIES is
        # silently accepted at v0.1 — that's the documented gap. Refuse
        # unknown names here so the registry is no longer decorative.
        capabilities=tuple(_validate_capabilities(contributed_capabilities)),
        plugins_enabled=tuple(sorted(enabled)),
        plugin_trust_warnings=tuple(plugin_trust_warnings),
        profile=cfg.profile,
        agent_permissions=resolved_permissions,
        composed_at=identity.now_iso8601_utc(),
    )
    # NOTE (audit T10): these are rendered here from the COMPOSE-time spec —
    # BEFORE host_pre_launch/pre_session hooks add their binds/env. The launcher
    # re-renders them via `render_agent_files(spec)` AFTER the hooks (start.py),
    # so the agent's AGENT_ACCESS/AGENT_HINTS reflect the credential binds, git
    # overlay, and #160 module software-root binds it actually has. This initial
    # write keeps the dry-run / no-hooks path populated.
    aas_path.write_text(access_renderer.render(spec), encoding="utf-8")
    hints_path.write_text(agent_hints_renderer.render(spec), encoding="utf-8")
    # Persist the session record (spec.json) to the session dir so external
    # commands (`botainer nudge`, `botainer status`, etc.) can find it.
    # Runtime handle (container_id, jobid) is filled in later by adapter.
    session_record.write(session_scratch, session_record.from_spec(spec))
    return spec


def render_argv(spec: SessionSpec) -> list[str]:
    adapter = _adapter_for(spec.runtime)
    return adapter.render_argv(spec)


# AUDIT (H1, expanded after the adversarial review): env vars that
# FORCE code execution, hijack the dynamic loader/resolver, or override
# commands. No legitimate `module load` output sets ANY of these (module env
# is library/path-discovery only). A host_pre_launch env_file is passed
# verbatim to the runtime via --env-file (crossing apptainer --cleanenv), so
# its CONTENTS are a real injection sink — and the contributing plugin's own
# self-filtering (e.g. hpc-modules' _MODULE_TRUSTED_DEFAULTS, which even
# allows PYTHONPATH and only drops BASH_FUNC_* by convention) must NOT be
# trusted as the chokepoint. The launcher refuses these at the env_file gate.
#
# Scope note (#160-deferred, documented in CAPABILITY-SURFACE §4g): pure
# library/PATH-routing vars (PATH, LD_LIBRARY_PATH, and the package routers
# PYTHONPATH/PERL5LIB/R_LIBS*/JULIA_* the hook self-allows) still FLOW through
# the env_file today — they are path-discovery, not direct code-load, and
# refusing them would break real modules. The curated path-var allowlist +
# per-plugin cap gate that bounds them is #160 (DESIGN-160-module-binds.md).
# What is closed here is the force-code-load / command-override class below.
_HOST_ENVFILE_EXEC_INJECTION = frozenset({
    # Dynamic-linker preload / audit / write-primitives (Linux).
    "LD_PRELOAD", "LD_AUDIT", "LD_BIND_NOW", "LD_PROFILE", "LD_DEBUG_OUTPUT",
    # glibc loader/charset/resolver hijacks. GCONV_PATH + GLIBC_TUNABLES are
    # force-code-load / loader-control (cf. CVE-2023-4911 "Looney Tunables");
    # HOSTALIASES/RES_OPTIONS/LOCALDOMAIN/NIS_PATH redirect name resolution.
    "GLIBC_TUNABLES", "GCONV_PATH",
    "HOSTALIASES", "RES_OPTIONS", "LOCALDOMAIN", "NIS_PATH",
    # macOS dynamic-loader (the LD_PRELOAD analog; the docker dev path is Mac,
    # and the project's own auth.py blocklists these).
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
    # Shell startup / command override.
    "BASH_ENV", "ENV", "PROMPT_COMMAND",
    # Interpreter option/RC injection (force flags / startup code).
    "PERL5OPT", "RUBYOPT",
    "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
    "NODE_OPTIONS", "PYTHONSTARTUP",
    # Git command/config override (→ arbitrary command on git operations).
    "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
})

# A legitimate env var name is a plain identifier. Anything else — notably
# bash's exported-function scheme `BASH_FUNC_<name>%%` — is refused: a
# BASH_FUNC_ entry is imported as a SHELL FUNCTION by a bash entrypoint and
# would shadow/override a command (the hpc-modules hook drops these by
# convention, but the gate must not rely on the plugin doing so).
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Botainer-managed package-routing vars. The container image sets these to
# /packages/* so installs persist per-project; a module env-file that sets them
# would silently redirect the agent's pip/node/cargo/conda routing. The
# hpc-modules hook already strips these (its own _BOTAINER_MANAGED), but the
# launcher MUST re-enforce at this chokepoint — the hook's filtering is not
# trusted (adversarial-review T2; mirrors load_modules.py::_BOTAINER_MANAGED, a
# drift test pins the two). PATH / LD_LIBRARY_PATH and the curated module
# location vars deliberately stay ALLOWED (the documented §4g residual).
_BOTAINER_MANAGED_ROUTES = frozenset({
    "PIP_TARGET", "PYTHONPATH", "NODE_PATH", "JULIA_DEPOT_PATH", "R_LIBS_USER",
    "CONDA_ENVS_PATH", "CARGO_HOME", "GOPATH", "CLAUDE_CONFIG_DIR",
    # CODEX_HOME is CLAUDE_CONFIG_DIR's exact counterpart and was missing:
    # agent-codex-shared now sets it to locate the credential, so
    # a module env-file or a git-shared .botainer/config.yaml that redirected
    # it would point codex at an attacker-chosen config dir — auth.json and all.
    # It is managed here for the same reason and bounded the same way: the
    # plugin channel below still allows an AGENT plugin to set it.
    "CODEX_HOME",
    # HOME is launcher-anchored to the writable /home/user bind (composition sets
    # it). Managed so a git-shared .botainer/config.yaml can't redirect it — e.g.
    # back to /tmp (re-triggering the ENOSPC the bind fixes) or into /workspace
    # (caches polluting the git tree). Fable-5 review (LOW-3).
    "HOME",
})


def _split_module_env_for_inner_prepend(
    env_file: Path, session_state_dir: Path
) -> tuple[Path | None, list[tuple[str, str]]]:
    """Step B (#216, internal design note DN-005): split a module env_file into a
    scalar-only file (returned as a NEW Path under the session state dir) and
    a list of (var, value) pairs for PATH-list vars that the adapter will
    PREPEND in-container via a shell trampoline.

    Returns (scalar_env_path, path_list_prepends). If no path-list vars are
    present, returns (None, []) and the caller keeps the original env_file
    unchanged. If only path-list vars are present, returns (None, [...]) and
    the caller drops the env_file from spec.env_files entirely.

    The scalar-only file is written to a sibling path next to the original
    (under the session state dir, which is already path-contained by the H1
    audit fix). Permissions match the original.
    """
    from botainer.hpc.module_binds import PATH_LIST_VARS
    text = env_file.read_text(encoding="utf-8")
    scalar_lines: list[str] = []
    prepends: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            scalar_lines.append(line)
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k in PATH_LIST_VARS:
            # Strip surrounding quotes/whitespace exactly as the trampoline
            # will see it (the value is passed via --env KEY=VAL which
            # doesn't interpret quotes; only the env-file shell-style does).
            prepends.append((k, v.strip().strip('"').strip("'")))
        else:
            scalar_lines.append(line)
    if not prepends:
        return None, []
    if not any(s.strip() and not s.startswith("#") and "=" in s for s in scalar_lines):
        # No scalars left → caller drops the env_file entirely.
        return None, prepends
    scalar_path = session_state_dir / f"{env_file.stem}-scalar-only{env_file.suffix or '.env'}"
    # write_secure ensures restrictive perms + atomic rename (same primitive the
    # rest of the launcher uses for state writes — task #264/#265).
    from botainer.state.secure_write import write_secure
    write_secure(scalar_path, "\n".join(scalar_lines) + "\n", mode=0o600)
    return scalar_path, prepends


def _handle_module_env_path_clobber(
    env_file: Path, runtime: str, plugin: str
) -> None:
    """ACKNOWLEDGED-RISK #1 (Step A): on direct flows (docker / direct-apptainer),
    a module env_file carrying PATH-list vars (PATH, LD_LIBRARY_PATH, …) is
    delivered via `--env-file` which under `--cleanenv` SETs (replaces) them —
    clobbering the container's own PATH (the agent binary's dir at /opt/conda/bin
    etc.) instead of PREPENDING the way the sbatch flow does
    (cli/start._apply_module_env_file). The result was a confusing `claude: not
    found` agent-launch failure with no clear cause.

    Default behavior: **REFUSE** the launch with an actionable
    message. The previous warn-and-continue produced a silent-on-paper / loud-on-
    launch break; refusing surfaces the same constraint at compose time when the
    user can do something about it.

    Two opt-outs honor the user's intent:
      - `BOTAINER_ALLOW_PATH_CLOBBER=1` reproduces the previous warn-and-continue
        (the emergency escape valve — the user accepts the clobber).
      - `BOTAINER_USE_INNER_PREPEND=1` is the Step B opt-in for the in-container
        trampoline fix (NOT YET WIRED on direct paths; raises a clearer "this
        fix isn't ready yet on the docker/direct path" message until Step B
        lands). Until then it behaves like the default refusal but with the
        forward-looking message.

    Sbatch flow does NOT reach here (the inner `--in-container` path skips
    host_pre_launch hooks — adversarial-review B1)."""
    if runtime not in ("docker", "apptainer"):
        return
    # The colon-list vars the sbatch path PREPENDS (so SET-via-env-file is a
    # clobber); scalar home-dir vars (CUDA_HOME, JAVA_HOME, …) are fine to SET
    # and are deliberately excluded.
    from botainer.hpc.module_binds import PATH_LIST_VARS
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return
    clobbered = sorted({
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and line.split("=", 1)[0].strip() in PATH_LIST_VARS
    })
    if not clobbered:
        return
    import os as _os
    allow_clobber = _os.environ.get("BOTAINER_ALLOW_PATH_CLOBBER") == "1"
    if allow_clobber:
        import sys as _sys
        _sys.stderr.write(
            f"[botainer] WARNING (BOTAINER_ALLOW_PATH_CLOBBER=1, #1): the "
            f"{plugin} module env sets path-list vars {clobbered} which the "
            f"{runtime} container will receive via `--env-file` (SET, not "
            f"prepend), clobbering the container's own PATH. You opted in to "
            f"accept this. The agent launch may fail with `<agent>: not found` "
            f"if the agent binary's dir (e.g. /opt/conda/bin) is dropped from "
            f"PATH.\n"
        )
        return
    raise Refused(
        RefusalCategory.ENV_VAR_DENIED,
        f"refusing to launch: the {plugin} host_pre_launch hook contributed "
        f"path-list var(s) {clobbered} which would be SET (clobber) in the "
        f"{runtime} container via `--env-file` under --cleanenv, dropping the "
        f"agent binary's directory from PATH and breaking the launch. "
        f"(ACKNOWLEDGED-RISK #1; the previous warn-and-continue behavior was "
        f"silent-on-paper / loud-on-launch.)\n"
        f"\n"
        f"Pick one:\n"
        f"  • Use the sbatch flow (`botainer hpc submit`) — it PREPENDS path-"
        f"list vars in-container and is the supported path for module-loaded "
        f"software on HPC.\n"
        f"  • `module unload` the offending modules before launching direct; "
        f"the bound /apps roots are still RO-available by absolute path.\n"
        f"  • Accept the clobber: re-run with `BOTAINER_ALLOW_PATH_CLOBBER=1` "
        f"(you'll see the old WARNING and the agent may fail to start).\n"
        f"  • Wait for the in-container trampoline fix (Step B; tracked "
        f"#216).",
    )


# Back-compat alias: tests outside this module that imported the old name
# (test_module_binds_wiring.py) keep working. Remove once those tests update
# (committed in the same change as the rename).
_warn_module_env_file_clobbers_path = _handle_module_env_path_clobber


def _validate_host_env_text(
    text: str, *, plugin: str, denylist: frozenset[str] | set[str] = frozenset()
) -> None:
    """AUDIT (H1): validate module env-file CONTENTS (the hook's
    self-filtering is not trusted). Refuses:
    (a) any execution-injection var (`_HOST_ENVFILE_EXEC_INJECTION`) — the
        LD_PRELOAD-class injection the --cleanenv channel previously allowed;
    (b) any botainer-managed package route (`_BOTAINER_MANAGED_ROUTES`);
    (c) any key on the effective policy `env_var_denylist` EXCEPT the curated
        module location vars (`module_binds.PATH_VARS`) that genuinely must flow
        — re-audit (audit 2): closes CA-bundle / interpreter-routing vars
        (SSL_CERT_FILE, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE, PERL5LIB, RUBYLIB,
        PYTHONHOME, JULIA_LOAD_PATH, CONDA_*) that the prior fix let through;
    (d) non-identifier keys (e.g. BASH_FUNC_<name>%% shell-function imports); and
    (e) any credential-shaped key — module env must never carry secrets.
    Library/path-discovery vars (PATH, LD_LIBRARY_PATH, …) are allowed.

    Content-only (no file read) so a caller that already has the text — or that
    wants to distinguish an UNREADABLE file (degradable) from HOSTILE contents
    (fatal) — can do its own read (#160 --module-env-file, adversarial-review).
    `denylist` is the effective policy env_var_denylist (callers thread it in).
    """
    from botainer.core import credential_leak_check
    from botainer.hpc.module_binds import PATH_VARS as _MODULE_LOCATION_VARS
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if not k:
            continue
        if not _ENV_NAME_RE.match(k):
            raise Refused(
                RefusalCategory.ENV_VAR_DENIED,
                f"module env from {plugin} has env name {k!r} that is not a plain "
                f"identifier. Notably bash exported-function entries "
                f"(BASH_FUNC_<name>%%) are imported as shell functions by a bash "
                f"entrypoint and would override a command; refused.",
            )
        if k in _HOST_ENVFILE_EXEC_INJECTION:
            raise Refused(
                RefusalCategory.ENV_VAR_DENIED,
                f"module env from {plugin} sets execution-injection var {k!r}. It "
                f"crosses --cleanenv into the container; no legitimate `module "
                f"load` sets {k!r}, and the launcher refuses it here regardless of "
                f"the plugin's own filtering.",
            )
        if k in _BOTAINER_MANAGED_ROUTES:
            raise Refused(
                RefusalCategory.ENV_VAR_DENIED,
                f"module env from {plugin} sets botainer-managed package-routing "
                f"var {k!r}. The container image points this at /packages/* so "
                f"per-project installs persist; a module must not redirect it. "
                f"The launcher refuses it here regardless of the plugin's own "
                f"filtering.",
            )
        # Re-audit (audit 2): enforce the effective policy env_var_denylist on
        # the env-file channel too (symmetric with cfg.env + pre_session env),
        # EXEMPTING only the curated module location vars that must flow. Closes
        # CA-bundle / interpreter-routing vars (SSL_CERT_FILE, REQUESTS_CA_BUNDLE,
        # CURL_CA_BUNDLE, PERL5LIB, PYTHONHOME, …) — a hostile modulefile setting
        # SSL_CERT_FILE would otherwise redirect the agent's TLS trust store.
        if k in denylist and k not in _MODULE_LOCATION_VARS:
            raise Refused(
                RefusalCategory.ENV_VAR_DENIED,
                f"module env from {plugin} sets {k!r}, which is on the policy "
                f"env_var_denylist and is not a curated module location var. It "
                f"crosses --cleanenv into the container; refused (e.g. a module "
                f"redirecting SSL_CERT_FILE/REQUESTS_CA_BUNDLE would hijack the "
                f"agent's TLS trust).",
            )
        parsed[k] = v
    # Adversarial-review T2 (newline-forge): a module value containing a
    # line-boundary char would, in the line-oriented env-file, become its OWN
    # line — but `splitlines()` above ALREADY splits on every line boundary, so
    # a forged entry is parsed as a separate KEY=VALUE and validated by THIS
    # loop like any other (a forged LD_PRELOAD / botainer-managed / credential
    # line is therefore refused above). The hook additionally drops
    # newline-bearing values before writing the file (load_modules.py) so the
    # on-disk file stays well-formed. No separate value-newline check is needed
    # here (it would be dead — v can't contain a line boundary post-splitlines).
    # Credentials never belong in a module env_file. Use EXACT-name matching
    # (not the heuristic), so a real provider credential is refused but
    # legitimate ML module config like TOKENIZERS_PARALLELISM is not
    # false-rejected (AUDIT H1, adversarial-review false-reject).
    credential_leak_check.check_env_for_named_credentials(
        parsed, source=f"module env from {plugin}",
    )


def _validate_host_env_file(
    resolved: Path, *, plugin: str,
    denylist: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Read a host_pre_launch env_file and validate its contents (the §4g
    docker/direct chokepoint). An unreadable file is a malformed contribution
    (the hook promised a file it didn't write); content violations raise via
    `_validate_host_env_text`. `denylist` = effective policy env_var_denylist."""
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refused(
            RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
            f"hook {plugin}.host_pre_launch env_file {resolved} is unreadable: {exc}",
        ) from exc
    _validate_host_env_text(
        text, plugin=f"hook {plugin}.host_pre_launch env_file", denylist=denylist
    )


def _software_root_binds_from_contribution(
    contribution: dict,
    *,
    plugin: str,
    inst_by_name: dict,
    effective_policy,
) -> list:
    """#160: turn a host_pre_launch hook's `software_root_env` contribution into
    validated, read-only software-root binds.

    The hpc-modules hook (stdlib-only, runs as the user) emits the RAW
    post-purge `baseline` and post-load `loaded` env subsets; the actual
    security derivation happens HERE in trusted launcher code:

      1. cap-grant gate (fail-closed): the contributing plugin's manifest MUST
         declare `caps.modules_software_roots`. A plugin that didn't declare it
         (and so wasn't trust-locked / tier-gated for it) cannot contribute
         software-root binds even if its hook emits the contribution.
      2. `derive_software_root_binds` enforces the umbrella-bar: baseline-diff,
         path-var allowlist, SitePolicy-ceiling containment, system-root
         denylist, min-depth floor, RO identity binds, loud max_roots ceiling.
      3. The empty ceiling (`mounts.cluster_software_roots == []`) yields no
         binds — the feature is OFF unless the root-owned site policy lists
         software roots.

    Returns a list of Bind. `validate_mount_plan` (run by the caller after all
    binds are collected) is the independent denylist/realpath backstop.
    """
    from botainer.core.spec import Bind, BindMode
    from botainer.hpc.module_binds import (
        SoftwareRootBindError,
        derive_software_root_binds_verbose,
    )
    from botainer.plugins.manifest import load_manifest

    sw_env = contribution.get("software_root_env")
    if sw_env is None:
        return []
    if not isinstance(sw_env, dict):
        raise Refused(
            RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
            f"hook {plugin}.host_pre_launch contributed software_root_env that "
            f"is not an object",
        )
    baseline = sw_env.get("baseline") or {}
    loaded = sw_env.get("loaded") or {}
    if not (isinstance(baseline, dict) and isinstance(loaded, dict)):
        raise Refused(
            RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
            f"hook {plugin}.host_pre_launch software_root_env.baseline/.loaded "
            f"must both be objects",
        )
    # Coerce + validate to str→str (derive splits values on os.pathsep). A
    # non-string key/value is malformed; refuse rather than crash in derive.
    for label, d in (("baseline", baseline), ("loaded", loaded)):
        for k, v in d.items():
            if not (isinstance(k, str) and isinstance(v, str)):
                raise Refused(
                    RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
                    f"hook {plugin}.host_pre_launch software_root_env.{label} "
                    f"entry {k!r}={v!r} is not string/string",
                )
    # Nothing to derive if the hook emitted no loaded env (e.g. no modules).
    if not loaded:
        return []

    # (1) cap-grant gate — FAIL CLOSED. Mirror the pre_session bind-envelope
    # discipline (#125): if we cannot load the contributing plugin's manifest,
    # refuse rather than honor an unverifiable contribution.
    inst = inst_by_name.get(plugin)
    if inst is None:
        raise Refused(
            RefusalCategory.PLUGIN_CONTRIBUTION_OUT_OF_ENVELOPE,
            f"hook {plugin}.host_pre_launch contributed software_root_env but "
            f"plugin {plugin!r} is not in the installed plugin list (cannot "
            f"verify it holds caps.modules_software_roots). Refusing fail-closed.",
        )
    try:
        man = load_manifest(inst.plugin_dir)
    except Refused as _exc:
        raise Refused(
            RefusalCategory.PLUGIN_CONTRIBUTION_OUT_OF_ENVELOPE,
            f"hook {plugin}.host_pre_launch contributed software_root_env but "
            f"its manifest at {inst.plugin_dir} failed to load: {_exc}. Refusing "
            f"fail-closed (cannot verify the capability grant).",
        ) from _exc
    if "caps.modules_software_roots" not in man.capabilities:
        raise Refused(
            RefusalCategory.CAPABILITY_DENIED_BY_POLICY,
            f"hook {plugin}.host_pre_launch contributed software_root_env (module "
            f"software-root binds) but plugin {plugin!r} does not declare the "
            f"caps.modules_software_roots capability in its manifest. Declare it "
            f"to contribute these binds (it is gated by the root-owned SitePolicy "
            f"mounts.cluster_software_roots ceiling).",
        )

    # (2)+(3) derive under the SitePolicy ceiling. Empty ceiling → [] (OFF).
    ceiling = list(effective_policy.mounts.cluster_software_roots)
    try:
        result = derive_software_root_binds_verbose(baseline, loaded, ceiling)
    except SoftwareRootBindError as exc:
        # LOUD failure (never silent truncation): a dropped root = an
        # unreachable binary with no signal. Refuse the session.
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"hook {plugin}.host_pre_launch software-root derivation refused: "
            f"{exc}",
        ) from exc

    # Adversarial-review T1: when `module load` ADDED software dirs but NONE
    # were bound (empty/excluding site ceiling), the feature is silently OFF —
    # the agent will not find the module software. WARN loudly (the user
    # otherwise sees the module env "delivered" and assumes it works).
    if result.is_off:
        import sys as _sys
        ceiling_desc = ceiling or "(empty — mounts.cluster_software_roots unset)"
        _sys.stderr.write(
            f"[botainer] WARNING: hpc-modules loaded software but NONE of its "
            f"dirs are within the site policy ceiling {ceiling_desc}, so they "
            f"are NOT bound into the container — module tools will be "
            f"UNREACHABLE by name.\n"
            f"  dropped: {', '.join(f'{d} ({why})' for d, why in result.dropped[:6])}"
            f"{' …' if len(result.dropped) > 6 else ''}\n"
            f"  An admin must add the software root(s) to "
            f"mounts.cluster_software_roots in the root-owned "
            f"/etc/botainer/policy.yaml. (See docs/CAPABILITY-SURFACE.md §4h.)\n"
        )

    binds: list = []
    for d in result.binds:
        binds.append(
            Bind(
                source=d["source"],
                target=d["target"],
                mode=BindMode.RO,
                provenance=Provenance.PLUGIN,
                provenance_detail=(
                    f"plugin {plugin} host_pre_launch software-root bind (#160)"
                ),
                agent_rendering=AgentRendering.SHOWN,
                self_test="SELFTEST_MODULE_SOFTWARE_BIND",
            )
        )
    return binds



def _agent_writable_bind_sources(spec) -> list[Path]:
    """Host paths this session binds WRITABLE into the container.

    Used to enforce the containment invariant in
    `botainer/plugins/hooks.py::refuse_agent_writable_hook`: code the HOST
    executes must never live somewhere the CAGED AGENT can write. Anything
    rendered without `:ro` counts — RW plus the socket/FIFO modes, which also
    render writable (see BindMode's note in core/spec.py).
    """
    out: list[Path] = []
    for b in getattr(getattr(spec, "mount_plan", None), "binds", []) or []:
        mode = getattr(b, "mode", None)
        mode_val = getattr(mode, "value", mode)
        if mode_val in ("rw", "unix-socket", "fifo"):
            src = getattr(b, "source", None)
            if src:
                out.append(Path(src))
    return out


def run_host_pre_launch_hooks(
    spec: SessionSpec,
    *,
    hook_env_extra: dict[str, str] | None = None,
    module_env_delivery: str = "auto",
) -> SessionSpec:
    """Execute all `host_pre_launch` hooks declared by enabled plugins.

    hook_env_extra: extra vars merged into EVERY host_pre_launch hook's
    subprocess env. Needed because `run_hook` scrubs the host env down to
    `_HOOK_ENV_ALLOWLIST` — a var like `BOTAINER_MODBINDS_DERIVE_ONLY`
    (B3) is NOT on that allowlist, so it must be threaded through the
    explicit env dict, not `os.environ`. Used by the HPC compose-at-submit
    path to run the hpc-modules discovery in derive-only mode on the login
    node.

    module_env_delivery: `"inner-prepend"` FORCES the Step-B split of a
    module env_file into scalar-only (`--env-file`) + PATH-list PREPEND
    pairs (adapter trampoline), regardless of the `BOTAINER_USE_INNER_PREPEND`
    env opt-in. On the HPC sbatch path PREPEND-in-container is the contracted
    semantic (there is no in-container botainer to re-apply the env), so the
    clobber refusal in `_handle_module_env_path_clobber` must never fire.
    `"auto"` preserves the env-var opt-in for the direct docker/apptainer path.

    These run on the host BEFORE the container starts and can return a
    contribution that the launcher merges into the SessionSpec. Currently
    the supported contribution is `env_file`: a host path the adapter
    passes to `--env-file`. Used by hpc-modules to inject the captured
    `module load` env into the container.

    Hook contract (stdout JSON):

        {
          "version": "plugin-contribution-v1",
          "kind": "host_pre_launch",
          "env_file": "/host/path/to/env-file"     # optional
        }

    #160: a host_pre_launch hook may ALSO contribute `software_root_env`
    (the raw post-purge `baseline` + post-load `loaded` env subsets that the
    hpc-modules hook emits). The hook is stdlib-only and emits RAW env; THIS
    trusted code calls `derive_software_root_binds` to compute the bounded,
    read-only identity binds for module-loaded software dirs, gated by the
    granting plugin's `caps.modules_software_roots` declaration + the
    root-owned SitePolicy ceiling `mounts.cluster_software_roots`. See
    botainer/hpc/module_binds.py, which documents all four guards.
    Full derivation and the SitePolicy ceiling: internal design note DN-002.

    Returns a new SessionSpec with any contributions merged in. If no
    hooks fire (or none contribute env_file / software-root binds), returns
    the spec unchanged.
    """
    from botainer.core.spec import Bind
    from botainer.plugins import hooks as plugin_hooks
    from botainer.plugins.manifest import load_manifest

    # #160: effective policy (the ceiling for module binds lives here) +
    # installed-plugin map (to verify the contributing plugin actually
    # declares caps.modules_software_roots before honoring its binds).
    # Cheap to recompute (~ms); both are only consumed by the software_root
    # path below but computed once up front for clarity.
    effective_policy = policy_module.intersect(
        policy_module.load_site_policy(), policy_module.load_user_policy()
    )
    inst_by_name = {inst.name: inst for inst in list_installed()}
    additional_binds: list[Bind] = []

    session_dir = Path(spec.state_dir) / "sessions" / spec.session_id
    record_path = session_dir / session_record.RECORD_FILENAME
    # Canonical state env. STATE_ROOT comes from the helper (honors
    # MY_BOTAINER); STATE_DIR is the per-project dir from spec.
    from botainer.state.dir import subprocess_state_env
    env = {
        **subprocess_state_env(spec.project_uuid),
        "BOTAINER_SESSION_RECORD_PATH": str(record_path),
        "BOTAINER_SESSION_ID": spec.session_id,
        "BOTAINER_PROJECT_ROOT": spec.project_root,
        "BOTAINER_STATE_DIR": spec.state_dir,  # override helper's value with spec's
        "BOTAINER_SESSION_SCRATCH": str(session_dir),
        "BOTAINER_RUNTIME": spec.runtime,
        # The auth profile the hooks must use. Sourced from the SPEC, never
        # inherited from the host env: run_hook scrubs the environment to an
        # allowlist, and widening that allowlist to carry a profile would make
        # every future host variable a candidate too. This is a composition
        # decision, so composition states it.
        "BOTAINER_PROFILE": spec.profile,
    }
    if hook_env_extra:
        env.update(hook_env_extra)
    new_env_files: list[str] = list(spec.env_files)
    new_path_prepends: list[tuple[str, str]] = list(spec.module_env_path_prepends)
    # Step B opt-in for the in-container PREPEND fix on the direct/docker flow.
    # When set, _handle_module_env_path_clobber's would-be refusal is replaced
    # by a split of the env_file into scalar-only + path-list prepends, and
    # the adapter wraps the entrypoint in a shell trampoline that PREPENDs
    # the path-list values at exec time. (#216; internal design note DN-005.)
    import os as _os
    _use_inner_prepend = (
        module_env_delivery == "inner-prepend"
        or _os.environ.get("BOTAINER_USE_INNER_PREPEND") == "1"
    )
    for hook in spec.hooks:
        if hook.when != "host_pre_launch":
            continue
        result = plugin_hooks.run_hook(
            plugin_name=hook.plugin,
            hook_when="host_pre_launch",
            script_path=Path(hook.script_path),
            env=env,
            agent_writable_roots=_agent_writable_bind_sources(spec),
            timeout_seconds=hook.timeout_seconds,
        )
        # #138: a successful hook's warnings used to be captured and dropped.
        plugin_hooks.surface_hook_stderr(result, hook.plugin, "host_pre_launch")
        contribution = result.parsed_contribution or {}
        # Task #294: previously this branch ONLY consumed env_file and
        # silently dropped any env / binds / command_append the hook
        # tried to contribute. Warn (don't fail) so plugin authors see
        # their contributions aren't reaching the spec; a full merge is
        # in-flight as a separate sub-task.
        for unsupported_key in ("env", "binds", "command_append", "sidecars"):
            if contribution.get(unsupported_key):
                import sys as _sys
                _sys.stderr.write(
                    f"[hook] {hook.plugin}.host_pre_launch contributed "
                    f"{unsupported_key!r} but this hook phase only consumes "
                    f"'env_file' at v0.1.0. Drop the field or emit it from "
                    f"pre_session instead.\n"
                )
        env_file = contribution.get("env_file")
        if env_file:
            ef_path = Path(env_file)
            if not ef_path.exists():
                raise Refused(
                    RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
                    f"hook {hook.plugin}.host_pre_launch returned env_file "
                    f"{env_file!r} that doesn't exist",
                )
            resolved = ef_path.resolve()
            # AUDIT (H1): the comment here previously PROMISED a
            # containment check ("must not escape the project state dir") that
            # was never implemented, and the file's contents were passed
            # verbatim to --env-file (crossing --cleanenv) with NO validation.
            # (1) Containment: the env_file must live under the session state
            # dir (the hook writes it to BOTAINER_SESSION_SCRATCH). A path
            # outside is refused — a hook cannot point --env-file at /etc/* or
            # a co-tenant's file.
            state_root = Path(spec.state_dir).resolve()
            if resolved != state_root and not resolved.is_relative_to(state_root):
                raise Refused(
                    RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
                    f"hook {hook.plugin}.host_pre_launch env_file {env_file!r} "
                    f"resolves to {resolved} which is outside the session state "
                    f"dir {state_root}; refusing (it would --env-file an "
                    f"arbitrary host file into the container).",
                )
            # (2) Contents: refuse execution-injection vars + credentials
            # (the chokepoint the hook's self-filtering must not be trusted
            # to enforce).
            _validate_host_env_file(
                resolved, plugin=hook.plugin,
                denylist=set(effective_policy.capabilities.env_var_denylist),
            )
            # Step B (#216, opt-in BOTAINER_USE_INNER_PREPEND=1): split the
            # env_file into scalar-only + path-list prepends so the adapter
            # can PREPEND path-list vars in-container via a shell trampoline
            # (instead of `--env-file` SET which clobbers). Only fires on
            # direct/docker flows (sbatch uses its own propagation channel).
            split_handled = False
            if _use_inner_prepend and spec.runtime in ("docker", "apptainer"):
                scalar_path, prepends = _split_module_env_for_inner_prepend(
                    resolved, Path(spec.state_dir)
                )
                if prepends:
                    new_path_prepends.extend(prepends)
                    if scalar_path is not None:
                        new_env_files.append(str(scalar_path))
                    # If scalar_path is None: env_file was path-list-only, so
                    # we don't add anything to new_env_files. The trampoline
                    # carries all the values.
                    split_handled = True
            if not split_handled:
                new_env_files.append(str(resolved))
            # Re-audit round 3 (#1, HIGH — HPC-parity gap): this env_file is
            # delivered to the docker/direct-apptainer adapter via `--env-file`,
            # which under `--cleanenv` SETS (replaces) each var — so PATH-list
            # vars (PATH/LD_LIBRARY_PATH/…) here CLOBBER the container's own
            # values, dropping the agent binary's dir (e.g. /opt/conda/bin) and
            # breaking `exec <agent>`. The sbatch path is unaffected (it PREPENDS
            # in-container via _apply_module_env_file).
            # (Step A): default behavior is REFUSE, replacing the
            # previous warn-and-continue (which was silent-on-paper /
            # loud-on-launch). BOTAINER_ALLOW_PATH_CLOBBER=1 reproduces the
            # old behavior as an emergency escape valve.
            # Step B (#216): when BOTAINER_USE_INNER_PREPEND=1
            # already split the env_file (above), the clobber risk is
            # eliminated — skip the refusal check. Otherwise the original
            # env_file is unchanged in new_env_files and the handler is
            # the right gate.
            if not split_handled:
                _handle_module_env_path_clobber(
                    resolved, spec.runtime, hook.plugin
                )
        # #160: module software-root binds (separate, structured channel —
        # NOT the raw `binds` key, which stays unsupported above). Derived in
        # trusted code from the hook's raw baseline/loaded env, gated by the
        # plugin's caps.modules_software_roots + the SitePolicy ceiling.
        additional_binds.extend(
            _software_root_binds_from_contribution(
                contribution,
                plugin=hook.plugin,
                inst_by_name=inst_by_name,
                effective_policy=effective_policy,
            )
        )

    # caps.modules_inner_load: bind the Lmod install tree +
    # MODULEPATH dirs into the container RO, and set LMOD_PKG/LMOD_DIR/
    # LMOD_CMD/MODULEPATH/BASH_ENV so the agent can run `module load foo`
    # INSIDE the container (dynamic; batch-job friendly). Independent of
    # any host `module load` capture — this cap is precisely the "no host
    # module load required" mode. Gated by:
    #   (a) at least one enabled plugin declaring caps.modules_inner_load
    #       in its manifest;
    #   (b) root-owned SitePolicy mounts.cluster_lmod_root non-empty.
    # Same SITE-ONLY trust discipline as cluster_software_roots.
    inner_binds, inner_env = _compute_inner_load_contribution(
        spec=spec, effective_policy=effective_policy, inst_by_name=inst_by_name,
    )
    additional_binds.extend(inner_binds)

    env_files_changed = list(spec.env_files) != new_env_files
    path_prepends_changed = (
        list(spec.module_env_path_prepends) != new_path_prepends
    )
    env_changed = bool(inner_env)
    if not env_files_changed and not additional_binds and not path_prepends_changed and not env_changed:
        return spec

    updates: dict = {}
    if env_files_changed:
        updates["env_files"] = tuple(new_env_files)
    if path_prepends_changed:
        updates["module_env_path_prepends"] = tuple(new_path_prepends)
    if additional_binds:
        new_mount_plan = spec.mount_plan.with_many(additional_binds)
        # Independent backstop: revalidate the augmented plan against the
        # source/target denylists, normalization, null-bind rule, and the
        # policy allowlist — exactly as run_pre_session_hooks does for plugin
        # bind contributions. derive_software_root_binds already bounded the
        # roots; this is defense-in-depth, not the primary control.
        validate_mount_plan(new_mount_plan, policy=effective_policy)
        updates["mount_plan"] = new_mount_plan
    if env_changed:
        # Launcher-owned invariants (not user config, not plugin contribution):
        # LMOD_PKG/LMOD_DIR/LMOD_CMD/MODULEPATH/BASH_ENV all land in spec.env
        # directly. The env_var_denylist gates user-supplied and plugin-hook-
        # supplied vars; here the source is trusted (root-owned SitePolicy
        # ceiling + trusted composition code), so BASH_ENV (normally a denylist
        # entry) is permitted with the value fixed to the bound Lmod init/bash
        # script — no attacker-controlled input reaches it.
        merged_env = dict(spec.env.values)
        for k, v in inner_env.items():
            if k in merged_env and merged_env[k] != v:
                # Conflict resolution: user-set env wins. If the user explicitly
                # set MODULEPATH/BASH_ENV in cfg.env, they've opted out of the
                # inner-load defaults — don't clobber their choice.
                continue
            merged_env[k] = v
        updates["env"] = spec.env.model_copy(update={"values": merged_env})
    return spec.model_copy(update=updates)


def _compute_inner_load_contribution(
    *, spec, effective_policy, inst_by_name,
) -> tuple[list[Bind], dict[str, str]]:
    """Compute the (binds, env) contribution for caps.modules_inner_load.

    Returns ([], {}) if the feature is OFF (no enabled plugin declares the
    cap, or SitePolicy has empty cluster_lmod_root). Otherwise returns:
      - RO binds for the Lmod install tree + each MODULEPATH root, using
        identity (source == target) binds so the paths are visible at the
        same location inside the container that they had on the host (a
        module `MODULEPATH=/apps/modulefiles` value carries the same
        meaning);
      - env vars LMOD_PKG / LMOD_DIR / LMOD_CMD / MODULEPATH / BASH_ENV
        so the agent's shell can source the module machinery.

    Trust: BOTH ceilings (cluster_lmod_root + cluster_modulepath_roots) are
    SITE-ONLY (root-owned /etc/botainer/policy.yaml, verbatim from
    policies[0] in intersect — cannot be widened by user policy). At least
    one enabled plugin must declare the cap in its manifest.
    """
    # Normalize trailing slashes so a policy value "/apps/lmod/" matches the
    # bind-target normalization validate_mount_plan expects (Flow 2's
    # _reject_unsafe_software_root is also slash-tolerant). Without this, a
    # trailing slash in the root-owned policy refuses Flow 1 while Flow 2
    # tolerates it — a cross-flow asymmetry (audit LOW).
    def _norm(p: str) -> str:
        return p.rstrip("/") if p not in ("", "/") else p
    lmod_root = _norm(effective_policy.mounts.cluster_lmod_root)
    modulepath_roots = [_norm(p) for p in effective_policy.mounts.cluster_modulepath_roots]
    if not lmod_root:
        return [], {}
    # Check any enabled plugin declares the cap.
    declaring = _find_inner_load_declaring_plugins(
        spec=spec, inst_by_name=inst_by_name,
    )
    if not declaring:
        return [], {}
    # Shared semantic source guard (parity with Flow 2 + the inner disclosure —
    # single helper, no mirror drift). Refuses an /etc-class or sensitive-home
    # or shallow-system-root ceiling value fail-closed, BEFORE the binds are
    # built. validate_mount_plan is still the independent backstop below, but
    # this makes the refusal explicit + catches the shallow-/usr shadow case
    # that validate_mount_plan's source denylist alone would miss.
    from botainer.hpc.module_binds import is_unsafe_module_tree_source
    for _p in (lmod_root, *modulepath_roots):
        if not _p:
            continue
        _unsafe, _why = is_unsafe_module_tree_source(_p)
        if _unsafe:
            raise Refused(
                RefusalCategory.MOUNT_SOURCE_DENIED,
                f"caps.modules_inner_load: {_why}. Set mounts.cluster_lmod_root "
                f"/ cluster_modulepath_roots to a deep admin-managed tree "
                f"(e.g. /apps/lmod/lmod, /usr/share/lmod/lmod).",
            )

    # RO identity binds: /apps/lmod → /apps/lmod (same source + target).
    # Provenance = PLUGIN, matching #160's software-root binds
    # (_software_root_binds_from_contribution). This is load-bearing, not
    # cosmetic: validate_mount_plan (the backstop run right after this in
    # run_host_pre_launch_hooks) exempts ONLY Provenance.PLUGIN from the
    # extra_targets_allowlist check — a SITE_POLICY-provenance bind whose
    # target (/apps/lmod) isn't in the allowlist would be REFUSED, breaking
    # Flow 1 entirely. The bind is plugin-contributed (hpc-modules holds the
    # cap) and bounded by the SITE-ONLY ceiling, so PLUGIN is also bounded.
    from botainer.core.spec import AgentRendering as _AR
    from botainer.core.spec import Provenance as _Provenance
    # Dedupe against targets ALREADY in the spec's mount plan (the base binds +
    # any #160 software-root binds added earlier in the same hook run). A
    # MODULEPATH root that equals an existing bind target would make
    # validate_mount_plan's conflict detector refuse the WHOLE launch — Flow 2
    # dedupes against software_root_binds, so Flow 1 must too (mirror-drift
    # parity, audit MEDIUM). We compare on the normalized target.
    seen_targets = {b.target for b in spec.mount_plan.binds}
    binds: list[Bind] = []
    if lmod_root not in seen_targets:
        binds.append(Bind(
            source=lmod_root, target=lmod_root, mode=BindMode.RO,
            provenance=_Provenance.PLUGIN,
            provenance_detail="caps.modules_inner_load Lmod tree (SitePolicy ceiling)",
            agent_rendering=_AR.SHOWN,
            self_test="SELFTEST_MODULE_INNER_LOAD",
        ))
        seen_targets.add(lmod_root)
    for mp in modulepath_roots:
        if not mp or mp in seen_targets:
            # Skip empty entries + any dup (of lmod_root, a prior modulepath,
            # an existing base/#160 bind) — a duplicate target refuses launch.
            continue
        binds.append(Bind(
            source=mp, target=mp, mode=BindMode.RO,
            provenance=_Provenance.PLUGIN,
            provenance_detail="caps.modules_inner_load MODULEPATH root (SitePolicy ceiling)",
            agent_rendering=_AR.SHOWN,
            self_test="SELFTEST_MODULE_INNER_LOAD",
        ))
        seen_targets.add(mp)

    env = {
        "LMOD_PKG": lmod_root,
        "LMOD_DIR": f"{lmod_root}/libexec",
        "LMOD_CMD": f"{lmod_root}/libexec/lmod",
        "MODULEPATH": ":".join(modulepath_roots),
        # BASH_ENV sources init/bash for every `bash -c` subshell — so
        # claude's Bash tool and sbatch job scripts get `module` defined.
        "BASH_ENV": f"{lmod_root}/init/bash",
    }
    return binds, env


def _find_inner_load_declaring_plugins(
    *, spec, inst_by_name,
) -> list[str]:
    """Return the names of enabled plugins that declare
    caps.modules_inner_load in their manifest. Empty list → cap not
    granted to any plugin in this session."""
    from botainer.plugins.manifest import load_manifest
    declaring: list[str] = []
    for plugin_name in spec.plugins_enabled:
        inst = inst_by_name.get(plugin_name)
        if inst is None:
            continue
        try:
            man = load_manifest(inst.plugin_dir)
        except Exception:
            continue
        if "caps.modules_inner_load" in man.capabilities:
            declaring.append(plugin_name)
    return declaring


def run_pre_session_hooks(spec: SessionSpec) -> SessionSpec:
    """Execute all `pre_session` hooks declared by enabled plugins.

    Hooks run on the host as the user (codex HIGH 4). Two responsibilities:

    1. Side-effecty preparation that doesn't change the spec — e.g.
       a plugin recording derived state into spec.json for later steps.
    2. Contributions that ARE merged back into the SessionSpec — e.g.
       agent-claude-proxy returns env vars and binds the launcher must
       inject so the container talks to the proxy rather than the real
       Anthropic API.

    A hook's contribution is the JSON it prints on stdout, parsed into
    plugin_contribution. Recognized keys:
      env: {NAME: VALUE}    — env vars added to spec.env (denylist still applies)
      binds: [{source, target, mode}]  — mounts added to spec.mount_plan

    SECURITY: env contributions are re-checked against the
    `env_var_denylist` from policy; bind contributions are re-validated
    against `validate_mount_plan` and the contributing plugin's
    declared envelope. A hook CANNOT bypass these by returning a JSON
    contribution. Sharp-edges review HIGH-1/HIGH-2.

    Returns a (possibly new) SessionSpec with contributions merged.
    Called only by `start` (not by `dry-run` / `inspect`).
    """
    from botainer.core import credential_leak_check
    from botainer.core.spec import Bind, BindMode, EnvSpec
    from botainer.mount_plan.validation import validate as validate_mount_plan
    from botainer.plugins import hooks as plugin_hooks
    from botainer.plugins.manifest import load_manifest

    # We need the effective policy here to re-check env denylist.
    # Recomputing is cheap (~ms) and the spec doesn't store it.
    effective_policy = policy_module.intersect(
        policy_module.load_site_policy(), policy_module.load_user_policy()
    )
    denylist = set(effective_policy.capabilities.env_var_denylist)

    session_dir = Path(spec.state_dir) / "sessions" / spec.session_id
    record_path = session_dir / session_record.RECORD_FILENAME
    # Canonical state env via the single helper.
    from botainer.state.dir import subprocess_state_env
    env_vars = {
        **subprocess_state_env(spec.project_uuid),
        "BOTAINER_SESSION_RECORD_PATH": str(record_path),
        "BOTAINER_SESSION_ID": spec.session_id,
        "BOTAINER_PROJECT_ROOT": spec.project_root,
        "BOTAINER_STATE_DIR": spec.state_dir,  # override with spec's exact path
        "BOTAINER_SESSION_SCRATCH": str(session_dir),
        "BOTAINER_RUNTIME": spec.runtime,
        "BOTAINER_PROFILE": spec.profile,
        # Tells plugins about peer plugins so they can implement mutual
        # exclusion (e.g. agent-claude skips its credential bind when
        # agent-claude-proxy is also enabled).
        "BOTAINER_PLUGINS_ENABLED": ",".join(spec.plugins_enabled),
    }
    merged_env = dict(spec.env.values)
    additional_binds: list[Bind] = []
    has_changes = False
    # Map plugin name → installed plugin dir, so we can load each
    # contributing plugin's manifest envelope for bind validation.
    inst_by_name = {inst.name: inst for inst in list_installed()}
    for hook in spec.hooks:
        if hook.when != "pre_session":
            continue
        result = plugin_hooks.run_hook(
            plugin_name=hook.plugin,
            hook_when="pre_session",
            script_path=Path(hook.script_path),
            env=env_vars,
            agent_writable_roots=_agent_writable_bind_sources(spec),
            timeout_seconds=hook.timeout_seconds,
        )
        # #138: a successful hook's warnings used to be captured and dropped.
        plugin_hooks.surface_hook_stderr(result, hook.plugin, "pre_session")
        contribution = result.parsed_contribution or {}
        # Task #290: REFUSE command_append from hooks at v0.1. The feature
        # is documented (FEATURE-PARTITION-LOCKED.md A6) as needing an
        # envelope check before it can land. Without that, a plugin could
        # append '--dangerously-skip-permissions' or similar to the agent
        # entrypoint. Silently dropping these contributions is wrong —
        # plugin authors won't know their feature didn't ship. Refuse loudly.
        if contribution.get("command_append"):
            raise Refused(
                RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
                f"hook {hook.plugin}.pre_session contributed 'command_append' "
                f"({contribution['command_append']!r}). This contribution kind "
                f"is NOT supported at v0.1.0 because no per-arg envelope check "
                f"exists yet (#290). A plugin appending --dangerously-skip-"
                f"permissions or similar would bypass the agent's security "
                f"flags. Remove the contribution; track via FEATURE-PARTITION-"
                f"LOCKED.md A6 for v0.2.",
            )
        # ── env contributions ──
        ctx_env = contribution.get("env") or {}
        if isinstance(ctx_env, dict):
            # Validate ALL keys for shape (EnvSpec rules) + denylist
            # BEFORE merging, so a single bad key refuses the whole
            # contribution rather than partial-merging.
            for k, v in ctx_env.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise Refused(
                        RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
                        f"hook {hook.plugin}.pre_session env entry "
                        f"{k!r}={v!r} not string/string",
                    )
                if k in denylist:
                    raise Refused(
                        RefusalCategory.ENV_VAR_DENIED,
                        f"hook {hook.plugin}.pre_session tried to set "
                        f"denylisted env var {k!r}. The denylist exists to "
                        f"prevent dynamic-loader hijacks (LD_PRELOAD, "
                        f"PYTHONPATH, etc.); plugins cannot bypass it.",
                    )
                # Audit (plugin-envelope sweep, MEDIUM): the
                # pre_session env channel gated ONLY on env_var_denylist + the
                # credential-leak check — STRICTLY WEAKER than the sibling
                # host_pre_launch env_file gate (_validate_host_env_text) for the
                # SAME threat. Force-code-load / resolver-hijack vars the code
                # itself recognizes elsewhere (GCONV_PATH, GLIBC_TUNABLES,
                # HOSTALIASES, RES_OPTIONS, DYLD_*, BASH_ENV, GIT_SSH_COMMAND, …)
                # passed here and crossed `--cleanenv` as `--env`, so a
                # compromised/malicious plugin could set HOSTALIASES (redirect
                # api.anthropic.com) or GCONV_PATH (loader hijack). Apply the SAME
                # exec-injection set here (parity). NOTE: _BOTAINER_MANAGED_ROUTES
                # (CLAUDE_CONFIG_DIR, PIP_TARGET, …) is deliberately NOT
                # blanket-refused here — agent-claude-shared legitimately sets
                # CLAUDE_CONFIG_DIR via this channel; the existing conflict guard
                # below (a second plugin can't overwrite an agent plugin's value)
                # bounds it. Tightening managed-routes to agent-plugin-only is a
                # tracked follow-up.
                if not _ENV_NAME_RE.match(k):
                    raise Refused(
                        RefusalCategory.ENV_VAR_DENIED,
                        f"hook {hook.plugin}.pre_session env var {k!r} is not a "
                        f"plain identifier (e.g. a BASH_FUNC_* shell-function "
                        f"import); refused.",
                    )
                if k in _HOST_ENVFILE_EXEC_INJECTION:
                    raise Refused(
                        RefusalCategory.ENV_VAR_DENIED,
                        f"hook {hook.plugin}.pre_session tried to set {k!r}, a "
                        f"force-code-load / loader-or-resolver-hijack env var "
                        f"(same class blocked on the module env_file channel). "
                        f"Plugins cannot inject code execution or redirect name "
                        f"resolution via env.",
                    )
            # Credential-leak check (same patterns as cfg.env).
            credential_leak_check.check_env_for_leaks(
                ctx_env, source=f"hook {hook.plugin}.pre_session contribution",
            )
            for k, v in ctx_env.items():
                if k in merged_env and merged_env[k] != v:
                    raise Refused(
                        RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
                        f"hook {hook.plugin}.pre_session env var {k!r} "
                        f"conflicts with existing spec.env",
                    )
                merged_env[k] = v
                has_changes = True
        # ── bind contributions ──
        ctx_binds = contribution.get("binds") or []
        # Only enter envelope-load (which fails closed) when the hook
        # ACTUALLY contributed a bind. No binds → nothing to validate.
        if isinstance(ctx_binds, list) and ctx_binds:
            # Task #125: FAIL CLOSED if manifest doesn't load or plugin
            # isn't installed. Previous behavior was envelope_loaded=False
            # → envelope check skipped → plugin contributions accepted
            # unbounded (only validate_mount_plan caught dangerous targets).
            # A corrupt-manifest plugin got MORE privilege than a clean one.
            # Now: if we cannot enumerate the declared prefixes, refuse
            # the bind contribution outright.
            envelope_prefixes: list[str] = []
            inst = inst_by_name.get(hook.plugin)
            if inst is None:
                raise Refused(
                    RefusalCategory.PLUGIN_CONTRIBUTION_OUT_OF_ENVELOPE,
                    f"hook {hook.plugin}.pre_session contributed binds but "
                    f"plugin {hook.plugin!r} is not in the installed plugin "
                    f"list (cannot load envelope). Refusing fail-closed.",
                )
            try:
                man = load_manifest(inst.plugin_dir)
            except Refused as _exc:
                raise Refused(
                    RefusalCategory.PLUGIN_CONTRIBUTION_OUT_OF_ENVELOPE,
                    f"hook {hook.plugin}.pre_session contributed binds but "
                    f"manifest at {inst.plugin_dir} failed to load: {_exc}. "
                    f"Refusing fail-closed (cannot verify envelope).",
                ) from _exc
            envelope_prefixes = list(man.contributes.mount_target_prefixes)
            for b in ctx_binds:
                if not isinstance(b, dict):
                    raise Refused(
                        RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
                        f"hook {hook.plugin}.pre_session bind entry "
                        f"is not an object",
                    )
                source = b.get("source")
                target = b.get("target")
                mode = b.get("mode")
                if not (isinstance(source, str) and isinstance(target, str)):
                    raise Refused(
                        RefusalCategory.PLUGIN_CONTRIBUTION_MALFORMED,
                        f"hook {hook.plugin}.pre_session bind missing "
                        f"source / target",
                    )
                # Reject obvious path-traversal / shell-special chars in
                # source/target before they reach the MountPlan validator.
                for label, val in (("source", source), ("target", target)):
                    if (
                        "\x00" in val or "\n" in val
                        or ".." in Path(val).parts
                    ):
                        raise Refused(
                            RefusalCategory.MOUNT_PATH_NOT_NORMALIZED,
                            f"hook {hook.plugin}.pre_session bind "
                            f"{label}={val!r} not a valid path",
                        )
                # Task #125: envelope_prefixes is ALWAYS loaded by the
                # fail-closed block above; no more 'if envelope_loaded'
                # bypass. Empty prefixes list refuses every bind (a plugin
                # that declares no prefixes cannot contribute binds).
                #
                # T0-1: trailing-slash normalization. A plugin that
                # binds the prefix DIRECTORY ITSELF (target `/home/agent/.claude`)
                # against a declared prefix WITH a trailing slash
                # (`/home/agent/.claude/`) previously failed BOTH arms —
                # `target == p` (slash differs) and `startswith(p+"/")` (target is
                # shorter) — so the DEFAULT agent-claude isolated launch was
                # refused by its own envelope. Compare with trailing slashes
                # stripped on BOTH sides so the exact-directory case matches,
                # WITHOUT loosening containment: a sibling like
                # `/home/agent/.claudeX` still fails (rstrip-equal is False and it
                # does not start with `<prefix>/`). This strictly adds the
                # exact-dir match to the old behavior.
                if not any(
                    target.rstrip("/") == p.rstrip("/")
                    or target.startswith(p.rstrip("/") + "/")
                    for p in envelope_prefixes
                ):
                    raise Refused(
                        RefusalCategory.PLUGIN_CONTRIBUTION_OUT_OF_ENVELOPE,
                        f"hook {hook.plugin}.pre_session contributed bind "
                        f"target {target!r} not within declared "
                        f"mount_target_prefixes "
                        f"{envelope_prefixes or '(empty — refuse all binds)'}. "
                        f"Declare the prefix in the plugin manifest's "
                        f"contributes.mount_target_prefixes to allow this.",
                    )
                # Map plugin-supplied mode to BindMode. Plugins use
                # "ro"/"rw"/"unix-socket"; spec uses BindMode enum.
                # T1-1: keep "unix-socket" AS BindMode.UNIX_SOCKET,
                # NOT collapsed to RW. The renderer emits the identical
                # `--bind src:dest` / `--mount type=bind` for UNIX_SOCKET as for
                # RW (mount_plan/render.py:26,52 — no `:ro`), so the socket file
                # is bind-mounted exactly as before on docker/direct. But the mode
                # was being lost, which made composition._refuse_cross_node_binds
                # (which keys on UNIX_SOCKET/FIFO) DEAD for plugin sockets — a
                # login-node socket bound into a compute-node container on the
                # HPC sbatch path (wolfram-sidecar, any auth proxy) silently
                # baked a dead cross-node rendezvous instead of being refused.
                if mode == "rw":
                    bm = BindMode.RW
                elif mode == "unix-socket":
                    bm = BindMode.UNIX_SOCKET
                else:
                    bm = BindMode.RO
                additional_binds.append(
                    Bind(
                        source=source,
                        target=target,
                        mode=bm,
                        provenance=Provenance.PLUGIN,
                        provenance_detail=(
                            f"plugin {hook.plugin} pre_session contribution"
                        ),
                        agent_rendering=AgentRendering.SHOWN,
                        self_test="SELFTEST_EXTRA_BIND",
                    )
                )
                has_changes = True
    if not has_changes:
        return spec
    new_mount_plan = (
        spec.mount_plan.with_many(additional_binds)
        if additional_binds
        else spec.mount_plan
    )
    # Re-validate the augmented mount plan ONLY when we actually added
    # binds. validate_mount_plan checks source/target denylists,
    # conflicts, normalization, the null-bind rule, and the policy
    # allowlist. If a hook contributes a bind that would let the agent
    # see /var/run/docker.sock or /etc/passwd, this raises Refused.
    # (The original spec.mount_plan was already validated at compose,
    # so revalidating an unchanged plan is wasteful.)
    if additional_binds:
        validate_mount_plan(new_mount_plan, policy=effective_policy)
    # THREAT-MODEL audit (BS-1): keep credential-shaped values OFF the
    # command line. Both adapters render spec.env.values as `--env K=V` / `-e K=V`,
    # and on a shared HPC login/compute node /proc/<pid>/cmdline is world-readable
    # — so a co-tenant could read the codex broker's sentinel, which is not merely
    # the container's OPENAI_API_KEY but the daemon's ONLY TCP access token, and
    # spend the user's subscription for the life of the session.
    #
    # The project already knows this rule and states it in
    # plugins/browser/hooks/start_viewer.py:12 — "NEVER via --env, which is
    # visible in `ps` on a shared apptainer node". The viewer obeyed it; the
    # brokers did not. Enforcing it HERE rather than per-plugin makes it a
    # property: no plugin author has to remember, and a new plugin that
    # contributes a secret gets the safe path automatically.
    #
    # Both adapters already consume env_files, and apply them BEFORE the explicit
    # flags, so file-delivered values keep identical semantics.
    merged_env, secret_env = _split_secret_env(merged_env)
    new_env_files = list(spec.env_files)
    if secret_env:
        new_env_files.append(str(_write_secret_env_file(spec, secret_env)))
    return spec.model_copy(
        update={
            "env": EnvSpec(values=merged_env),
            "env_files": tuple(new_env_files),
            "mount_plan": new_mount_plan,
        }
    )


def _split_secret_env(env: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Partition env into (argv-safe, secret). See the BS-1 note above.

    Uses the same credential-shaped-name detector the leak guard uses, so the two
    stay consistent: anything the guard would call a credential is also something
    we refuse to put in argv. Broker SENTINELS are deliberately included even
    though the leak guard exempts them — the sentinel is provably fake as a
    provider credential, but it IS the broker's access token.
    """
    from botainer.core.credential_leak_check import detect_credential_env_keys

    secret_keys = set(detect_credential_env_keys(env))
    # detect_credential_env_keys allowlists broker sentinels (they carry no real
    # secret). They still authenticate to the broker, so route them by file too.
    for k in env:
        up = k.upper()
        if any(t in up for t in ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")):
            secret_keys.add(k)
    safe = {k: v for k, v in env.items() if k not in secret_keys}
    secret = {k: v for k, v in env.items() if k in secret_keys}
    return safe, secret


def _write_secret_env_file(spec: SessionSpec, secret_env: dict[str, str]) -> Path:
    """Write secret env to a 0600 file in the host-private session dir.

    Format is `KEY=VALUE` per line, which is what both `docker --env-file` and
    `apptainer --env-file` consume. Values are written verbatim (neither runtime
    shell-interprets an env-file value); a value containing a newline would break
    the format, so those are refused rather than silently truncated.
    """
    import os as _os

    session_dir = Path(spec.state_dir) / "sessions" / spec.session_id
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = session_dir / "secret-env"
    lines = []
    for k in sorted(secret_env):
        v = secret_env[k]
        if "\n" in v or "\r" in v:
            raise Refused(
                RefusalCategory.CAPABILITY_VALUE_INVALID,
                f"env {k!r} contains a newline; it cannot be delivered via an "
                f"--env-file and must not be placed on the command line.",
            )
        lines.append(f"{k}={v}")
    fd = _os.open(str(path),
                  _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC | _os.O_NOFOLLOW, 0o600)
    with _os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    _os.chmod(path, 0o600)
    return path


def render_agent_files(spec: SessionSpec) -> None:
    """Re-render AGENT_ACCESS.txt + AGENT_HINTS.md from the (post-hook) spec.

    Audit T10: compose_session writes these from the COMPOSE-time spec, before
    host_pre_launch/pre_session hooks add their binds/env — so the agent was
    told a STALE, UNDERSTATED view (missing the credential bind, the git
    overlay, and the #160 module software-root binds). The launcher calls this
    AFTER the hooks so the files reflect what the agent actually has. The bind
    targets (/workspace/.botainer/AGENT_*) already point at these paths; we only
    refresh their CONTENT. Best-effort: a write failure is logged, not fatal
    (the agent still launches; it just reads slightly stale hints).
    """
    session_dir = Path(spec.state_dir) / "sessions" / spec.session_id
    try:
        (session_dir / "AGENT_ACCESS.txt").write_text(
            access_renderer.render(spec), encoding="utf-8"
        )
        (session_dir / "AGENT_HINTS.md").write_text(
            agent_hints_renderer.render(spec), encoding="utf-8"
        )
    except OSError as exc:
        import sys as _sys
        _sys.stderr.write(
            f"[botainer] could not refresh agent files post-hooks: {exc} "
            f"(the agent may see a compose-time view)\n"
        )
    # Re-audit round 3 (#12/#16): re-serialize the session record (spec.json)
    # from the POST-hook spec too, so the persisted provenance reflects the
    # binds/env the container actually ran with (the compose-time write at
    # session_record.write above predates the hooks). Runtime handle +
    # timestamps are preserved. Best-effort: a write failure is logged, not fatal.
    try:
        session_record.reserialize_spec(session_dir, spec)
    except OSError as exc:
        import sys as _sys
        _sys.stderr.write(
            f"[botainer] could not refresh session record post-hooks: {exc} "
            f"(spec.json may show a compose-time view)\n"
        )


# Node-local filesystem prefixes: a bind SOURCE under one of these on the
# LOGIN node does not exist (or is a different filesystem) on the COMPUTE node,
# so the compose-at-submit shared-FS assumption breaks and apptainer FATALs on a
# missing bind source after a queue wait. This is a best-effort heuristic (a
# site could make /tmp shared, or use an exotic node-local mount we don't list)
# — it WARNS, it does not refuse, because a false refusal would block a legit
# submit. The hard refusal is reserved for socket/FIFO rendezvous (below), which
# are unambiguously node-local regardless of filesystem.
_NODE_LOCAL_SOURCE_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/shm/", "/run/")

# Hosts that resolve to the COMPOSING machine and therefore cannot be reached
# from a different node. Used by _refuse_cross_node_binds check 3 to catch a
# host-local rendezvous that carries no BIND at all (e.g. a TCP credential
# broker) — the bind-shaped checks above are blind to it. A loopback address is
# host-local by definition, which is what makes this transport-agnostic rather
# than another plugin-name list.
_LOOPBACK_RENDEZVOUS_HOSTS = (
    "127.0.0.1",
    "localhost",
    "[::1]",
    "host.docker.internal",
)


def _refuse_cross_node_binds(spec: SessionSpec) -> None:
    """Enforce the compose-at-submit cross-node contract on the HPC sbatch path.

    Two checks, because "node-local" has two distinct shapes:

    1. REFUSE (hard) any UNIX_SOCKET / FIFO bind. A socket/FIFO is a rendezvous
       with a process the pre_session hook started on the COMPOSING host
       (agent-*-proxy's broker socket, wolfram-sidecar's helper FIFO). On the
       sbatch path the compose host is the LOGIN node and the container runs on a
       COMPUTE node, so that endpoint is unreachable and the capability would be
       silently dead. This is the mechanical generalization of submit.py's
       `_refuse_proxy_on_hpc` (which refuses by plugin name) — it catches any
       plugin contributing such a rendezvous, named or not.

    2. WARN (best-effort) on any regular bind whose SOURCE is under a known
       node-local filesystem prefix (`/tmp`, `/dev/shm`, …). The whole
       compose-at-submit premise is "shared FS → login-node paths are valid on
       the compute node"; a node-local source violates it and would apptainer-
       FATAL on the compute node. We do NOT refuse (a site may share /tmp; the
       module software-root binds under /apps are legitimately not under $HOME),
       we warn so the failure is diagnosable instead of mysterious. No bundled
       plugin trips this — bundled hooks write under $HOME session scratch.
    """
    import sys as _sys
    for b in spec.mount_plan.binds:
        if b.mode in (BindMode.UNIX_SOCKET, BindMode.FIFO):
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                f"the HPC sbatch path cannot carry the {b.mode.value} bind "
                f"{b.target!r} (provenance {b.provenance}). A socket/FIFO is a "
                f"rendezvous with a process on the COMPOSING host (the login "
                f"node); the container runs on a COMPUTE node where that "
                f"endpoint is unreachable. The plugin that contributed it "
                f"(e.g. an auth proxy or a compute sidecar) is not supported "
                f"on the sbatch path at v0.1.0. Disable it for HPC, or use "
                f"the direct `botainer start --runtime apptainer` path inside "
                f"an salloc where compose and run share a node.",
            )
        src = str(b.source)
        if any(src.startswith(p) for p in _NODE_LOCAL_SOURCE_PREFIXES):
            _sys.stderr.write(
                f"[botainer] WARNING: bind source {src!r} (target {b.target!r}, "
                f"provenance {b.provenance}) is under a node-local filesystem. "
                f"compose-at-submit assumes a SHARED filesystem — if this path "
                f"is not shared between the login and compute nodes, the job "
                f"will fail with `apptainer FATAL: bind source does not exist`. "
                f"Point the bind at a $HOME/project/scratch (shared) path.\n"
            )

    # 3. REFUSE any env value naming a LOOPBACK rendezvous.
    #
    # HPC-parity audit (C2): check 1 is BIND-shaped, so it only
    # catches a rendezvous that happens to be a socket/FIFO. `agent-codex-broker`
    # is TCP on EVERY runtime and contributes no bind at all, so it sailed
    # through: `botainer hpc submit` succeeded, the broker daemon started on the
    # LOGIN node, `OPENAI_BASE_URL=http://127.0.0.1:<login-node-port>/…` was baked
    # into the sbatch script, the submit process exited, the daemon died ~30s
    # later via its launcher-PID watchdog — and hours later the job started on a
    # compute node and got ECONNREFUSED on every request, after burning the queue
    # wait and the allocation. submit.py's name gate matches only `*-proxy`, so
    # it missed this too.
    #
    # A loopback address is host-local BY DEFINITION — that is what makes this
    # transport-agnostic rather than another name list: it catches any plugin,
    # named or not, that hands the container an endpoint only the composing host
    # can reach.
    for _k, _v in (spec.env.values or {}).items():
        _val = str(_v)
        if any(h in _val for h in _LOOPBACK_RENDEZVOUS_HOSTS):
            raise Refused(
                RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
                f"the HPC sbatch path cannot carry the loopback endpoint in "
                f"{_k}={_val!r}. It points at the COMPOSING host (the login "
                f"node), but the container runs on a COMPUTE node where that "
                f"address is a different machine — the job would start, fail "
                f"every request with connection-refused, and burn the "
                f"allocation. This is usually a credential broker in TCP mode "
                f"(e.g. agent-codex-broker). Use the direct "
                f"`botainer start --runtime apptainer` path inside an salloc, "
                f"where compose and run share a node, or disable that plugin "
                f"for the sbatch path.",
            )


def compose_agent_exec_for_hpc(
    project_root: Path, *, image_override: str | None = None
) -> tuple[SessionSpec, list[str]]:
    """Compose a full apptainer session ON THE LOGIN NODE and return the
    `apptainer exec …` argv to bake into the sbatch script.

    This is the heart of the compose-at-submit restructure (design/
    HPC-COMPOSE-AT-SUBMIT.md). The old sbatch path built a minimal bind set
    by hand and deferred the real composition to `botainer start
    --in-container` INSIDE the .sif — but the agent images bundle only the
    agent CLI, not botainer, so that FATAL'd on the compute node. Instead we
    run the identical composition the direct apptainer path runs, here on the
    login node (shared filesystem → login-node paths are valid on the compute
    node), and hand the ADAPTER-rendered argv to the sbatch generator. The
    compute-node container then execs ONLY the agent entrypoint (which IS in
    the image).

    Ordering mirrors `cli/start.py`'s non-in-container path:
      compose_session → host_pre_launch hooks → pre_session hooks →
      render_agent_files → placeholders → adapter.render_argv.

    Two HPC-specific deviations:
      • host_pre_launch runs in derive-only mode (B3) so a login-invisible
        module doesn't abort discovery, and forces inner-prepend delivery so
        module PATH-list vars PREPEND in-container via the trampoline (there
        is no in-container botainer to re-apply them).
      • `_refuse_cross_node_binds` rejects any node-local socket/FIFO the
        pre_session hooks contributed.

    Returns (spec, argv). Raises Refused on any composition/validation
    failure (the caller maps it to an exit code + structured message).
    """
    spec = compose_session(
        project_root,
        runtime_choice="apptainer",
        identity_accept=True,
        image_override=image_override,
    )
    spec = run_host_pre_launch_hooks(
        spec,
        hook_env_extra={"BOTAINER_MODBINDS_DERIVE_ONLY": "1"},
        module_env_delivery="inner-prepend",
    )
    spec = run_pre_session_hooks(spec)
    # A pre_session hook may have started a node-local sidecar/proxy on the login
    # node (wolfram-sidecar Popens a helper; agent-*-proxy a broker). If the
    # cross-node check then refuses, compose aborts and the normal post_session
    # teardown never runs — leaving that process orphaned on the login node
    # (review LOW-4). Run the cleanup hooks on the refusal path so a refused HPC
    # compose doesn't leak a helper process.
    try:
        _refuse_cross_node_binds(spec)
    except Refused:
        run_post_session_hooks(spec)
        raise
    render_agent_files(spec)
    # Apptainer (like docker on virtiofs) needs the host-side mountpoint for
    # every nested bind to pre-exist; the anchor placeholders live under $HOME
    # state (shared FS) so creating them on the login node is valid on the node.
    _prepare_nested_bind_placeholders(spec.mount_plan)
    argv = ApptainerAdapter().render_argv(spec)
    return spec, argv


def run_post_session_hooks(spec: SessionSpec) -> None:
    """Execute `post_session` hooks for cleanup. Errors are logged but
    not propagated — session teardown should be best-effort."""
    from botainer.plugins import hooks as plugin_hooks

    session_dir = Path(spec.state_dir) / "sessions" / spec.session_id
    record_path = session_dir / session_record.RECORD_FILENAME
    from botainer.state.dir import subprocess_state_env
    env = {
        **subprocess_state_env(spec.project_uuid),
        "BOTAINER_SESSION_RECORD_PATH": str(record_path),
        "BOTAINER_SESSION_ID": spec.session_id,
        "BOTAINER_STATE_DIR": spec.state_dir,
        "BOTAINER_RUNTIME": spec.runtime,
        "BOTAINER_PROFILE": spec.profile,
    }
    for hook in spec.hooks:
        if hook.when != "post_session":
            continue
        try:
            _post_result = plugin_hooks.run_hook(
                plugin_name=hook.plugin,
                hook_when="post_session",
                script_path=Path(hook.script_path),
                env=env,
                agent_writable_roots=_agent_writable_bind_sources(spec),
                timeout_seconds=hook.timeout_seconds,
            )
            # #138: post_session is where the exit-time reconcile reports a
            # host-wide credential change. Never shown until now.
            plugin_hooks.surface_hook_stderr(_post_result, hook.plugin, "post_session")
        except Refused as exc:
            import sys
            sys.stderr.write(f"[botainer] post_session hook failed: {exc}\n")


def _prepare_nested_bind_placeholders(plan) -> None:
    """Create placeholder files/dirs at the null-bind source for every
    nested bind, so the host-side mountpoint exists before Docker tries
    to mount onto it.

    Background: Docker Desktop on macOS uses virtiofs, which (unlike a
    native Linux kernel bind-mount) refuses to create the destination
    on the fly. If we bind-mount `<src>/AGENT_HINTS.md` to
    `/workspace/.botainer/AGENT_HINTS.md`, and `/workspace/.botainer`
    is itself a null-bind from `<anchor>/`, virtiofs looks for
    `<anchor>/AGENT_HINTS.md` on the host. If that path doesn't exist,
    Docker refuses with `mountpoint is outside of rootfs`.

    The fix is to mirror the nested target's filename into the anchor
    dir before launch. The placeholder type (file vs directory) is
    derived from the actual bind source so virtiofs sees a matching
    type. Linux-native Docker doesn't need this but the placeholders
    are harmless there.

    Takes a MountPlan (not a SessionSpec) so it's trivially testable
    without constructing a full session spec.
    """
    from botainer.core.spec import BindMode
    null_anchors = {
        b.target.rstrip("/"): Path(b.source)
        for b in plan.binds
        if b.mode == BindMode.NULL_BIND
    }
    if not null_anchors:
        return
    for b in plan.binds:
        if not b.nested_under:
            continue
        anchor_source = null_anchors.get(b.nested_under.rstrip("/"))
        if anchor_source is None:
            continue
        try:
            rel = Path(b.target).relative_to(b.nested_under)
        except ValueError:
            # Mis-declared nested_under — bind target isn't actually under
            # the parent. Skip; validation should have caught this earlier.
            continue
        placeholder = anchor_source / rel
        src_path = Path(b.source)
        if src_path.is_dir():
            placeholder.mkdir(parents=True, exist_ok=True)
        else:
            placeholder.parent.mkdir(parents=True, exist_ok=True)
            if not placeholder.exists():
                placeholder.touch()


def launch(spec: SessionSpec, *, detach: bool = False) -> RuntimeHandle:
    adapter = _adapter_for(spec.runtime)
    # Detach is only meaningful for Docker at v0.1.0 (Apptainer + Slurm
    # has its own batch-submit mode handled by the hpc-launcher plugin).
    # Check BEFORE adapter.validate so the detach-refusal isn't masked by
    # other runtime constraints (e.g. apptainer refusing network.mode=none).
    if detach and spec.runtime != "docker":
        raise Refused(
            RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
            f"--detach is only supported for the docker runtime at v0.1.0; "
            f"got runtime={spec.runtime!r}. For HPC, submit via sbatch using "
            f"hpc-launcher plugin's normal flow.",
        )
    adapter.validate(spec)
    # Mac virtiofs (Docker Desktop) requires the host-side mountpoint
    # for every bind to exist before `docker run`. Native Linux Docker
    # doesn't care. Do it unconditionally — placeholders are cheap.
    _prepare_nested_bind_placeholders(spec.mount_plan)
    # Task #109: install SIGINT/SIGTERM handler BEFORE adapter.launch so a
    # Ctrl-C between launch() and the try/finally below doesn't orphan
    # the just-started container. The handler raises KeyboardInterrupt
    # which the caller catches; the foreground process group ensures
    # the container child also receives the signal naturally.
    import signal as _signal
    _orig_sigint = _signal.getsignal(_signal.SIGINT)
    _orig_sigterm = _signal.getsignal(_signal.SIGTERM)
    def _raise_kbint(_sig, _frame):
        raise KeyboardInterrupt(f"received signal {_sig}")
    _signal.signal(_signal.SIGINT, _raise_kbint)
    _signal.signal(_signal.SIGTERM, _raise_kbint)
    try:
        handle = adapter.launch(spec, detach=True) if detach else adapter.launch(spec)
    finally:
        _signal.signal(_signal.SIGINT, _orig_sigint)
        _signal.signal(_signal.SIGTERM, _orig_sigterm)
    # Capture runtime handle into the session record so external commands
    # (botainer nudge, status, etc.) can find this running session.
    # Architecture review #10: composition is runtime-agnostic — the
    # adapter populated handle.extras with Slurm jobid / node / step_id
    # if relevant. Composition just forwards.
    session_dir = Path(spec.state_dir) / "sessions" / spec.session_id
    try:
        update_fields: dict[str, str | None] = {
            "started_at": identity.now_iso8601_utc(),
        }
        if spec.runtime == "docker":
            update_fields["container_id"] = handle.id
            # §A19: when the nudge plugin is enabled, the docker adapter
            # wraps `docker run` in `screen -dmS botainer-<sid>` on the
            # host and reports the session name in extras. The nudge CLI
            # reads screen_session_id from the session record.
            if handle.extras:
                screen_sid = handle.extras.get("screen_session_id")
                if screen_sid:
                    update_fields["screen_session_id"] = screen_sid
        elif spec.runtime == "apptainer":
            update_fields.update(handle.extras)
        session_record.update_runtime(session_dir, **update_fields)
    except (FileNotFoundError, OSError) as exc:
        import sys
        sys.stderr.write(
            f"[botainer] could not update session record: {exc}\n"
        )
    return handle


def attach(handle: RuntimeHandle) -> int:
    adapter = _adapter_for(handle.runtime)
    return adapter.attach(handle)


def _activate_only_the_selected_agent(cfg):
    """Keep exactly ONE agent plugin enabled: the one for `cfg.agent`.

    FOUND BY THE USER,, and their framing was the right one: "In a
    mode where we're not doing multi agent, codex cred leaks into claude???"
    Yes. `botainer auth use <mode>` switches EVERY installed family by design,
    so a project ends up with `[agent-claude-shared, agent-codex-shared]`
    enabled. Composition then honoured BOTH, which produced two distinct
    failures from one cause:

    1. SECURITY — cross-agent credential exposure. A Claude-only session had the
       OpenAI credential bound RW at /home/agent/.codex, the host-wide codex
       store at /shared-auth/agent-codex, and CODEX_HOME announcing where to
       find it. A prompt-injected Claude could read and exfiltrate a credential
       belonging to an agent that was not even running. Each plugin was
       individually correct; nothing checked that only ONE should be active.

    2. BROKEN LAUNCH — two entrypoint wraps stacked:
           agent-claude-entrypoint  agent-codex-entrypoint --sandbox … --ask-for-approval never
       Claude's wrap ends in `exec claude "$@"`, so codex's entrypoint path
       became Claude's first positional (shown to the user as a PROMPT) and
       codex's flags became Claude's flags:
           error: unknown option '--sandbox'
       The session died in 1.2s.

    WHY FILTER HERE rather than guard the wrap list. The wrap was only the
    loudest symptom; the credential bind was the dangerous one, and a
    wrap-specific guard would have fixed the crash and left the leak. Dropping
    the plugin before the contribution loop runs means binds, env, hooks and
    wraps are ALL consistent with one selected agent — one chokepoint instead of
    four places that must each remember. Structure over rules.

    `plugins_enabled` may legitimately list several agent plugins: that is what
    lets `--agent` pick either without editing config. Listing them is a menu;
    exactly one is activated per session.
    """
    from botainer.plugins.lifecycle import list_installed
    from botainer.plugins.manifest import load_manifest

    selected_prefix = f"agent-{cfg.agent}"

    agent_plugins: set[str] = set()
    for inst in list_installed():
        try:
            man = load_manifest(inst.plugin_dir)
        except Exception:
            continue
        # Auth-slot membership, not `kind` — broker/proxy are host_helpers.
        if getattr(man, "auth_family", "") and getattr(man, "auth_mode", ""):
            agent_plugins.add(inst.name)

    kept, dropped = [], []
    for name in cfg.plugins_enabled:
        if name in agent_plugins and not name.startswith(selected_prefix):
            dropped.append(name)
        else:
            kept.append(name)

    if not dropped:
        return cfg

    sys.stderr.write(
        f"[botainer] agent is {cfg.agent!r}; not activating "
        f"{', '.join(dropped)} for this session (its credentials are NOT bound "
        f"and its entrypoint is NOT used). Enter that agent with "
        f"`botainer start --agent <name>`.\n")
    return cfg.model_copy(update={"plugins_enabled": kept})


def _apply_agent_override_in_memory(cfg, agent: str):
    """Swap which AGENT plugin is enabled, for this composition only.

    User,: "I start the project, I get claude, not codex! and start
    --agent codex not recognized." `agent:` lived only in .botainer/config.yaml,
    so entering the same project with the other agent meant hand-editing config
    and editing it back. Meanwhile `start` already had one-shot overrides for
    the NEIGHBOURING concept (--auth-mode, --auth-profile) — so this was an
    inconsistency, not a decision.

    Same discipline as _apply_auth_mode_override_in_memory: IN-MEMORY ONLY, no
    disk mutation. The earlier auth-mode implementation edited config.yaml and
    relied on atexit to restore it, which does not fire on SIGKILL/OOM/exec —
    users could end up silently switched. A one-shot override that persists is
    worse than no override, because the NEXT launch would quietly use the other
    agent.

    Changing the agent also changes which PLUGIN is enabled (agent-claude ->
    agent-codex), and the auth-mode suffix has to survive the swap: someone in
    shared mode should land on agent-codex-shared, not agent-codex.
    """
    from botainer.plugins.lifecycle import list_installed
    from botainer.plugins.manifest import load_manifest

    base = agent if agent.startswith("agent-") else f"agent-{agent}"

    installed: dict[str, object] = {}
    for inst in list_installed():
        try:
            man = load_manifest(inst.plugin_dir)
        except Exception:
            continue
        # Select on "declares an auth family AND mode", NOT on kind == "agent".
        #
        # Grace,: `auth use broker` then `start --agent codex` was
        # refused with "'agent-codex-broker' and 'agent-codex' are mutually
        # exclusive". Cause: broker and proxy plugins are kind='host_helper'
        # (they run a host-side process), only the isolated/shared ones are
        # kind='agent'. So this filter skipped every broker plugin, the swap saw
        # NO current agent, defaulted the mode to isolated, and appended bare
        # agent-codex alongside the agent-codex-broker already enabled.
        #
        # `kind` describes HOW the plugin is implemented; the question here is
        # WHICH AUTH SLOT it fills. Those are different, and using one for the
        # other silently excluded two of the four modes.
        if getattr(man, "auth_family", "") and getattr(man, "auth_mode", ""):
            installed[inst.name] = man

    enabled = list(cfg.plugins_enabled)
    current_agents = [n for n in enabled if n in installed]

    # Carry the current auth mode across the swap where possible.
    current_mode = ""
    for name in current_agents:
        current_mode = getattr(installed[name], "auth_mode", "") or current_mode

    target = base
    if current_mode and current_mode != "isolated":
        variant = f"{base}-{current_mode}"
        if variant in installed:
            target = variant

    if target not in installed:
        from botainer.core.refusal import RefusalCategory, Refused
        have = sorted(n for n in installed)
        raise Refused(
            RefusalCategory.PLUGIN_DEPENDENCY_UNRESOLVED,
            f"--agent {agent!r} needs plugin {target!r}, which is not "
            f"installed. Installed agent plugins: {have}. "
            f"Run `botainer setup` to install bundled plugins.",
        )

    for name in current_agents:
        if name != target:
            enabled.remove(name)
    if target not in enabled:
        enabled.append(target)

    short = base[len("agent-"):]
    return cfg.model_copy(update={"agent": short, "plugins_enabled": enabled})


def _apply_auth_mode_override_in_memory(cfg, mode: str):
    """Swap auth-family plugin variants in cfg.plugins_enabled.

    Per insecure-defaults H3 + sharp-edges F3: the previous
    implementation eagerly edited .botainer/config.yaml and relied on
    atexit to restore. atexit doesn't fire on SIGKILL/OOM/exec*; users
    could end up with config silently switched. This version mutates
    only the in-memory cfg object; no disk side effects.
    """
    from botainer.plugins.lifecycle import list_installed
    from botainer.plugins.manifest import load_manifest
    # Build family -> {mode: plugin_name}
    families: dict[str, dict[str, str]] = {}
    for inst in list_installed():
        try:
            man = load_manifest(inst.plugin_dir)
        except Exception:
            continue
        if not man.auth_family or not man.auth_mode:
            continue
        families.setdefault(man.auth_family, {})[man.auth_mode] = inst.name
    # For each family, swap to the requested mode if a matching variant exists.
    # Independent-read F-6: previously this silently `continue`d when no
    # matching variant existed for a family — the user passed
    # `--auth-mode proxy` but stayed in shared mode with no signal. Now
    # we log the no-op to stderr so the divergence is visible.
    import sys as _sys
    new_enabled = list(cfg.plugins_enabled)
    no_op_families: list[str] = []
    for fam, modes_for_fam in families.items():
        # Only relevant if SOME variant of this family is currently enabled;
        # otherwise the family isn't in play and silence is correct.
        currently_active = next(
            (p for p in modes_for_fam.values() if p in new_enabled),
            None,
        )
        if currently_active is None:
            continue
        target = modes_for_fam.get(mode)
        if not target:
            no_op_families.append(fam)
            continue
        for _sibling_mode, sibling_plugin in modes_for_fam.items():
            if sibling_plugin in new_enabled and sibling_plugin != target:
                new_enabled.remove(sibling_plugin)
        if target not in new_enabled:
            new_enabled.append(target)
    for fam in no_op_families:
        _sys.stderr.write(
            f"[botainer] --auth-mode={mode!r}: no installed {mode!r} variant "
            f"for family {fam!r}; left this family's current plugin enabled. "
            f"Install the missing variant or pick a different mode.\n"
        )
    # cfg is a pydantic model; reconstruct with new plugins_enabled.
    return cfg.model_copy(update={"plugins_enabled": new_enabled})


def _adapter_for(runtime: str) -> Adapter:
    if runtime == "docker":
        return DockerAdapter()
    if runtime == "apptainer":
        return ApptainerAdapter()
    if runtime == "mock":
        return MockAdapter()
    raise Refused(
        RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
        f"unknown runtime {runtime!r}",
    )


def _validate_capabilities(
    grants: list[CapabilityGrant],
) -> list[CapabilityGrant]:
    """Task #68 + #258: refuse unknown capability names.

    Walks plugin-contributed capability grants and looks each name up in
    the closed-namespace CAPABILITIES registry. Any unknown name is a
    refusal — silently accepting an unknown capability is the
    pre-#258 bug where 10 registry names had zero external consumers
    AND any typo'd name (e.g. 'mounts.extras') went unnoticed.
    """
    from botainer.capabilities.registry import all_capability_names
    known = set(all_capability_names())
    for g in grants:
        if g.name not in known:
            raise Refused(
                RefusalCategory.CAPABILITY_UNKNOWN,
                f"plugin contributed unknown capability {g.name!r}. "
                f"v0.1 capabilities are a closed namespace; known names: "
                f"{sorted(known)}. If you meant a new capability, add it "
                f"to botainer/capabilities/registry.py.",
            )
    return grants


def _resolve_runtime(cfg_runtime: str, cli_runtime: str) -> str:
    """Resolve auto → docker|apptainer based on PATH discovery."""
    runtime = cli_runtime if cli_runtime != "auto" else cfg_runtime
    if runtime == "auto":
        if shutil.which("docker"):
            return "docker"
        if shutil.which("apptainer") or shutil.which("singularity"):
            return "apptainer"
        # No runtime found — fall back to mock so dry-run/inspect still work.
        return "mock"
    if runtime not in ("docker", "apptainer", "mock"):
        raise Refused(
            RefusalCategory.UNSUPPORTED_RUNTIME_FEATURE,
            f"unknown runtime {runtime!r}; valid: docker, apptainer, mock, auto",
        )
    return runtime


def _verify_apptainer_sif_provenance(agent_name: str, sif_path: Path) -> None:
    """AUDIT (MEDIUM): refuse if the resolved .sif's sha256 differs
    from the `apptainer:sha256:<hex>:<path>` marker recorded in installed.lock
    at build time. No marker → nothing to verify (proceed)."""
    from botainer.plugins.provenance import read_lock
    from botainer.state import dir as _state_dir
    try:
        paths = _state_dir.ensure_user_state_dir(create_if_missing=False)
        entries = read_lock(paths.installed_lock_path)
    except Exception:
        # No lock / state dir, OR a transient read error → no usable baseline,
        # so proceed (unchanged behavior). This is fail-open by the same
        # rationale as a deleted marker: the lock is user-co-writable, so it is
        # not a tamper-proof root of trust — the check raises the bar against
        # accidental/partial .sif swaps, not against an attacker who already has
        # write to the state dir. Signed-provenance is v2 (#143/#144).
        return
    # Prefer the entry carrying an apptainer:sha256 marker (image build records
    # it via read-modify-write, so production has one entry per plugin; be
    # robust if an older append-style entry without the marker also exists).
    marker = next(
        (e.image_digest for e in entries
         if e.name == agent_name and (e.image_digest or "").startswith("apptainer:sha256:")),
        None,
    )
    if not marker:
        return  # no recorded apptainer provenance → nothing to verify
    recorded_hex = marker[len("apptainer:sha256:"):].split(":", 1)[0]
    import hashlib
    h = hashlib.sha256()
    try:
        with open(sif_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError as exc:
        raise Refused(
            RefusalCategory.CONFIG_MISSING,
            f"agent plugin {agent_name!r} .sif {sif_path} is unreadable: {exc}",
        ) from exc
    actual_hex = h.hexdigest()
    if actual_hex != recorded_hex:
        raise Refused(
            RefusalCategory.IMAGE_INVALID,
            f"agent plugin {agent_name!r} .sif {sif_path} sha256 {actual_hex[:16]}… "
            f"does NOT match the value recorded at build time "
            f"({recorded_hex[:16]}…) in installed.lock. The image was replaced "
            f"out-of-band (not via `botainer image build`). Refusing to exec a "
            f".sif whose provenance can't be verified — rebuild it, or remove "
            f"the stale installed.lock entry if this change is intentional.",
        )


def _resolve_apptainer_sif_path(agent_name: str) -> Path | None:
    """Locate an apptainer .sif for the given agent plugin.

    Mirrors the dual-name candidate list in
    `botainer/cli/doctor.py::collect_image_findings` so users whose
    .sif lives under either convention are recognized. The unification
    work is tracked in DN-036.
    """
    from botainer.state import dir as _state_dir
    paths = _state_dir.ensure_user_state_dir(create_if_missing=False)
    images_dir = paths.root / "images"
    candidates = [
        images_dir / f"botainer-{agent_name}.sif",
        images_dir / f"{agent_name}.sif",
    ]
    # Also check next to the plugin source (some build flows write here).
    try:
        from botainer.plugins.lifecycle import list_installed
        for inst in list_installed():
            if inst.name == agent_name:
                candidates.append(inst.plugin_dir / f"botainer-{agent_name}.sif")
                candidates.append(inst.plugin_dir / f"{agent_name}.sif")
                break
    except Exception:
        pass
    for c in candidates:
        if c.exists():
            return c
    return None


def _resolve_session_image(
    cfg: config_module.ProjectConfig,
    *,
    runtime: str = "docker",
    image_override: str | None = None,
) -> str:
    """Resolve the container image for this session.

    Per Phase 0 of v0.1.0 plan:
    - cfg.image: per-project override; honored if set (warned in inspect).
    - Else: look up the agent plugin in installed.lock; use its recorded
      image_digest (e.g. "sha256:abc...") combined with the manifest's
      image.tag-without-digest part.
    - Else: read agent plugin's manifest image.tag.
    - Else: refuse.

    For apptainer runtime, the returned value must be an absolute path
    to a .sif file. A docker-style tag would be interpreted by
    apptainer as a path relative to CWD (real-host Grace bug
). After determining the agent plugin name, we look up
    the .sif via `_resolve_apptainer_sif_path` and return its absolute
    path. cfg.image is honored only if it points at an existing file
    (e.g. user-supplied .sif path); a docker tag in cfg.image is
    ignored for apptainer.

    image_override (HPC compose-at-submit): if set, it wins over cfg.image
    and the agent-plugin lookup. It flows through the SAME validation as a
    user-supplied cfg.image so the frozen-plan chokepoint discipline holds:
    validate_image_reference (reject flag-like/whitespace) and, for
    apptainer, require an existing absolute .sif file / sandbox dir. The
    hpc-launcher already resolves the .sif via its own (HPC-specific)
    precedence and passes the result here — this is the trust chokepoint.
    """
    if image_override:
        try:
            validate_image_reference(image_override)
        except ValueError as exc:
            raise Refused(RefusalCategory.IMAGE_INVALID, str(exc)) from exc
        if runtime == "apptainer":
            p = Path(image_override)
            if not (p.is_absolute() and (p.is_file() or p.is_dir())):
                raise Refused(
                    RefusalCategory.CONFIG_MISSING,
                    f"image_override {image_override!r} is not an existing "
                    f"absolute .sif file or apptainer sandbox dir; the HPC "
                    f"compose-at-submit path must be handed a built .sif.",
                )
            # SUPPLY-CHAIN audit (H2): this branch used to return
            # WITHOUT verifying the .sif against the sha256 recorded at build
            # time — and `plugins/hpc-launcher/host_helper/submit.py` ALWAYS
            # takes this branch. So `botainer hpc submit`, the product's primary
            # path, never hashed the image it was about to exec, even though the
            # hpc-launcher had just parsed that path OUT of the
            # `apptainer:sha256:<hex>:<path>` marker and discarded the hex half.
            # The docstring above calls this "the trust chokepoint"; it now is
            # one. Same fail-open-on-no-marker semantics as the resolved path.
            _verify_apptainer_sif_provenance(_agent_plugin_name(cfg), p)
        return image_override
    if cfg.image:
        # AC7 hole-hunt: refuse a flag/shell-hostile image
        # ref BEFORE it reaches `docker run <image>` / `apptainer exec
        # <image>`. `image: "--privileged"` would otherwise be parsed by
        # docker as a flag (no `--` separator before the image operand),
        # yielding a privileged container that defeats --cap-drop ALL.
        # (SessionSpec.image also validates as a type-level backstop;
        # this gives the user-facing IMAGE_INVALID with a clear message.)
        try:
            validate_image_reference(cfg.image)
        except ValueError as exc:
            raise Refused(RefusalCategory.IMAGE_INVALID, str(exc)) from exc
        # For apptainer, accept cfg.image only if it's a usable image
        # target: an absolute path that is a regular file (a .sif) OR a
        # directory (an apptainer sandbox). Otherwise fall through to the
        # agent-plugin → .sif lookup. A docker-style tag in cfg.image
        # would crash apptainer with a CWD-relative path lookup.
        # AC7 image-positional review: require is_file/
        # is_dir() rather than bare exists() so a char/block device, FIFO,
        # or socket (e.g. /dev/zero) is not handed to `apptainer exec`.
        # Robustness, not an escalation — apptainer still runs --no-privs
        # --drop-caps all regardless of the operand.
        if runtime == "apptainer":
            p = Path(cfg.image)
            if p.is_absolute() and (p.is_file() or p.is_dir()):
                return str(p)
            # else: drop cfg.image and fall through.
        else:
            return cfg.image
    agent_name = _agent_plugin_name(cfg)
    if agent_name is None:
        raise Refused(
            RefusalCategory.CONFIG_MISSING,
            "no `image:` and no agent plugin installed. Run `botainer setup` to "
            "install bundled plugins, then re-run.",
        )

    # Apptainer path: resolve to .sif absolute path. installed.lock and
    # manifest image.tag are docker-shaped — useless to apptainer.
    if runtime == "apptainer":
        sif_path = _resolve_apptainer_sif_path(agent_name)
        if sif_path is None:
            raise Refused(
                RefusalCategory.CONFIG_MISSING,
                f"agent plugin {agent_name!r} has no apptainer .sif built. "
                f"Run `botainer image build {agent_name} --runtime apptainer` "
                f"to build it (~10-20 min on a compute node). "
                f"Or set `image:` in .botainer/config.yaml to an absolute "
                f"path of an existing .sif file.",
            )
        # AUDIT (MEDIUM): verify the .sif against the sha256 recorded
        # at build time (the `apptainer:sha256:<hex>:<path>` marker in
        # installed.lock). `image build` re-records the marker on every build,
        # so a mismatch means the .sif was REPLACED out-of-band (e.g. a
        # tampered file swapped under the images dir) — refuse before exec. When
        # NO marker is recorded (manually-placed .sif, old install) there is no
        # baseline to verify against, so we proceed (unchanged). Docker registry
        # plugins already pin via image_digest; this gives the apptainer path
        # parity. (Partial #143/#144 — full signed-provenance is v0.2.)
        _verify_apptainer_sif_provenance(agent_name, sif_path)
        return str(sif_path)

    from botainer.plugins.provenance import read_lock
    from botainer.state import dir as _state_dir
    paths = _state_dir.ensure_user_state_dir(create_if_missing=False)
    entries = read_lock(paths.installed_lock_path)
    by_name = {e.name: e for e in entries}
    entry = by_name.get(agent_name)
    if entry and entry.image_digest:
        # We have a digest in installed.lock. Whether to use the
        # `name:tag@digest` form depends on where the digest came from:
        #   - Registry pull → digest is a REPO digest; `name:tag@digest`
        #     is valid Docker syntax that resolves locally.
        #   - Local `docker build` → digest is the local image ID, NOT a
        #     repo digest. Docker treats `name:tag@<image-id>` as "pull
        #     this from the registry" and refuses because the image was
        #     never pushed (refused: "pull access denied").
        #
        # `image.source` in the manifest tells us which case applies:
        # `dockerfile` → built locally → use the plain tag. `registry`
        # → use digest form. Anything else (e.g. apptainer-def): plain
        # tag is safest.
        from botainer.plugins.manifest import load_manifest as _load
        plugin_dir = paths.plugins_dir / agent_name
        name_part: str | None = None
        image_source: str = "registry"
        if plugin_dir.exists():
            try:
                m = _load(plugin_dir)
                if m.image and m.image.tag:
                    name_part = m.image.tag.split("@", 1)[0]
                if m.image and m.image.source:
                    image_source = m.image.source
            except Refused:
                pass
        if name_part is None:
            raise Refused(
                RefusalCategory.CONFIG_MISSING,
                f"agent plugin {agent_name!r} has an image digest in "
                f"installed.lock but no readable image.tag in its manifest. "
                f"Either rebuild the image (`botainer image build {agent_name}`) "
                f"or set `image:` in .botainer/config.yaml as an explicit "
                f"override.",
            )
        if image_source != "registry":
            # Locally built (dockerfile / apptainer-def): the recorded
            # "digest" is a local image ID, not a registry-published
            # digest. `name:tag` resolves to the locally-built image
            # by name; this is the only form Docker accepts for it.
            return name_part
        digest = entry.image_digest
        if not digest.startswith("sha256:"):
            digest = f"sha256:{digest}"
        return f"{name_part}@{digest}"
    # Fall back to manifest's declared image.tag (may not be digest-pinned).
    plugin_dir = paths.plugins_dir / agent_name
    if plugin_dir.exists():
        from botainer.plugins.manifest import load_manifest as _load
        try:
            m = _load(plugin_dir)
            if m.image and m.image.tag:
                return m.image.tag
        except Refused:
            pass
    raise Refused(
        RefusalCategory.CONFIG_MISSING,
        f"agent plugin {agent_name!r} has no recorded image. "
        f"Run `botainer image build {agent_name}` to build it now "
        f"(needs Docker on host; ~8-12 min first time), or set `image:` "
        f"in .botainer/config.yaml as an override (e.g. `image: ubuntu:24.04` "
        f"for a no-tools test).",
    )


def _agent_plugin_name(cfg: config_module.ProjectConfig) -> str | None:
    """Return the canonical agent plugin name from the project config.

    Config has `agent: claude` (short form); plugin is `agent-claude`.
    """
    if not cfg.agent:
        return None
    if cfg.agent.startswith("agent-"):
        return cfg.agent
    return f"agent-{cfg.agent}"


def _new_session_id() -> str:
    return str(_uuid.uuid4()).replace("-", "")[:16]


def _browser_viewer_on(cfg: config_module.ProjectConfig) -> bool:
    """True iff `plugins.browser.viewer` is truthy. Mirrors the browser plugin's
    start_viewer.viewer_enabled — that hook is the source of truth for headed vs
    headless; this gate only decides whether the noVNC port forward is added."""
    bcfg = cfg.plugins.get("browser") or {}
    if not isinstance(bcfg, dict):
        return False
    val = bcfg.get("viewer", False)
    return val is True or (
        isinstance(val, str) and val.strip().lower() in {"1", "true", "yes", "on"}
    )


def _browser_viewer_mode(cfg: config_module.ProjectConfig) -> str:
    """`plugins.browser.viewer_mode`: 'gateway' (default) or 'legacy'.

    Only decides WHICH port the compose-time forward publishes — 5900 RFB for
    gateway, 6080 noVNC for legacy. `hooks/start_viewer.py` is the loud
    validator and issues the typed refusal on a bad value.

    ONLY AN EXPLICIT 'legacy' SELECTS LEGACY. Everything else — absent, wrong
    type, misspelled — resolves to gateway, because the two modes are not
    equally safe: legacy has the container serve the page your browser
    executes, gateway does not. A typo must not silently buy the weaker
    posture. This used to read the other way round, which was correct while
    legacy was the default and became fail-open the moment it stopped being.
    """
    bcfg = cfg.plugins.get("browser") or {}
    if not isinstance(bcfg, dict):
        return "gateway"
    val = bcfg.get("viewer_mode", "gateway")
    if isinstance(val, str) and val.strip().lower() == "legacy":
        return "legacy"
    return "gateway"


def _free_loopback_port(avoid: set[int] | None = None) -> int:
    """Ask the OS for a free loopback TCP port for the browser-viewer noVNC
    publish. Pre-picked at compose — a small TOCTOU window vs launch (same shape
    as the broker's port pick); if it races, `docker run` fails loudly and the
    fast-fail / doctor hint points the user at it. Retries to dodge `avoid` (host
    ports web-ports already claimed)."""
    import socket

    avoid = avoid or set()
    for _ in range(20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        finally:
            s.close()
        if port not in avoid:
            return port
    raise Refused(
        RefusalCategory.PORT_FORWARD_INVALID,
        "could not find a free loopback port for the browser viewer",
    )


def _resolve_port_forwards(
    cfg: config_module.ProjectConfig, enabled: set[str], runtime: str = "docker",
) -> tuple[PortForward, ...]:
    """Typed inbound loopback port forwards. Two sources:
    - the web-ports plugin's `ports:` config (the explicit user grant), and
    - the browser plugin's noVNC port when `plugins.browser.viewer: true` on
      DOCKER (viewer:true IS the explicit grant; apptainer refuses port_forwards
      and reaches noVNC via a node-local socket + `ssh -L` instead).

    Refuses (typed) on: port out of 1..65535 (Pydantic), duplicate host_port,
    non-loopback host_bind (v0.1.0 loopback-only).
    """
    out: list[PortForward] = []
    seen_host_ports: set[tuple[str, int]] = set()
    raw_ports: list = []
    if "web-ports" in enabled:
        plugin_cfg = cfg.plugins.get("web-ports", {}) or {}
        raw_ports = plugin_cfg.get("ports", []) or []
        if not isinstance(raw_ports, list):
            raise Refused(
                RefusalCategory.PORT_FORWARD_INVALID,
                f"plugins.web-ports.ports must be a list; got {type(raw_ports).__name__}",
            )
    for idx, item in enumerate(raw_ports):
        if isinstance(item, int):
            container_port = item
            host_port = item
            host_bind = "127.0.0.1"
            label = ""
        elif isinstance(item, dict):
            if "container" not in item:
                raise Refused(
                    RefusalCategory.PORT_FORWARD_INVALID,
                    f"plugins.web-ports.ports[{idx}] missing required 'container' key",
                )
            container_port = int(item["container"])
            host_port = int(item.get("host", container_port))
            host_bind = str(item.get("host_bind", "127.0.0.1"))
            label = str(item.get("label", ""))
        else:
            raise Refused(
                RefusalCategory.PORT_FORWARD_INVALID,
                f"plugins.web-ports.ports[{idx}] must be int or object; got {type(item).__name__}",
            )
        # Sharp-edges + insecure-defaults review (HIGH/CRITICAL):
        # host_bind needs an ALLOWLIST, not a denylist. Docker treats
        # 0.0.0.0, "", "0", "::", "*" and resolvable hostnames as "all
        # interfaces" — a blacklist on "0.0.0.0" misses every other form.
        # Only loopback addresses are allowed at v0.1.0.
        #
        # Independent-read F-3: `localhost` is a NAME resolved by the host
        # resolver against /etc/hosts; on a misconfigured host it may not
        # resolve to 127.0.0.1. Restrict to literal loopback IPs.
        _ALLOWED_BINDS = {"127.0.0.1", "::1"}
        if host_bind not in _ALLOWED_BINDS:
            raise Refused(
                RefusalCategory.PORT_FORWARD_INVALID,
                f"plugins.web-ports.ports[{idx}]: host_bind={host_bind!r} not allowed. "
                f"v0.1.0 allows only {sorted(_ALLOWED_BINDS)} (literal loopback IPs). "
                f"`localhost` is name-resolved via /etc/hosts and can differ; use "
                f"`127.0.0.1` or `::1` explicitly. If you really need wide binding "
                f"(which exposes the agent's web app to other processes on the host), "
                f"file an issue.",
            )
        key = (host_bind, int(host_port))
        if key in seen_host_ports:
            raise Refused(
                RefusalCategory.PORT_FORWARD_CONFLICT,
                f"port {host_bind}:{host_port} forwarded twice "
                f"(plugins.web-ports.ports[{idx}])",
            )
        seen_host_ports.add(key)
        try:
            out.append(
                PortForward(
                    container_port=int(container_port),
                    host_port=int(host_port),
                    host_bind=str(host_bind),
                    label=label,
                )
            )
        except Exception as exc:
            raise Refused(
                RefusalCategory.PORT_FORWARD_INVALID,
                f"plugins.web-ports.ports[{idx}]: {exc}",
            ) from exc

    # Browser viewer noVNC port (DOCKER ONLY). `plugins.browser.viewer: true` IS
    # the explicit inbound grant (capability contract amended — CAPABILITY-SURFACE
    # §4av). Apptainer is excluded: it refuses port_forwards (docker.py /
    # apptainer adapter) and reaches noVNC over a node-local 0700 unix socket +
    # `ssh -L` instead. The chosen host port lands in spec.port_forwards, which
    # `botainer plugin browser watch` reads to print the URL.
    if runtime == "docker" and "browser" in enabled and _browser_viewer_on(cfg):
        host_port = _free_loopback_port(avoid={p for (_b, p) in seen_host_ports})
        if _browser_viewer_mode(cfg) == "gateway":
            # Track B gateway mode: the container exposes ONLY RFB (x11vnc,
            # per-session-password-authenticated — see start_viewer.py +
            # viewer-start.sh). noVNC/websockify run on the LAPTOP
            # (`botainer plugin browser gateway`), which finds this published
            # RFB port in spec.port_forwards by label. Loopback-only publish;
            # the RFB password (not -nopw) gates other local processes.
            out.append(
                PortForward(
                    container_port=5900,
                    host_port=host_port,
                    host_bind="127.0.0.1",
                    label="browser viewer (RFB)",
                )
            )
        else:
            out.append(
                PortForward(
                    container_port=6080,
                    host_port=host_port,
                    host_bind="127.0.0.1",
                    label="browser viewer (noVNC)",
                )
            )
    return tuple(out)


# Re-export Refused so callers can `from botainer.core.composition import Refused`.
__all__ = ["Refused", "attach", "compose_session", "launch", "render_argv"]
