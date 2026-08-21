"""Shared helpers for the hpc-launcher host_helper scripts.

Cluster detection, sbatch flag building, and config loading live here so
`submit`, `status`, and `attach` don't duplicate logic. v0.0.x's 400–600
lines of duplicated bash become one small Python module.
"""
from __future__ import annotations

import os
import shutil
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# AUDIT (H4): the root-owned SITE policy (capability/network
# ceiling). Module-level so it is (a) the single source of truth and (b)
# monkeypatchable in tests. Bound into the compute-node container by
# to_apptainer_argv so the ceiling is enforced on HPC (see load_site_policy).
_SITE_POLICY_PATH = Path("/etc/botainer/policy.yaml")


@dataclass(frozen=True)
class ClusterProfile:
    """Defaults a cluster prefers (auto-detected from hostname).

    Task #189 SHIM: this is a host_helper-local SUBSET of
    botainer.state.cluster_profile.ClusterProfile. Two classes existed
    in parallel with drifting defaults. v0.1.0 freezes this one's
    surface area: do NOT add new fields here. Any new cluster-profile
    field goes in the launcher's class first; if host_helper needs it,
    add a parser that flows the value across the env boundary
    (BOTAINER_CLUSTER_PROFILE as JSON; a tracked follow-up).
    """
    name: str
    default_partition: str | None
    default_account: str | None
    default_time_minutes: int
    notes: str = ""


# Task #164 (CLAUDE.md "no personal info in dist"): the v0.0.x port
# carried a hardcoded Yale fallback. v0.1 distributable plugins must
# NOT reference a specific institution. Cluster defaults are now
# user-configured exclusively (via `botainer hpc setup` writing
# cluster.yaml). Removed: _YALE_HOSTS, _YALE_DEFAULT, and the
# hostname-match branch in detect_cluster (`for token in _YALE_HOSTS`).
# Yale-grace example lives in cluster_profiles/us-yale-grace.yaml as an
# OPT-IN profile under cluster_profiles/ (dist-marked example, not a
# default).


def _load_user_cluster_yaml() -> ClusterProfile | None:
    """Load ~/.botainer/cluster.yaml (or $MY_BOTAINER/cluster.yaml).

    Parses the `cluster-profile-v1` schema written by
    `botainer.state.cluster_profile.write_user_profile`:

        version: cluster-profile-v1
        cluster: {name, hostname_patterns, description}
        slurm:
          default_partition: <str>
          default_account:   <str>
          default_time_minutes: <int>
          partitions:
            <name>: {max_time_minutes, max_cpus, max_memory_gb, gpu_types}
        ...

    Codex review 45 finding #3: hpc-launcher previously read a flat shape
    (`data["partitions"]`, `data["account"]`) that never matched what
    `botainer hpc setup` wrote, so the user's cluster.yaml was silently
    ignored and either the Yale fallback or a refusal kicked in.
    """
    state_root = Path(
        os.environ.get("BOTAINER_STATE_ROOT")
        or os.environ.get("MY_BOTAINER")
        or str(Path.home() / ".botainer")
    )
    cluster_yaml = state_root / "cluster.yaml"
    if not cluster_yaml.exists():
        return None
    try:
        data = yaml.safe_load(cluster_yaml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != "cluster-profile-v1":
        # Unknown schema version → silently skip. The launcher writes
        # cluster-profile-v1; anything else is either future or hand-rolled.
        return None
    cluster = data.get("cluster") or {}
    slurm = data.get("slurm") or {}
    if not isinstance(cluster, dict) or not isinstance(slurm, dict):
        return None

    default_partition = slurm.get("default_partition") or None
    if isinstance(default_partition, str) and not default_partition.strip():
        default_partition = None
    default_account = slurm.get("default_account") or None
    if isinstance(default_account, str) and not default_account.strip():
        default_account = None
    raw_time = slurm.get("default_time_minutes", 240)
    try:
        default_time_minutes = int(raw_time)
    except (TypeError, ValueError):
        default_time_minutes = 240
    if default_time_minutes <= 0 or default_time_minutes > 7 * 24 * 60:
        default_time_minutes = 240

    # If `default_partition` wasn't explicitly set, fall back to the
    # first non-scavenge partition in the partitions map. The map shape
    # is {<name>: {max_time_minutes, max_cpus, ...}} per
    # cluster_profile.write_user_profile.
    partitions_map = slurm.get("partitions") or {}
    if default_partition is None and isinstance(partitions_map, dict):
        for name, spec in partitions_map.items():
            if not isinstance(name, str) or name == "scavenge":
                continue
            default_partition = name
            if isinstance(spec, dict):
                cap = spec.get("max_time_minutes")
                if isinstance(cap, int) and 0 < cap < default_time_minutes:
                    default_time_minutes = cap
            break

    return ClusterProfile(
        name=str(cluster.get("name") or "user-defined"),
        default_partition=default_partition,
        default_account=default_account,
        default_time_minutes=default_time_minutes,
        notes=f"loaded from {cluster_yaml}",
    )


def detect_cluster() -> ClusterProfile:
    """Resolve which cluster we're on, preferring user-configured profile.

    Order:
      1. ~/.botainer/cluster.yaml (written by `botainer hpc setup`) —
         if present, this wins.
      2. Fallback to unknown profile with no defaults (#164: no
         hardcoded institution-specific hostname matching at v0.1).
    """
    user_profile = _load_user_cluster_yaml()
    if user_profile is not None:
        return user_profile
    # No hostname-based fallback at v0.1 (#164: no Yale-specific
    # defaults in dist code). Unknown cluster → require user setup.
    # Unknown cluster — no defaults at all (no partition, no account,
    # no time). _do_submit refuses without partition+account; this
    # surfaces as a clean refusal pointing the user at `botainer hpc setup`.
    # Sharp-edges F-7 (independent read): previously this defaulted to
    # 60 minutes which would silently submit short jobs on cluster
    # autodetect failure. Better to make the user pick `--time` or
    # configure cluster.yaml.
    return ClusterProfile(
        name="unknown",
        default_partition=None,
        default_account=None,
        default_time_minutes=0,
        notes="Unknown cluster; user must specify partition + account + "
              "time (or run `botainer hpc setup` to write a cluster.yaml).",
    )


def have_slurm() -> bool:
    return bool(shutil.which("sbatch") and shutil.which("squeue"))


# Security-audit Finding 1 (CONFIRMED-PoC class): SLURM writes the
# job's --output/--error as the UNCAGED user (slurmstepd), FOLLOWING symlinks
# (stock SLURM has no O_NOFOLLOW). If that path lives in a directory the caged
# agent can write to, a prompt-injected agent pre-plants a symlink there and
# slurmstepd writes attacker content into an arbitrary host file (~/.bashrc,
# ~/.ssh/authorized_keys) → full policy-cage escape. The v0.0.x PoC used a
# client-chosen id; v0.1.0 uses %j (SLURM-assigned) but the class still holds
# via a DIRECTORY symlink on the output dir (no jobid needed).
#
# FIX (directional isolation — the audit's #1 recommendation): SLURM output
# goes to a HOST-ONLY per-project dir that is NEVER bind-mounted into the
# container (unlike state/<uuid>/, which is RW-bound by to_apptainer_argv). The
# caged agent has no write path here, so it cannot pre-position the target at
# all. `botainer hpc logs` reads it host-side; the agent never needs to (its
# stdout IS this file). Single source of truth so the --output directive,
# prepare_host_paths, and the hpc-logs reader can't drift (mirrored + parity-
# tested in botainer/state/dir.py::StatePaths.hpc_job_output_dir).
def _job_output_dir(state_root: Path, project_uuid: str) -> Path:
    """Host-only dir for SLURM --output/--error (Finding-1 directional
    isolation). NOT under state/<uuid>/ → NOT bound into the container."""
    return Path(state_root) / "hpc-job-outputs" / project_uuid


def _has_agent_credential(
    state_root: Path, project_uuid: str, agent_name: str, profile: str
) -> bool:
    """True if the active agent appears to have a credential on the host, in
    EITHER shared-auth (shared mode) OR the per-project profile dir (isolated
    mode). Conservative — ANY non-empty file under either dir counts — so a
    valid login of either mode is never false-refused.

    Recon T-D: when NEITHER exists, the user never ran `botainer auth login`;
    to_apptainer_argv then SILENTLY skips the shared-auth bind (line ~436
    `if shared_for_agent.is_dir()`), the job lands on the compute node, and
    the agent fails to authenticate with a confusing error after a queue wait.
    submit.main refuses early instead (mirrors the time=0/partition gates)."""
    if not agent_name:
        return True  # non-agent session — no credential requirement
    shared = Path(state_root) / "shared-auth" / f"agent-{agent_name}"
    isolated = (
        Path(state_root) / "state" / project_uuid / "data"
        / f"agent-{agent_name}" / "profiles" / profile
    )
    for d in (shared, isolated):
        try:
            if d.is_dir() and any(
                f.is_file() and f.stat().st_size > 0 for f in d.iterdir()
            ):
                return True
        except OSError:
            continue
    return False


@dataclass(frozen=True)
class SubmissionPlan:
    project_root: Path
    project_uuid: str
    state_root: Path
    profile: str
    partition: str | None
    account: str | None
    time_minutes: int
    cpus: int
    memory_gb: int | None
    gpus: int
    gpu_type: str | None
    apptainer_image: str
    submission_mode: str  # submit | attach | here
    existing_jobid: str | None
    # HPC-IMPL #1: agent identity (claude/codex/...). Used by make_plan for the
    # image resolver + the credential-presence gate; the per-agent binds now come
    # from the composed spec (agent-*-shared pre_session) via the adapter.
    agent_name: str = ""
    # §A19: when the nudge plugin is enabled, the sbatch script wraps
    # the `apptainer exec` call in `screen -dmS botainer-${SLURM_JOB_ID}`
    # on the compute node so that `botainer nudge` running on the login
    # node can `srun --overlap` into the compute node and run
    # `screen -X stuff -- ...`. The screen name uses ${SLURM_JOB_ID}
    # (not the in-container session_id, which is generated AFTER the
    # apptainer exec and is therefore unknown to the sbatch script).
    # This matches the convention in hpc-launcher attach.py.
    nudge_enabled: bool = False
    # Audit T7 (HPC consent-gate parity): security-posture disclosure shown in
    # the login-node confirmation (the compute node has no TTY, so this is the
    # consent surface). Display-only — NOT interpolated into argv/paths.
    plugins_enabled: tuple[str, ...] = ()
    network_mode: str = ""
    # Compose-at-submit (task #52): the
    # `apptainer exec …` argv composed on the LOGIN NODE by
    # composition.compose_agent_exec_for_hpc — i.e. the direct
    # ApptainerAdapter.render_argv of the fully-composed spec (agent
    # entrypoint + all binds + env + module trampoline). submit.py sets it
    # before rendering. When populated, `to_apptainer_argv` returns it
    # verbatim and the compute-node container execs the AGENT (which IS in
    # the .sif), NOT `botainer start --in-container` (which isn't). Empty on
    # the legacy path / before submit.py composes.
    agent_exec_argv: tuple[str, ...] = ()
    # The composed session's id + on-host session dir, recorded post-submit
    # (jobid/node provenance) so `hpc logs`/`stop`/`status` can find the job.
    session_id: str = ""
    session_dir: str = ""

    def __post_init__(self) -> None:
        # AC7 image-injection backstop (completeness lens):
        # `_resolve_apptainer_image` only validates config-sourced images
        # (cases 1-2). The `--image` CLI override in submit.py rebuilds the
        # plan via `plan.__class__(**{**plan.__dict__, **overrides})` and
        # NEVER re-runs that resolver, so `botainer hpc --image --bind=/etc`
        # reached `apptainer exec <image>` as a flag-injectable positional.
        # Validating here makes the frozen dataclass the universal chokepoint
        # (mirrors SessionSpec.image's field_validator): every construction —
        # make_plan, the override merge, the auto_yes rebuild, tests — is
        # guarded. Generated .sif paths (resolver cases 3-5) are absolute and
        # pass cleanly. Raises SystemExit on a flag-like/whitespace image.
        _reject_flaglike_image(self.apptainer_image)
        # AC7 validator-parity audit: project_uuid arrives via
        # `BOTAINER_PROJECT_UUID` (make_plan reads it from env). It is
        # interpolated UNQUOTED into the generated sbatch script
        # (`# project_uuid: ...`, `#SBATCH --job-name=...`, `--output=...`),
        # so a newline yields sbatch-directive / shell-line injection that
        # runs as the user on the compute node — confirmed HIGH from a
        # tampered, git-shareable .botainer/project-id reaching `botainer hpc
        # submit`. Reject any non-canonical uuid here (the standalone mirror
        # of identity._validate_uuid + SessionSpec._validate_project_uuid).
        _reject_non_canonical_uuid(self.project_uuid)
        # AUDIT (CRITICAL C1): agent_name is interpolated into
        # apptainer --bind source/target paths (and the .sif resolver) on the
        # compute node, RW + mkdir, on a standalone path that skips
        # validate_mount_plan. Reject traversal/flag/whitespace here so the
        # frozen plan is the universal chokepoint (mirrors the image/uuid
        # backstops above and config.py's `agent:` field_validator on the
        # laptop path). Empty is allowed (the bind block is guarded by it).
        _reject_traversal_agent(self.agent_name)
        # AUDIT (C1 sibling, caught by the adversarial review):
        # profile reaches the SAME RW per-agent --bind source + on-host
        # mkdir/chmod as agent_name (to_apptainer_argv / prepare_host_paths),
        # on a path that skips validate_mount_plan. Validate it at the same
        # frozen-plan chokepoint so "the frozen plan is the universal
        # chokepoint" holds for EVERY path-bearing field. make_plan coerces an
        # empty/unset BOTAINER_PROFILE to "default", so a token is expected.
        _reject_unsafe_profile(self.profile)
        # Compose-at-submit never-regress chokepoint. DO NOT DELETE THESE
        # ASSERTIONS as redundant: they are the only thing keeping the sbatch
        # cage identical to the adapter cage, and the two drifting apart is the
        # regression class (S2) this whole restructure exists to end. When the
        # argv is populated it IS the compute-node exec line baked into the
        # sbatch script — the frozen plan:
        #   • it must be an `apptainer exec` invocation,
        #   • it must carry the §4 cage flags (the sbatch cage == the adapter
        #     cage now — a single source; the S2 regression class dies),
        #   • the resolved image must appear in it (render_sbatch_script splices
        #     the SLURM_TMPDIR pair by indexing the image),
        #   • NO element may be the bare `botainer` CLI. The agent .sif has no
        #     botainer; the entire point is that the container execs the AGENT,
        #     never `botainer start --in-container` (the FATAL this fixes).
        if self.agent_exec_argv:
            _argv = list(self.agent_exec_argv)
            if _argv[:2] != ["apptainer", "exec"]:
                raise SystemExit(
                    "refused: agent_exec_argv must begin with `apptainer exec` "
                    f"(got {_argv[:2]!r})."
                )
            for _flag in ("--containall", "--cleanenv", "--no-privs"):
                if _flag not in _argv:
                    raise SystemExit(
                        f"refused: agent_exec_argv is missing the §4 cage flag "
                        f"{_flag!r}; the compute-node cage must match the adapter."
                    )
            _dc = _argv.index("--drop-caps") if "--drop-caps" in _argv else -1
            if _dc < 0 or _dc + 1 >= len(_argv) or _argv[_dc + 1] != "all":
                raise SystemExit(
                    "refused: agent_exec_argv is missing `--drop-caps all`."
                )
            if self.apptainer_image not in _argv:
                raise SystemExit(
                    "refused: agent_exec_argv does not contain the resolved "
                    f"image {self.apptainer_image!r}."
                )
            # Never-botainer: the in-container exec line is the argv AFTER the
            # image. Match on BASENAME (review LOW-2), not exact-element — this
            # catches both `botainer` and `/usr/bin/botainer`, while NOT
            # false-refusing the image name itself (`botainer-agent-claude.sif`,
            # which contains "botainer" but sits BEFORE the exec line and whose
            # basename is not "botainer"). The agent .sif has no botainer CLI; the
            # container must exec the agent entrypoint, never `botainer start
            # --in-container`.
            import os.path as _osp
            _img_i = _argv.index(self.apptainer_image)
            for _a in _argv[_img_i + 1:]:
                if _osp.basename(_a) == "botainer":
                    raise SystemExit(
                        "refused: agent_exec_argv execs `botainer` inside the "
                        "container. The agent image bundles no botainer CLI; "
                        "compose-at-submit must exec the agent entrypoint, never "
                        "`botainer start --in-container`."
                    )
        # session_id is interpolated UNQUOTED into the provenance `echo` in
        # render_sbatch_script (a double-quoted shell string that intentionally
        # contains $(hostname) / ${SLURM_JOB_ID}); hold the frozen-plan chokepoint
        # discipline and validate its shape so it can never become a
        # shell-injection sink (review LOW-3). session_dir is shlex-quoted at its
        # sink, but reject control chars there too (defense in depth).
        # Audit (defense-in-depth): these run whenever the fields are
        # SET — NOT only inside the `if self.agent_exec_argv:` block above. The
        # provenance echo that consumes session_id is gated on `session_dir`
        # (render_sbatch_script), so keying the guard on `agent_exec_argv` left a
        # theoretical decoupling (both are always co-set + host-generated today,
        # so not attacker-reachable — but the guard now tracks its actual sink).
        import re as _re_pi
        if self.session_id and not _re_pi.fullmatch(r"[A-Za-z0-9._-]+", self.session_id):
            raise SystemExit(
                f"refused: session_id {self.session_id!r} contains characters "
                f"outside [A-Za-z0-9._-]; it is interpolated into the sbatch "
                f"provenance echo (shell-injection sink)."
            )
        if self.session_dir and any(c in self.session_dir for c in ("\x00", "\n", "\r")):
            raise SystemExit(
                f"refused: session_dir {self.session_dir!r} contains a "
                f"NUL/newline/CR."
            )

    def to_sbatch_argv(self) -> list[str]:
        argv = ["sbatch"]
        if self.partition:
            argv += [f"--partition={self.partition}"]
        if self.account:
            argv += [f"--account={self.account}"]
        argv += [f"--time={self.time_minutes // 60:02d}:{self.time_minutes % 60:02d}:00"]
        argv += [f"--cpus-per-task={self.cpus}"]
        if self.memory_gb is not None:
            argv += [f"--mem={self.memory_gb}G"]
        if self.gpus > 0:
            gres = f"gpu:{self.gpu_type}:{self.gpus}" if self.gpu_type else f"gpu:{self.gpus}"
            argv += [f"--gres={gres}"]
        argv += [f"--job-name=botainer-{self.project_uuid[:8]}"]
        # Security-audit Finding 1: --output goes to the HOST-ONLY
        # job-output dir (NOT the container-bound state/<uuid>/ subtree), so a
        # caged agent can't symlink the slurmstepd-written output path. Single
        # source of truth via _job_output_dir. (Matches render_sbatch_script.)
        argv += [f"--output={_job_output_dir(self.state_root, self.project_uuid)}/slurm-%j.out"]
        return argv

    def to_apptainer_argv(self) -> list[str]:
        """The `apptainer exec ...` argv for the compute-node run.

        Compose-at-submit: this is not a re-derivation of the cage — it is the
        argv composed on the login node by
        composition.compose_agent_exec_for_hpc and stored on the plan by
        submit.py -- i.e. the direct ApptainerAdapter.render_argv of the
        fully-composed spec (agent entrypoint + all binds + env + module
        trampoline, behind the SAME `--containall --cleanenv --no-privs
        --drop-caps all` cage the direct-apptainer path emits). The
        compute-node container execs the AGENT, never `botainer start
        --in-container` (the .sif has no botainer CLI -- the FATAL this
        restructure fixes). The rendering + validation live in the adapter (a
        single source shared with the direct path); the __post_init__
        chokepoint pins the never-regress invariants on the frozen plan.
        """
        return list(self.agent_exec_argv)

    def prepare_host_paths(self) -> None:
        """Create host-side bind sources before `apptainer exec` runs.

        Apptainer refuses to launch if a bind's source path doesn't
        exist (no auto-mkdir like docker has on some platforms). Several
        of our bind sources (per-project state subtree, per-agent
        profile dir) are created by hooks that run INSIDE the container
        — too late. This method is called from submit.py before render.
        """
        per_project_state = self.state_root / "state" / self.project_uuid
        per_project_state.mkdir(parents=True, exist_ok=True)
        # Slurm output dir — sbatch writes <dir>/slurm-<jobid>.out and won't
        # auto-mkdir parents. Security-audit Finding 1: this is the
        # HOST-ONLY dir (NOT bound into the container), created 0700, with a
        # symlink TRIPWIRE: if the dir (or its parent) is already a symlink,
        # refuse loudly — a symlink here is near-unambiguous evidence of an
        # attempted output-redirect escape. Because the dir isn't
        # container-writable this is belt-and-suspenders (the directional
        # isolation is the real fix), but the tripwire catches any other
        # tamper + satisfies the audit's interim recommendation.
        import os as _os
        import stat as _stat
        outputs_dir = _job_output_dir(self.state_root, self.project_uuid)
        for _check in (outputs_dir.parent, outputs_dir):
            try:
                _st = _os.lstat(_check)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if _stat.S_ISLNK(_st.st_mode):
                raise SystemExit(
                    f"hpc-launcher: refusing to submit — SLURM output path "
                    f"component {_check} is a SYMLINK. SLURM writes --output as "
                    f"your uncaged user following symlinks; a symlink here is "
                    f"an attempted host-file-write escape (security-audit "
                    f"2026-07-01 Finding 1). Remove it and re-run."
                )
        outputs_dir.mkdir(parents=True, exist_ok=True)
        try:
            _os.chmod(outputs_dir, 0o700)
        except OSError:
            pass
        if self.agent_name:
            per_project_agent = (
                per_project_state / "data"
                / f"agent-{self.agent_name}" / "profiles" / self.profile
            )
            per_project_agent.mkdir(parents=True, exist_ok=True)
            try:
                import os as _os
                _os.chmod(per_project_agent, 0o700)
            except OSError:
                pass

    def render_sbatch_script(self) -> str:
        """Render a self-contained sbatch script (one-shot; no dtach trickery).

        Sharp-edges F7 + insecure-defaults L9: defensive validation of
        every field that flows into #SBATCH directives. Newlines or
        shell metacharacters could let attacker-controlled config inject
        additional directives. Re-checks even though parse_args also
        validates (defense in depth — config files aren't parsed by
        parse_args).
        """
        import re as _re
        _SAFE = _re.compile(r"^[A-Za-z0-9._-]+$")
        # AC7 validator-parity audit: project_uuid was MISSING
        # from this loop, yet it is interpolated unquoted into the script
        # (comment + --job-name + --output path) — the confirmed HIGH sbatch
        # injection. It is canonical-or-empty by SubmissionPlan.__post_init__;
        # this is the defense-in-depth check AT the sink. A canonical UUID
        # matches _SAFE (hyphens allowed); the `if value` guard skips the
        # allowed empty id (and any empty partition/account/gpu_type).
        for name, value in (
            ("partition", self.partition),
            ("account", self.account),
            ("gpu_type", self.gpu_type),
            ("project_uuid", self.project_uuid),
        ):
            if value and not _SAFE.fullmatch(str(value)):
                raise ValueError(
                    f"refused: {name}={value!r} contains characters "
                    f"outside [A-Za-z0-9._-]. This is rejected to prevent "
                    f"newline injection into the sbatch script."
                )
        # AC7 review parity note: state_root is ALSO interpolated
        # unquoted into `#SBATCH --output={output_path}` (output_path is rooted
        # at state_root). It is host-private (launcher-set BOTAINER_STATE_ROOT /
        # MY_BOTAINER / ~/.botainer — a higher trust boundary than the
        # git-shareable project-id), so it is not the primary attacker vector,
        # but a control char would inject the same way. A real filesystem path
        # never contains NUL/newline/CR (it legitimately may contain '/', '.',
        # spaces — so the _SAFE charset above does NOT apply); reject only the
        # injection chars.
        if any(c in str(self.state_root) for c in ("\x00", "\n", "\r")):
            raise ValueError(
                f"refused: state_root={self.state_root!r} contains a NUL/"
                f"newline/CR; it is interpolated unquoted into the sbatch "
                f"--output directive (script injection)."
            )
        # Task #197: SLURM_TMPDIR needs runtime expansion at sbatch-exec
        # time. We splice the unquoted `--env SLURM_TMPDIR="$SLURM_TMPDIR"`
        # between the apptainer image and the inner command so bash actually
        # evaluates it. Done here (script render) instead of to_apptainer_argv
        # because shlex.quote single-quotes `${...}` into a literal.
        _argv_full = self.to_apptainer_argv()
        try:
            _img_idx = _argv_full.index(self.apptainer_image)
            _pre = _argv_full[:_img_idx]
            _post = _argv_full[_img_idx:]
        except ValueError:
            _pre, _post = _argv_full, []
        body = (
            " ".join(_shell_quote(a) for a in _pre)
            # GUARDED, and it must stay guarded. The script runs under
            # `set -euo pipefail`, so a bare $SLURM_TMPDIR is an "unbound
            # variable" fatal on any site that does not export it — and it
            # is NOT a stock Slurm export, it needs job_container/tmpfs or
            # a site TmpFS setting. Unguarded, the job died before the
            # agent started, with a bash error in the Slurm output file and
            # nothing else. Every other reference to this variable in the
            # repo already uses the ${VAR:-/tmp} form; this one was spliced
            # in for nudge (#197) when nudge was its only consumer, and
            # became unconditional later.
            + ' --env "SLURM_TMPDIR=${SLURM_TMPDIR:-/tmp}" '
            + " ".join(_shell_quote(a) for a in _post)
        )
        # Slurm output path. Uses %j (jobid substitution) so per-job logs
        # are separable. Security-audit Finding 1 (directional
        # isolation): this dir is HOST-ONLY — NOT the container-bound
        # state/<uuid>/ subtree — so slurmstepd (which writes here as the
        # UNCAGED user, following symlinks) can't be redirected by a symlink
        # the caged agent planted. prepare_host_paths() creates it 0700 +
        # refuses a symlink there (tripwire). bug (kept fixed):
        # previous version omitted --output entirely; output went to the
        # user's shell CWD.
        output_dir = _job_output_dir(self.state_root, self.project_uuid)
        output_path = output_dir / "slurm-%j.out"
        # §A19: when the nudge plugin is enabled, wrap the apptainer
        # exec under a compute-node-local `screen` session named
        # `botainer-<sid>`. The Slurm job stays alive while the screen
        # session does (waiting via `screen -ls` polling, since `screen
        # -dmS` returns immediately). When the agent exits, screen
        # ends, the wait loop terminates, the sbatch script exits, and
        # Slurm marks the job COMPLETED.
        #
        # The `screen -ls` poll on the same node is cheap. We avoid
        # signal/wait on the screen pid because screen forks twice
        # and the pid we'd see is the wrapper, not the session.
        if self.nudge_enabled:
            # The screen-session name is computed at exec time on the
            # compute node from ${SLURM_JOB_ID} so we don't need to
            # know the jobid before sbatch returns. Same name used by
            # hpc-launcher attach.py for `screen -r botainer-<jobid>`.
            #
            # T3-7: GUARD the screen path. The sbatch body runs
            # under `set -euo pipefail`, and many HPC compute nodes are minimal
            # (no `screen`). An unguarded `screen -dmS` there fails and — under
            # `set -e` — kills the whole job before the agent ever launches
            # (cryptic "screen: command not found" in the SLURM output). Nudge
            # is a convenience; the agent running is the point. So: if `screen`
            # is present, wrap as before (nudge + `hpc attach` work); if absent,
            # warn LOUDLY in the SLURM output and `exec` the agent directly so
            # the job still runs (nudge/attach just unavailable for this job).
            # `command -v` in an `if` condition is exempt from `set -e`, and the
            # caged `{body}` is byte-identical on both paths (no cage change).
            run_section = (
                'if command -v screen >/dev/null 2>&1; then\n'
                '    SCREEN_SID="botainer-${SLURM_JOB_ID:?SLURM_JOB_ID unset; '
                'sbatch script must run under Slurm}"\n'
                f'    screen -dmS "$SCREEN_SID" {body}\n'
                '    # Wait for the screen session to terminate (agent exit).\n'
                '    while screen -ls 2>/dev/null | '
                'grep -qE "[0-9]+\\.${SCREEN_SID}\\b"; do\n'
                '        sleep 30\n'
                '    done\n'
                'else\n'
                '    echo "botainer: nudge is enabled but '"'"'screen'"'"' is not '
                'on this compute node'"'"'s PATH — running the agent directly '
                'WITHOUT a screen session. \\`botainer nudge\\` and \\`hpc attach\\` '
                '(screen -r) will NOT work for this job. Install screen in the '
                'agent image / load a screen module, or disable the nudge plugin, '
                'to restore them." >&2\n'
                f'    exec {body}\n'
                'fi\n'
            )
        else:
            run_section = f"exec {body}\n"
        # Compose-at-submit provenance (design #12): the runtime adapter is NOT
        # invoked on the sbatch path, so record the actual compute node + a
        # header into the SLURM output here, as the UNCAGED user, BEFORE exec.
        # `<session_dir>/node` lives in the host-only session dir (NOT bound
        # into the container under the narrow compose-at-submit binds), so the
        # caged agent cannot forge it. Best-effort (|| true): a provenance
        # write must never abort the job under `set -e`.
        provenance = ""
        if self.session_dir:
            _sd = _shell_quote(self.session_dir)
            _node = _shell_quote(self.session_dir.rstrip("/") + "/node")
            provenance = (
                f'echo "botainer: session {self.session_id} job '
                f'${{SLURM_JOB_ID:-?}} node $(hostname)"\n'
                f"mkdir -p {_sd} 2>/dev/null || true\n"
                f"printf '%s\\n' \"$(hostname)\" > {_node} 2>/dev/null || true\n"
            )
        return (
            "#!/bin/bash\n"
            "# Generated by botainer hpc-launcher. Submit via `sbatch <this-file>`.\n"
            f"# project_uuid: {self.project_uuid}\n"
            f"#SBATCH --job-name=botainer-{self.project_uuid[:8]}\n"
            f"#SBATCH --time={self.time_minutes // 60:02d}:{self.time_minutes % 60:02d}:00\n"
            f"#SBATCH --cpus-per-task={self.cpus}\n"
            f"#SBATCH --output={output_path}\n"
            + (f"#SBATCH --partition={self.partition}\n" if self.partition else "")
            + (f"#SBATCH --account={self.account}\n" if self.account else "")
            + (f"#SBATCH --mem={self.memory_gb}G\n" if self.memory_gb is not None else "")
            + (
                f"#SBATCH --gres=gpu:{(self.gpu_type + ':') if self.gpu_type else ''}{self.gpus}\n"
                if self.gpus > 0 else ""
            )
            + "\n"
            "set -euo pipefail\n"
            + provenance
            + run_section
        )


def _shell_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def load_plugin_config(project_root: Path) -> dict[str, Any]:
    p = project_root / ".botainer" / "config.yaml"
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return dict((data.get("plugins") or {}).get("hpc-launcher") or {})


def _reject_flaglike_image(value: str) -> str:
    """HPC parity for AC7's image-injection fix: a config-sourced
    apptainer image must not be parsable as an `apptainer exec` flag or
    break argv tokenization. Mirror of
    botainer.core.spec.validate_image_reference (inlined — this
    host_helper is deliberately standalone, no botainer import). Reject
    leading '-' and NUL/newline/CR/tab/space; the recorded/conventional
    .sif paths below are botainer-generated absolute paths and skip this.
    """
    if not value:
        raise SystemExit("hpc-launcher: apptainer_image is empty")
    if value[0] == "-" or any(c in value for c in ("\x00", "\n", "\r", "\t", " ")):
        raise SystemExit(
            f"hpc-launcher: refusing apptainer image {value!r} — it starts "
            f"with '-' or contains whitespace/control chars (argv injection "
            f"into `apptainer exec`). Use a plain .sif path or image ref."
        )
    return value


def _reject_non_canonical_uuid(value: str) -> str:
    """HPC-side mirror of botainer.core.identity._validate_uuid (inlined —
    this host_helper is deliberately standalone, no botainer import).

    project_uuid is written UNQUOTED into the generated sbatch script, so a
    non-canonical value (esp. one containing a newline) injects directives /
    shell lines that run as the user on the compute node. The empty string is
    allowed: make_plan defaults it to "" when BOTAINER_PROJECT_UUID is unset,
    and an empty id produces only harmless empty/`_outputs`-rooted paths — no
    injection. Any non-empty value MUST parse as a canonical UUID.
    """
    if not value:
        return value
    try:
        return str(_uuid.UUID(value))
    except (ValueError, TypeError):
        raise SystemExit(
            f"hpc-launcher: refusing project_uuid {value!r} — not a canonical "
            f"UUID. project_uuid is interpolated into the generated sbatch "
            f"script; a non-UUID value (e.g. containing a newline) would "
            f"inject #SBATCH directives or shell lines. Check that "
            f".botainer/project-id was not tampered with."
        ) from None


def _reject_unsafe_profile(value: str) -> str:
    """HPC-side mirror of botainer.core.spec.validate_profile_name and
    cli/start.py's _PROFILE_RE (inlined — this host_helper is deliberately
    standalone, no botainer import).

    AUDIT (C1 sibling): `profile` is interpolated into the
    per-agent apptainer --bind SOURCE/TARGET (`.../data/agent-<agent>/
    profiles/<profile>`) and the prepare_host_paths `mkdir(parents=True)` +
    `chmod 0o700` on the compute node — the same RW sink as agent_name, and
    it is NOT covered by validate_mount_plan on this standalone path. Without
    this, `profile='../../../../tmp/X'` normpath-collapses to an arbitrary
    host dir bound read-write and created on the host. Constrain to a flat
    token (`^[a-z][a-z0-9_-]{0,31}$`) — no '/', no '.', no leading '-' — so
    traversal is structurally impossible.
    """
    import re as _re
    if not _re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value):
        raise SystemExit(
            f"hpc-launcher: refusing profile {value!r} — must match "
            f"^[a-z][a-z0-9_-]{{0,31}}$. profile is a path component "
            f"(profiles/<profile>) used as an apptainer --bind source and an "
            f"on-host mkdir; a '/' or '.' would traverse out into an arbitrary "
            f"host directory bound read-write into the container."
        )
    return value


def _reject_traversal_agent(value: str) -> str:
    """HPC-side mirror of botainer.core.config.ProjectConfig._validate_agent
    (inlined — this host_helper is deliberately standalone, no botainer
    import).

    AUDIT (CRITICAL C1): the `hpc submit/attach/here` flow never
    builds a ProjectConfig, so config.py's `agent:` field_validator is
    bypassed; `_load_agent_name` returns the raw string and `to_apptainer_argv`
    interpolates it into apptainer `--bind` SOURCE and TARGET paths
    (`state_root/shared-auth/agent-<v>`, `.../data/agent-<v>/profiles/...`),
    RW and mkdir-ed, on the compute node. `validate_mount_plan` (the
    `~/.ssh` source-denylist + realpath backstop) does NOT run on this
    standalone path, so a `..`-laden value traverses out of the intended
    per-agent dirs into an arbitrary host directory bound read-write into the
    container. Same config-vs-mirror drift class as the image/uuid sinks
    (CAPABILITY-SURFACE §4b/§4c). The empty string is allowed (a "no agent"
    project; the bind block is skipped). Any non-empty value must be a flat
    token — no '/', no '..', no leading '-', no whitespace/control chars —
    exactly the charset config.py enforces on the laptop path.
    """
    if not value:
        return value
    if "/" in value or ".." in value:
        raise SystemExit(
            f"hpc-launcher: refusing agent {value!r} — it becomes a plugin "
            f"name (agent-<v>) interpolated into apptainer --bind source/target "
            f"paths on the compute node; a '/' or '..' would traverse out of "
            f"the per-agent dir into an arbitrary host path bound read-write. "
            f"Check that .botainer/config.yaml's `agent:` was not tampered with."
        )
    if value[0] == "-":
        raise SystemExit(
            f"hpc-launcher: refusing agent {value!r} — must not start with '-' "
            f"(would be parsed as a flag where the resolved name reaches argv)."
        )
    if any(c in value for c in ("\x00", "\n", "\r", "\t", " ")):
        raise SystemExit(
            f"hpc-launcher: refusing agent {value!r} — must not contain "
            f"whitespace/control chars (argv/path injection)."
        )
    return value


def _load_plugins_enabled(project_root: Path) -> tuple[str, ...]:
    """Top-level `plugins_enabled:` as a tuple (audit T7 consent disclosure)."""
    data = _load_project_top_level(project_root)
    enabled = data.get("plugins_enabled")
    return tuple(enabled) if isinstance(enabled, list) else ()


def _load_network_mode(project_root: Path) -> str:
    """Project `network.mode` for the consent disclosure. "" if unset (the
    effective default applies — `internet` on apptainer)."""
    data = _load_project_top_level(project_root)
    net = data.get("network")
    if isinstance(net, dict) and isinstance(net.get("mode"), str):
        return net["mode"]
    return ""


def _resolve_apptainer_image(
    cfg: dict[str, Any],
    project_root: Path,
    state_root: Path,
    agent_name: str,
) -> str:
    """Pick the apptainer .sif path to exec.

    Codex review HIGH #2: image build and submit must agree on the
    same image. Three competing names existed:
      - `botainer image build agent-claude --runtime apptainer` →
            <state_root>/images/botainer-agent-claude.sif
      - `botainer hpc build agent-claude` (legacy) →
            <state_root>/images/agent-claude.sif
      - hpc-launcher previously defaulted to "botainer-claude.sif".

    Resolution order, most-specific first:
      1. `plugins.hpc-launcher.apptainer_image` from project config.
      2. top-level `image:` from project config (matches docker mode).
      3. installed.lock entry for agent-<name> whose image_digest
         field is the apptainer marker `apptainer:sha256:<hex>:<path>`.
      4. conventional path: <state_root>/images/botainer-agent-<name>.sif.
      5. last-resort legacy default: <state_root>/images/botainer-claude.sif.

    Returning a relative path or non-existent file is OK; the submit
    flow renders a script that apptainer will reject at exec time
    with a clear error.
    """
    # 1. Plugin-specific override.
    if cfg.get("apptainer_image"):
        return _reject_flaglike_image(str(cfg["apptainer_image"]))
    # 2. Top-level project image (matches docker mode + DEPLOY.md docs).
    proj = _load_project_top_level(project_root)
    if proj.get("image"):
        return _reject_flaglike_image(str(proj["image"]))
    # 3. installed.lock recorded path (set by `bot1 image build
    #    --runtime apptainer`, marker format `apptainer:sha256:<hex>:<path>`).
    if agent_name:
        recorded = _recorded_apptainer_image_path(
            state_root, f"agent-{agent_name}",
        )
        if recorded:
            return recorded
    # 4. Conventional path (matches what `bot1 image build` writes).
    if agent_name:
        return str(
            state_root / "images" / f"botainer-agent-{agent_name}.sif"
        )
    # 5. Last-resort legacy default.
    return str(state_root / "images" / "botainer-claude.sif")


def _recorded_apptainer_image_path(
    state_root: Path, plugin_name: str,
) -> str | None:
    """Look up the recorded .sif path for a plugin in installed.lock.

    The image build writes entries of the form
    `apptainer:sha256:<hex>:<absolute-path>` into the image_digest
    field. Parse that out; return None if not recorded or not the
    apptainer format.
    """
    lock_path = state_root / "plugins" / "installed.lock"
    if not lock_path.exists():
        return None
    try:
        import json as _json
        for line in lock_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if d.get("name") != plugin_name:
                continue
            digest = d.get("image_digest")
            if not isinstance(digest, str):
                continue
            if not digest.startswith("apptainer:"):
                continue
            # Format: apptainer:sha256:<hex>:<path>
            parts = digest.split(":", 3)
            if len(parts) == 4:
                return parts[3]
    except OSError:
        return None
    return None


def _load_project_top_level(project_root: Path) -> dict[str, Any]:
    """Read the top-level project config dict. Returns {} on missing /
    malformed file so callers can use `.get(...)` safely."""
    p = project_root / ".botainer" / "config.yaml"
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _load_agent_name(project_root: Path) -> str:
    """Read top-level `agent:` from .botainer/config.yaml. Used by
    to_apptainer_argv to construct per-agent binds. Returns empty
    string if no project config; the caller's bind logic tolerates
    that."""
    data = _load_project_top_level(project_root)
    agent = data.get("agent")
    name = str(agent) if isinstance(agent, str) else ""
    # AUDIT (CRITICAL C1): validate at the source as well as in
    # SubmissionPlan.__post_init__ (chokepoint at every layer). This is the
    # config-vs-CLI sibling of config.py's `agent:` field_validator.
    return _reject_traversal_agent(name)


def _load_nudge_enabled(project_root: Path) -> bool:
    """True if the nudge plugin is listed in top-level
    `plugins_enabled:` in .botainer/config.yaml. Used by render_sbatch_script
    to decide whether to wrap apptainer exec in a host-side screen
    session (§A19)."""
    data = _load_project_top_level(project_root)
    enabled = data.get("plugins_enabled")
    if not isinstance(enabled, list):
        return False
    return "nudge" in enabled


def make_plan(project_root: Path) -> SubmissionPlan:
    """Build a SubmissionPlan from env + config."""
    cfg = load_plugin_config(project_root)
    cluster = detect_cluster()
    uid = os.environ.get("BOTAINER_PROJECT_UUID", "")
    state_root = Path(
        os.environ.get("BOTAINER_STATE_ROOT")
        or os.environ.get("MY_BOTAINER")
        or str(Path.home() / ".botainer")
    )
    # Coerce empty/unset to "default" so the __post_init__ profile validator
    # (which requires a non-empty flat token) doesn't false-reject a present
    # but empty BOTAINER_PROFILE.
    profile = os.environ.get("BOTAINER_PROFILE") or "default"
    # Read top-level cfg.agent for per-agent bind mapping in
    # to_apptainer_argv. Empty string is acceptable — to_apptainer_argv
    # falls back to umbrella binds.
    agent_name = _load_agent_name(project_root)
    apptainer_image = _resolve_apptainer_image(
        cfg, project_root, state_root, agent_name,
    )
    # #160: make_plan stays SIDE-EFFECT-FREE (no module-load subprocess) so
    # status / --dry-run / attach are cheap and hermetic (adversarial-review
    # B2). The submit path derives software_root_binds explicitly AFTER
    # make_plan, once it knows the mode + dry-run intent (see submit.py).
    return SubmissionPlan(
        project_root=project_root.resolve(),
        project_uuid=uid,
        state_root=state_root,
        profile=profile,
        agent_name=agent_name,
        partition=cfg.get("partition") or cluster.default_partition,
        account=cfg.get("account") or cluster.default_account,
        time_minutes=int(cfg.get("time_minutes") or cluster.default_time_minutes),
        cpus=int(cfg.get("cpus") or 1),
        memory_gb=int(cfg["memory_gb"]) if cfg.get("memory_gb") is not None else None,
        gpus=int(cfg.get("gpus") or 0),
        gpu_type=cfg.get("gpu_type"),
        apptainer_image=apptainer_image,
        submission_mode=str(cfg.get("submission_mode") or "submit"),
        existing_jobid=os.environ.get("SLURM_JOB_ID"),
        nudge_enabled=_load_nudge_enabled(project_root),
        plugins_enabled=_load_plugins_enabled(project_root),
        network_mode=_load_network_mode(project_root),
    )
