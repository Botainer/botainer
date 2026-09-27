#!/usr/bin/env bash
# Build a clean distribution staging directory — the paths listed in
# DIST_PATHS below, nothing else. Personal working material (handoff/,
# design/, strategy/, private/, external/, tools/dev/, tests/manual/,
# v0_0_ref, the .git/ tree) stays out.
#
# Each build goes into a VERSIONED subfolder of dist/ so multiple
# snapshots can coexist (compare, roll back, label experiments):
#
#   dist/0.1.0a1/                       (default: read from pyproject.toml)
#   dist/0.1.0a1-mycluster-test/        (--label override appends to version)
#   dist/explicit-name/                 (--out overrides versioning entirely)
#
# A BUILD_INFO.txt is dropped in the output dir with version, git SHA,
# dirty flag, build timestamp and content-manifest digest. Host paths,
# free-form labels and Git tag names stay in the local terminal output.
#
# Usage:
#   tools/pkg/build-distrib.sh                            # dist/<version>/
#   tools/pkg/build-distrib.sh --label mycluster-1        # dist/<version>-mycluster-1/
#   tools/pkg/build-distrib.sh --out /tmp/staging         # custom path
#
# Then to sync:
#   rsync -av --delete dist/<version>/ <cluster>:~/src/botainer/

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

LABEL=""
EXPLICIT_OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    --out)   EXPLICIT_OUT="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,28p' "$0"
      exit 0
      ;;
    -*)
      echo "unknown flag: $1" >&2
      exit 2
      ;;
    *)
      # Backwards-compat: a bare positional is treated as --out for
      # callers that used the old `build-distrib.sh /path` signature.
      if [[ -z "$EXPLICIT_OUT" ]]; then
        EXPLICIT_OUT="$1"
        shift
      else
        echo "unexpected extra arg: $1" >&2
        exit 2
      fi
      ;;
  esac
done

# Resolve the version from pyproject.toml (used in both the default
# output path AND the BUILD_INFO stamp).
VERSION="$(awk -F'"' '/^version = / {print $2; exit}' "$REPO_ROOT/pyproject.toml")"
if [[ -z "$VERSION" ]]; then
  echo "REFUSED: could not parse version from pyproject.toml" >&2
  exit 3
fi

# Compute the output path.
if [[ -n "$EXPLICIT_OUT" ]]; then
  OUTPUT_DIR="$EXPLICIT_OUT"
elif [[ -n "$LABEL" ]]; then
  OUTPUT_DIR="dist/${VERSION}-${LABEL}"
else
  OUTPUT_DIR="dist/${VERSION}"
fi
# Normalize to absolute.
case "$OUTPUT_DIR" in
  /*) ;;
  *)  OUTPUT_DIR="$REPO_ROOT/$OUTPUT_DIR" ;;
esac

# Task #221: the 'wipe the output dir' step has destroyed real trees.
# Multi-layer safety BEFORE the destructive find:
#   1) refuse a literally-empty path
#   2) refuse paths that resolve to the repo root, $HOME, /, /tmp, etc.
#   3) require a sentinel marker file (.botainer-build-staging) inside
#      the dir unless the dir is fresh-empty; refuses any pre-existing
#      dir that wasn't created by us
SAFETY_OUTPUT="$(readlink -f -- "$OUTPUT_DIR" 2>/dev/null || echo "$OUTPUT_DIR")"
REPO_ROOT_RESOLVED="$(readlink -f -- "$REPO_ROOT" 2>/dev/null || echo "$REPO_ROOT")"
HOME_RESOLVED="$(readlink -f -- "$HOME" 2>/dev/null || echo "$HOME")"
FORBIDDEN=(
  ""  # literally empty
  "/" "/home" "/Users" "/root" "/etc" "/var" "/usr" "/bin" "/opt"
  "/tmp" "/workspace"
  "$REPO_ROOT_RESOLVED" "$HOME_RESOLVED"
)
for bad in "${FORBIDDEN[@]}"; do
  if [[ "$SAFETY_OUTPUT" == "$bad" ]]; then
    echo "REFUSED: --out resolves to forbidden path: $SAFETY_OUTPUT" >&2
    echo "         (would have wiped its contents)" >&2
    exit 4
  fi
done

mkdir -p "$OUTPUT_DIR"
# Sentinel check: if dir is non-empty AND lacks our sentinel, refuse.
if [[ -n "$(ls -A "$OUTPUT_DIR" 2>/dev/null)" ]]; then
  if [[ ! -f "$OUTPUT_DIR/.botainer-build-staging" ]]; then
    echo "REFUSED: $OUTPUT_DIR exists and is non-empty but has no" >&2
    echo "         .botainer-build-staging sentinel. Refusing to wipe" >&2
    echo "         contents we did not create." >&2
    echo "         If you really want to use this dir, run:" >&2
    echo "           touch $OUTPUT_DIR/.botainer-build-staging" >&2
    echo "         and re-run; or use --out <fresh-dir>." >&2
    exit 4
  fi
  # Sentinel present — safe to wipe (still scope-limited to OUTPUT_DIR).
  find "${OUTPUT_DIR:?}" -mindepth 1 -maxdepth 1 \
    ! -name ".botainer-build-staging" -exec rm -rf {} +
fi
# Drop the sentinel so the next invocation sees it.
touch "$OUTPUT_DIR/.botainer-build-staging"

# Git provenance for the BUILD_INFO stamp.
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --verify HEAD 2>/dev/null || echo unknown)"
GIT_DESCRIBE="$(git -C "$REPO_ROOT" describe --always --dirty --tags 2>/dev/null || echo unknown)"
# NOT `git status | head -1 | grep -q .`: head exits after one line, git dies of
# SIGPIPE, and under pipefail that reported a DIRTY tree as CLEAN — a false
# all-clear stamped into a release artefact (row 177, the worst direction of it).
_GIT_PORCELAIN="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || true)"
if [[ -n "$_GIT_PORCELAIN" ]]; then GIT_DIRTY=yes; else GIT_DIRTY=no; fi
BUILD_TIMESTAMP="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

echo "── building distribution staging ───────────────────────────────"
echo "  source:  $REPO_ROOT"
echo "  output:  $OUTPUT_DIR"
echo "  version: $VERSION"
echo "  git:     $GIT_DESCRIBE${GIT_DIRTY:+ (DIRTY)}"
echo ""

# ── Directories included verbatim ─────────────────────────────────
# AUTHORITATIVE LIST — this IS the packaging decision, not a copy of one
# made elsewhere. The repo-layout table that used to hold it is not
# distributed, so a future editor of this file has nothing to consult.
# Adding a top-level directory? Decide HERE whether it ships, and say why.
DIST_DIRS=(
  "botainer"
  "plugins"
  "cluster_profiles"
  "examples"
  "docs"
  "bin"
  "licenses"    # third-party license texts (MPL-2.0 for the vendored noVNC)
)
# SUPPLY-CHAIN / dist audit 2026-07-27: `cp -a docs` pulls the WHOLE tree,
# including the internal Claude-session process docs that pyproject.toml
# deliberately excludes from the sdist. Verified: dist/0.1.0a1/ shipped
# AGENTS.md, docs/PROTOCOLS/ and docs/REVIEW-PROTOCOL.md — and the leak check
# below passed, because it only tested top-level names. Pruned after the copy.
DIST_PRUNE=(
  "docs/PROTOCOLS"
  "docs/REVIEW-PROTOCOL.md"
  "docs/TESTING-0.1.0.md"          # dev runbook (parallel bot1 install)
  "docs/HPC-IMPLEMENTATION-PLAN.md"  # internal work plan, not user docs
)
for d in "${DIST_DIRS[@]}"; do
  if [[ -d "$d" ]]; then
    cp -a "$d" "$OUTPUT_DIR/"
    echo "  ✓ $d/"
  else
    echo "  · $d/ (skipped — not present)"
  fi
done
for _p in "${DIST_PRUNE[@]}"; do
  if [[ -e "$OUTPUT_DIR/$_p" ]]; then
    rm -rf "${OUTPUT_DIR:?}/$_p"
    echo "  ✓ pruned $_p (internal process doc)"
  fi
done

# ── tools/ — only the pkg/ subdir (dev/ is personal) ──────────────
mkdir -p "$OUTPUT_DIR/tools"
if [[ -d "tools/pkg" ]]; then
  cp -a "tools/pkg" "$OUTPUT_DIR/tools/"
  echo "  ✓ tools/pkg/  (tools/dev/ deliberately excluded)"
fi

# ── tests/ — unit + integration + hostile only (manual = personal) ──
mkdir -p "$OUTPUT_DIR/tests"
for sub in unit integration hostile; do
  if [[ -d "tests/$sub" ]]; then
    cp -a "tests/$sub" "$OUTPUT_DIR/tests/"
    echo "  ✓ tests/$sub/"
  fi
done
for f in tests/__init__.py tests/conftest.py; do
  if [[ -f "$f" ]]; then
    cp "$f" "$OUTPUT_DIR/tests/"
    echo "  ✓ $f"
  fi
done

# ── Top-level files ────────────────────────────────────────────────
TOP_FILES=(
  "pyproject.toml"
  "README.md"
  "LICENSE"
  "NOTICE"
  "SECURITY.md"
  "CHANGELOG.md"
  "CONTRIBUTING.md"
  "CLA.md"
  "THIRD-PARTY-LICENSES.md"
  "DEPLOY.md"
  "GETTING_STARTED.md"
  "GETTING_STARTED-HPC.md"
  "TROUBLESHOOTING.md"
  "environment.yml"
)
for f in "${TOP_FILES[@]}"; do
  if [[ -f "$f" ]]; then
    cp "$f" "$OUTPUT_DIR/"
    echo "  ✓ $f"
  else
    echo "  · $f (skipped — not present)"
  fi
done

# Remove generated Python caches before measuring the distributed bytes.
find "$OUTPUT_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$OUTPUT_DIR" -name '*.pyc' -delete 2>/dev/null || true
find "$OUTPUT_DIR" -name '.pytest_cache' -type d -exec rm -rf {} + 2>/dev/null || true

# Bind provenance to the actual staged files, including uncommitted content.
# Only relative paths and content metadata enter the public manifest.
MANIFEST_SHA="$(python3 - "$OUTPUT_DIR" <<'PYMANIFEST'
import hashlib
import json
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
manifest = {}
for path in sorted(root.rglob("*")):
    mode = path.lstat().st_mode
    if stat.S_ISDIR(mode):
        continue
    if not stat.S_ISREG(mode):
        raise SystemExit("REFUSED: distribution contains a non-regular file")
    rel = path.relative_to(root).as_posix()
    if rel in {"BUILD_INFO.txt", "BUILD_MANIFEST.json", ".botainer-build-staging"}:
        continue
    blob = path.read_bytes()
    manifest[rel] = {"sha256": hashlib.sha256(blob).hexdigest(),
                     "bytes": len(blob), "executable": bool(mode & 0o111)}
blob = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
(root / "BUILD_MANIFEST.json").write_bytes(blob)
print(hashlib.sha256(blob).hexdigest())
PYMANIFEST
)"
cat > "$OUTPUT_DIR/BUILD_INFO.txt" <<EOF
botainer distribution staging
version          = $VERSION
git_sha          = $GIT_SHA
git_dirty        = $GIT_DIRTY
built_at         = $BUILD_TIMESTAMP
manifest_sha256  = $MANIFEST_SHA

BUILD_MANIFEST.json records relative paths, hashes, sizes and executable flags
for the staged payload. Compare those records with the deployed files.
The manifest excludes itself, this stamp and the staging marker.
If git_dirty=yes, the source commit alone does not identify the staged bytes.
EOF
echo "  ✓ BUILD_INFO.txt and BUILD_MANIFEST.json"

# ── Sanity: nothing personal made it through ───────────────────────
echo ""
echo "── leak check ──────────────────────────────────────────────────"
LEAKS=0
# MUST stay a superset of pyproject.toml's [tool.hatch.build.targets.sdist]
# exclude list — the two release paths (wheel/sdist vs this script) previously
# disagreed and this one shipped what the other excluded.
# tests/unit/test_dist_excludes_agree.py asserts they do not drift apart.
for forbidden in handoff design strategy private external v0_0_ref tools/dev \
                 tests/manual tests/private CLAUDE.md AGENTS.md docs/PROTOCOLS \
                 docs/REVIEW-PROTOCOL.md docs/TESTING-0.1.0.md \
                 docs/HPC-IMPLEMENTATION-PLAN.md; do
  if [[ -e "$OUTPUT_DIR/$forbidden" ]]; then
    echo "  ✗ LEAK: $OUTPUT_DIR/$forbidden should not be in the distribution"
    LEAKS=$((LEAKS + 1))
  fi
done
if [[ $LEAKS -gt 0 ]]; then
  echo ""
  echo "REFUSED: $LEAKS personal path(s) leaked into the distribution."
  echo "  This is a bug in tools/pkg/build-distrib.sh — fix the copy logic."
  exit 1
fi
echo "  ✓ no personal paths in the distribution staging"

# ── Summary ────────────────────────────────────────────────────────
echo ""
echo "── done ────────────────────────────────────────────────────────"
echo "  size: $(du -sh "$OUTPUT_DIR" | cut -f1)"
echo "  file count: $(find "$OUTPUT_DIR" -type f | wc -l | tr -d ' ')"
echo ""
echo "Next:"
echo "  rsync -av --delete $OUTPUT_DIR/ <user>@<host>:~/src/botainer-v0_1/"
echo ""
echo "  # or, on the HPC side, after rsync:"
echo "  cd ~/src/botainer-v0_1 && bash tools/pkg/install-hpc.sh"
