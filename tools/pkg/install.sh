#!/usr/bin/env bash
# Install botainer v0.1.0 to a non-colliding location.
#
# This script is designed so v0.0.x users can install v0.1.0 alongside
# their existing setup without anything breaking. Paths:
#
#   ~/opt/botainer-v0_1/.venv/     v0.1.0 isolated venv
#   ~/opt/botainer-v0_1/bin/       symlink to the v0.1.0 botainer binary
#   ~/.botainer-v0_1/              v0.1.0 state dir (via MY_BOTAINER env var)
#
# v0.0.x at ~/.botainer/ and any v0.0.x `botainer` binary on PATH are
# untouched.
#
# Usage:
#   bash tools/pkg/install.sh [--source <repo-url-or-path>] [--target <dir>]
#
# Default --source is the cwd (assumes you cloned this repo). Default --target
# is ~/opt/botainer-v0_1.

set -euo pipefail

SOURCE="${PWD}"
TARGET="${HOME}/opt/botainer-v0_1"
STATE_DIR="${HOME}/.botainer-v0_1"
ALIAS_NAME="bot1"

usage() {
  cat <<'EOF'
botainer v0.1.0 installer (non-colliding)

  --source PATH    Path to botainer source tree (default: cwd)
  --target DIR     Install prefix (default: ~/opt/botainer-v0_1)
  --state-dir DIR  State directory (default: ~/.botainer-v0_1)
  --alias NAME     Shell alias for the v0.1.0 binary (default: bot1)
  -h, --help       Show this message

After install, add to your shell rc:
  alias bot1='MY_BOTAINER=~/.botainer-v0_1 ~/opt/botainer-v0_1/bin/botainer'

Then:
  bot1 --version       (should print 0.1.0a1)
  bot1 setup           (writes ~/.botainer-v0_1/policy.yaml)
  cd /path/to/project
  bot1 init --agent claude
  bot1 inspect
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --alias) ALIAS_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

echo ">> botainer v0.1.0 installer"
echo "   source:    $SOURCE"
echo "   target:    $TARGET"
echo "   state-dir: $STATE_DIR"
echo "   alias:     $ALIAS_NAME"
echo

# Safety check: refuse to install on top of a v0.0.x layout.
if [[ "$TARGET" == "$HOME/.botainer" ]] || [[ "$TARGET" == "$HOME/opt/botainer" ]]; then
  echo "ERROR: target $TARGET conflicts with v0.0.x conventions; refusing." >&2
  echo "Use a v1-suffixed path like ~/opt/botainer-v0_1 (the default)." >&2
  exit 3
fi
if [[ "$STATE_DIR" == "$HOME/.botainer" ]]; then
  echo "ERROR: state-dir $STATE_DIR is v0.0.x's; refusing." >&2
  echo "Use a v1-suffixed path like ~/.botainer-v0_1 (the default)." >&2
  exit 3
fi

# Verify Python 3.10+ is available.
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: no python3 found" >&2
  exit 4
fi
# Task #222: lexicographic compare with [[ ]] makes "3.10" < "3.9" TRUE
# (string compare, not numeric). Ask Python itself for the comparison
# since we already have it on PATH.
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 4)'; then
  PY_VER="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  echo "ERROR: python $PY_VER < 3.10; please install python 3.10+" >&2
  exit 4
fi
PY_VER="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo ">> using python $PY ($PY_VER)"

# Create venv.
mkdir -p "$TARGET/bin"
"$PY" -m venv "$TARGET/.venv"
# shellcheck source=/dev/null
source "$TARGET/.venv/bin/activate"
pip install --upgrade pip >/dev/null

# Install botainer + dev deps from source.
# Task #224: per-user tmp file to avoid symlink-TOCTOU on shared
# HPC. /tmp/botainer-install.log is world-writable target; an
# attacker could pre-create a symlink to clobber another user's
# files. Use mktemp under $TMPDIR (or $TARGET) for safety.
INSTALL_LOG=$(mktemp -t "botainer-install.XXXXXX.log") || INSTALL_LOG="$TARGET/install.log"
pip install "$SOURCE" >"$INSTALL_LOG" 2>&1 || {
  echo "ERROR: pip install failed; see $INSTALL_LOG" >&2
  exit 5
}

# Symlink the venv binary.
ln -sf "$TARGET/.venv/bin/botainer" "$TARGET/bin/botainer"

# State dir.
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

echo
echo ">> installed at $TARGET"
echo ">> state dir   $STATE_DIR (mode 0700)"
echo
echo ">> Add this to your shell rc (e.g., ~/.bashrc, ~/.zshrc):"
echo
echo "    alias $ALIAS_NAME='MY_BOTAINER=$STATE_DIR $TARGET/bin/botainer'"
echo
echo ">> Then:"
echo "    $ALIAS_NAME --version          # should print 0.1.0a1"
echo "    $ALIAS_NAME setup              # writes $STATE_DIR/policy.yaml"
echo "    cd /path/to/project"
echo "    $ALIAS_NAME init --agent claude"
echo "    $ALIAS_NAME inspect            # see what would be mounted"
echo "    $ALIAS_NAME plugin agent-claude login   # OAuth (requires Claude CLI on PATH)"
echo "    $ALIAS_NAME start"
echo
echo ">> Verify v0.0.x is untouched:"
echo "    which botainer                  # should still show v0.0.x path on PATH"
echo "    ls $HOME/.botainer/              # should still show v0.0.x state (if present)"
