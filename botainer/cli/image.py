"""`botainer image build|list|inspect` — agent image lifecycle.

Two runtimes, parity required (per CLAUDE.md "HPC parity is
non-negotiable"):

  docker (laptop / dev):    `docker build -t botainer/<name>:0.1 ...`
                            digest recorded in installed.lock
  apptainer (HPC):          `apptainer build <state_root>/images/
                            botainer-<name>.sif plugins/<name>/<name>.def`
                            sif path + sha256 recorded in installed.lock

The `--runtime` flag picks; default is `auto` (whichever of docker /
apptainer is on PATH).

Per-plugin convention:
  plugins/<name>/Dockerfile   → docker tag `botainer/<name>:0.1`
  plugins/<name>/<name>.def   → apptainer .sif at
                                <state_root>/images/botainer-<name>.sif
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.plugins import lifecycle as lifecycle_module
from botainer.plugins import provenance as prov_module
from botainer.state import dir as state_dir


@click.group("image", invoke_without_command=True)
@click.pass_context
def image(ctx: click.Context) -> None:
    """Build / list / inspect agent container images."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@image.command("list")
def list_() -> None:
    """List images known on this host.

    Reports BOTH docker images (via `docker image inspect`) and
    apptainer .sif files (via filesystem stat under
    <state_root>/images/), for every installed plugin with a buildable
    definition. Codex review workflow-UX MEDIUM #9.
    """
    installed = lifecycle_module.list_installed()
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    lock = {e.name: e for e in prov_module.read_lock(paths.installed_lock_path)}
    docker_present = shutil.which("docker") is not None
    apptainer_present = (
        shutil.which("apptainer") is not None
        or shutil.which("singularity") is not None
    )
    any_emitted = False
    for p in installed:
        dockerfile = p.plugin_dir / "Dockerfile"
        def_file = p.plugin_dir / f"{p.name}.def"
        entry = lock.get(p.name)
        recorded_digest = entry.image_digest if entry else None

        # Docker image (if Dockerfile present and docker available)
        if dockerfile.exists():
            any_emitted = True
            tag = f"botainer/{p.name}:0.1"
            if docker_present:
                try:
                    proc = subprocess.run(
                        ["docker", "image", "inspect", "-f", "{{.Id}}", tag],
                        capture_output=True, text=True, timeout=10,
                    )
                    live_id = (
                        proc.stdout.strip()
                        if proc.returncode == 0 else ""
                    )
                except subprocess.TimeoutExpired:
                    live_id = ""
                if live_id:
                    detail = f"✓ id={live_id[:19]}"
                else:
                    detail = f"✗ not built — `botainer image build {p.name}`"
            else:
                detail = "(docker not on PATH; can't query)"
            rec = (
                f"recorded={recorded_digest[:16]}..."
                if recorded_digest and not recorded_digest.startswith("apptainer:")
                else "no recorded digest"
            )
            click.echo(f"  [docker]    {tag:34s}  {detail}  ({rec})")

        # Apptainer .sif (if .def present)
        if def_file.exists():
            any_emitted = True
            sif_path = paths.apptainer_sif_path(p.name)
            if sif_path.exists():
                size_mb = sif_path.stat().st_size // (1024 * 1024)
                detail = f"✓ {sif_path}  ({size_mb} MB)"
            elif apptainer_present:
                detail = f"✗ not built — `botainer image build {p.name} --runtime apptainer`"
            else:
                detail = "(apptainer not on PATH; check on a node that has it)"
            # Apptainer lock marker format: apptainer:sha256:<hex>:<path>
            apptainer_recorded = (
                recorded_digest
                if recorded_digest and recorded_digest.startswith("apptainer:")
                else None
            )
            if apptainer_recorded:
                rec_hex = apptainer_recorded.split(":", 3)[2][:16]
                rec = f"sha256={rec_hex}..."
            else:
                rec = "no recorded sha256"
            click.echo(f"  [apptainer] {p.name + '.sif':34s}  {detail}  ({rec})")

    if not any_emitted:
        click.echo("(no installed plugins with Dockerfile or .def files)")
        click.echo("Run `botainer setup` to install bundled plugins.")


@image.command("build")
@click.argument("plugin_name", required=False)
@click.option(
    "--no-cache",
    is_flag=True,
    help="docker build --no-cache / apptainer build --force (force full rebuild)",
)
@click.option(
    "--all",
    "build_all",
    is_flag=True,
    help="Build images for all installed plugins that ship a buildable definition.",
)
@click.option(
    "--runtime",
    type=click.Choice(["auto", "docker", "apptainer"]),
    default="auto",
    show_default=True,
    help="Which runtime to build for. `auto` picks docker if available, "
    "else apptainer.",
)
@handle_refusals
def build(
    plugin_name: str | None,
    no_cache: bool,
    build_all: bool,
    runtime: str,
) -> None:
    """Build the container image for a plugin (or all with --all).

    Docker: looks under <plugin>/Dockerfile, builds tag
    `botainer/<name>:0.1`, records the resulting image digest into
    installed.lock so `botainer start` finds it.

    Apptainer (HPC): looks under <plugin>/<name>.def, builds
    `<state_root>/images/botainer-<name>.sif`, records the .sif path
    and sha256 in installed.lock.
    """
    chosen = _resolve_build_runtime(runtime)
    installed = lifecycle_module.list_installed()
    targets = _select_targets(installed, plugin_name, build_all, chosen)
    if not targets:
        return

    # Dev-friction guard: if the user has edited the bundled plugin
    # source in their clone but hasn't re-run `botainer setup`, the
    # installed plugin tree at $MY_BOTAINER/plugins/<name>/ is stale.
    # Detect this by comparing tree hashes; warn loudly so the user
    # knows why a build that they "just fixed" is still failing.
    _warn_if_installed_tree_is_stale_vs_source(targets)

    for p in targets:
        if chosen == "docker":
            _build_one_docker(p, no_cache=no_cache)
        elif chosen == "apptainer":
            _build_one_apptainer(p, no_cache=no_cache)


def _resolve_build_runtime(runtime: str) -> str:
    """Pick the build runtime. Refuses with a useful message if the
    chosen runtime isn't on PATH."""
    if runtime == "auto":
        if shutil.which("docker"):
            return "docker"
        if shutil.which("apptainer") or shutil.which("singularity"):
            return "apptainer"
        click.secho(
            "refused: runtime-not-available: neither `docker` nor "
            "`apptainer` on PATH",
            fg="red", err=True,
        )
        click.secho(
            "    On laptop: install Docker Desktop. On HPC: `module load "
            "apptainer` or contact your cluster admin.",
            fg="cyan", err=True,
        )
        sys.exit(4)
    if runtime == "docker":
        if not shutil.which("docker"):
            click.secho(
                "refused: runtime-not-available: `docker` not on PATH",
                fg="red", err=True,
            )
            click.secho(
                "    Pass `--runtime apptainer` if you're on HPC.",
                fg="cyan", err=True,
            )
            sys.exit(4)
        # Audit T6: the daemon must be REACHABLE, not just the binary present —
        # otherwise `docker build` fails much later with a raw daemon error.
        # Probe and refuse early with the actionable message.
        try:
            _probe = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=8,
            )
            _ok = _probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _ok = False
        if not _ok:
            click.secho(
                "refused: Docker daemon not reachable (the binary is on PATH but "
                "the daemon isn't running).",
                fg="red", err=True,
            )
            click.secho(
                "    macOS: start Docker Desktop. Linux: `sudo systemctl start "
                "docker`. Then retry. (Or `--runtime apptainer` on HPC.)",
                fg="cyan", err=True,
            )
            sys.exit(4)
        return "docker"
    if runtime == "apptainer":
        if not (shutil.which("apptainer") or shutil.which("singularity")):
            click.secho(
                "refused: runtime-not-available: neither `apptainer` nor "
                "`singularity` on PATH",
                fg="red", err=True,
            )
            from botainer.cli import _common
            click.secho("    " + _common.apptainer_missing_advice().replace(
                "\n", "\n    "), fg="cyan", err=True)
            sys.exit(4)
        return "apptainer"
    raise click.UsageError(f"unknown --runtime {runtime!r}")


def _select_targets(installed, plugin_name, build_all, chosen):
    """Filter installed plugins down to those that have a buildable
    definition for the chosen runtime."""
    def has_def(p) -> bool:
        if chosen == "docker":
            return (p.plugin_dir / "Dockerfile").exists()
        return (p.plugin_dir / f"{p.name}.def").exists()

    def_kind = "Dockerfile" if chosen == "docker" else "<name>.def"
    if build_all:
        targets = [p for p in installed if has_def(p)]
        if not targets:
            click.echo(f"(no installed plugins with a {def_kind})")
            return []
        return targets
    if not plugin_name:
        click.secho(
            "refused: specify a plugin name or use --all", fg="red", err=True
        )
        click.echo(f"Available plugins with a {def_kind}:")
        for p in installed:
            if has_def(p):
                click.echo(f"  {p.name}")
        sys.exit(2)
    matches = [p for p in installed if p.name == plugin_name]
    if not matches:
        click.secho(
            f"refused: plugin {plugin_name!r} not installed", fg="red", err=True
        )
        click.echo("Run `botainer plugin list` to see installed plugins.")
        sys.exit(2)
    p = matches[0]
    if not has_def(p):
        click.secho(
            f"refused: plugin {plugin_name!r} has no {def_kind} for runtime={chosen!r}",
            fg="red", err=True,
        )
        sys.exit(2)
    return matches


def _build_one_docker(p, *, no_cache: bool) -> None:
    """Build one plugin's Docker image.

    Reads `image.dockerfile` from the plugin manifest (relative to
    plugin root) if set; falls back to ./Dockerfile. ImageDecl's
    `_validate_build_path` field validator refuses an absolute or
    `..`-traversing value at manifest-parse time (AUDIT: this
    docstring previously asserted that guard while no such validator
    existed — now it does), so `plugin_dir / dockerfile_rel` stays inside
    the plugin tree.
    """
    dockerfile_rel = "Dockerfile"
    try:
        from botainer.plugins.manifest import load_manifest
        m = load_manifest(p.plugin_dir)
        if m.image and m.image.dockerfile:
            dockerfile_rel = m.image.dockerfile
    except Exception:
        # Manifest issues (malformed yaml etc.) are surfaced by other
        # paths; the build code just falls back to the convention.
        pass
    dockerfile = p.plugin_dir / dockerfile_rel
    tag = f"botainer/{p.name}:0.1"
    cmd = ["docker", "build", "-t", tag, "-f", str(dockerfile)]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(p.plugin_dir))
    click.secho(
        f"Building {tag} from {dockerfile} (this can take 8-12 min the first time)...",
        fg="cyan",
    )
    rc = subprocess.call(cmd)
    if rc != 0:
        click.secho(
            f"refused: image build failed for {p.name} (rc={rc})",
            fg="red", err=True,
        )
        click.secho(
            "    The REAL error is in the build output ABOVE — scroll up to the\n"
            "    step that stopped. A line starting `FATAL:` is an explicit check\n"
            "    in the image recipe and names the exact cause; report it as-is.\n"
            "\n"
            "    If you see 'no space left on device' / ENOSPC (disk full):\n"
            "      • reclaim SAFELY:  docker builder prune -af   (build cache; keeps\n"
            "        your images). Do NOT use `docker system prune -a` — its `-a`\n"
            "        deletes your built agent image and you'd rebuild it.\n"
            "      • Docker Desktop (Mac/Windows): the limit is the VM's virtual\n"
            "        disk, NOT your host free space — raise it under Settings →\n"
            "        Resources → Disk. A build can ENOSPC with 100s of GB free on\n"
            "        the host. (The agent image is ~3.5 GB, +~0.4 GB with browser.)\n"
            "    Otherwise: check network / the apt mirror.",
            fg="cyan", err=True,
        )
        sys.exit(rc)
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "-f", "{{.Id}}", tag],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        click.secho(
            f"refused: docker image inspect timed out for {tag}",
            fg="red", err=True,
        )
        sys.exit(2)
    if result.returncode == 0:
        image_id = result.stdout.strip()
        _record_image_digest(p.name, image_id)
        click.secho(
            f"✓ built {tag} ({image_id[:19]}); recorded in installed.lock",
            fg="green",
        )
    else:
        click.secho(
            f"warning: built {tag} but couldn't query its digest",
            fg="yellow",
        )


def _build_one_apptainer(p, *, no_cache: bool) -> None:
    """Build one plugin's Apptainer .sif image.

    Convention: produces `<state_root>/images/botainer-<plugin>.sif`.
    The .def file is taken from `image.definition` in the plugin
    manifest if set; otherwise falls back to `<plugin>.def` in the
    plugin dir. Records the absolute .sif path and its sha256 into
    installed.lock so `botainer start` (and the HPC submit flow) can
    resolve to the built image without the user copy-pasting paths
    into config.
    """
    def_rel = f"{p.name}.def"
    try:
        from botainer.plugins.manifest import load_manifest
        m = load_manifest(p.plugin_dir)
        if m.image and m.image.definition:
            def_rel = m.image.definition
    except Exception:
        pass
    def_file = p.plugin_dir / def_rel
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    paths.images_dir.mkdir(parents=True, exist_ok=True)
    sif_path = paths.apptainer_sif_path(p.name)
    bin_name = "apptainer" if shutil.which("apptainer") else "singularity"
    cmd = [bin_name, "build"]
    if no_cache:
        cmd.append("--force")  # apptainer's equivalent of "ignore existing"
    cmd.append(str(sif_path))
    # Use just the file name for the def — apptainer resolves %files
    # paths relative to its CWD, and we'll run it from the plugin dir.
    cmd.append(def_file.name)
    click.secho(
        f"Building {sif_path} from {def_file} ({bin_name}; this can "
        f"take 10-20 min the first time)...",
        fg="cyan",
    )
    # Real-host bug: apptainer build was invoked with cwd =
    # wherever the user happened to be (typically their HOME), and the
    # .def file's %files section uses relative paths like
    # `entrypoint_wrap.sh`. apptainer resolves those against its CWD,
    # not against the .def's directory. Result: `cannot stat
    # 'entrypoint_wrap.sh': No such file or directory` mid-build.
    # Fix: cd into the plugin dir so %files paths resolve correctly.
    # Task #287: scrub host env vars that apptainer would interpret as
    # additional bind paths. APPTAINER_BIND / APPTAINER_BINDPATH /
    # SINGULARITY_BIND in the user's shell rc would otherwise apply to
    # the build sandbox, letting random host paths leak into the image.
    # Also strip LD_PRELOAD / PYTHONPATH / etc. for safety.
    _scrubbed_env = {
        k: v for k, v in os.environ.items()
        if k not in {
            "APPTAINER_BIND", "APPTAINER_BINDPATH",
            "SINGULARITY_BIND", "SINGULARITY_BINDPATH",
            "APPTAINER_CONTAIN", "SINGULARITY_CONTAIN",
            "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH",
            "PYTHONHOME", "NODE_PATH", "NODE_OPTIONS",
        }
    }
    rc = subprocess.call(cmd, cwd=str(p.plugin_dir), env=_scrubbed_env)
    if rc != 0:
        click.secho(
            f"refused: apptainer build failed for {p.name} (rc={rc})",
            fg="red", err=True,
        )
        click.secho(
            "    The REAL error is in the build output ABOVE — scroll up to the\n"
            "    step that stopped; a line starting `FATAL:` names the exact cause.\n"
            "    Otherwise: missing base image, no network on the compute node, or\n"
            "    /tmp full (try APPTAINER_TMPDIR=/scratch/$USER).",
            fg="cyan", err=True,
        )
        sys.exit(rc)
    # Record .sif sha256 + path. The format `apptainer:<sha256>:<path>`
    # disambiguates this entry from docker image IDs (which start sha256:).
    digest = _sha256_file(sif_path)
    marker = f"apptainer:sha256:{digest}:{sif_path}"
    _record_image_digest(p.name, marker)
    click.secho(
        f"✓ built {sif_path}\n"
        f"  sha256={digest[:19]}...; recorded in installed.lock\n"
        f"  Reference in .botainer/config.yaml as:\n"
        f"      image: {sif_path}",
        fg="green",
    )


def _sha256_file(path) -> str:
    import hashlib as _hashlib
    h = _hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_image_digest(plugin_name: str, image_id: str) -> None:
    """Update installed.lock with the freshly-built image digest."""
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    lock_path = paths.installed_lock_path
    # AUDIT (MEDIUM): hold the installed.lock flock across the whole
    # read-modify-write so a concurrent append_lock() (e.g. `plugin add`) can't
    # be clobbered by this rewrite (lost update). append_lock takes the same
    # lock; this nests under it via the single guard file.
    with prov_module.lock_for(lock_path):
        _record_image_digest_locked(plugin_name, image_id, lock_path)


def _record_image_digest_locked(plugin_name: str, image_id: str, lock_path) -> None:
    entries = prov_module.read_lock(lock_path)
    new_entries = []
    found = False
    for e in entries:
        if e.name == plugin_name:
            # Replace the entry with updated image_digest.
            new_e = prov_module.ProvenanceEntry(
                name=e.name,
                version=e.version,
                source=e.source,
                tree_sha=e.tree_sha,
                image_digest=image_id,
                installed_at=e.installed_at,
                tier=e.tier,
            )
            new_entries.append(new_e)
            found = True
        else:
            new_entries.append(e)
    if not found:
        # Plugin wasn't recorded (rare; would mean install_bundled didn't
        # add it). Add a minimal entry.
        new_entries.append(prov_module.ProvenanceEntry(
            name=plugin_name,
            version="?",
            source="image-built-locally",
            tree_sha="sha256:0",
            image_digest=image_id,
            installed_at=datetime.now(timezone.utc).isoformat(),
            tier="first-party",
        ))
    # Atomic rewrite as JSONL (matches provenance.read_lock format).
    import json
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for e in new_entries:
        lines.append(json.dumps({
            "name": e.name,
            "version": e.version,
            "source": e.source,
            "tree_sha": e.tree_sha,
            "image_digest": e.image_digest,
            "installed_at": e.installed_at,
            "tier": e.tier,
        }, sort_keys=True))
    # Task #264 + #265: atomic-rename + restrictive perms at create.
    from botainer.state.secure_write import write_secure
    write_secure(lock_path, "\n".join(lines) + "\n", mode=0o644)


def _warn_if_installed_tree_is_stale_vs_source(targets) -> None:
    """For dev / editable installs: warn if the installed plugin tree
    differs from the bundled-source tree.

    The trap: a user edits `plugins/agent-claude/Dockerfile` in their
    clone, runs `botainer image build`, gets the SAME build failure
    they "just fixed". Reason: `image build` reads the INSTALLED
    Dockerfile under `$MY_BOTAINER/plugins/<name>/`, not the source.
    `botainer setup` copies source → installed; without a re-setup
    the edit doesn't propagate.

    This helper compares tree hashes and prints a single warning if
    any installed plugin tree differs from its source. It does NOT
    block — power users may want this divergence (e.g. hand-edited
    Dockerfile for testing).
    """
    from botainer.plugins import builtin as _builtin
    from botainer.plugins import provenance as _prov
    source_root = _builtin.find_builtin_plugins_root()
    if source_root is None:
        return  # production install with no source tree alongside.
    stale: list[str] = []
    for p in targets:
        src = source_root / p.name
        if not src.exists():
            continue
        try:
            src_sha = _prov.compute_tree_sha(src)
            installed_sha = _prov.compute_tree_sha(p.plugin_dir)
        except OSError:
            continue
        if src_sha != installed_sha:
            stale.append(p.name)
    if stale:
        click.secho(
            f"⚠ Installed plugin tree differs from source for: {', '.join(stale)}",
            fg="yellow",
        )
        click.secho(
            "  Source has been edited since the last `botainer setup`. "
            "The image build below uses the INSTALLED Dockerfile, NOT the "
            "edited source. To pick up your changes:",
            fg="yellow",
        )
        click.secho(
            "      botainer setup   # re-copies source → installed",
            fg="cyan",
        )
        click.secho(
            "  Then re-run `botainer image build`. (Continuing the current "
            "build against the installed tree.)",
            fg="yellow",
        )
