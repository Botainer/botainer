"""`botainer hpc <verb>` — HPC user's top-level commands.

HPC is a first-class target. Today's plugin commands at
`botainer plugin hpc-launcher submit/status/attach` are too deep; this
surfaces them at top level for the HPC user's daily flow.

Subcommands:
  setup     — first-time wizard: detect cluster, write cluster.yaml
  info      — show active cluster profile
  build     — apptainer build (with cache hints from profile)
  submit    — sbatch a fresh session (delegates to hpc-launcher)
  attach    — srun --overlap into a running session (jobid required)
  status    — squeue-driven status of MY botainer jobs
  list      — alias of status
  cancel    — alias of stop
  stop      — scancel
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.state import cluster_profile as profile_module

# Pool-control request ids are agent-supplied and become a FILENAME component in
# the out/ mailbox (`_write_status`). Unlike job ids they are not 16-hex — the
# shipped flow uses short labels — so allow a permissive but strictly
# NON-PATH-SHAPED charset: no '/', no '.', no NUL, bounded length. See the
# HIGH-9 note in _handle_pool_control (bug audit).
_POOL_CTL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@click.group("hpc", invoke_without_command=True)
@click.pass_context
def hpc(ctx: click.Context) -> None:
    """HPC (Slurm + Apptainer) operations.

    Run `botainer hpc setup` first to configure your cluster.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@hpc.command("setup")
@click.option(
    "--profile",
    "--cluster",
    "profile_name",
    default=None,
    help=(
        "Use a specific bundled profile by name (e.g. generic-slurm, "
        "generic-slurm). `--cluster` is an alias for `--profile`."
    ),
)
@click.option(
    "--account",
    "account",
    default=None,
    help=(
        "Optional: set your default Slurm account (allocation to charge) for "
        "THIS cluster, written to ~/.botainer/cluster.yaml. Only needed if your "
        "cluster requires an explicit account; otherwise Slurm uses your "
        "default. A project can override via plugins.hpc-launcher.account."
    ),
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="No prompts; use autodetect or fail.",
)
@click.option(
    "--probe",
    is_flag=True,
    help=(
        "Run read-only cluster-prereq checks after the profile is written: "
        "apptainer on PATH, sbatch on PATH, Slurm account visible via "
        "sacctmgr, default partition visible via sinfo. Surfaces gaps the "
        "docs imply but the command otherwise doesn't verify."
    ),
)
@handle_refusals
def setup(profile_name: str | None, account: str | None,
          non_interactive: bool, probe: bool) -> None:
    """First-time HPC wizard: detect cluster, write ~/.botainer/cluster.yaml.

    Interactive flow:
    1. Look at the bundled profiles.
    2. Try to autodetect from hostname.
    3. If multiple matches: ask user to pick.
    4. If no match: list bundled profiles and offer to copy `example.yaml`.
    5. Write the chosen profile to `~/.botainer/cluster.yaml`.
    6. Print where each storage component should go on this cluster
       (state in $HOME; per-session scratch via the profile template).
    7. (--probe only) Run read-only cluster-prereq checks.
    """
    hostname = socket.gethostname()
    bundled = profile_module.list_bundled()
    chosen: profile_module.ClusterProfile | None = None

    if profile_name:
        # Accept both the profile's cluster.name AND the filename
        # stem (without .yaml). yale-grace.yaml has cluster.name=grace;
        # users naturally type --cluster yale-grace OR --cluster grace.
        # Both should work.
        bundled_root = profile_module._bundled_profiles_root()
        filename_stems: dict[str, profile_module.ClusterProfile] = {}
        if bundled_root is not None:
            # Build {filename_stem: ClusterProfile} by re-parsing each
            # bundled yaml's cluster.name and matching it back to a
            # ClusterProfile in `bundled`. There's no direct
            # back-reference from ClusterProfile to its yaml path.
            for path in bundled_root.glob("*.yaml"):
                try:
                    import yaml as _yaml
                    data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    cname = ((data.get("cluster") or {}).get("name")) or ""
                    for p in bundled:
                        if p.name == cname:
                            filename_stems[path.stem] = p
                            break
                except (OSError, _yaml.YAMLError):
                    continue
        # Match the catalogue identifier OR any alias the site answers to.
        # Profiles were renamed to unique identifiers (`us-yale-grace`) on
        # because cluster names collide between institutions; the
        # alias keeps `--profile grace` — what a user actually types, and what
        # every existing instruction says — working.
        want = profile_name.strip().lower()
        match = [p for p in bundled if want in p.match_names()]
        if not match and profile_name in filename_stems:
            match = [filename_stems[profile_name]]
        if not match:
            click.secho(
                f"refused: bundled profile {profile_name!r} not found",
                fg="red", err=True,
            )
            click.echo("Available bundled profiles (cluster.name | filename):")
            for p in bundled:
                stems = [s for s, prof in filename_stems.items() if prof is p]
                aliases = f" (also: {', '.join(stems)})" if stems else ""
                click.echo(f"  {p.name:20s} {p.description}{aliases}")
            sys.exit(2)
        chosen = match[0]
    else:
        # Autodetect.
        autodetected = [p for p in bundled if p.matches_hostname(hostname)]
        if len(autodetected) == 1:
            chosen = autodetected[0]
            click.secho(
                f"Autodetected cluster: {chosen.name}", fg="cyan",
            )
            # (trust label now printed at the single write point below, so it
            # cannot be skipped on a branch someone forgets to update)
        elif len(autodetected) > 1:
            click.secho(
                f"Multiple cluster profiles match hostname {hostname!r}:",
                fg="yellow",
            )
            for p in autodetected:
                click.echo(f"  {p.name:20s} {p.description}")
            if non_interactive:
                click.secho(
                    "refused: ambiguous match in --non-interactive mode",
                    fg="red", err=True,
                )
                sys.exit(2)
            choice = click.prompt("Pick one (name)", type=str)
            match = [p for p in autodetected if p.name == choice]
            if not match:
                click.secho("refused: no such profile", fg="red", err=True)
                sys.exit(2)
            chosen = match[0]
        else:
            click.secho(
                f"No bundled profile matches hostname {hostname!r}.",
                fg="yellow",
            )
            if not bundled:
                click.secho(
                    "And no bundled profiles found at all. "
                    "Reinstall botainer.",
                    fg="red", err=True,
                )
                sys.exit(2)
            click.echo("Available bundled profiles:")
            for p in bundled:
                click.echo(f"  {p.name:20s} {p.description}")
            click.echo("")
            if non_interactive:
                click.secho(
                    "refused: --non-interactive but autodetect failed; "
                    "pass --profile <name>",
                    fg="red", err=True,
                )
                sys.exit(2)
            click.echo(
                "Pick a profile to copy as a starting point, or `example` "
                "for a generic template you can edit:"
            )
            choice = click.prompt("Profile name", default="example", type=str)
            match = [p for p in bundled if p.name == choice]
            if not match:
                click.secho("refused: no such profile", fg="red", err=True)
                sys.exit(2)
            chosen = match[0]

    # `--account` sets the user-level default Slurm account for this cluster.
    if account:
        import dataclasses
        chosen = dataclasses.replace(chosen, slurm_default_account=account)

    # Write the profile.
    #
    # THE TRUST LABEL PRINTS HERE, AT THE ONE POINT EVERY BRANCH REACHES.
    # It used to be called from the single-autodetect branch only, so the three
    # branches where the USER IS GUESSING — --profile, the ambiguous-match
    # prompt, and the no-match `example` default — skipped it and landed
    # straight on the green ✓ below. The label was shown when botainer was
    # confident and withheld when it was not, which is backwards, and it
    # defeated the directive the label was built for ("clearly mark what's
    # tested and not tested").
    #
    # Structural rather than "call it on each branch": a chokepoint cannot be
    # forgotten by the next person who adds a fourth way to choose a profile.
    path = profile_module.write_user_profile(chosen)
    click.secho(f"✓ wrote cluster profile to {path}", fg="green")
    _echo_profile_trust(chosen)
    click.echo(f"   cluster:   {chosen.name}")
    click.echo(f"   partition: {chosen.slurm_default_partition or '(set in config)'}")
    click.echo(f"   Lmod:      {chosen.lmod_bootstrap or '(auto-detect)'}")
    click.echo(f"   scratch:   {chosen.scratch_template or '(not configured)'}")
    # The Slurm account is OPTIONAL (many clusters give you a default). Show it
    # if set; otherwise a gentle reminder of how to set it — NOT a "you must".
    if chosen.slurm_default_account:
        click.echo(f"   account:   {chosen.slurm_default_account}")
    else:
        click.echo("   account:   (not set — Slurm will use your default account)")
        click.echo(
            "              If your cluster requires a specific allocation, set it:\n"
            "                user-level:  botainer hpc setup --account <name>   "
            "(~/.botainer/cluster.yaml)\n"
            "                per-project: plugins.hpc-launcher.account in "
            ".botainer/config.yaml"
        )
    click.echo("")

    # Storage-partitioning guidance. Per DN-036:
    # earlier versions blanket-suggested `export MY_BOTAINER=$SCRATCH/.botainer`
    # — which is actively DANGEROUS on every YCRC cluster (and most HPC sites):
    # scratch auto-purges (Grace: 60 days) and would wipe credentials, project
    # UUIDs, built .sif images, and the installed plugin tree. Replace with the
    # the partitioning: state stays in $HOME (small, unrecoverable if lost);
    # per-session scratch comes from the profile's scratch_template (the right
    # thing to put on scratch — it's GENUINELY disposable).
    click.secho(
        "─── Storage layout on this cluster ───────────────────",
        fg="cyan",
    )
    click.echo(
        "Where each botainer component should live (most HPC sites have"
    )
    click.echo(
        "$HOME quota-limited but durable, and $SCRATCH auto-purged):"
    )
    click.echo("")
    click.secho("  ~/.botainer/  (state — keep on $HOME)", fg="green")
    click.echo(
        "    credentials, project UUIDs, session records, installed"
    )
    click.echo(
        "    plugins, built .sif images. Small (~100 MB typical) but"
    )
    click.echo(
        "    LOSING this is unrecoverable (project identity lost; agents"
    )
    click.echo(
        "    refuse to launch; .sif must be rebuilt). Do NOT redirect via"
    )
    click.echo(
        "    `MY_BOTAINER=$SCRATCH/...` — scratch auto-purge wipes it."
    )
    click.echo("")
    if chosen.scratch_template:
        sample = chosen.scratch_template
        click.secho(
            f"  {sample}  (per-session scratch)", fg="green",
        )
        click.echo(
            "    Genuinely per-session disposable. Sourced from this"
        )
        click.echo(
            "    profile's `scratch.template`; set per-cluster in your"
        )
        click.echo("    ~/.botainer/cluster.yaml if needed.")
        if chosen.scratch_cleanup_days:
            click.echo(
                f"    Cluster purges after ~{chosen.scratch_cleanup_days} days "
                f"of no access."
            )
    else:
        click.echo("  (per-session scratch not configured for this cluster —")
        click.echo("   set `scratch.template` in ~/.botainer/cluster.yaml)")
    click.echo("")
    click.echo("Next steps:")
    click.echo("  botainer setup                          # install plugins")
    click.echo("  # build the .sif on a COMPUTE node (apptainer isn't on login nodes):")
    click.echo("  salloc -t 30 -c 4")
    click.echo("  botainer image build agent-claude --runtime apptainer")
    click.echo("  exit   # back to the login node")
    click.echo("  cd ~/project")
    click.echo("  botainer init --agent claude --runtime apptainer")
    click.echo("  botainer auth login --agent claude      # OAuth")
    click.echo("  botainer hpc submit                     # submit to Slurm")
    click.secho(
        "──────────────────────────────────────────────────────",
        fg="cyan",
    )

    if probe:
        _run_setup_probe(chosen)


def _run_setup_probe(profile: profile_module.ClusterProfile) -> None:
    """Read-only cluster-prereq probes (P3c / cluster-ease B3).

    Each check prints one line with a status sigil + diagnostic text. None
    of these run as root, none mutate cluster state, and each gracefully
    degrades when its tool is missing — the goal is to surface gaps the
    docs imply (`hpc setup verifies your cluster`) but the command
    otherwise doesn't verify. Six checks today; small + boring beats
    silent-on-missing.
    """
    click.secho("─── Cluster prereq probe ─────────────────────────────", fg="cyan")

    # 1. apptainer reachability (login OR compute node — caller picks).
    apptainer_bin = shutil.which("apptainer") or shutil.which("singularity")
    if apptainer_bin:
        click.secho(f"  ✓ apptainer on PATH:    {apptainer_bin}", fg="green")
    else:
        click.secho(
            "  ⚠ apptainer NOT on PATH (may be compute-only on this cluster — "
            "`salloc -t 30 -p devel`, then re-probe).",
            fg="yellow",
        )

    # 2. sbatch reachability.
    sbatch_bin = shutil.which("sbatch")
    if sbatch_bin:
        click.secho(f"  ✓ sbatch on PATH:       {sbatch_bin}", fg="green")
    else:
        click.secho(
            "  ✗ sbatch NOT on PATH (the hpc-launcher flow can't submit "
            "jobs here; usually means you're not on a Slurm cluster).",
            fg="red",
        )

    # 3. sacctmgr account discovery.
    sacctmgr_bin = shutil.which("sacctmgr")
    if not sacctmgr_bin:
        click.secho(
            "  - sacctmgr NOT on PATH (skipping account discovery).",
            fg="yellow",
        )
    else:
        user = os.environ.get("USER", "")
        if not user:
            click.secho(
                "  - $USER not set (skipping account discovery).",
                fg="yellow",
            )
        else:
            try:
                result = subprocess.run(
                    [
                        sacctmgr_bin, "-nP",
                        "show", "assoc",
                        f"user={user}",
                        "format=account",
                    ],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                accounts = sorted({
                    line.strip()
                    for line in (result.stdout or "").splitlines()
                    if line.strip()
                })
                if accounts:
                    click.secho(
                        f"  ✓ Slurm accounts visible: {', '.join(accounts)}",
                        fg="green",
                    )
                    click.echo(
                        "      Set the submit account with: "
                        "`botainer config set plugins.hpc-launcher.account <name>`"
                    )
                elif result.returncode != 0:
                    click.secho(
                        f"  ⚠ sacctmgr exit {result.returncode} "
                        f"(can't discover accounts).",
                        fg="yellow",
                    )
                else:
                    click.secho(
                        "  ⚠ sacctmgr returned no accounts for "
                        f"user={user!r}. Ask your cluster admin to add you "
                        "to a group account.",
                        fg="yellow",
                    )
            except (OSError, subprocess.TimeoutExpired) as exc:
                click.secho(f"  ⚠ sacctmgr probe failed: {exc}", fg="yellow")

    # 4. sinfo for the profile's default partition.
    sinfo_bin = shutil.which("sinfo")
    part = profile.slurm_default_partition
    if not sinfo_bin:
        click.secho(
            "  - sinfo NOT on PATH (skipping partition reachability).",
            fg="yellow",
        )
    elif not part:
        click.secho(
            "  - profile has no slurm.default_partition (skipping "
            "partition reachability).",
            fg="yellow",
        )
    else:
        try:
            result = subprocess.run(
                [sinfo_bin, "-h", "-p", part, "-o", "%P %a"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            lines = [
                line.strip()
                for line in (result.stdout or "").splitlines()
                if line.strip()
            ]
            if lines and result.returncode == 0:
                click.secho(
                    f"  ✓ default partition reachable: {part!r}",
                    fg="green",
                )
            elif result.returncode != 0:
                click.secho(
                    f"  ⚠ sinfo exit {result.returncode} for partition "
                    f"{part!r} — may not exist on this cluster.",
                    fg="yellow",
                )
            else:
                click.secho(
                    f"  ⚠ sinfo returned no rows for partition {part!r} — "
                    "check the partition name in your cluster profile.",
                    fg="yellow",
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            click.secho(f"  ⚠ sinfo probe failed: {exc}", fg="yellow")

    # 5. Lmod bootstrap reachability.
    lmod_pkg = os.environ.get("LMOD_PKG")
    if lmod_pkg:
        click.secho(
            f"  ✓ $LMOD_PKG set: {lmod_pkg}",
            fg="green",
        )
    else:
        click.secho(
            "  - $LMOD_PKG not set (Lmod bootstrap autodetect will fall "
            "through to canonical-path scan).",
            fg="yellow",
        )

    # 5b. caps.modules_inner_load ON/OFF (in-container `module load`).
    # Reads the effective site policy so a user can SEE whether the admin
    # turned it on (the feature is otherwise only discoverable by running
    # `module avail` inside a session). SITE-ONLY: the ceiling is root-owned.
    try:
        from botainer.core.policy import load_site_policy
        _m = load_site_policy().mounts
        if _m.cluster_lmod_root:
            click.secho(
                f"  ✓ in-container `module load`: ENABLED "
                f"(Lmod tree {_m.cluster_lmod_root}; "
                f"{len(_m.cluster_modulepath_roots)} MODULEPATH root(s))",
                fg="green",
            )
            if lmod_pkg and os.path.realpath(lmod_pkg.rstrip("/")) != os.path.realpath(_m.cluster_lmod_root.rstrip("/")):
                click.secho(
                    f"    ⚠ note: $LMOD_PKG ({lmod_pkg}) differs from the "
                    f"policy cluster_lmod_root — confirm the policy points at "
                    f"the real Lmod tree.",
                    fg="yellow",
                )
        else:
            click.secho(
                "  - in-container `module load`: OFF (site policy sets no "
                "mounts.cluster_lmod_root). The agent can't run `module` "
                "inside the container; ask the admin to enable it "
                "(docs/SITE-ADMIN.md) or rely on the host module-env capture.",
                fg="yellow",
            )
    except Exception as _exc:
        click.secho(
            f"  - could not read site policy for inner-load status: {_exc}",
            fg="yellow",
        )

    # 6. Scratch template existence (just the base dir, not the full
    # job-specific subpath).
    if profile.scratch_template:
        base = profile.scratch_template
        # Strip the $SLURM_JOBID / ${USER}-style trailing variables to test
        # the static prefix; if there's no variable, this is a no-op.
        for var in ("${SLURM_JOBID}", "$SLURM_JOBID"):
            base = base.replace(var, "")
        base = base.replace("${USER}", os.environ.get("USER", "${USER}"))
        base = base.rstrip("/")
        try:
            if Path(base).exists():
                click.secho(
                    f"  ✓ scratch base reachable: {base}",
                    fg="green",
                )
            else:
                click.secho(
                    f"  ⚠ scratch base {base!r} does not exist (may need "
                    "to be created by the user OR by the cluster's first-"
                    "login provisioning).",
                    fg="yellow",
                )
        except OSError:
            click.secho(
                f"  ⚠ scratch base {base!r} could not be probed.",
                fg="yellow",
            )

    click.secho("──────────────────────────────────────────────────────", fg="cyan")


@hpc.command("jobs-doctor")
@handle_refusals
def jobs_doctor() -> None:
    """Diagnose why the caged agent can/can't dispatch jobs.

    Walks the whole chain — config file → top-level job_profiles → hpc-launcher
    plugin → the botainer-job CLI — and prints a plain-English verdict + the
    specific fix for whatever is broken. Run it in your project directory. This
    is the built-in replacement for hand-rolled python one-liners.
    """
    from botainer.cli import _common
    from botainer.hpc import diagnose as _diag

    project_root = _common.find_project_root() or Path.cwd()
    ok, lines = _diag.diagnose(project_root)
    click.secho("botainer job-dispatch diagnosis", fg="cyan", bold=True)
    click.echo("─" * 60)
    for ln in lines:
        if ln.startswith("VERDICT"):
            click.secho(ln, fg=("green" if ok else "red"), bold=True)
        elif "✗" in ln:
            click.secho(ln, fg="yellow")
        else:
            click.echo(ln)
    if not ok:
        raise SystemExit(1)


@hpc.command("jobs-status")
@click.option("--project", default=None, help="Project dir (default: cwd).")
@handle_refusals
def jobs_status(project: str | None) -> None:
    """Show the dispatched-job queue for a project FROM THE HOST (outside the
    container). Reads the mailbox the agent writes to — so you can see what your
    caged agent has queued/running/stuck without attaching. Run in the project
    dir (or pass --project). For a PENDING job it shows the SLURM reason (e.g.
    `Resources`, `PartitionNodeLimit`) so you know if it's contention or a bad
    request."""
    from botainer.cli import _common
    from botainer.core import identity as _identity
    from botainer.hpc import dispatcher as _disp
    from botainer.hpc import jobs as _jobs
    from botainer.state import dir as _sd

    proj = Path(project).resolve() if project else (
        _common.find_project_root() or Path.cwd())
    uid = _identity.read_project_id(proj)
    if not uid:
        click.secho(f"{proj} is not a botainer project (no project-id).", fg="red")
        raise SystemExit(2)
    # Validate the uuid before it becomes a path segment (mailbox_for →
    # hpc_jobs_dir(uid)) — a hostile project-id with `../`/absolute could redirect
    # the read otherwise. Parity with resolve_identity used by every other CLI
    # (sharp-edges #2).
    uid = _identity._validate_uuid(uid)

    def _clean(v, width=None):
        # Strip control chars/ANSI so a hostile profile NAME (a git-shareable
        # config key, unscrubbed) can't inject terminal escapes into the host
        # user's table (sharp-edges #3). Data-only render.
        s = "".join(ch for ch in str(v) if ord(ch) >= 0x20 and ch != "\x7f")
        return f"{s[:width]:{width}}" if width else s

    paths = _sd.ensure_user_state_dir(create_if_missing=False)
    mb = _jobs.mailbox_for(paths, uid)
    if not mb.out_dir.is_dir():
        click.echo("no jobs dispatched yet for this project.")
        return
    recs = []
    _MAX = 500  # cap the read so a flooded out/ can't blow up host memory (#4)
    for p in sorted(mb.out_dir.glob("*.status.json"))[:_MAX]:
        r = _disp._read_status(p)
        if r:
            recs.append(r)
    if not recs:
        click.echo("no jobs dispatched yet for this project.")
        return
    click.secho(f"{'JOB':16} {'STATE':10} {'PROFILE':12} {'SLURM':10} REASON/NOTE",
                bold=True)
    for r in sorted(recs, key=lambda x: x.get("submitted_at", "")):
        note = _clean(r.get("squeue_reason") or r.get("reason") or "")
        click.echo(f"{_clean(r.get('id',''), 15):16} {_clean(r.get('state',''), 10)} "
                   f"{_clean(r.get('profile',''), 12)} "
                   f"{_clean(r.get('slurm_job_id','-'), 10)} {note}")
        for w in (r.get("warnings") or []):
            click.secho(f"    ⚠ {_clean(w)}", fg="yellow")
    # also show warm-pool workers if any
    pj = mb.out_dir / "pool.json"
    if pj.is_file():
        click.echo()
        click.secho("warm pool:", bold=True)
        click.echo(_clean(pj.read_text(encoding="utf-8").strip()))


@hpc.command("jobs-explain")
@click.argument("profile_name")
@click.option("--project", default=None, help="Project dir (default: cwd).")
@handle_refusals
def jobs_explain(profile_name: str, project: str | None) -> None:
    """Show EXACTLY what a dispatched job for a profile can see and do — the
    transparency view. Composes the caged child job (without submitting) and dumps
    its binds (+ mode), the module/cluster exposure, the resolved resources, what
    is FROZEN, and what is deliberately NOT exposed. Run this before trusting a
    profile with real work — it's the answer to "what does this job actually get?"."""
    from botainer.cli import _common
    from botainer.core import config as _cfgm
    from botainer.core import identity as _identity
    from botainer.core import policy as _pol
    from botainer.hpc import jobs as _jobs
    from botainer.hpc import resources as _res
    from botainer.state import dir as _sd

    proj = Path(project).resolve() if project else (
        _common.find_project_root() or Path.cwd())
    cfg = _cfgm.load_config(proj)
    prof = cfg.job_profiles.get(profile_name)
    if prof is None:
        click.secho(f"no job profile named {profile_name!r}. Available: "
                    f"{', '.join(cfg.job_profiles) or '(none)'}", fg="red")
        raise SystemExit(2)
    eff = _pol.intersect(_pol.load_site_policy(), _pol.load_user_policy())
    paths = _sd.ensure_user_state_dir(create_if_missing=True)
    uid = _identity.read_project_id(proj)
    if not uid:
        click.secho(f"{proj} is not a botainer project (no project-id).", fg="red")
        raise SystemExit(2)
    uid = _identity._validate_uuid(uid)
    core = _jobs.child_core_binds(proj, paths.for_project(uid), paths.state_dir)
    cluster, env = _jobs.child_cluster_contribution(eff)
    resolved = _res.resolve_resources(prof, None)

    click.secho(f"job profile: {profile_name}", fg="cyan", bold=True)
    click.echo("─" * 60)
    click.secho("RESOURCES (defaults; agent may override up to max_*):", bold=True)
    for k in ("partition", "account", "time", "cpus", "memory", "gpus",
              "gpu_type", "nodes", "ntasks", "ntasks_per_node"):
        v = resolved.get(k)
        if v:
            click.echo(f"  {k:16} {v}")
    if prof.exclusive:
        click.echo("  exclusive        yes (whole-node)")
    click.secho("\nBINDS the job gets (source → target):", bold=True)
    for b in (*core, *cluster):
        mode = b.mode.value
        click.echo(f"  [{mode:9}] {b.source} → {b.target}")
    click.secho("\nMODULE / CLUSTER EXPOSURE:", bold=True)
    if prof.modules:
        click.echo(f"  auto module load: {', '.join(prof.modules)}")
    elif prof.modules == []:
        click.echo("  modules: [] (none loaded)")
    else:
        click.echo("  modules: not set (none auto-loaded)")
    if env:
        click.echo(f"  module env set inside: {', '.join(sorted(env))}")
    else:
        click.echo("  cluster module system NOT exposed (site policy "
                   "mounts.cluster_* empty) — `module` won't work in the job.")
    click.secho("\nFROZEN / PROTECTED:", bold=True)
    click.echo("  /workspace/.botainer  masked (job can't read/write config.yaml)")
    if (proj / ".git").is_dir():
        click.echo("  /workspace/.git       read-only (no hook-plant / config-poison)")
    click.secho("\nNOT exposed to the job (never):", bold=True)
    click.echo("  credentials, home, ~/.ssh, ~/.claude/~/.codex, the broker socket,")
    click.echo("  the OAuth token, other projects' state.")
    click.secho("\nCAGE: apptainer --containall --cleanenv --no-privs --drop-caps "
                "all (same §4 cage as the agent session).", fg="green")


def _echo_profile_trust(profile) -> None:
    """Show HOW MUCH a cluster profile has been verified, at the moment it matters.

    User requirement: "clearly mark what's tested and not tested".
    A profile transcribed from a vendor's docs and one actually run on the
    hardware look identical in a list; the difference decides whether a failed
    submit means "my config is wrong" or "this profile was always a guess".
    Unverified is stated loudly rather than omitted — silence would read as
    endorsement.
    """
    label = profile.verification_label()
    if profile.is_verified_on_hardware():
        click.secho(f"  verification:       {label}", fg="green")
    elif profile.verification_status:
        click.secho(f"  verification:       {label}", fg="yellow")
    else:
        click.secho(f"  verification:       {label}", fg="red")
    if not profile.is_verified_on_hardware():
        click.secho(
            "                      Verify it against this cluster by hand:\n"
            "                        sinfo -o '%P %l %m %c %G'\n"
            "                        scontrol show partition\n"
            "                      (an automated check is planned, not built)",
            fg="cyan",
        )


@hpc.command("info")
@handle_refusals
def info() -> None:
    """Show the active cluster profile."""
    profile = profile_module.active_profile()
    if profile is None:
        click.echo("(no cluster profile configured)")
        click.echo("Run `botainer hpc setup` to choose one.")
        return
    click.secho(f"Cluster: {profile.name}", fg="cyan", bold=True)
    click.echo(f"  Description:        {profile.description}")
    click.echo(f"  Hostname patterns:  {', '.join(profile.hostname_patterns)}")
    click.echo(f"  Lmod bootstrap:     {profile.lmod_bootstrap or '(auto)'}")
    _echo_profile_trust(profile)
    click.echo()
    click.secho("Slurm:", fg="cyan")
    click.echo(f"  Default partition:  {profile.slurm_default_partition or '(none)'}")
    click.echo(f"  Default account:    {profile.slurm_default_account or '(none)'}")
    click.echo(f"  Default time:       {profile.slurm_default_time_minutes} min")
    if profile.partitions:
        click.echo("  Partitions:")
        for p in profile.partitions:
            limits = []
            if p.max_time_minutes:
                limits.append(f"max-time={p.max_time_minutes}min")
            if p.max_cpus:
                limits.append(f"max-cpus={p.max_cpus}")
            if p.max_memory_gb:
                limits.append(f"max-mem={p.max_memory_gb}GB")
            if p.gpu_types:
                limits.append(f"gpus={','.join(p.gpu_types)}")
            click.echo(f"    {p.name:20s} {', '.join(limits)}")
    click.echo()
    click.secho("Scratch:", fg="cyan")
    click.echo(f"  Template:           {profile.scratch_template or '(not set)'}")
    if profile.scratch_cleanup_days:
        # Audit: the cleanup-days value is DISPLAY-ONLY at v0.1.0
        # (cluster_profile.py:55-64 documents this). Label it so a user setting
        # it in their cluster.yaml doesn't expect botainer to enforce it.
        click.echo(
            f"  Auto-cleanup:       {profile.scratch_cleanup_days} days  "
            f"(display-only at v0.1; the cluster's own purge policy applies)"
        )
    click.echo()
    click.secho("Apptainer:", fg="cyan")
    click.echo(f"  Cache dir:          {profile.apptainer_cachedir or '(none)'}")
    # Audit: prebuilt_url is DISPLAY-ONLY at v0.1.0 — `hpc build`
    # does not consume it (cluster-ease roadmap A1 / B5). Label so a user
    # setting it doesn't expect builds to skip.
    _purl = profile.apptainer_prebuilt_url or "(none)"
    _purl_note = "  (display-only at v0.1; build always runs locally)" if profile.apptainer_prebuilt_url else ""
    click.echo(f"  Prebuilt URL:       {_purl}{_purl_note}")


def _agent_plugin_for_cwd() -> str:
    """The agent plugin of the project you're standing in.

    Refuses rather than guessing: a wrong guess here costs a ten-minute build
    and produces an image for the wrong agent, which then surfaces as an
    unrelated "no recorded image" error at `start`.
    """
    from botainer.cli import _common
    try:
        project_root = _common.find_project_root()
    except Exception:
        project_root = None
    if project_root is None:
        click.secho(
            "refused: not inside a botainer project, so there is no agent to "
            "build for.", fg="red", err=True,
        )
        click.secho(
            "  hint: name it explicitly — `botainer hpc build agent-claude` "
            "(or agent-codex) — or cd into a project.",
            fg="cyan", err=True,
        )
        sys.exit(2)
    agent = ""
    try:
        import yaml
        data = yaml.safe_load(
            (project_root / ".botainer" / "config.yaml").read_text()) or {}
        agent = str(data.get("agent") or "")
    except Exception:
        agent = ""
    if not agent:
        click.secho(
            f"refused: {project_root}/.botainer/config.yaml has no `agent:` "
            f"field, so there is nothing to build.", fg="red", err=True,
        )
        click.secho(
            "  hint: name the plugin explicitly, e.g. "
            "`botainer hpc build agent-codex`.", fg="cyan", err=True,
        )
        sys.exit(2)
    return agent if agent.startswith("agent-") else f"agent-{agent}"


@hpc.command("build")
@click.argument("plugin_name", required=False, default=None)
@click.option("--force", is_flag=True, help="Overwrite existing .sif.")
@handle_refusals
def build(plugin_name: str | None, force: bool) -> None:
    """Build the Apptainer .sif for a plugin.

    With no argument, builds the image for THE AGENT THIS PROJECT USES. It used
    to default to the literal string `agent-claude` (#128), so a codex user
    standing in a codex project ran `botainer hpc build`, waited out a
    ten-minute build, and got the claude image — then hit "no recorded image"
    on their next `start` with nothing connecting the two.

    Uses the cluster profile's apptainer.cachedir (if configured) for
    $APPTAINER_CACHEDIR, which speeds up rebuilds by re-using layers.

    NOTE: `apptainer.prebuilt_url` is DISPLAY-ONLY at v0.1.0 — this command
    does NOT consume it; it always builds locally. Fetching an admin-hosted
    prebuilt .sif is tracked as a follow-up (cluster-ease roadmap B5; needs
    URL-scheme allowlist + sha256 digest pinning + the security review
    protocol). Audit (solidity-check) confirmed the dead-claim
    docstring.
    """
    if not shutil.which("apptainer") and not shutil.which("singularity"):
        click.secho(
            "refused: neither `apptainer` nor `singularity` on PATH",
            fg="red", err=True,
        )
        from botainer.cli import _common
        click.secho("    " + _common.apptainer_missing_advice().replace(
            "\n", "\n    "), fg="cyan", err=True)
        sys.exit(2)
    if plugin_name is None:
        plugin_name = _agent_plugin_for_cwd()
        click.secho(
            f"building for this project's agent: {plugin_name}",
            fg="cyan", err=True,
        )
    from botainer.plugins import lifecycle as lifecycle_module
    from botainer.state import dir as state_dir
    installed = lifecycle_module.list_installed()
    match = [p for p in installed if p.name == plugin_name]
    if not match:
        click.secho(
            f"refused: plugin {plugin_name!r} not installed. "
            f"Run `botainer setup` first.",
            fg="red", err=True,
        )
        sys.exit(2)
    plugin = match[0]
    def_path = plugin.plugin_dir / f"{plugin_name}.def"
    if not def_path.exists():
        click.secho(
            f"refused: no {plugin_name}.def file in {plugin.plugin_dir}",
            fg="red", err=True,
        )
        click.echo(
            "This plugin doesn't have an Apptainer build recipe. "
            "Check `botainer plugin info <name>` for what build paths it supports."
        )
        sys.exit(2)

    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    paths.images_dir.mkdir(parents=True, exist_ok=True)
    # The .sif-naming follow-up (DN-036): was `images_dir / f"{plugin_name}.sif"`
    # (unprefixed) — drifted from `botainer image build` + the resolver,
    # both of which use `botainer-<plugin>.sif`. Route through the
    # single-source-of-truth helper.
    sif_path = paths.apptainer_sif_path(plugin_name)

    profile = profile_module.active_profile()
    env = dict(os.environ)
    if profile and profile.apptainer_cachedir:
        env["APPTAINER_CACHEDIR"] = os.path.expandvars(profile.apptainer_cachedir)

    if sif_path.exists() and not force:
        click.secho(
            f"{sif_path} already exists; pass --force to rebuild",
            fg="yellow",
        )
        return

    cmd = ["apptainer", "build"]
    if force:
        cmd.append("--force")
    cmd.append(str(sif_path))
    cmd.append(str(def_path))
    click.secho(f"Building {sif_path} (this can take 10-20 min)...", fg="cyan")
    click.echo(f"  cmd: {' '.join(cmd)}")
    click.echo(f"  cache: {env.get('APPTAINER_CACHEDIR', '(default)')}")
    # Apptainer resolves `%files` SOURCE paths relative to the build's WORKING
    # DIRECTORY, not the .def's location. The bundled .defs copy a sibling
    # `entrypoint_wrap.sh` by a RELATIVE path, so the build MUST run from the
    # plugin dir or apptainer can't stat it (seen on a real cluster: build launched from
    # the user's project dir → "cannot stat 'entrypoint_wrap.sh'"). sif_path +
    # def_path are absolute, so cwd only affects the relative %files lookup.
    rc = subprocess.call(cmd, env=env, cwd=str(plugin.plugin_dir))
    if rc != 0:
        click.secho(f"refused: build failed (rc={rc})", fg="red", err=True)
        sys.exit(rc)
    # A ZERO EXIT IS NOT A BUILT IMAGE. This printed "✓ built … (0 MiB)" on a
    # login node whose apptainer executes nothing: it computed st_size, got 0,
    # and put a green tick next to it. `doctor --strict` — the command the guide
    # sends you to before launching jobs — passed the same empty file.
    #
    # A green tick on an empty image is worse than a red X, because it sends the
    # user downstream to debug authentication or Slurm when the problem is two
    # layers below. That is exactly what happened in a UX walk: the user spent
    # their whole session past this point chasing a login failure.
    #
    # Check the ARTEFACT, not the exit code — the same rule this project applies
    # to its own tests. `apptainer build` can exit 0 having produced nothing.
    size = sif_path.stat().st_size if sif_path.exists() else 0
    if size < _MIN_PLAUSIBLE_SIF_BYTES:
        click.secho(
            f"refused: apptainer exited 0 but {sif_path} is "
            f"{size} bytes — no image was produced.",
            fg="red", err=True,
        )
        click.secho(
            "    An `apptainer build` that succeeds writes hundreds of MiB.\n"
            "    This usually means apptainer is present but cannot actually\n"
            "    run here — common on login nodes, where the binary exists and\n"
            "    the kernel features it needs do not.\n"
            "    Check with:  apptainer exec <any.sif> echo ok\n"
            "    If that prints nothing, build from a compute node:\n"
            "        salloc -t 60 -c 4      # on the login node\n"
            "        botainer hpc build agent-claude   # on the compute node",
            fg="cyan", err=True,
        )
        sys.exit(4)
    click.secho(
        f"✓ built {sif_path} ({size // 1024 // 1024} MiB)",
        fg="green",
    )


#: Smallest thing that could possibly be a container image. A real agent .sif
#: is hundreds of MiB; the failure mode this guards is a 0-byte or few-KB stub
#: left behind by an apptainer that exited 0 without doing anything. Deliberately
#: far below any real image so it can only ever catch the "nothing happened"
#: case, never a legitimately small one.
_MIN_PLAUSIBLE_SIF_BYTES = 1024 * 1024      # 1 MiB


@hpc.command("job-profiles")
@click.option("--write", is_flag=True,
              help="Append to this project's .botainer/config.yaml instead of "
                   "printing. Refuses if a job_profiles: block already exists.")
def job_profiles_cmd(write: bool) -> None:
    """Generate a starter `job_profiles:` block for THIS cluster.

    Partition names, walltime ceilings and GPU types come from the active
    cluster profile, so the result is valid here rather than a template with
    "replace this" placeholders. Preemptible and whole-node partitions are
    deliberately never offered as defaults — they are listed separately with
    what they cost.
    """
    from botainer.hpc.job_profile_gen import render_job_profiles
    from botainer.state import cluster_profile as _cp

    prof = _cp.active_profile()
    if prof is None:
        click.secho("refused: no cluster profile is active.", fg="red", err=True)
        click.echo(
            "  Nothing is known about this machine's partitions, so any\n"
            "  generated config would be invented. Run `botainer hpc setup`\n"
            "  to pick a bundled profile, or read the partitions off the live\n"
            "  scheduler with `sinfo` and write the profile by hand.\n"
            "  (Automated discovery is planned, not built.)")
        raise SystemExit(2)

    block = render_job_profiles(prof)
    if not write:
        click.echo(block)
        click.secho(
            "Paste this into .botainer/config.yaml (TOP LEVEL — job_profiles "
            "is not\nnested under plugins:), or re-run with --write.",
            fg="cyan")
        return

    cfg = Path.cwd() / ".botainer" / "config.yaml"
    if not cfg.exists():
        click.secho(f"refused: no {cfg} — run `botainer init` first.",
                    fg="red", err=True)
        raise SystemExit(2)
    existing = cfg.read_text(encoding="utf-8")
    if "job_profiles:" in existing:
        click.secho(
            "refused: this config already has a job_profiles: block.",
            fg="red", err=True)
        click.echo("  Not overwriting what you wrote. Run without --write and "
                   "merge by hand.")
        raise SystemExit(2)
    cfg.write_text(existing.rstrip() + "\n\n" + block, encoding="utf-8")
    click.secho(f"appended job_profiles: to {cfg}", fg="green")
    click.echo("  Set `account:` before submitting — the command to find it is "
               "in the comments.")


def _refuse_unsupported_scheduler(what: str) -> None:
    """Refuse CLEARLY on a site whose batch system is not Slurm.

    Every hpc command emits `sbatch` and `#SBATCH` directives and parses
    `squeue`. On a PBS, LSF or Grid Engine site that fails with a bare
    command-not-found, which tells the user nothing and looks like a broken
    install rather than an unsupported platform. Roughly a quarter of the
    profiles bundled  are in that category (ALCF Polaris and NCI
    Gadi are PBS Pro, OLCF Summit was LSF, UCL Myriad is Grid Engine, and
    Jetstream2 has no batch scheduler at all).

    Declaring the gap is deliberately NOT the same as closing it: real PBS/LSF
    support needs its own security review, because the dispatcher's directional
    mailbox was reasoned about in terms of what slurmstepd does with --output
    as the uncaged user, and that analysis does not carry over. Shipping a
    half-ported adapter would repeat the yale-grace error with a far larger
    blast radius. See DN-016 4a.

    Silent on Slurm and when no profile is loaded — absence of a profile must
    not become an accusation.
    """
    try:
        from botainer.state import cluster_profile as _cp
        prof = _cp.active_profile()
    except Exception:
        return
    if prof is None:
        return
    sched = (prof.scheduler or "slurm").lower()
    if sched == "slurm":
        return
    if sched == "none":
        click.secho(
            f"refused: {what} needs a batch scheduler, and the profile for "
            f"{prof.name!r} records that this resource has none.",
            fg="red", err=True)
        click.echo(
            "  It is a cloud/VM resource, not a batch cluster. Run botainer "
            "directly\n  on the instance instead of submitting a job.")
        raise SystemExit(2)
    click.secho(
        f"refused: {what} emits Slurm commands, but the profile for "
        f"{prof.name!r} records this site as {sched.upper()}.",
        fg="red", err=True)
    click.echo(
        f"  botainer v0.1 supports Slurm only. On a {sched.upper()} site the\n"
        f"  submission would fail with a confusing command-not-found, so it is\n"
        f"  refused here instead.\n"
        f"\n"
        f"  You can still run botainer directly on a node you already hold\n"
        f"  (e.g. inside an interactive allocation you obtained yourself).\n"
        f"  If the profile is wrong and this site really does run Slurm, set\n"
        f"  `slurm.scheduler: slurm` in your ~/.botainer/cluster.yaml.")
    raise SystemExit(2)


def _warn_if_jobs_without_dispatcher(project_root) -> None:
    """Warn when a batch session could dispatch jobs but nothing will run them.

    WHY THE SUBMIT PATH AND NOT `botainer start`. The interactive path already
    auto-starts a dispatcher for the session's lifetime
    (`hpc/autodispatch.maybe_start`, called from cli/start.py), so warning there
    would be a false alarm on every launch — the cry-wolf class this project has
    a rule about. I wrote it there first; this comment exists so nobody moves it
    back.

    `hpc submit` is the real gap: the session becomes a BATCH JOB on a compute
    node, nothing auto-starts for it, and the dispatcher has to live on the
    LOGIN node anyway. So a user who submits a jobs-enabled project with no
    dispatcher gets an agent whose `botainer-job submit` writes into a mailbox
    nobody reads — silently, from the agent's point of view.

    Warn, never refuse: a batch session with jobs configured is perfectly usable
    without them.
    """
    try:
        from botainer.core import config as _cfgm
        cfg = _cfgm.load_config(project_root)
        if not getattr(cfg, "job_profiles", None):
            return
        state, _detail = dispatcher_liveness(project_root)
        if state in ("running", "unknown"):
            # "running" includes a dispatcher heartbeating on ANOTHER login
            # node: the mailbox is on shared home, so it really does serve this
            # submit, and warning would be the false alarm that teaches the
            # user to ignore this channel. "unknown" means we could not even
            # identify the project, so there is nothing to warn about.
            return
        how = ("it died without releasing its claim"
               if state == "stale" else "it is not started")
        click.echo("")
        click.secho(
            f"\u26a0 This project can dispatch jobs, but no dispatcher is "
            f"running ({how}).", fg="yellow", bold=True, err=True)
        click.secho(
            "    A batch session cannot start one for itself \u2014 the dispatcher\n"
            "    must run on the LOGIN NODE, outside the job. Without it, jobs the\n"
            "    agent asks for are written to the mailbox and never start, and\n"
            "    the agent sees no error.\n"
            "\n"
            "    Start it HERE, on this login node, in another terminal (or under\n"
            "    screen/tmux so it survives your logout):\n"
            "        botainer hpc dispatcher start\n"
            "    Check it any time with:\n"
            "        botainer hpc dispatcher status",
            fg="cyan", err=True)
    except Exception:                                           # noqa: BLE001
        return


@hpc.command("submit", context_settings={"ignore_unknown_options": True})
@click.option("--mode", type=click.Choice(["submit", "attach", "here"]),
              default=None, help="Override submission mode.")
@click.option("--jobid", default=None, help="Existing jobid for --mode=attach.")
@click.option("--dry-run", is_flag=True,
              help="Print sbatch script that would be submitted; don't submit.")
@click.option("--yes", "-y", "auto_yes", is_flag=True,
              help="Skip the login-node confirmation prompt.")
@click.option("--partition", default=None, help="Override partition.")
@click.option("--account", default=None, help="Override account.")
# --time accepts integer minutes, HH:MM:SS, '2h', '90m'. The plugin
# parser handles the parsing; we just forward the string.
@click.option("--time", "time_str", default=None,
              help=("Override walltime. Accepts integer minutes ('120'), "
                    "HH:MM:SS ('02:00:00'), or duration ('2h', '90m')."))
# --cpus and --cpus-per-task are aliases.
@click.option("--cpus", "--cpus-per-task", "cpus", type=int, default=None,
              help="Override CPUs per task.")
# --memory-gb (integer GB) and --mem (Slurm-style: '16G', '8000M') are aliases.
@click.option("--memory-gb", "memory_gb", type=int, default=None,
              help="Override memory in integer GB.")
@click.option("--mem", "mem_str", default=None,
              help="Override memory (Slurm-style: '16G', '8000M').")
@click.option("--gpus", type=int, default=None,
              help="Override GPU count (0 = none).")
@click.option("--gpu-type", "gpu_type", default=None,
              help="Override GPU type (a100/v100/etc).")
# --gres is the Slurm shorthand for gpus+gpu-type combined.
@click.option("--gres", "gres_str", default=None,
              help="Slurm --gres shorthand: 'gpu:1' or 'gpu:a100:1'.")
@click.option("--image", default=None, help="Override apptainer .sif path.")
def submit(
    mode: str | None, jobid: str | None, dry_run: bool, auto_yes: bool,
    partition: str | None, account: str | None,
    time_str: str | None, cpus: int | None,
    memory_gb: int | None, mem_str: str | None,
    gpus: int | None, gpu_type: str | None, gres_str: str | None,
    image: str | None,
) -> None:
    """Submit a fresh sbatch allocation hosting the agent.

    Defaults come from .botainer/config.yaml's plugins.hpc-launcher
    section + ~/.botainer/cluster.yaml. Flags below override for THIS
    submission only.

    Slurm-style aliases (forwarded to the plugin parser):

      --cpus  / --cpus-per-task     same field
      --memory-gb (int GB) / --mem (Slurm: '16G', '8000M')
      --time accepts '120' (min) / '02:00:00' / '2h' / '90m'
      --gres 'gpu:a100:1' is shorthand for --gpus 1 --gpu-type a100

    For persistent project defaults: edit .botainer/config.yaml or
    use `botainer config set plugins.hpc-launcher.time_minutes 240`.
    """
    _refuse_unsupported_scheduler("hpc submit")
    from botainer.cli import _common as _c
    _warn_if_jobs_without_dispatcher(_c.find_project_root())
    # SAME check as `botainer start`, on the path that matters on a cluster.
    # It was added to start.py first and NOT here — which put the guard on the
    # laptop path and left the product's main path unprotected. That is the
    # sibling-drift class this project keeps repeating (two sbatch call sites,
    # three shared-mode hardenings applied to one plugin of a pair). Both
    # callers now go through the same helper; adding a third entry point means
    # calling it there too.
    _c.confirm_no_other_shared_session(_c.find_project_root())
    from botainer.plugins import lifecycle as lifecycle_module
    if not any(p.name == "hpc-launcher" for p in lifecycle_module.list_installed()):
        click.secho(
            "refused: the hpc-launcher plugin is not INSTALLED on this host.",
            fg="red", err=True,
        )
        click.echo("Run `botainer setup` to install bundled plugins.")
        click.echo("(You do NOT need it in `plugins_enabled` — it contributes "
                   "nothing to the container; jobs are wired by `job_profiles`.)")
        sys.exit(2)
    argv: list[str] = []
    if mode:
        argv += ["--mode", mode]
    if jobid:
        argv += ["--jobid", jobid]
    if dry_run:
        argv += ["--dry-run"]
    if auto_yes:
        argv += ["--yes"]
    if partition:
        argv += ["--partition", partition]
    if account:
        argv += ["--account", account]
    if time_str is not None:
        argv += ["--time", time_str]
    if cpus is not None:
        argv += ["--cpus", str(cpus)]
    if memory_gb is not None:
        argv += ["--memory-gb", str(memory_gb)]
    if mem_str is not None:
        argv += ["--mem", mem_str]
    if gpus is not None:
        argv += ["--gpus", str(gpus)]
    if gpu_type:
        argv += ["--gpu-type", gpu_type]
    if gres_str is not None:
        argv += ["--gres", gres_str]
    if image:
        argv += ["--image", image]
    rc = subprocess.call(
        [sys.executable, "-m", "botainer.cli.main",
         "plugin", "hpc-launcher", "submit", *argv],
        env={**os.environ, "BOTAINER_NO_TIPS": "1"},  # one footer, not two (M1)
    )
    sys.exit(rc)


@hpc.command("attach", context_settings={"ignore_unknown_options": True})
@click.argument("jobid_arg", required=False, metavar="[JOBID]")
@click.option("--jobid", default=None,
              help="Slurm jobid (alias for the positional JOBID). If omitted, "
                   "uses $SLURM_JOB_ID when inside an allocation, else refuses "
                   "with a hint to run `botainer hpc status`.")
@click.option("--dry-run", is_flag=True,
              help="Print the srun command that would be run; don't execute.")
def attach_cmd(jobid_arg: str | None, jobid: str | None, dry_run: bool) -> None:
    """Attach your terminal to a running botainer Slurm job.

    JOBID may be given positionally (`botainer hpc attach 12345`, consistent
    with `hpc logs`/`stop`) or via `--jobid`; if omitted, `$SLURM_JOB_ID` is used
    when inside an allocation.

    If the job was launched with nudge ENABLED, this REATTACHES to the batch
    agent's `screen -r botainer-<jobid>` — so what you see IS the running agent,
    and it's the same session `botainer nudge` delivers into (#56). If
    nudge was NOT enabled, the batch agent had no TTY and already exited, so this
    falls back to a FRESH agent in the allocation (unchanged behavior).

    Typical flow:
      botainer hpc submit            # returns jobid=12345
      # ...wait until squeue shows it RUNNING...
      botainer hpc attach 12345      # reattaches if nudge is enabled
    """
    jobid = jobid or jobid_arg   # positional or --jobid (positional wins if both)
    from botainer.plugins import lifecycle as lifecycle_module
    if not any(p.name == "hpc-launcher" for p in lifecycle_module.list_installed()):
        click.secho(
            "refused: the hpc-launcher plugin is not INSTALLED on this host.",
            fg="red", err=True,
        )
        click.echo("Run `botainer setup` to install bundled plugins.")
        click.echo("(You do NOT need it in `plugins_enabled` — it contributes "
                   "nothing to the container; jobs are wired by `job_profiles`.)")
        sys.exit(2)
    # Task #102 + #280: cross-project leak guard. `bot hpc attach --jobid=X`
    # re-launches an agent inside allocation X using THIS project's config,
    # binds, and credentials. If X is a sibling project's running job, the
    # re-launched agent runs alongside the other project's session with
    # project-A credentials wired into project-B's container. Refuse if
    # squeue says the job-name doesn't match botainer-<this-project-uuid8>.
    if jobid and shutil.which("squeue"):
        try:
            from pathlib import Path as _Path

            from botainer.core import identity as _identity
            _root = _Path.cwd().resolve()
            try:
                uid, _ = _identity.resolve_identity(_root, identity_accept=False)
                this_prefix = f"botainer-{uid[:8]}"
            except Exception:
                this_prefix = None
            if this_prefix is not None:
                _q = subprocess.run(
                    ["squeue", "-h", "-j", jobid, "-o", "%j"],
                    capture_output=True, text=True, timeout=10,
                )
                name = (_q.stdout or "").strip().splitlines()[0:1]
                name = name[0] if name else ""
                if name and not name.startswith(this_prefix):
                    click.secho(
                        f"refused: jobid {jobid} (name={name!r}) does not match "
                        f"this project's prefix {this_prefix!r}. Attach from "
                        f"the OTHER project's directory, or pass --force-cross-"
                        f"project (not yet implemented at v0.1.0).",
                        fg="red", err=True,
                    )
                    sys.exit(4)
        except subprocess.TimeoutExpired:
            click.secho("warning: squeue cross-project check timed out; "
                        "proceeding without verification", fg="yellow", err=True)
    argv: list[str] = ["--mode", "attach"]
    if jobid:
        argv += ["--jobid", jobid]
    if dry_run:
        argv += ["--dry-run"]
    rc = subprocess.call(
        [sys.executable, "-m", "botainer.cli.main",
         "plugin", "hpc-launcher", "submit", *argv],
        env={**os.environ, "BOTAINER_NO_TIPS": "1"},  # one footer, not two (M1)
    )
    sys.exit(rc)


@hpc.command("status")
@click.option(
    "--all-users",
    is_flag=True,
    help="Show all users' jobs (no -u filter).",
)
@click.option(
    "--all",
    "all_my_jobs",
    is_flag=True,
    help=(
        "Show ALL my Slurm jobs, not just botainer-managed ones. "
        "Default filters job-name to botainer-*/botjob-*/botpool-* "
        "(sessions, dispatched jobs, warm workers)."
    ),
)
def status_cmd(all_users: bool, all_my_jobs: bool) -> None:
    """Show squeue-driven status of botainer Slurm jobs.

    By default, only jobs whose name starts with `botainer-` are
    shown (matching the convention set by `to_sbatch_argv`'s
    `--job-name=botainer-<uuid_prefix>`). Pass `--all` to see
    everything you've queued; `--all-users` to drop the user filter
    entirely (useful for cluster admins or curiosity).
    """
    if not shutil.which("squeue"):
        click.secho("refused: `squeue` not on PATH", fg="red", err=True)
        sys.exit(2)
    # Task #161: squeue --name is an EXACT match, not a prefix. Passing
    # 'botainer-' returned zero jobs because no job is literally named
    # 'botainer-'. The fix is to ask squeue for everything (--user filter
    # only) and filter to 'botainer-*' in Python on the job-name column.
    fmt = "%.18i %.12P %.20j %.8u %.2t %.10M %.6D %R"
    cmd = ["squeue", "-o", fmt]
    if not all_users:
        cmd += ["-u", os.environ.get("USER", "")]
    if all_my_jobs:
        # Show everything (no botainer-prefix filter).
        rc = subprocess.call(cmd)
        sys.exit(rc)
    # Else: filter to job names starting with 'botainer-' in Python.
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        click.echo(proc.stdout, nl=False)
        click.echo(proc.stderr, nl=False, err=True)
        sys.exit(proc.returncode)
    lines = proc.stdout.splitlines()
    # First line is the header from -o; keep it.
    if lines:
        click.echo(lines[0])
        # NAME column is the 3rd field per format string above. Match ALL botainer
        # job-name prefixes, not just sessions: dispatched jobs are `botjob-` and
        # warm workers `botpool-` — filtering to `botainer-` only used to HIDE them
        # from the user's own queue view.
        for ln in lines[1:]:
            parts = ln.split()
            if len(parts) >= 3 and parts[2].startswith(_BOTAINER_JOB_NAME_PREFIXES):
                click.echo(ln)
    sys.exit(0)


@hpc.command("list")
@click.pass_context
def list_cmd(ctx: click.Context) -> None:
    """Alias for `botainer hpc status` (current user's botainer jobs).

    Mirrors the language of the user-facing workflow doc: "what's in
    my queue?" is `botainer hpc list`. Maps to the same code path.
    """
    ctx.invoke(status_cmd, all_users=False, all_my_jobs=False)


@hpc.command("cancel")
@click.argument("jobid", required=False)
@click.option("--all", "cancel_all", is_flag=True, help="scancel all my botainer jobs.")
@click.pass_context
def cancel_cmd(ctx: click.Context, jobid: str | None, cancel_all: bool) -> None:
    """Alias for `botainer hpc stop`. Cancel one or all botainer jobs."""
    ctx.invoke(stop_cmd, jobid=jobid, stop_all=cancel_all)


@hpc.command("stop")
@click.argument("jobid", required=False)
@click.option("--all", "stop_all", is_flag=True, help="scancel all my botainer jobs.")
def stop_cmd(jobid: str | None, stop_all: bool) -> None:
    """scancel one or all botainer Slurm jobs."""
    if not shutil.which("scancel"):
        click.secho("refused: `scancel` not on PATH", fg="red", err=True)
        sys.exit(2)
    if not stop_all and not jobid:
        click.secho("refused: specify a jobid or use --all", fg="red", err=True)
        sys.exit(2)
    # Task #163: pass --user $USER to scancel so we can only cancel our
    # own jobs. Without it, hpc stop --all reading from session_record
    # could in principle scancel jobs that belong to other users if a
    # corrupt or shared state record carried a foreign jobid. scancel
    # itself enforces UID checks, but defense-in-depth — fail fast at
    # the launcher.
    import os as _os
    me = _os.environ.get("USER") or _os.environ.get("LOGNAME") or ""
    if not me:
        click.secho("refused: cannot determine $USER for scancel --user", fg="red", err=True)
        sys.exit(2)
    if stop_all:
        # Read from session_record (more reliable than parsing squeue).
        from botainer.state import dir as state_dir
        from botainer.state import session_record
        paths = state_dir.ensure_user_state_dir(create_if_missing=False)
        for proj in state_dir.list_projects():
            proj_paths = paths.for_project(proj.uuid)
            for rec in session_record.list_sessions(proj_paths.sessions_dir):
                if rec.apptainer and rec.apptainer.slurm_jobid:
                    rc = subprocess.call([
                        "scancel", "--user", me, "--", rec.apptainer.slurm_jobid,
                    ])
                    click.echo(
                        f"{rec.session_id[:12]}: scancel "
                        f"{rec.apptainer.slurm_jobid} → rc={rc}"
                    )
        return
    rc = subprocess.call(["scancel", "--user", me, jobid])
    sys.exit(rc)


@hpc.command("logs")
@click.argument("jobid", required=False)
@click.option("-f", "--follow", is_flag=True, help="Tail the file (`tail -f`).")
@click.option(
    "--lines", "-n", type=int, default=200,
    help="Initial lines to show (default 200). Set 0 for whole file.",
)
@click.option(
    "--all-projects", is_flag=True,
    help="Search across all known botainer projects (not just cwd's).",
)
def logs_cmd(
    jobid: str | None, follow: bool, lines: int, all_projects: bool,
) -> None:
    """Show (or tail) the Slurm output file for a botainer sbatch job.

    Cluster-ease roadmap C5: closes the submit→watch loop.
    After `hpc submit`, the user was told to manually splice a jobid + UUID
    into a path (`<state_root>/hpc-job-outputs/<uuid>/slurm-<jobid>.out`)
    to tail output. This resolves it for you. (That dir is host-only — NOT
    bind-mounted into the container — per security-audit Finding 1.)

    With no JOBID: picks the MOST RECENT botainer sbatch job for the current
    project (or all projects with --all-projects). The resolved path is
    printed so power users still learn it.
    """
    from botainer.state import dir as state_dir
    from botainer.state import session_record

    try:
        paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    except Exception as exc:
        click.secho(f"refused: state dir not initialized ({exc}); "
                    f"run `botainer setup` first.", fg="red", err=True)
        sys.exit(2)

    # Build the candidate list: (started_at, jobid, output_path).
    candidates: list[tuple[str, str, Path]] = []
    if all_projects:
        projects = state_dir.list_projects()
    else:
        # Restrict to projects whose known paths include cwd. ProjectListEntry
        # exposes `paths: tuple[str, ...]` (state/dir.py) — NOT a `path_history`
        # attribute (that AttributeError crashed `hpc logs` on the exact command
        # the submit output recommends; compose-at-submit review MEDIUM-1).
        cwd = str(Path.cwd().resolve())
        projects = [
            p for p in state_dir.list_projects()
            if cwd in (p.paths or ())
        ]
        if not projects:
            click.secho(
                f"refused: no botainer project for {cwd!r}. "
                f"Use --all-projects to search globally, or `cd` into the "
                f"project root, or pass a jobid explicitly.",
                fg="red", err=True,
            )
            sys.exit(2)
    for proj in projects:
        proj_paths = paths.for_project(proj.uuid)
        # Security-audit Finding 1: SLURM output now lives in the
        # HOST-ONLY dir (paths.hpc_job_output_dir), NOT under the container-
        # bound sessions/_outputs. Read it there. (Single source of truth
        # mirrored by hpc-launcher's _job_output_dir; parity-tested.)
        outputs_dir = paths.hpc_job_output_dir(proj.uuid)
        for rec in session_record.list_sessions(proj_paths.sessions_dir):
            if not (rec.apptainer and rec.apptainer.slurm_jobid):
                continue
            jid = rec.apptainer.slurm_jobid
            if jobid is not None and jid != jobid:
                continue
            out_path = outputs_dir / f"slurm-{jid}.out"
            candidates.append((rec.started_at or "", jid, out_path))

    if not candidates:
        if jobid is not None:
            click.secho(
                f"refused: no botainer session record carries jobid {jobid!r} "
                f"({'across all projects' if all_projects else 'in cwd project'}). "
                f"Run `botainer hpc list` to see what's tracked.",
                fg="red", err=True,
            )
        else:
            click.secho(
                f"refused: no botainer sbatch jobs found "
                f"({'across all projects' if all_projects else 'in cwd project'}).",
                fg="red", err=True,
            )
        sys.exit(2)

    # Newest by started_at (lexicographic ISO is correct ordering).
    candidates.sort(reverse=True)
    _, picked_jobid, out_path = candidates[0]

    click.secho(f"# jobid {picked_jobid}", fg="cyan")
    click.secho(f"# output: {out_path}", fg="cyan")
    if not out_path.exists():
        click.secho(
            f"# (file not yet written — the job may still be queued; check "
            f"`hpc status {picked_jobid}`)",
            fg="yellow", err=True,
        )
        # Don't refuse: if --follow, the file may appear shortly.
        if not follow:
            sys.exit(3)

    # Delegate to `tail` for the actual streaming — handles partial writes,
    # rotation-by-truncate, and ^C cleanly.
    if not shutil.which("tail"):
        click.secho("refused: `tail` not on PATH (BusyBox?). "
                    f"Read the file directly: {out_path}",
                    fg="red", err=True)
        sys.exit(2)
    tail_args = ["tail"]
    if lines > 0:
        tail_args += ["-n", str(lines)]
    if follow:
        tail_args += ["-F"]  # -F (capital): follows by name, survives rotate
    tail_args += ["--", str(out_path)]
    sys.exit(subprocess.call(tail_args))


# ── #54: job-dispatcher daemon (login-node; validates in/, submits caged jobs) ──


def _dispatcher_state_dir():
    from botainer.state import dir as _sd
    p = _sd.ensure_user_state_dir(create_if_missing=True).root / "dispatcher"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def _resolve_child_image(cfg, paths) -> str:
    """Child jobs run in the SAME agent .sif (has python/bash/etc.)."""
    if cfg.image:
        return cfg.image
    agent_plugin = cfg.agent if str(cfg.agent).startswith("agent-") else f"agent-{cfg.agent}"
    sif = paths.apptainer_sif_path(agent_plugin)
    if not sif.exists():
        raise click.ClickException(
            f"child-job image not found: {sif}. Build it with "
            f"`botainer hpc build {agent_plugin}`, or set `image:` in config.")
    return str(sif)


# All botainer SLURM job-name prefixes: sessions, dispatched child jobs, warm
# workers. `hpc status` filters on these so a user sees ALL their botainer jobs.
_BOTAINER_JOB_NAME_PREFIXES = ("botainer-", "botjob-", "botpool-")


def _sbatch_env() -> dict:
    """The environment for a child/worker `sbatch` call, with the parent
    allocation's SLURM_*/SBATCH_*/SRUN_* stripped. The dispatcher is auto-spawned
    INSIDE the session's SLURM allocation, so it inherits e.g. SLURM_MEM_PER_CPU;
    unscrubbed, sbatch's default --export=ALL leaks it into the child, where the
    child's own `#SBATCH --mem` collides → "SLURM_MEM_PER_CPU/GPU/NODE are mutually
    exclusive" (sharp-edges). The child's resources come SOLELY from its
    generated #SBATCH directives; SLURM_CONF (controller location) is kept.

    ALSO scrub APPTAINERENV_*/SINGULARITYENV_* (mirrors the session-launch scrub in
    adapters/apptainer.py + host_helper/submit.py): with sbatch --export=ALL, a
    host-login-poisoned APPTAINERENV_LD_PRELOAD would otherwise ride into the caged
    child and cross --cleanenv into the container. The MPI path is already covered
    by `srun --export=NONE`, but the plain `exec`/`exec srun` child paths are not —
    this closes that parity gap for every child launch (security review).
    """
    keep = {"SLURM_CONF"}
    return {k: v for k, v in os.environ.items()
            if k in keep or not k.startswith(
                ("SLURM_", "SBATCH_", "SRUN_", "APPTAINERENV_", "SINGULARITYENV_"))}


def _run_dispatcher_cycle(project_root: Path) -> int:
    """One cycle for one project: submit new requests + poll running + cancels.
    Returns the number of newly-submitted jobs."""
    from botainer.core import config as _cfgm
    from botainer.core import identity as _identity
    from botainer.core import policy as _pol
    from botainer.hpc import dispatcher as _disp
    from botainer.hpc import jobs as _jobs
    from botainer.state import dir as _sd

    cfg = _cfgm.load_config(project_root)
    if not cfg.job_profiles:
        return 0
    eff = _pol.intersect(_pol.load_site_policy(), _pol.load_user_policy())
    paths = _sd.ensure_user_state_dir(create_if_missing=True)
    uid = _identity.read_project_id(project_root)
    if not uid:
        raise click.ClickException(f"{project_root} is not a botainer project (no project-id).")
    mb = _jobs.ensure_mailbox(paths, uid)
    image = _resolve_child_image(cfg, paths)
    # #68: the project binds every child job gets so it can see its code/data
    # (/workspace rw, /packages ro, /scratch rw) — the foundational fix; before
    # this, dispatched jobs got ZERO binds and couldn't see /workspace at all.
    child_binds = _jobs.child_core_binds(project_root, paths.for_project(uid),
                                         paths.state_dir)
    # #68 cluster layer: expose the cluster software tree + module system RO
    # (from the root-owned site policy; empty = OFF) so a job can use cluster
    # software and `module load` inside. child_env carries MODULEPATH/LMOD_*.
    _cluster_binds, child_env = _jobs.child_cluster_contribution(eff)
    child_binds = (*child_binds, *_cluster_binds)
    user = os.environ.get("USER", "")

    def _sbatch(script_path: Path, profile_name: str = "") -> str:
        prof = cfg.job_profiles.get(profile_name) if profile_name else None
        return sbatch_submit(
            script_path, profile_name=profile_name,
            has_account=bool(getattr(prof, "account", "") or ""))

    def _squeue_info() -> tuple[set, dict]:
        # ids currently in the queue + {id: (state_code, reason)} so poll_running
        # can distinguish pending vs running and record WHY a job is stuck.
        try:
            out = subprocess.run(["squeue", "-h", "-o", "%A|%t|%r", "-u", user],
                                 capture_output=True, text=True, check=True,
                                 env=_sbatch_env())
        except (OSError, subprocess.CalledProcessError):
            return set(), {}
        ids, info = set(), {}
        for ln in out.stdout.splitlines():
            parts = ln.strip().split("|")
            if not parts or not parts[0]:
                continue
            jid = parts[0].strip()
            ids.add(jid)
            info[jid] = (parts[1].strip() if len(parts) > 1 else "",
                         parts[2].strip() if len(parts) > 2 else "")
        return ids, info

    results = _disp.process_inbox_once(
        mb, cfg.job_profiles, eff.jobs, image, child_binds, child_env,
        sbatch=_sbatch, now=_identity.now_iso8601_utc(),
    )
    _ids, _info = _squeue_info()
    _disp.poll_running(mb, _ids, _info)
    # cancel markers → scancel + mark cancelled.
    import json as _json
    for marker in sorted(mb.in_dir.glob("*.cancel")):
        jid = marker.name[:-len(".cancel")]
        sp = mb.out_dir / f"{jid}.status.json"
        rec = _disp._read_status(sp) if sp.exists() else None
        if rec and rec.get("slurm_job_id"):
            subprocess.run(["scancel", str(rec["slurm_job_id"])], capture_output=True)
            rec["state"] = "cancelled"
            _disp._write_status(mb, jid, rec)
        try:
            marker.unlink()
        except OSError:
            pass
    # #68 warm/hot pool: apply agent-requested pool control (start/stop workers)
    # and publish the pool status the agent reads via `botainer-job pool status`.
    # (Bugfix: this passed an undefined `proj` — a NameError that,
    # swallowed by the per-cycle guard, made ALL pool control silently dead.)
    _autostart_warm_pools(project_root, cfg, mb, eff)
    _handle_pool_control(project_root, cfg, mb, eff, image)
    _publish_pool_status(mb)
    return len([r for r in results if r.state == "queued"])


# (proj:profile) keys already auto-warmed this dispatcher process — config warm
# pools start ONCE per session, not re-warmed after idle-release.
_AUTOWARMED_POOLS: set[str] = set()


def _start_pool_workers(proj: Path, profile_name: str, prof, mb, eff,
                        *, size: int, idle_to: int) -> int:
    """Start `size` persistent §4-caged warm workers for `profile_name`, bounded
    by the profile's max_concurrent + the site ceiling. Shared by the agent's
    `pool start` request AND config-declared auto-warm. Returns the count started.
    """
    import secrets
    import shlex

    from botainer.core import identity as _identity
    from botainer.core import policy as _pol
    from botainer.core.refusal import RefusalCategory, Refused
    from botainer.hpc import pool as _pool
    # A warm worker resolves the SESSION image for its loop and ignores a
    # per-profile `image:` — so warm-pooling an image-bearing profile would
    # silently run tasks in the WRONG .sif. The config model_validator rejects
    # image+warm_pool_size at parse, but a profile with `image:` and NO
    # warm_pool_size can still be warm-pooled at RUNTIME (agent `pool start` /
    # CLI). This is the single chokepoint all three start paths funnel through,
    # so enforce it here too (fail-closed) — not just at parse (sharp-edges).
    if getattr(prof, "image", None):
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"profile {profile_name!r} sets `image:` — a warm worker runs the "
            f"session image and would silently use the wrong .sif. Cold-submit "
            f"this profile's jobs (no warm pool) instead.",
        )
    # An MPI / multi-task profile CANNOT be warm-pooled yet: the warm worker
    # holds a plain allocation and `worker_process_one` runs a single caged
    # `apptainer exec` (NOT `srun`), and render_worker_sbatch does not request
    # --ntasks-per-node/--exclusive — so a hot MPI task would run as ONE
    # non-parallel process and look like it worked (silent-wrong). Fail closed
    # until the worker path srun-launches across the allocation (#68 follow-up,
    # sharp-edges). Cold submits DO run MPI correctly (render_child_sbatch).
    if (int(getattr(prof, "nodes", 1) or 1) > 1
            or getattr(prof, "ntasks", None)
            or getattr(prof, "ntasks_per_node", None)):
        raise Refused(
            RefusalCategory.CAPABILITY_VALUE_INVALID,
            f"profile {profile_name!r} is MPI/multi-task (nodes>1 or ntasks*) — "
            f"warm pools don't srun-launch across the allocation yet, so a hot "
            f"task would silently run as one non-parallel process. Cold-submit "
            f"MPI jobs (no warm pool); they run correctly under srun.",
        )
    _pol.check_profile_against_ceiling(
        profile_name, prof.partition, prof.account, prof.gpus,
        prof.max_concurrent, eff.jobs, nodes=int(getattr(prof, "nodes", 1) or 1))
    # Clamp to the profile cap MINUS workers already up for this profile, so
    # repeated `pool start` can't accumulate past max_concurrent (sharp-edges F4
    #). If already at/over the cap, start none.
    workers = _pool.read_pool_state(mb)
    _existing = sum(1 for w in workers if getattr(w, "profile", None) == profile_name)
    _room = max(0, int(prof.max_concurrent) - _existing)
    size = max(0, min(int(size), _room))
    if size == 0:
        return 0
    for _ in range(size):
        wid = _pool.new_worker_id(secrets.token_hex(6))
        _pool.ensure_worker_dirs(mb, wid)
        script = _pool.render_worker_sbatch(
            wid, prof, str(proj), idle_to, quote=shlex.quote,
            # Host-private (mb.run_dir/pool/<wid>) — NEVER bound into a container.
            # Without this Slurm defaulted to <submit-dir>/slurm-%j.out, and the
            # submit CWD is the RW-bound project root (bug audit CRITICAL-1).
            out_dir=_pool.worker_dir(mb, wid))
        sp = _pool.pool_root(mb) / f"{wid}.sbatch"
        sp.write_text(script, encoding="utf-8")
        slurm_id = sbatch_submit(sp, profile_name=profile_name,
                                 has_account=bool(getattr(prof, "account", "")))
        workers.append(_pool.WorkerRec(
            wid, slurm_id, profile_name, idle_to,
            _identity.now_iso8601_utc()))
    _pool.write_pool_state(mb, workers)
    return size


def _autostart_warm_pools(project_root: Path, cfg, mb, eff) -> None:
    """Config-declared warm pools: for each profile with `warm_pool_size`, start
    it ONCE per dispatcher process (at session start). Idempotent via the module
    set; workers that idle-release are NOT re-warmed (that respects the idle
    timeout — the agent re-warms with `botainer-job pool start` if it wants)."""
    for name, prof in cfg.job_profiles.items():
        n = getattr(prof, "warm_pool_size", None)
        if not n or int(n) < 1:
            continue
        key = f"{project_root}:{name}"
        if key in _AUTOWARMED_POOLS:
            continue
        _AUTOWARMED_POOLS.add(key)  # mark BEFORE attempting, so a failure can't loop
        try:
            _start_pool_workers(
                project_root, name, prof, mb, eff, size=int(n),
                idle_to=int(getattr(prof, "warm_pool_idle_timeout", 300)))
            import sys as _sys
            _sys.stderr.write(
                f"dispatcher: auto-started warm pool for {name!r} "
                f"(size {n}).\n")
        except Exception as exc:
            import sys as _sys
            _sys.stderr.write(
                f"dispatcher: warm_pool auto-start for {name!r} failed: {exc}\n")


def _handle_pool_control(proj: Path, cfg, mb, eff, image: str) -> None:
    """Process `kind: pool_control` requests the agent dropped in the inbox
    (start/stop warm workers). Reuses the profile→policy ceiling + the caged
    worker sbatch render. Each request is consumed (unlinked) after handling."""
    import shlex

    from botainer.core import identity as _identity
    from botainer.core import policy as _pol
    from botainer.hpc import dispatcher as _disp
    from botainer.hpc import pool as _pool
    for entry in sorted(mb.in_dir.glob("*.json")):
        # The inbox is AGENT-WRITABLE — read with the same O_NOFOLLOW + fstat
        # guards (INV-1) as the main dispatch path, not a plain read_text() that
        # would follow an agent-planted symlink (sharp-edges). Refused
        # (symlink / non-regular / bad-JSON) → skip this entry.
        try:
            req = _disp.read_request_safely(mb.in_dir, entry.name)
        except Exception:
            continue
        if not isinstance(req, dict) or req.get("kind") != "pool_control":
            continue
        action, rid = req.get("action"), req.get("id", "?")
        # SECURITY (bug audit, HIGH-9): `rid` comes from the CAGED
        # agent's mailbox and is the ONLY _write_status argument that was not
        # id-checked. _write_status interpolates it into
        # `out_dir/.{job_id}.status.json.tmp` and os.replace()s it, so a
        # path-shaped id (e.g. "./../../../evil") wrote OUTSIDE the mailbox.
        # Reachable with no warm pool at all: an unknown profile raises and the
        # except-branch below does the write.
        #
        # NB `id` is OPTIONAL here (it defaults to "?") and legitimate control
        # requests use short ids, so we must NOT drop the request — that would
        # break pool start/stop. Gate only the STATUS WRITE, which is the sole
        # place the value becomes a path component.
        _rid_safe = isinstance(rid, str) and _POOL_CTL_ID_RE.match(rid) is not None
        try:
            if action == "start":
                profile = req.get("profile")
                if profile not in cfg.job_profiles:
                    raise ValueError(f"unknown profile {profile!r}")
                _start_pool_workers(
                    proj, profile, cfg.job_profiles[profile], mb, eff,
                    size=int(req.get("size", 1)),
                    idle_to=int(req.get("idle_timeout", 300)))
            elif action == "stop":
                wid = req.get("worker")
                keep = []
                for w in _pool.read_pool_state(mb):
                    if wid and w.worker_id != wid:
                        keep.append(w)
                        continue
                    (_pool.worker_dir(mb, w.worker_id) / "STOP").write_text("")
                    if w.slurm_job_id:
                        subprocess.run(["scancel", "--", w.slurm_job_id], capture_output=True)
                _pool.write_pool_state(mb, keep)
        except Exception as exc:  # record so the agent sees the failure
            if _rid_safe:
                _disp._write_status(mb, rid, {
                    "id": rid, "state": "refused",
                    "reason": f"pool {action} failed: {exc}"})
            else:
                # Can't name a status file safely for a path-shaped id. The
                # request is still consumed (finally: entry.unlink) so it can't
                # be replayed; the agent simply gets no status echo for it.
                print(f"botainer: pool_control request has an unsafe id "
                      f"{rid!r}; refusing to write a status file for it.",
                      file=sys.stderr)
        finally:
            try:
                entry.unlink()
            except OSError:
                pass


def _publish_pool_status(mb) -> None:
    """Write out/pool.json (agent-readable) from pool state + live heartbeats."""
    import json as _json
    import os as _os

    from botainer.hpc import pool as _pool
    manifest = {"version": "botainer-pool-status-v1", "workers": []}
    for w in _pool.read_pool_state(mb):
        beat = _pool.read_beat(mb, w.worker_id) or {}
        manifest["workers"].append({
            "worker_id": w.worker_id, "profile": w.profile,
            "slurm_job_id": w.slurm_job_id, "state": beat.get("state", "?")})
    mb.out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = mb.out_dir / ".pool.json.tmp"
    tmp.write_text(_json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    _os.replace(tmp, mb.out_dir / "pool.json")


@hpc.group("dispatcher")
def dispatcher_grp() -> None:
    """Run the job dispatcher (login node): validates agent job requests and
    submits them as CAGED sbatch jobs. Start once per login session."""


@dispatcher_grp.command("once")
@click.option("--project", default=".", help="Project dir (default: cwd).")
@handle_refusals
def dispatcher_once(project: str) -> None:
    """Run a single dispatch cycle for one project and exit (cron-friendly)."""
    from botainer.hpc import dispatcher_claim as _claim
    proj = Path(project).resolve()
    mb = _project_mailbox(proj)
    # `once` is the cron shape, so it is the ONE most likely to run alongside a
    # `dispatcher start` — a crontab does not know a session opened. Without
    # this it would dispatch beside the live dispatcher and double-submit,
    # which is precisely what the claim exists to stop. Same claim, so the two
    # shapes cannot both be active.
    if mb is not None:
        won, blocking = _claim.acquire(mb, str(proj))
        if not won:
            where = ("here" if blocking is None or blocking.is_local
                     else f"on {blocking.host}")
            pid = blocking.pid if blocking else "?"
            click.echo(f"dispatcher: a dispatcher is already running for this "
                       f"project (pid {pid} {where}); nothing to do.")
            return
        try:
            n = _run_dispatcher_cycle(proj)
        finally:
            _claim.release(mb)
    else:
        n = _run_dispatcher_cycle(proj)
    click.echo(f"dispatcher: cycle done ({n} newly submitted).")


@dispatcher_grp.command("start")
@click.option("--project", default=".", help="Project dir (default: cwd).")
@click.option("--interval", default=15, help="Poll seconds (default 15).")
@handle_refusals
def dispatcher_start(project: str, interval: int) -> None:
    """Run the dispatcher loop in the foreground (Ctrl-C to stop). For a
    background daemon, run under `nohup … &` or a systemd-user unit; `once` +
    cron is the other supported shape."""
    import signal
    import time
    from botainer.core import identity as _identity
    from botainer.hpc import dispatcher_claim as _claim
    proj = Path(project).resolve()

    mb = _project_mailbox(proj)
    if mb is None:
        raise click.ClickException(
            f"{proj} is not a botainer project with jobs enabled "
            f"(no project-id, or no job_profiles in .botainer/config.yaml).")

    # ONE DISPATCHER PER PROJECT, structurally. Before nothing
    # checked, so a second session (or a user following a stale "restart it"
    # hint) put a second poller on the same mailbox — and the re-submit guard is
    # a read-then-act status check, not a lock, so both submitted. That spends
    # the allocation twice. See botainer/hpc/dispatcher_claim.py.
    won, blocking = _claim.acquire(mb, str(proj))
    if not won:
        if blocking is not None:
            where = "here" if blocking.is_local else f"on {blocking.host}"
            click.secho(
                f"dispatcher: already running for this project "
                f"(pid {blocking.pid} {where}, last beat "
                f"{int(blocking.age)}s ago). Not starting a second one — two "
                f"dispatchers on one mailbox submit every job twice.",
                fg="yellow")
            return
        # No blocking claim, yet we did not win: the filesystem could not tell
        # us who owns this. Before the claim existed the dispatcher ALWAYS
        # started, so starting is no worse than the old behaviour, while
        # declining to start would silently strand every dispatched job. Say
        # loudly what we could not establish, then run.
        click.secho(
            "dispatcher: could not establish ownership of this project "
            "(filesystem error reading the claim). Starting anyway — if you "
            "have another dispatcher running for THIS project, stop one of "
            "them, or every job will be submitted twice.",
            fg="yellow", err=True)

    # Watchdog: when auto-started for a session (start.py sets
    # BOTAINER_LAUNCHER_PID), self-exit if that launcher process disappears so a
    # crashed/killed launcher doesn't leave an orphan poller running forever.
    _launcher_raw = os.environ.get("BOTAINER_LAUNCHER_PID", "")
    _launcher_pid = int(_launcher_raw) if _launcher_raw.isdigit() else None

    # SIGTERM is the REAL session-end path (autodispatch.stop sends it), and
    # Python's default handler runs no `finally` — which is why the old pid
    # record was always left behind, telling the next reader "NOT running,
    # restart it". Turn it into a normal exception so cleanup happens.
    def _on_term(_signum, _frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        pass  # not on the main thread; the finally still covers the other paths

    # A dispatcher auto-started for a session has stdout AND stderr on
    # /dev/null (autodispatch.maybe_start), so  every error it
    # ever printed — "Slurm rejected this job", a crashed cycle, a bad account —
    # went nowhere at all. Not to the user, not to a file, not to the agent.
    # Its own log is host-private (run/ is never bound), so nothing here is
    # readable or forgeable by the caged agent.
    log_path = mb.run_dir / "dispatcher.log"

    def _log(msg: str) -> None:
        # CAPPED. A dispatcher polls every 10-15s for the life of a session; a
        # cycle that errors every time (a bad account, a down scheduler) writes
        # thousands of identical lines a day. An unbounded log in $MY_BOTAINER
        # is the same defect as everything else that quietly filled the
        # maintainer's home quota — see storage tiering, task #107. One rotation
        # at the cap, so the last error is always the visible one.
        try:
            if log_path.exists() and log_path.stat().st_size > _DISPATCH_LOG_MAX:
                log_path.replace(log_path.with_suffix(".log.1"))
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{_identity.now_iso8601_utc()} {msg}\n")
        except OSError:
            pass

    _log(f"started pid={os.getpid()} project={proj} interval={interval}s")
    click.echo(f"dispatcher: watching {proj} every {interval}s (pid {os.getpid()}). Ctrl-C to stop.")
    click.echo(f"dispatcher: log -> {log_path}")
    try:
        while True:
            if _launcher_pid is not None:
                try:
                    os.kill(_launcher_pid, 0)
                except ProcessLookupError:
                    click.echo("dispatcher: launcher gone; stopping.")
                    break
                except PermissionError:
                    pass  # exists but not signalable — still alive
            # Heartbeat FIRST: if we were taken over (our claim expired while
            # this process was stopped//suspended and someone else took it), we
            # must not dispatch — that is the double-submit case.
            if not _claim.beat(mb, str(proj)):
                _log("stopping: another dispatcher took over this project")
                click.secho("dispatcher: another dispatcher took over this "
                            "project; stopping to avoid double-submitting.",
                            fg="yellow", err=True)
                break
            try:
                _run_dispatcher_cycle(proj)
            except click.ClickException as exc:
                _log(f"ERROR {exc}")
                click.echo(f"dispatcher: {exc}", err=True)
            except Exception as exc:  # noqa: BLE001
                # Defense-in-depth (audit HIGH): a single cycle must
                # NEVER kill the daemon. submit_request already fails closed per
                # request, but if any OTHER cycle step (pool control, status
                # publish, an unforeseen crash) raises, log and keep polling
                # rather than letting the launcher's job dispatch die silently.
                _log(f"ERROR cycle {type(exc).__name__}: {exc}")
                click.echo(f"dispatcher: cycle error ({type(exc).__name__}): "
                           f"{exc}", err=True)
            time.sleep(max(1, interval))
    except KeyboardInterrupt:
        _log("stopped")
        click.echo("\ndispatcher: stopped.")
    finally:
        _claim.release(mb)


_SBATCH_ERR_MAX = 240
_DISPATCH_LOG_MAX = 1 << 20        # 1 MiB, then one rotation. See _log().


def _sanitize_scheduler_error(text: str) -> str:
    """sbatch's own words, safe to show a caged agent.

    The agent is untrusted and may be prompt-injected, so the dispatcher's
    `except Exception` deliberately refuses to leak internals to it — which is
    WHY a rejected job reached the agent as "internal error handling request
    (CalledProcessError)" and nobody, user included, could tell that Slurm had
    said "Invalid account". The scheduler's message is worth forwarding; the
    host paths in it are not. Strip absolute paths, drop control characters
    (ANSI/NUL — same discipline as _scrub_profile_text), cap the length.
    """
    import re
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = line.removeprefix("sbatch: error: ").removeprefix("sbatch: ")
        line = re.sub(r"/\S+", "<path>", line)
        line = "".join(ch if ch.isprintable() else " " for ch in line)
        line = " ".join(line.split())
        if line:
            out.append(line)
    joined = "; ".join(out[:2])
    return joined[:_SBATCH_ERR_MAX] if joined else "no reason given"


def sbatch_submit(script_path: Path, *, profile_name: str = "",
                  has_account: bool = True) -> str:
    """Submit one script and return the Slurm job id, or raise Refused.

    THE SINGLE SBATCH CALL SITE. There were two — the cold submit and the warm
    pool — and a fix to one would not have reached the other. Sibling drift is
    this project's most-repeated defect, so the two now share this instead of
    each carrying a copy of the error handling.
    """
    from botainer.core.refusal import RefusalCategory, Refused
    try:
        out = subprocess.run(["sbatch", "--parsable", str(script_path)],
                             capture_output=True, text=True, check=True,
                             env=_sbatch_env())
    except FileNotFoundError:
        raise Refused(RefusalCategory.JOB_SUBMIT_REJECTED,
                      "sbatch is not on PATH — this is not a scheduler host. "
                      "The dispatcher must run on a LOGIN node.") from None
    except subprocess.CalledProcessError as exc:
        why = _sanitize_scheduler_error(exc.stderr or exc.stdout or "")
        hint = ""
        if not has_account and "account" in why.lower():
            # The generated template ships `account: ""`, so this is the
            # DEFAULT path on any cluster that requires an allocation — not an
            # edge case. Name the fix rather than restating the error.
            hint = (f" — job profile {profile_name or '(unnamed)'} has no "
                    f"`account:` set. Find yours with: "
                    f"sacctmgr -nP show assoc user=$USER format=account")
        raise Refused(RefusalCategory.JOB_SUBMIT_REJECTED,
                      f"Slurm rejected this job: {why}{hint}") from None
    return out.stdout.strip().split(";")[0]


def _project_mailbox(project_root: Path):
    """This project's mailbox, or None if it isn't a jobs-enabled project.

    Never raises: every caller is a status/warning path where a config problem
    must not become a traceback.
    """
    try:
        from botainer.core import config as _cfgm
        from botainer.core import identity as _identity
        from botainer.hpc import jobs as _jobs
        from botainer.state import dir as _sd
        cfg = _cfgm.load_config(project_root)
        if not getattr(cfg, "job_profiles", None):
            return None
        uid = _identity.read_project_id(project_root)
        if not uid:
            return None
        return _jobs.mailbox_for(
            _sd.ensure_user_state_dir(create_if_missing=True), uid)
    except Exception:                                           # noqa: BLE001
        return None


def dispatcher_liveness(project_root: Path | None = None) -> tuple[str, str]:
    """(state, detail) for THIS PROJECT's job dispatcher.

    state is running|stale|stopped, plus "unknown" only when the project itself
    cannot be identified (not a project, or jobs not enabled) — in which case
    there is nothing to report on and no warning should fire.

    Was per-USER until, which was simply the wrong shape: the
    dispatcher process has always been per-project (`--project`), so with two
    projects open the single record described whichever started last, and its
    exit deleted the other's. Observed:

        A started; record = 58689    B started; record = 58694
        status -> "running (58694)"  while A is the one serving project A

    Liveness now comes from a heartbeat in the project's own host-private
    `run/dispatcher.json`. A heartbeat also answers across login nodes, where a
    pid cannot be checked at all — so the "unknown" state this function briefly
    carried is gone: a fresh beat means alive somewhere, a stale one means gone
    wherever it was.

    "stale" stays a distinct, louder state than "stopped": stopped is a choice,
    stale is a failure, and the user's jobs are piling up unprocessed.
    """
    from botainer.hpc import dispatcher_claim as _claim
    proj = Path(project_root or ".").resolve()
    mb = _project_mailbox(proj)
    if mb is None:
        return "unknown", (f"{proj} is not a botainer project with jobs "
                           f"enabled — nothing to dispatch")
    return _claim.state(mb)


@dispatcher_grp.command("status")
@click.option("--project", default=".", help="Project dir (default: cwd).")
@handle_refusals
def dispatcher_status(project: str) -> None:
    """Is a dispatcher serving THIS project? (Answers per project — a
    dispatcher for another project is a different dispatcher.)"""
    proj = Path(project).resolve()
    state, detail = dispatcher_liveness(proj)
    if state == "running":
        click.secho(f"dispatcher: running for {proj.name} ({detail}).", fg="green")
    elif state == "stale":
        click.secho(f"dispatcher: NOT running — {detail}.", fg="red", err=True)
        click.secho(
            "  Jobs the agent dispatches will sit in the mailbox and never "
            "start.\n"
            "  Restart it on the LOGIN node:  botainer hpc dispatcher start",
            fg="cyan", err=True)
    elif state == "unknown":
        click.echo(f"dispatcher: {detail}.")
    else:
        click.echo(f"dispatcher: not running for {proj.name} ({detail}).")
        click.secho(
            "  If this project dispatches jobs, start it on the LOGIN node:\n"
            "      botainer hpc dispatcher start",
            fg="cyan")
    _show_last_dispatcher_error(proj)
    if state in ("stale", "stopped"):
        _warn_legacy_dispatcher()


def _show_last_dispatcher_error(project_root: Path) -> None:
    """The most recent dispatcher error, if any.

    A log nobody is pointed at is the same defect as no log. `dispatcher
    status` is the one command a user runs when jobs are not moving, so the
    reason belongs here — otherwise they would have to know the file exists,
    inside a host-private directory they have never been told about.
    """
    mb = _project_mailbox(project_root)
    if mb is None:
        return
    log_path = mb.run_dir / "dispatcher.log"
    try:
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines()
                 if " ERROR " in ln]
    except OSError:
        return
    if not lines:
        return
    click.secho(f"  last error: {lines[-1].strip()}", fg="yellow")
    if len(lines) > 1:
        click.secho(f"  ({len(lines)} errors logged) full log: {log_path}",
                    fg="cyan")
    else:
        click.secho(f"  full log: {log_path}", fg="cyan")


def _warn_legacy_dispatcher() -> None:
    """Surface a dispatcher started by a botainer older than.

    UPGRADE HAZARD, and one this change would otherwise CREATE. Before the
    per-project claim, the dispatcher recorded itself in a single per-user
    `dispatcher/pid`. A poller started by the old code is still running and
    still servicing the mailbox, but the new code does not look there — so
    status would say "not running", the user would start a second one, and they
    would land in exactly the double-submit this change exists to prevent.

    Only fires when that legacy pid is alive on THIS host, so it disappears by
    itself once the old process is gone. Nothing writes this file any more.
    """
    try:
        legacy = _dispatcher_state_dir() / "pid"
        raw = legacy.read_text().strip().split()
        pid = int(raw[0])
        host = raw[1] if len(raw) > 1 else ""
    except (OSError, ValueError, IndexError):
        return
    import socket as _socket
    if host and host != _socket.gethostname():
        return
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            legacy.unlink()          # provably finished; stop nagging about it
        except OSError:
            pass
        return
    click.secho(
        f"\n  NOTE: pid {pid} is a dispatcher started by an older botainer.\n"
        f"  It uses the old per-user record and still services mailboxes, but\n"
        f"  this version cannot track it. Stop it before starting a new one,\n"
        f"  or you will have two dispatchers submitting every job twice:\n"
        f"      kill {pid}",
        fg="yellow", err=True)


# ─────────────────────── warm/hot worker pool (#68) ───────────────────────


def _pool_ctx(project_root: Path):
    """Shared setup for the pool commands: (cfg, mailbox, image, eff_policy, uid).
    Mirrors _run_dispatcher_cycle so the pool reuses the same config/policy/image."""
    from botainer.core import config as _cfgm
    from botainer.core import identity as _identity
    from botainer.core import policy as _pol
    from botainer.hpc import jobs as _jobs
    from botainer.state import dir as _sd
    cfg = _cfgm.load_config(project_root)
    if not cfg.job_profiles:
        raise click.ClickException(f"{project_root} has no job_profiles (jobs disabled).")
    eff = _pol.intersect(_pol.load_site_policy(), _pol.load_user_policy())
    paths = _sd.ensure_user_state_dir(create_if_missing=True)
    uid = _identity.read_project_id(project_root)
    if not uid:
        raise click.ClickException(f"{project_root} is not a botainer project (no project-id).")
    mb = _jobs.ensure_mailbox(paths, uid)
    image = _resolve_child_image(cfg, paths)
    return cfg, mb, image, eff, uid


@hpc.group("pool")
def pool_grp() -> None:
    """Warm/hot worker pool: persistent caged workers that run agent tasks
    immediately (no per-task SLURM queue wait)."""


@pool_grp.command("worker")
@click.argument("worker_id")
@click.option("--project", default=".", help="Project dir (default: cwd).")
@click.option("--idle-timeout", default=300, help="Exit after N idle seconds.")
@click.option("--gpus", type=int, default=0,
              help="GPU count of this worker's allocation (adds --nv to hot tasks).")
@handle_refusals
def pool_worker(worker_id: str, project: str, idle_timeout: int,
                gpus: int = 0) -> None:
    """Run ONE warm worker's loop (invoked by the worker sbatch script inside the
    allocation). Polls its inbox, runs each task §4-caged, exits when idle."""
    from botainer.hpc import dispatcher as _disp
    from botainer.hpc import jobs as _jobs
    from botainer.hpc import pool as _pool
    proj = Path(project).resolve()
    _cfg, mb, image, _eff, _uid = _pool_ctx(proj)
    # #68: warm workers cage each hot task the same as the cold path — give them
    # the same project binds (/workspace, /packages, /scratch) so a hot task can
    # see the code it runs, exactly like a cold submit.
    from botainer.state import dir as _sd
    _paths = _sd.ensure_user_state_dir(create_if_missing=True)
    child_binds = _jobs.child_core_binds(proj, _paths.for_project(_uid),
                                         _paths.state_dir)
    _cluster_binds, child_env = _jobs.child_cluster_contribution(_eff)
    child_binds = (*child_binds, *_cluster_binds)

    def _status(jid: str, rec: dict) -> None:
        _disp._write_status(mb, jid, rec)

    reason = _pool.worker_loop(
        mb, worker_id, image, idle_timeout, binds=child_binds, env=child_env,
        run_caged=_pool.make_subprocess_runner(), write_status=_status,
        gpus=gpus,
    )
    click.echo(f"worker {worker_id}: exit ({reason}).")


@pool_grp.command("start")
@click.argument("profile")
@click.option("--project", default=".", help="Project dir (default: cwd).")
@click.option("--size", default=1, help="Number of warm workers to start.")
@click.option("--idle-timeout", default=300, help="Worker idle-exit seconds.")
@handle_refusals
def pool_start(profile: str, project: str, size: int, idle_timeout: int) -> None:
    """Start SIZE warm workers for a profile (host-side sbatch submit)."""
    proj = Path(project).resolve()
    cfg, mb, _image, eff, _uid = _pool_ctx(proj)
    if profile not in cfg.job_profiles:
        raise click.ClickException(f"unknown profile {profile!r} (not in job_profiles).")
    # Shared with the dispatcher's pool control + config auto-warm (one place that
    # imports secrets, bounds size by max_concurrent + the site ceiling, renders +
    # sbatches the caged workers) — previously this was a 3rd copy that also
    # NameError'd on an unimported `secrets`.
    started = _start_pool_workers(
        proj, profile, cfg.job_profiles[profile], mb, eff,
        size=int(size), idle_to=int(idle_timeout))
    click.echo(f"pool: started {started} warm worker(s) for profile {profile!r}.")


@pool_grp.command("status")
@click.option("--project", default=".", help="Project dir (default: cwd).")
@handle_refusals
def pool_status(project: str) -> None:
    """Show the warm workers + their live idle/busy state."""
    from botainer.hpc import pool as _pool
    proj = Path(project).resolve()
    _cfg, mb, _image, _eff, _uid = _pool_ctx(proj)
    workers = _pool.read_pool_state(mb)
    if not workers:
        click.echo("pool: no warm workers.")
        return
    for w in workers:
        beat = _pool.read_beat(mb, w.worker_id) or {}
        click.echo(f"  {w.worker_id}  profile={w.profile}  slurm={w.slurm_job_id}  "
                   f"state={beat.get('state','?')}")


@pool_grp.command("stop")
@click.option("--project", default=".", help="Project dir (default: cwd).")
@click.option("--worker", "worker_id", default=None, help="Stop one worker (default: all).")
@handle_refusals
def pool_stop(project: str, worker_id: str | None) -> None:
    """Stop warm workers: drop a STOP marker (clean exit) + scancel."""
    from botainer.hpc import pool as _pool
    proj = Path(project).resolve()
    _cfg, mb, _image, _eff, _uid = _pool_ctx(proj)
    workers = _pool.read_pool_state(mb)
    keep = []
    stopped = 0
    for w in workers:
        if worker_id and w.worker_id != worker_id:
            keep.append(w)
            continue
        (_pool.worker_dir(mb, w.worker_id) / "STOP").write_text("")
        if w.slurm_job_id:
            subprocess.run(["scancel", "--", w.slurm_job_id], capture_output=True)
        stopped += 1
    _pool.write_pool_state(mb, keep)
    click.echo(f"pool: stopped {stopped} worker(s).")
