"""Plugin install from file:// URL, git URL, or tarball.

For v0.1.0 prototype: supports file:// (local plugin tree) and tarball
(.tar.gz). Git URL support is stubbed (would require git binary + isolated
clone — host-only).

Hardened per sharp-edges + insecure-defaults review:
- Symlinks in local plugin trees are pre-scanned and refused if they escape
  the tree (sharp-edges #2).
- Tarballs use `filter="data"` (or refuse non-file/non-dir members on
  older Python) and the mode bits are stripped to 0o777 (sharp-edges #1).
- Staging dirs use `tempfile.mkdtemp` (sharp-edges #3) — no hash-based names.
- `_source_is_first_party` ALWAYS returns False (audit S1): first-party
  is a property of the install MECHANISM (`builtin.install_bundled`), never of a
  user-supplied source string. URL-scheme sources are refused, not path-coerced.
- First-party tier requires *both* the origin allowlist match *and* the
  plugin name appearing in `policy.plugins.first_party_allowlist`
  (insecure-defaults M).
"""
from __future__ import annotations

import re
import shutil
import tarfile
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from botainer.core.policy import intersect, load_site_policy, load_user_policy
from botainer.core.refusal import RefusalCategory, Refused
from botainer.plugins import provenance
from botainer.plugins.manifest import (
    check_reserved_name,
    load_manifest,
)
from botainer.state import dir as state_dir

# A source carrying ANY scheme (https:, git+ssh:, file:, ftp:, …) must never be
# reinterpreted as a local filesystem path — see `_source_is_first_party`'s note
# on the path-confusion class this closes (audit, S1).
_RE_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


@dataclass(frozen=True)
class InstalledPluginInfo:
    name: str
    version: str
    tree_sha: str
    tier: str
    source: str


class PluginInstallError(Exception):
    def __init__(self, category: RefusalCategory, message: str) -> None:
        self.category = category
        super().__init__(message)


def install(source: str, *, consent: bool) -> InstalledPluginInfo:
    """Install a plugin from `source`. Returns provenance info."""
    paths = state_dir.ensure_user_state_dir(create_if_missing=True)
    target_root = paths.plugins_dir

    extracted: Path
    if source.startswith("file://"):
        local = Path(urllib.parse.urlparse(source).path)
        extracted = _stage_local(local, target_root)
    elif source.endswith((".tar.gz", ".tgz")):
        extracted = _stage_tarball(Path(source), target_root)
    elif source.startswith(("git+", "git@", "git://")) or (
        source.startswith("https://") and source.endswith(".git")
    ):
        raise PluginInstallError(
            RefusalCategory.PLUGIN_MANIFEST_INVALID,
            "git URL plugin install is not implemented in the prototype; use file:// or .tar.gz",
        )
    else:
        # SECURITY (audit, S1): a source carrying a URL SCHEME must
        # never fall through to `Path(source)`. `Path("https://github.com/x")` is
        # the relative path `https:/github.com/x`, so a directory of that shape —
        # which the caged agent can create inside /workspace — was silently
        # installed as if it were the URL. Refuse the whole scheme-shaped class
        # rather than trying to spot the confusing ones (structure, not a filter).
        if _RE_URL_SCHEME.match(source):
            raise PluginInstallError(
                RefusalCategory.PLUGIN_MANIFEST_INVALID,
                f"source {source!r} looks like a URL, but network plugin install "
                f"is not implemented. Refusing to interpret it as a local path "
                f"(a directory named like a URL is not that URL). Use a real "
                f"local path or a .tar.gz.",
            )
        local = Path(source)
        if not local.exists():
            raise PluginInstallError(
                RefusalCategory.PLUGIN_MANIFEST_INVALID,
                f"source {source!r} not found",
            )
        extracted = _stage_local(local, target_root)

    try:
        manifest = load_manifest(extracted)
    except Refused as exc:
        shutil.rmtree(extracted, ignore_errors=True)
        raise PluginInstallError(exc.category, str(exc)) from exc

    source_is_first_party = _source_is_first_party(source)
    try:
        check_reserved_name(manifest.name, source_is_first_party=source_is_first_party)
    except Refused as exc:
        shutil.rmtree(extracted, ignore_errors=True)
        raise PluginInstallError(exc.category, str(exc)) from exc

    # AUDIT (MEDIUM): use the EFFECTIVE policy (site ∩ user), not
    # user-only — otherwise a site policy restricting allowed_tiers /
    # first_party_allowlist (the exact multi-tenant use case, DN-027 §9) is
    # ignored at install time.
    policy = intersect(load_site_policy(), load_user_policy())

    # AUDIT (MEDIUM): tier is LAUNCHER-determined, NOT manifest-
    # declared (DN-027 §9). The old code gated on manifest.tier (self-
    # declared) throughout: a plugin self-declaring `community-verified` (a tier
    # that does not exist at v0.1 — signing infra deferred) skipped the hooked-
    # plugin consent gate whenever a user widened allowed_tiers. Compute the
    # effective tier from the SOURCE: first-party iff the source is in the
    # first-party origin allowlist (and, when a name allowlist is set, the name
    # is in it); everything else is third-party regardless of self-declaration.
    if source_is_first_party and (
        not policy.plugins.first_party_allowlist
        or manifest.name in policy.plugins.first_party_allowlist
    ):
        effective_tier = "first-party"
    else:
        effective_tier = "third-party"

    # A plugin that SELF-DECLARES first-party from a non-first-party source is a
    # red flag — refuse explicitly (don't silently downgrade it to third-party).
    if manifest.tier == "first-party" and effective_tier != "first-party":
        shutil.rmtree(extracted, ignore_errors=True)
        raise PluginInstallError(
            RefusalCategory.PLUGIN_TIER_NOT_ALLOWED,
            f"plugin {manifest.name!r} declares tier=first-party but its source "
            f"{source!r} is not in the first-party origin list "
            f"(or the name is not in the site first_party_allowlist).",
        )

    if effective_tier not in policy.plugins.allowed_tiers:
        shutil.rmtree(extracted, ignore_errors=True)
        raise PluginInstallError(
            RefusalCategory.PLUGIN_TIER_NOT_ALLOWED,
            f"plugin {manifest.name!r} resolves to tier {effective_tier!r} "
            f"(launcher-determined from its source), which is not in the "
            f"effective policy allowed_tiers {policy.plugins.allowed_tiers}",
        )

    # Any NON-first-party plugin with hooks → require explicit consent (hooks
    # run as you on the host, no sandbox). Gated on the EFFECTIVE tier so a
    # self-declared higher tier can't skip it.
    if (
        effective_tier != "first-party"
        and policy.plugins.third_party_must_be_declarative
        and manifest.hooks
        and not consent
    ):
        shutil.rmtree(extracted, ignore_errors=True)
        raise PluginInstallError(
            RefusalCategory.PLUGIN_DECLARATIVE_REQUIRED,
            f"plugin {manifest.name!r} ({effective_tier}) declares hooks but "
            f"non-first-party plugins must be declarative-only by site policy. "
            f"Re-run with --yes to consent (hooks run as you on the host with "
            f"no sandbox).",
        )

    final = target_root / manifest.name
    if final.exists():
        shutil.rmtree(final)
    extracted.rename(final)

    tree_sha = provenance.compute_tree_sha(final)
    entry = provenance.ProvenanceEntry(
        name=manifest.name,
        version=manifest.version,
        source=source,
        tree_sha=tree_sha,
        image_digest=manifest.image.tag.split("@", 1)[1]
        if (manifest.image and manifest.image.tag and "@" in manifest.image.tag)
        else None,
        installed_at=provenance.now_iso(),
        tier=effective_tier,  # AUDIT: record the launcher-determined
        # tier, not the manifest's self-declared one.
    )
    provenance.append_lock(paths.installed_lock_path, entry)
    return InstalledPluginInfo(
        name=manifest.name,
        version=manifest.version,
        tree_sha=tree_sha,
        tier=manifest.tier,
        source=source,
    )


def _source_is_first_party(source: str) -> bool:
    """`install()` can NEVER grant first-party. Always False.

    SECURITY (audit, S1) — this used to be a string prefix test:

        source.startswith("https://github.com/botainer/")

    Two facts made that both useless and dangerous:

    1. **It was 0% functional.** Network install is not implemented — every
       `git+` / `git@` / `git://` / `https://….git` source is refused above. So
       no real first-party install ever flowed through this test.
    2. **It was 100% spoofable.** A source that doesn't match any handled prefix
       falls through to `Path(source)`, and `Path("https://github.com/botainer/x")`
       is the *relative path* `https:/github.com/botainer/x`. Creating a directory
       of that shape — which the CAGED AGENT can do inside /workspace — made this
       return True, which bought: a reserved bundled plugin NAME (so
       `rmtree(final); rename(final)` overwrites e.g. the real `agent-claude`
       already enabled in every project), a skipped self-declaration refusal, and
       a skipped hook-consent gate. The bait is a README saying
       `botainer plugin add https://github.com/botainer/<name>` — which looks
       canonical.

    STRUCTURAL FIX: first-party is a property of the install MECHANISM, not of a
    string. The only first-party mechanism is `builtin.install_bundled`, which
    installs the tree shipped inside the launcher itself. `install_from_source`
    handles user-supplied sources and therefore never grants it. When a real
    network installer lands, IT may grant first-party after verifying a signature
    — the decision must stay attached to the verified fetch, never to the
    user-typed string.
    """
    return False


def _stage_local(src: Path, target_root: Path) -> Path:
    """Copy a local plugin tree to a staging dir.

    Per sharp-edges #2/#3: refuse symlinks that escape the tree, and use
    `tempfile.mkdtemp` instead of a hash-based name.
    """
    target_root.mkdir(parents=True, exist_ok=True)
    if not src.exists() or not src.is_dir():
        raise PluginInstallError(
            RefusalCategory.PLUGIN_MANIFEST_INVALID,
            f"plugin source {src!r} is not an existing directory",
        )
    src_resolved = src.resolve()
    # Pre-scan for unsafe symlinks before copying anything.
    for path in src.rglob("*"):
        if path.is_symlink():
            target = path.resolve()
            try:
                target.relative_to(src_resolved)
            except ValueError:
                raise PluginInstallError(
                    RefusalCategory.PLUGIN_MANIFEST_INVALID,
                    f"plugin tree contains symlink escaping its root: "
                    f"{path} -> {target}",
                ) from None
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=target_root))
    # Remove the empty mkdtemp dir before copytree.
    staging.rmdir()
    shutil.copytree(src, staging, symlinks=False)
    return staging


def _stage_tarball(tar: Path, target_root: Path) -> Path:
    """Extract a tarball to a staging dir.

    Per sharp-edges #1: belt-and-suspenders safety on tarfile extraction.
    """
    target_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=target_root))
    with tarfile.open(tar, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise PluginInstallError(
                    RefusalCategory.PLUGIN_MANIFEST_INVALID,
                    f"tarball contains unsafe path: {m.name!r}",
                )
            if not (m.isfile() or m.isdir()):
                raise PluginInstallError(
                    RefusalCategory.PLUGIN_MANIFEST_INVALID,
                    f"tarball contains non-regular member {m.name!r} (type={m.type!r})",
                )
            # Strip suid/sgid/sticky.
            m.mode = m.mode & 0o0777
            members.append(m)
        try:
            tf.extractall(staging, members=members, filter="data")  # type: ignore[call-arg]
        except TypeError:
            # Python < 3.12: no `filter` kwarg.
            tf.extractall(staging, members=members)
    children = list(staging.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return staging
