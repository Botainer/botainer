"""State-dir layout and management.

Layout (per DN-025 § 9, post-Q1-collapse):

    ${MY_BOTAINER:-~/.botainer}/
    ├── state/<uuid>/
    │   ├── meta.json                 # path history
    │   ├── protected.hashes          # (reserved; info-only at v0.1.0,
    │   │                             #  never written or verified;
    │   │                             #  task #74 — TOFU is per-project
    │   │                             #  info banner only, no security claim)
    │   ├── data/<plugin>/            # plugin runtime data
    │   ├── sessions/<sid>/           # session logs + spec snapshots
    │   └── locks/                    # concurrency locks
    ├── plugins/<name>/               # installed plugin packages
    ├── plugins/installed.lock        # plugin manifest lock
    ├── policy.yaml                   # user-wide policy
    └── logs/                         # reserved for v0.2 session transcripts;
                                      # NOT written at v0.1 (audit T11 — don't
                                      # imply a session command-log exists)

Non-collision with v0.0.x: v0.0.x uses `~/.botainer/`. During parallel
v0.1.x development the launcher honors `MY_BOTAINER=<alt-path>` to
keep state separate. See the project's internal dev notes for the
user-facing parallel-install
mechanism. Both can run on the same host.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_PATH_HISTORY_KEEP = 8  # Keep up to N most recent paths per project UUID.


@dataclass(frozen=True)
class StatePaths:
    root: Path

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def plugins_dir(self) -> Path:
        return self.root / "plugins"

    @property
    def policy_path(self) -> Path:
        return self.root / "policy.yaml"

    @property
    def installed_lock_path(self) -> Path:
        return self.plugins_dir / "installed.lock"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def images_dir(self) -> Path:
        return self.root / "images"

    def apptainer_sif_path(self, plugin_name: str) -> Path:
        """Canonical .sif path for a plugin's Apptainer image.

        Single source of truth for the `botainer-<plugin>.sif` naming
        convention. Both `botainer image build` and `botainer hpc build`
        write here; the hpc-launcher resolver + doctor look here.

        The .sif-naming follow-up (DN-036): `hpc build` used to write the unprefixed
        `<plugin>.sif`, which drifted from `image build`'s prefixed form
        and the resolver's expectation — a 15-min wasted rebuild on Grace.
        Routing every botainer-side site through this method means they
        can't drift again. (The hpc-launcher host_helper is deliberately
        standalone and keeps its own copy of the convention;
        tests/integration/test_sif_naming_consistent.py asserts the two
        agree.)
        """
        return self.images_dir / f"botainer-{plugin_name}.sif"

    def hpc_job_output_dir(self, uuid: str) -> Path:
        """Host-only dir for SLURM `--output`/`--error` (security-audit
 Finding 1 — directional isolation).

        SLURM's slurmstepd writes the job's output as the UNCAGED user,
        following symlinks. This dir therefore lives OUTSIDE the per-project
        state subtree (`state/<uuid>/`) that the hpc-launcher binds RW into
        the container — so a caged/prompt-injected agent has no write path to
        pre-plant a symlink at the output name. `botainer hpc logs` reads it
        here; the agent never needs to (its stdout IS this file).

        Single source of truth: the hpc-launcher host_helper is standalone
        and keeps its own `_job_output_dir(state_root, uuid)`; a parity test
        (tests/integration/test_hpc_output_isolation.py) asserts the two
        produce the same path — same discipline as apptainer_sif_path above.
        """
        return self.root / "hpc-job-outputs" / uuid

    def hpc_jobs_dir(self, uuid: str) -> Path:
        """Host root for the job-dispatcher mailbox (#54).

        Lives OUTSIDE the per-project `state/<uuid>/` subtree — which the
        hpc-launcher binds RW into the agent container — for the SAME reason as
        `hpc_job_output_dir`: the mailbox's `run/` (generated `.sbatch`, SLURM
        `--output`) and `out/` are written by UNCAGED host/slurmstepd processes,
        so they must sit where the caged agent has no write path to pre-plant a
        symlink (INV-1, the v0.0.x shared-rw `/jobs` escape). Layout under here:
          in/   agent writes  → bound RW  at /jobs/in   (host reads O_NOFOLLOW)
          out/  host writes    → bound RO  at /jobs/out  (agent can't plant here)
          run/  host-private   → NOT bound anywhere      (.sbatch + SLURM logs)
        Living outside `state/<uuid>/` is deliberate and load-bearing: that
        subtree is bound into the container, so a mailbox inside it would be
        agent-writable and the directional guarantee above would be void.
        """
        return self.root / "hpc-jobs" / uuid

    def for_project(self, uuid: str, *,
                    scratch_root: Path | None = None,
                    packages_root: Path | None = None,
                    home_root: Path | None = None) -> ProjectPaths:
        """Paths for one project.

        Each `*_root`, when given, places THAT component outside the state tree
        — see ProjectPaths.scratch_dir for why these are separate roots rather
        than more subdirectories of `base`.
        """
        base = self.state_dir / uuid
        if scratch_root is None:
            scratch_root = self._configured_component_root(uuid, "scratch")
        if packages_root is None:
            packages_root = self._configured_component_root(uuid, "packages")
        if home_root is None:
            home_root = self._configured_component_root(uuid, "home")
        return ProjectPaths(base=base, scratch_root=scratch_root,
                            packages_root=packages_root, home_root=home_root)

    def _configured_component_root(self, uuid: str,
                                   component: str) -> Path | None:
        """Per-project root for one component from the cluster profile, or None.

        RESOLVED HERE, AT THE ONE CHOKEPOINT, because there are nine callers of
        `for_project` and a per-caller lookup is nine chances to forget — which
        is how the two-tier storage layout stayed unimplemented while `hpc setup`
        advertised it (DN-008).

        ONE resolver for all three components, not three copies. `scratch` was
        the only one until, when the maintainer's cluster hit a
        500,000-INODE cap on $HOME — a different axis from bytes, and one the
        movable component did not address at all: `packages/` (conda/pip, ~8.6 GB
        but hundreds of thousands of tiny files) is what exhausted it, and
        `packages_dir` was hardcoded. Adding a second and third near-identical
        resolver is the shape the simplicity tzar exists to refuse.

        Returns None — the historical behaviour, the component under the state
        root — when no profile is active, no template is set, or the template
        cannot be expanded. Silence rather than a guess: putting a user's data
        somewhere they did not ask for is worse than leaving it where it was.
        """
        attr = f"{component}_template"
        try:
            from botainer.state import cluster_profile as _cp
            prof = _cp.active_profile()
        except Exception as exc:                                  # noqa: BLE001
            # A broken/unparseable cluster.yaml lands here. Swallowing it
            # silently would be the same defect as the unresolved-variable case
            # below: the user edited a config, it did nothing, and nothing said
            # so. Warn and fall back — never fail a launch over a storage path.
            _warn_component_fallback(component, "(cluster profile)",
                                     str(exc)[:120],
                                     "cluster profile could not be read")
            return None
        tmpl = (getattr(prof, attr, "") or "").strip() if prof else ""
        if not tmpl:
            return None
        # Expand $USER / ${USER} / ~ the way a site writes them in a profile.
        expanded = os.path.expanduser(os.path.expandvars(tmpl))
        if "$" in expanded or "[" in expanded:
            # An unset variable, or a literal placeholder a human was meant to
            # replace ("[project-code]"). Fall back — but SAY SO. This was a
            # silent fallback and that was wrong: 6 shipped profiles interpolate
            # ${SLURM_JOBID}, which is unset on a login node, so the user
            # configured two-tier storage, got HOME-volume scratch, and had no
            # way to find out. A fallback nobody is told about is indisitinguishable
            # from a setting that never took.
            _warn_component_fallback(component, tmpl, expanded,
                                     "unresolved variable")
            return None
        root = Path(expanded)
        if not root.is_absolute():
            _warn_component_fallback(component, tmpl, expanded,
                                     "not an absolute path")
            return None
        return root / uuid

    def _configured_scratch_root(self, uuid: str) -> Path | None:
        """Back-compat shim for the single-component era. Prefer
        `_configured_component_root(uuid, "scratch")`."""
        return self._configured_component_root(uuid, "scratch")


_SCRATCH_FALLBACK_WARNED: set[str] = set()

#: What each relocatable component costs you when its template does not take.
#: Named per component because the two failures are NOT the same: scratch and
#: home are byte-heavy, packages is byte-light and INODE-heavy, and a user
#: staring at "quota exceeded" needs to know which number they just blew.
_COMPONENT_COST = {
    "scratch": ("/scratch", "the agent's bulk intermediates — large files"),
    "packages": ("/packages",
                 "pip/npm/conda installs — HUNDREDS OF THOUSANDS OF SMALL "
                 "FILES. This exhausts an INODE quota long before a byte "
                 "quota; a 500,000-inode home cap is a real limit on real "
                 "clusters"),
    "home": ("/home/user",
             "the container's HOME: tool caches (pip, npm, HuggingFace) — "
             "both large and file-heavy"),
}


def _warn_component_fallback(component: str, tmpl: str, expanded: str,
                             why: str) -> None:
    """Tell the user a storage template did not take, once per template.

    Deduped because `for_project` is called several times per command and a
    warning repeated five times is a warning nobody reads.
    """
    key = f"{component}:{tmpl}"
    if key in _SCRATCH_FALLBACK_WARNED:
        return
    _SCRATCH_FALLBACK_WARNED.add(key)
    mount, cost = _COMPONENT_COST.get(component, (f"/{component}", "data"))
    import sys as _sys
    _sys.stderr.write(
        f"[botainer] {component} template not usable ({why}):\n"
        f"             template: {tmpl}\n"
        f"             expanded: {expanded}\n"
        f"           {mount} falls back to the state dir, on whatever volume\n"
        f"           MY_BOTAINER points at. That directory holds {cost}.\n"
        f"           Fix `{component}.template` in your cluster.yaml — it must\n"
        f"           expand to an absolute path with no unset variables.\n"
    )


def _warn_scratch_fallback(tmpl: str, expanded: str, why: str) -> None:
    """Back-compat shim. Prefer `_warn_component_fallback`."""
    _warn_component_fallback("scratch", tmpl, expanded, why)


@dataclass(frozen=True)
class ProjectPaths:
    base: Path

    #: Where THIS project's `/scratch` lives, when it must not live under
    #: `base`. None = the historical behaviour, `base/scratch`.
    #:
    #: WHY A SEPARATE ROOT AND NOT ANOTHER SUBDIR. `base` holds three things
    #: with three different lifetimes and three different storage needs:
    #:
    #:   state     (credentials, project uuid, plugins, .sif)  small, MUST persist
    #:   packages  (pip/npm caches)                            large, rebuildable
    #:   scratch   (the agent's bulk intermediates)            large, disposable
    #:
    #: On HPC those belong on different filesystems: state on $HOME (durable,
    #: quota-limited), scratch on the cluster's scratch (big, fast, auto-purged).
    #: Collapsing them into one root forces the user to choose which one to get
    #: wrong — put the root on $HOME and bulk data eats the quota; put it on
    #: scratch and the purge takes the credentials.
    #:
    #: The design specified from the start that a cluster profile's scratch
    #: template "resolves at session compose" (internal design note DN-008).
    #: It was printed by `hpc setup` and never resolved, so the product
    #: ADVISED a two-tier layout it did not implement.
    scratch_root: Path | None = None

    #: Where THIS project's `/packages` and `/home/user` live, when they must
    #: not live under `base`. Same mechanism and same reason as `scratch_root`.
    #:
    #: ADDED because the lifetimes table above was right and only
    #: one third of it was implemented. The maintainer's cluster has a hard
    #: 500,000-INODE cap on $HOME — a different axis from bytes — and
    #: `packages/` exhausted it: conda/pip trees are only ~8.6 GB but are
    #: hundreds of thousands of tiny files. Every session then died writing
    #: `.meta.json.tmp` with `[Errno 122] Disk quota exceeded`. Moving
    #: `/scratch`, the one relocatable component, did not help at all, because
    #: scratch is the byte-heavy one and this was an inode failure.
    #:
    #: The alternative on the table was letting `MY_BOTAINER` itself point off
    #: $HOME. That was rejected: it relocates `shared-auth/` — the OAuth
    #: credential — onto group-shared project space, trading a property
    #: ("credentials are in your private home") for a filter ("the permissions
    #: on this shared dir must stay correct forever"). Per-component roots buy
    #: the inodes without spending the guarantee.
    packages_root: Path | None = None
    home_root: Path | None = None

    @property
    def meta_path(self) -> Path:
        return self.base / "meta.json"

    @property
    def protected_hashes_path(self) -> Path:
        return self.base / "protected.hashes"

    @property
    def data_dir(self) -> Path:
        return self.base / "data"

    @property
    def sessions_dir(self) -> Path:
        return self.base / "sessions"

    @property
    def locks_dir(self) -> Path:
        return self.base / "locks"

    @property
    def packages_dir(self) -> Path:
        """Per-project persistent package install dir, mounted at /packages.

        Holds pip/, node_modules/, conda_envs/, julia_depot/, R_libs/, etc.
        Persists across sessions. User can `rm -rf` this dir to recover disk;
        agent will re-install on next session.

        Defaults under `base`. When `packages_root` is set it lives there
        instead — the HPC case, and specifically the INODE case: this is the
        component that exhausts a file-count quota, being byte-light and
        file-heavy. See the `packages_root` field.
        """
        if self.packages_root is not None:
            return self.packages_root
        return self.base / "packages"

    @property
    def scratch_dir(self) -> Path:
        """Per-project ephemeral scratch dir, mounted at /scratch.

        For downloads, raw data, intermediate results. The AGENT_HINTS.md
        tells the agent to treat this as ephemerality-by-policy: the user
        may delete it at any time without warning.

        Defaults under `base`. When `scratch_root` is set it lives there
        instead — the HPC case, where bulk intermediates belong on the
        cluster's scratch filesystem and NOT on the quota-limited home that
        must hold the credentials. See the `scratch_root` field for why these
        are separate roots.
        """
        if self.scratch_root is not None:
            return self.scratch_root
        return self.base / "scratch"

    @property
    def home_dir(self) -> Path:
        """Per-project writable HOME for the container, mounted at /home/user.

        The container runs as the host uid (`--user`), which has NO entry in the
        image's /etc/passwd and no home dir — so HOME would fall back to `/`
        (unwritable), breaking every home-dependent tool: `npm`/`npx` (~/.npmrc,
        ~/.npm), `pip` (~/.cache), and Claude Code (which then splatters its
        runtime dir into /tmp). Binding this host dir as HOME fixes that at the
        root. Persistent across sessions (caches live here); the user may delete
        it to reclaim disk.

        Defaults under `base`. When `home_root` is set it lives there instead.
        The caches named above — `~/.npm`, `~/.cache/pip`, HuggingFace — are the
        OTHER inode-heavy component besides `/packages`, so a cluster with a
        file-count quota usually wants both moved. See the `packages_root`
        field for the failure that prompted this.
        """
        if self.home_root is not None:
            return self.home_root
        return self.base / "home"

    def plugin_data_dir(self, plugin_name: str) -> Path:
        return self.data_dir / plugin_name


@dataclass(frozen=True)
class ProjectListEntry:
    uuid: str
    last_path: str
    path_exists: bool
    paths: tuple[str, ...]
    display_name: str = ""
    last_session_at: str = ""        # ISO8601 or empty
    last_session_runtime: str = ""   # "docker" / "apptainer" / "mock" / ""
    sessions_dir_count: int = 0      # number of session-record dirs (rough activity proxy)


_VALID_STATE_DIR_PATTERN = re.compile(r"^[A-Za-z0-9._/\-~]+$")


def _resolve_state_root() -> Path:
    """Return the host state-dir root.

    Honors `MY_BOTAINER` env var (validated). On no-env, defaults to `~/.botainer/`.

    Per insecure-defaults H: explicitly refuse `..` traversal even though the
    char-class regex allows the constituent `.` chars. The default behavior is
    `~/.botainer/`; non-default `MY_BOTAINER` values are validated for both
    syntax AND structural safety.
    """
    val = os.environ.get("MY_BOTAINER")
    if val:
        if not _VALID_STATE_DIR_PATTERN.match(val):
            raise ValueError(
                f"MY_BOTAINER contains invalid characters: {val!r}. Allowed: [A-Za-z0-9._/-~]"
            )
        expanded = Path(val).expanduser()
        if ".." in expanded.parts:
            raise ValueError(
                f"MY_BOTAINER contains '..' (path traversal): {val!r}"
            )
        # Task #210: resolve symlinks BEFORE validating the post-resolve
        # path. Previously we validated the literal string then resolved;
        # an attacker who controls ~/.foo as a symlink to /etc could set
        # MY_BOTAINER=~/.foo → passes pattern + `..` check → resolves to
        # /etc → launcher writes policy.yaml there.
        resolved = expanded.resolve()
        home = Path.home().resolve()
        # Post-resolve sanity: must be under $HOME or an explicit allowlist.
        # /tmp etc. would be a footgun in production. /workspace is the dev
        # container convention; allow it. Test environments use pytest tmp
        # paths under TMPDIR — allow paths under TMPDIR + /tmp + /var/folders
        # (mac pytest) so the test suite can set MY_BOTAINER to its own
        # fixture dirs without bypassing the safety guard for real users.
        import tempfile as _tempfile
        tmpdir = Path(_tempfile.gettempdir()).resolve()
        allowed_roots = [home]
        if Path("/workspace").exists():
            allowed_roots.append(Path("/workspace").resolve())
        # Allow test tmp roots (pytest, unittest).
        allowed_roots.extend([
            tmpdir,
            Path("/tmp").resolve() if Path("/tmp").exists() else home,
            Path("/var/folders").resolve() if Path("/var/folders").exists() else home,
            Path("/private/tmp").resolve() if Path("/private/tmp").exists() else home,
        ])
        if not any(_is_within(resolved, root) for root in allowed_roots):
            # A REFUSAL, NOT A TRACEBACK. This fires on the single most likely
            # misstep of a storage migration: the user points MY_BOTAINER at a
            # big volume and syncs, and every command — INCLUDING `botainer
            # doctor`, the one they would run to find out why — dies in nine
            # frames of click internals. Same class as the disk-quota traceback
            # (§4cv): a condition with a specific remedy, surfaced as a crash.
            #
            # The remedy is specific, so say it. Wanting bulk data off $HOME is
            # legitimate and supported — just per component, so credentials
            # stay on private storage.
            from botainer.core.refusal import RefusalCategory, Refused
            raise Refused(
                RefusalCategory.STATE_ROOT_NOT_ALLOWED,
                f"MY_BOTAINER points at {resolved}, which is outside your home "
                f"directory ({home}).\n"
                f"\n"
                f"  botainer keeps the state root on private storage on "
                f"purpose: it holds your\n"
                f"  logins (shared-auth/, state/<id>/data/). Moving the whole "
                f"root would put those\n"
                f"  on whatever volume you chose — often group-readable "
                f"project space.\n"
                f"\n"
                f"  If you are trying to get BULK DATA off a full or "
                f"inode-limited home, move\n"
                f"  the components instead — that is supported and is what you "
                f"want:\n"
                f"\n"
                f"      packages:  {{template: <big-volume>/botainer-pkgs}}   "
                f"# pip/npm/conda\n"
                f"      home:      {{template: <big-volume>/botainer-home}}   "
                f"# tool caches\n"
                f"      scratch:   {{template: <big-volume>/botainer-scratch}} "
                f"# bulk output\n"
                f"\n"
                f"  in $MY_BOTAINER/cluster.yaml. To move data you already "
                f"have:\n"
                f"      tools/pkg/relocate-storage.sh --component packages "
                f"--dest <big-volume>/botainer-pkgs\n"
                f"\n"
                f"  Then unset MY_BOTAINER (or point it back under {home}). "
                f"See docs/STORAGE.md §2b."
            )
        return resolved
    return Path.home() / ".botainer"


def _is_within(path: Path, root: Path) -> bool:
    """True if `path` is `root` or a descendant of `root`."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_user_state_dir(create_if_missing: bool = True) -> StatePaths:
    root = _resolve_state_root()
    paths = StatePaths(root=root)
    if create_if_missing:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths.plugins_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Tighten any modes that may have been created with a wider umask earlier.
        for p in (root, paths.state_dir, paths.plugins_dir, paths.logs_dir):
            with contextlib.suppress(OSError):
                os.chmod(p, 0o700)
    return paths


def subprocess_state_env(project_uuid: str | None = None) -> dict[str, str]:
    """Return the canonical state-related env vars for any plugin subprocess.

    The convention every plugin hook and subprocess script in this repo
    relies on:

        BOTAINER_STATE_ROOT     user-wide root, honors MY_BOTAINER
        BOTAINER_STATE_DIR      per-project dir (<root>/state/<uuid>/),
                                empty when no project context
        BOTAINER_PROJECT_UUID   the project's UUID (empty if not in a project)

    Convention: subprocess scripts read these names. They do NOT read
    `MY_BOTAINER` directly — that is the USER-facing var the launcher
    resolves once via `_resolve_state_root`; subprocesses then see the
    resolved value via `BOTAINER_STATE_ROOT`. Reading `MY_BOTAINER`
    directly from a plugin script bypasses validation and any future
    override (test fixtures, future per-session redirects, etc.).

    Every launcher dispatcher (auth, manifest commands, host_pre_launch,
    pre_session, post_session) should use this helper to populate the
    subprocess env block — drift between them has caused multiple bugs
    in v0.1.0 development.
    """
    paths = ensure_user_state_dir(create_if_missing=False)
    env: dict[str, str] = {
        "BOTAINER_STATE_ROOT": str(paths.root),
    }
    if project_uuid:
        env["BOTAINER_STATE_DIR"] = str(paths.state_dir / project_uuid)
        env["BOTAINER_PROJECT_UUID"] = project_uuid
    return env


def ensure_project_dirs(
    paths: StatePaths,
    uuid: str,
    *,
    project_name: str | None = None,
) -> ProjectPaths:
    """Create the per-project state dir tree.

    Canonical layout remains `state/<uuid>/` (UUID is durable; project root
    can be renamed without breaking identity). For user discoverability, we
    additionally maintain a symlink at `state/by-name/<name>-<short-uuid>/`
    pointing to the canonical UUID dir. Pure-add; no breakage if name is
    unavailable.

    Args:
        paths: StatePaths to operate on.
        uuid: project UUID (canonical identity).
        project_name: optional human-readable basename (typically
            project_root.name). When provided, a by-name symlink is created
            or refreshed.
    """
    proj = paths.for_project(uuid)
    proj.base.mkdir(parents=True, exist_ok=True, mode=0o700)
    proj.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    proj.sessions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    proj.locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    proj.packages_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    proj.scratch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for p in (
        proj.base,
        proj.data_dir,
        proj.sessions_dir,
        proj.locks_dir,
        proj.packages_dir,
        proj.scratch_dir,
    ):
        with contextlib.suppress(OSError):
            os.chmod(p, 0o700)
    # Create per-language subdirs so env-var routing finds existing dirs.
    for lang_subdir in (
        "pip", "node_modules", "conda_envs", "julia_depot",
        "R_libs", "cargo", "go",
    ):
        (proj.packages_dir / lang_subdir).mkdir(parents=True, exist_ok=True, mode=0o700)
    if project_name:
        _refresh_by_name_symlink(paths, uuid, project_name)
    return proj


_SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _refresh_by_name_symlink(
    paths: StatePaths, uuid: str, project_name: str
) -> None:
    """Create/refresh `state/by-name/<safe_name>-<short_uuid>` → ../<uuid>/.

    Best-effort: failures don't propagate. The symlink is convenience UX,
    not a correctness requirement.

    Safety:
    - project_name is sanitized to [A-Za-z0-9._-]; other chars are replaced
      with '_'. Defends against path traversal or shell-special names.
    - Symlink target is always the same root state_dir; we don't create
      links to arbitrary paths.
    - A name that is already taken BY A DIFFERENT PROJECT is never stolen.
      See _unique_link_path.
    """
    try:
        safe_name = _sanitize_for_dirname(project_name)
        link_dir = paths.state_dir / "by-name"
        link_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        link_path = _unique_link_path(link_dir, safe_name, uuid)
        if link_path is None:
            return
        target = Path("..") / uuid  # relative; stays inside state_dir
        # Refresh: remove OUR OWN existing link (same project, re-created).
        # _unique_link_path guarantees this is either absent or ours.
        if link_path.is_symlink() or link_path.exists():
            try:
                link_path.unlink()
            except OSError:
                return
        link_path.symlink_to(target)
        _prune_stale_aliases(link_dir, uuid, keep=link_path)
    except OSError:
        return


def _prune_stale_aliases(link_dir: Path, uuid: str, *, keep: Path) -> None:
    """Leave exactly ONE alias per project: the one matching its current name.

    `project_name` is `project_root.name`, re-read on every compose, so renaming
    the project folder used to ADD an alias and leave the old one in place —
    `old-name-abcd1234` and `new-name-abcd1234` both live, both pointing at the
    same project, forever, one more per rename. Nobody designed that; it is what
    you get from a create-if-absent with no counterpart.

    Removing the stale one is a deliberate choice over keeping both: the user
    renamed the folder, so the alias following it is what they asked for, and an
    alias list that accumulates every name a project has ever had is worse than
    useless for the thing aliases exist to do. (Contrast the collision case,
    where an incumbent alias is never touched — there the two names belong to
    two DIFFERENT projects, and taking one away would silently repoint someone's
    path at a stranger.)

    Best-effort like its caller: a failure here leaves a spare alias, which is
    untidy, not wrong.
    """
    try:
        entries = list(link_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry == keep or not entry.is_symlink():
            continue
        try:
            if Path(os.readlink(entry)).name != uuid:
                continue
        except OSError:
            continue
        with contextlib.suppress(OSError):
            entry.unlink()


# How many hex chars of the uuid the by-name alias carries. 8 is 32 bits: with
# a hundred projects on a host the chance that ANY two share a prefix is about
# one in 900,000, and only same-named projects can actually clash, which pushes
# it to ~1e-9 in practice. Those odds are why 8 was chosen and they are fine.
#
# The odds are NOT the reason this is safe, though. _unique_link_path is: it
# never gives two projects the same alias, at any probability. That distinction
# matters because the alias is a convenience today (a collision would lose a
# shortcut) but the plan is to invert it — make `<name>-<short>` the real
# directory and the bare uuid a symlink to it. Under that layout a collision
# stops being a lost shortcut and becomes TWO PROJECTS SHARING ONE STATE
# DIRECTORY: same /packages, same credential dir. Nothing about that should
# rest on a probability estimate, so it doesn't.
_SHORT_UUID_LEN = 8


def _unique_link_path(link_dir: Path, safe_name: str, uuid: str) -> Path | None:
    """The alias path for this project, never one another project already owns.

    Returns the path to use, or None if we cannot determine ownership safely
    (in which case the caller writes nothing — a missing alias is recoverable,
    a wrong one is not).

    On collision the uuid portion is lengthened (`name-deadbeef`, then
    `name-deadbeef1`, ...) rather than a counter being appended to the name:
    the extra characters still identify the project, whereas `name-2` does not.

    LIMITATION, stated rather than papered over: which of two colliding
    projects gets the 8-char form depends on which registered FIRST. Once
    created an alias never moves — a project that already owns one keeps it,
    because the loop returns the existing link when it points at us — so this
    is stable for any real install. But rebuilding `by-name/` from scratch in a
    different order could swap two colliding projects' aliases. Making that
    impossible would mean rewriting the incumbent's alias out from under it,
    which is worse for anyone who has the path in a shell history. Given a
    collision needs the same name AND the same 32 bits, this is the right
    trade; it is a documented limit, not an oversight.
    """
    flat = uuid.replace("-", "")
    for length in range(_SHORT_UUID_LEN, len(flat) + 1):
        candidate = link_dir / f"{safe_name}-{flat[:length]}"
        if not (candidate.is_symlink() or candidate.exists()):
            return candidate
        try:
            existing = os.readlink(candidate)
        except OSError:
            # Present but not a symlink we can read — someone else's, or not
            # ours to interpret. Try a longer suffix rather than clobbering.
            continue
        if Path(existing).name == uuid:
            return candidate          # already ours; refresh in place
    return None


# When a name survives sanitizing with nothing usable left. `project-<uuid>`
# reads as a project; `_-<uuid>` reads as a bug.
_FALLBACK_DIRNAME = "project"

_WIN_RESERVED_BASES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _sanitize_for_dirname(name: str) -> str:
    """Map an arbitrary project name to a safe directory name.

    THE CHARSET IS AN ALLOWLIST, deliberately: only [A-Za-z0-9._-] can ever
    reach a path we construct. That is a property, not a filter — it holds no
    matter what the input is. Widening it to "real Unicode" would buy prettier
    names and cost a great deal: macOS stores filenames NFD and Linux NFC, so
    the same name typed on two machines is two different byte strings; there is
    case-folding; and every path we print then needs shell quoting. Not worth
    it for a convenience alias.

    But an allowlist applied to raw input throws away information it doesn't
    have to. Three steps run BEFORE it, so the allowlist keeps its guarantee
    while the result stays readable:

    1. Accents are folded (NFKD, drop combining marks): `Café` -> `Cafe`,
       `Müller` -> `Muller`. Previously these became `Caf_` and `M_ller`.
    2. Whitespace becomes `-` (`my project` -> `my-project`). The alias is
       `<name>-<short-uuid>` and callers split on the LAST `-`, so an inner `-`
       is unambiguous.
    3. A run of separators collapses to its first character, so
       `my.project (v2)` -> `my.project-v2`, not `my.project-_v2_`. A `_` the
       user typed themselves survives.

    KNOWN LIMIT, and it is a real one: a name in a script with no Latin
    equivalent — Chinese, Cyrillic, Hebrew, Arabic — survives none of this and
    falls back to `project`, so those users get `project-<short-uuid>` and the
    uuid does all the identifying. Transliterating properly needs a dependency
    we do not want. The right fix is to let people SET the alias rather than
    derive it; until then this says it is degrading rather than emitting a
    row of underscores.

    Also (sharp-edges review MEDIUM 5) Windows-reserved basenames (con, prn,
    aux, nul, com1-9, lpt1-9) are prefixed with `_`; they break WSL projects
    where `state/by-name/` may sit on an NTFS mount.
    """
    import unicodedata

    if not name:
        return _FALLBACK_DIRNAME
    # 1. Fold accents to their base letters. NFKD splits `é` into `e` + a
    #    combining acute; dropping category Mn leaves the `e`.
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", name)
        if not unicodedata.combining(c)
    )
    # 2. Whitespace reads better as a dash than an underscore.
    folded = re.sub(r"\s+", "-", folded)
    # 3. The allowlist. Everything else becomes `_`, runs collapse to one.
    cleaned = "".join(c if _SAFE_NAME_PATTERN.match(c) else "_" for c in folded)
    # Collapse a run of separators to its FIRST character, so `my.project (v2)`
    # -> `my.project-v2` (the space's dash wins) while a user's own single `_`
    # in `Muller_data` is left alone.
    cleaned = re.sub(r"[-_]+", lambda m: m.group(0)[0], cleaned)
    cleaned = cleaned.strip("._-")
    # Nothing survived (non-Latin script, or punctuation only).
    if not cleaned or not any(ch.isalnum() for ch in cleaned):
        return _FALLBACK_DIRNAME
    if cleaned.lower() in _WIN_RESERVED_BASES:
        cleaned = "_" + cleaned
    return cleaned[:64]


def write_default_policy(root: Path, *, allow_tiers: list[str], force: bool = False) -> bool:
    """Write a minimal `policy.yaml`.

    Behavior:
      - File doesn't exist → write fresh defaults.
      - File exists + force=False → MERGE: add any newer fields that
        v0.1.x knows about but the on-disk file is missing (e.g.,
        `default_auth_mode` after it was introduced). Existing values
        are NEVER changed; only missing fields are added in place.
        This keeps users from getting stuck on the old default of a
        field that was added after their initial setup.
      - File exists + force=True → write fresh defaults, clobbering
        anything the user had customized.

    Returns True if any change was written; False if the file was
    already up to date.
    """
    policy_path = root / "policy.yaml"
    default = {
        "version": "policy-v1",
        "plugins": {
            "allowed_tiers": allow_tiers,
            "first_party_allowlist": [
                "agent-claude",
                "agent-codex",
                "git",
                "hpc-launcher",
                "wolfram-sidecar",
            ],
            "third_party_must_be_declarative": True,
        },
        "mounts": {
            "trusted_source_roots": [],
            "extra_targets_allowlist": ["/data", "/datasets", "/shared", "/scratch", "/mnt"],
        },
        "network": {
            # Re-audit round 3 (#2, CRITICAL onboarding): the user-policy
            # default_mode is a CEILING (most-permissive a project may request).
            # It MUST be at least as permissive as the project default `botainer
            # init` writes (network.mode: internet) and the NetworkPolicy class
            # default (internet) — otherwise intersect() lowers the effective
            # ceiling to `none` and the user's VERY FIRST `botainer start` hard-
            # refuses (`internet` > ceiling `none`). `internet` is also the
            # documented frictionless default (setup.py / README). A user/admin
            # tightens this deliberately; the out-of-the-box value must not break
            # the documented zero-to-running path.
            "default_mode": "internet",
            "allowed_endpoint_groups": ["anthropic", "openai", "pypi", "github-https"],
        },
        "capabilities": {
            "kernel_caps_keep_allowed": [],
            # Per sharp-edges F5: include known-dangerous env vars that can
            # alter what gets loaded by language runtimes / tools. Real
            # allowlist refactor is v0.1.x; for now this is "broad denylist".
            "env_var_denylist": [
                # Dynamic linker
                "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_BIND_NOW",
                # Python
                "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME",
                # Node
                "NODE_PATH", "NODE_OPTIONS",
                # Perl / Ruby
                "PERL5LIB", "PERL5OPT", "RUBYLIB", "RUBYOPT",
                # Shell injection vectors
                "BASH_ENV", "ENV", "PROMPT_COMMAND",
                # Java / JVM
                "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
                # Julia / R
                "JULIA_LOAD_PATH", "R_HOME",
                # Conda
                "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
                # Git
                "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                # SSL / certs
                "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
            ],
        },
        "naming": {
            "host_managed_folder": ".botainer",
        },
        # AUTH-PRODUCT-PLAN §9 / user direction:
        # default is shared with init-time + session-launch warning.
        "default_auth_mode": "shared",
    }
    if policy_path.exists() and not force:
        # Merge mode: add fields the on-disk file is missing.
        try:
            import yaml as _yaml
            existing = _yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            existing = {}
        if not isinstance(existing, dict):
            return False
        added: list[str] = []
        merged = dict(existing)
        for k, v in default.items():
            if k not in merged:
                merged[k] = v
                added.append(k)
        if not added:
            return False
        # Task #265: atomic-rename + restrictive perms at create.
        from botainer.state.secure_write import write_secure
        # Write back; preserve YAML formatting via safe_dump.
        write_secure(policy_path, yaml.safe_dump(merged, sort_keys=False), mode=0o600)
        # Log to stderr so the user sees what changed without us
        # taking over stdout.
        import sys as _sys
        _sys.stderr.write(
            f"[botainer setup] policy.yaml: added missing fields {added}\n"
        )
        return True
    # Fresh write (no file, or --force).
    from botainer.state.secure_write import write_secure
    write_secure(policy_path, yaml.safe_dump(default, sort_keys=False), mode=0o600)
    return True


def list_projects() -> list[ProjectListEntry]:
    """Enumerate all known projects on this host."""
    paths = ensure_user_state_dir(create_if_missing=False)
    if not paths.state_dir.exists():
        return []
    out: list[ProjectListEntry] = []
    for child in sorted(paths.state_dir.iterdir()):
        if not child.is_dir():
            continue
        # Skip the by-name/ symlink-only directory
        if child.name == "by-name":
            continue
        meta = child / "meta.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        path_hist_raw = data.get("path_history", [])
        # path_history can be either old-shape (list[str]) or new shape (list[dict])
        path_hist: list[str] = []
        if isinstance(path_hist_raw, list):
            for entry in path_hist_raw:
                if isinstance(entry, str):
                    path_hist.append(entry)
                elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    path_hist.append(entry["path"])
        last = path_hist[-1] if path_hist else "(no path recorded)"
        # Derive a friendly display name.
        display_name = str(data.get("display_name") or "")
        if not display_name and path_hist:
            display_name = Path(path_hist[-1]).name
        # Surface last session's metadata if we have any session records.
        sessions_dir = child / "sessions"
        last_session_at = ""
        last_session_runtime = ""
        sessions_count = 0
        if sessions_dir.exists() and sessions_dir.is_dir():
            session_subdirs = [d for d in sessions_dir.iterdir() if d.is_dir()]
            sessions_count = len(session_subdirs)
            if session_subdirs:
                # Pick the most-recently-modified session record dir.
                try:
                    newest = max(session_subdirs, key=lambda d: d.stat().st_mtime)
                    spec_path = newest / "spec.json"
                    if spec_path.exists():
                        spec_data = json.loads(spec_path.read_text())
                        last_session_at = str(spec_data.get("composed_at") or "")
                        last_session_runtime = str(spec_data.get("runtime") or "")
                except (OSError, json.JSONDecodeError):
                    pass
        out.append(
            ProjectListEntry(
                uuid=child.name,
                last_path=last,
                path_exists=Path(last).exists() if path_hist else False,
                paths=tuple(path_hist),
                display_name=display_name,
                last_session_at=last_session_at,
                last_session_runtime=last_session_runtime,
                sessions_dir_count=sessions_count,
            )
        )
    return out


def read_meta(proj_paths: ProjectPaths) -> dict[str, object]:
    if not proj_paths.meta_path.exists():
        return {}
    try:
        return dict(json.loads(proj_paths.meta_path.read_text()))
    except (json.JSONDecodeError, OSError):
        return {}


def write_meta(proj_paths: ProjectPaths, data: dict[str, object]) -> None:
    # Task #265: atomic-rename + restrictive perms at create.
    from botainer.state.secure_write import write_secure
    write_secure(
        proj_paths.meta_path,
        json.dumps(data, indent=2, sort_keys=True),
        mode=0o600,
    )


def path_history_records(meta: dict[str, object]) -> list[dict[str, str]]:
    """THE reader for `path_history`, in most-recently-seen-last order.

    Exists because there were two readers and they disagreed. `append_path_history`
    writes records — `{path, first_seen, last_seen, host}` — having migrated from
    a plain list of strings. `list_projects` was taught both shapes.
    `identity.resolve_identity` was NOT: it did `[str(x) for x in raw_hist]`, so
    every record stringified to `"{'first_seen': ..., 'path': ...}"`, which

      - never equalled the current path, so the "this is where I saw it last"
        branch could not be reached, and
      - was not a real path, so `Path(last_known).exists()` was always False,
        which sent EVERY case down the "prior path is gone -> moved, record
        silently" branch.

    Net effect: the clone/fork prompt was unreachable. Copying a project and
    running it from the copy silently reused the ORIGINAL's state dir — same
    /packages, same scratch, same per-project credentials — with no prompt and
    no message. That is precisely what the prompt exists to prevent.

    Everything that reads `path_history` goes through here now, so the two
    cannot drift apart again.
    """
    raw = meta.get("path_history") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for x in raw:
        if isinstance(x, str):
            out.append({"path": x, "host": "", "first_seen": "", "last_seen": ""})
        elif isinstance(x, dict):
            p = str(x.get("path", "") or "")
            if p:
                out.append({
                    "path": p,
                    "host": str(x.get("host", "") or ""),
                    "first_seen": str(x.get("first_seen", "") or ""),
                    "last_seen": str(x.get("last_seen", "") or ""),
                })
    return out


def last_known_path(meta: dict[str, object], *, host: str | None = None) -> str | None:
    """Where this project was last seen ON THIS HOST, else anywhere.

    Records are host-keyed on purpose (see `append_path_history`): the same
    project reached from a laptop and from a cluster has different paths, and
    without host-keying every switch between the two would look like a move.
    Falls back to the most recent record of any host, so a state dir written
    before hostnames were recorded still resolves.
    """
    records = path_history_records(meta)
    if not records:
        return None
    if host is None:
        import socket as _socket
        try:
            host = _socket.gethostname()
        except OSError:
            host = ""
    for rec in reversed(records):
        if rec["host"] and rec["host"] == host:
            return rec["path"]
    return records[-1]["path"]


def append_path_history(meta: dict[str, object], path: str) -> dict[str, object]:
    """Append `path` to `path_history`. Returns updated meta.

    Per v0.0.13 (project-identity.md) + sharp-edges + prior-art reviews:
    the path_history shape is a list of records:

        [{path, first_seen, last_seen, host}, ...]

    Host-keyed so Mac and HPC entries don't get confused. On move-detection,
    a known (host, path) tuple has its last_seen bumped; an unknown tuple
    appends a new record.

    Backward-compat: if the existing meta.json has the old shape
    (list of strings), this function migrates to the new shape silently.
    """
    import datetime
    import socket as _socket

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    host = _socket.gethostname()
    raw = meta.get("path_history") or []
    assert isinstance(raw, list), f"path_history must be a list, got {type(raw)}"

    # Migrate string-format records to dict-format on the fly.
    migrated: list[dict[str, str]] = []
    for x in raw:
        if isinstance(x, str):
            migrated.append({
                "path": x,
                "first_seen": now,
                "last_seen": now,
                "host": "(unknown; migrated from old schema)",
            })
        elif isinstance(x, dict):
            migrated.append({
                "path": str(x.get("path", "")),
                "first_seen": str(x.get("first_seen", now)),
                "last_seen": str(x.get("last_seen", now)),
                "host": str(x.get("host", host)),
            })

    # Find an existing (host, path) record; remove it (MRU semantics).
    existing: dict[str, str] | None = None
    for i, entry in enumerate(migrated):
        if entry["path"] == path and entry["host"] == host:
            existing = migrated.pop(i)
            break

    # Append updated/new record at the end (most recently seen).
    if existing is not None:
        existing["last_seen"] = now
        migrated.append(existing)
    else:
        migrated.append({
            "path": path,
            "first_seen": now,
            "last_seen": now,
            "host": host,
        })

    # Truncate to N most recent (by list position).
    migrated = migrated[-_PATH_HISTORY_KEEP:]

    meta["path_history"] = migrated
    return meta
