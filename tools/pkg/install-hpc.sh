#!/usr/bin/env bash
# Install botainer v0.1.x on an HPC cluster — designed for PARALLEL
# coexistence with a v0.0.x install.
#
# Parallel-coexistence guarantees:
#   - Installs into a separate venv (default: ~/opt/botainer-v0_1/.venv).
#   - Uses a separate state dir (default: ~/.botainer-v0_1/) so v0.0.x's
#     ~/.botainer/ is untouched.
#   - REFUSES to install if --state-dir is ~/.botainer (collision).
#   - Optional: writes an alias into your shell rc so you can call the
#     parallel install by a short name without colliding with v0.0.x's
#     `botainer` on PATH.
#
# Differences from the laptop install:
#   - No Docker assumption; uses apptainer instead.
#   - Skips the agent image build (do that separately with
#     `<alias> image build agent-claude --runtime apptainer`).
#
# Usage examples:
#   ./install-hpc.sh                                            # bare install
#   ./install-hpc.sh --editable                                 # pip install -e (for dev work)
#   ./install-hpc.sh --alias botainer-v0_1                        # write alias to shell rc
#   ./install-hpc.sh --editable --alias botainer-v0_1             # both
#   ./install-hpc.sh --state-dir /scratch/$USER/.botainer-v0_1    # state on scratch FS
#
# Pick any --alias name you like; `botainer-v0_1` mirrors the install
# layout. The alias name only matters in your shell; it isn't recorded
# anywhere persistent in the v0.1.x install.

set -euo pipefail

SOURCE="${PWD}"
TARGET="${HOME}/opt/botainer-v0_1"
STATE_DIR="${HOME}/.botainer-v0_1"
EDITABLE=0
ALIAS_NAME=""
WRITE_RC=0
SKIP_SETUP=0
SKIP_IMAGE_BUILD=0
IMAGE_AGENT="agent-claude"

usage() {
  cat <<'EOF'
botainer v0.1.x HPC installer (parallel-coexistence)

  --source PATH         Path to botainer source (default: cwd)
  --target DIR          Install prefix (default: ~/opt/botainer-v0_1)
  --state-dir DIR       State directory (default: ~/.botainer-v0_1)
                        Refused if equal to ~/.botainer (v0.0.x's).
  --editable            pip install -e (source edits propagate live).
                        Recommended if you're going to develop on Grace.
  --alias NAME          Write `alias NAME='...'` + `export MY_BOTAINER='...'`
                        into your shell rc so you can invoke the parallel
                        install by short name. Example: botainer-v0_1.
                        Pick whatever's natural for you (not recorded
                        anywhere in the v0.1.x install itself).
  --skip-setup          Don't run `botainer setup` after install. Default
                        behavior installs bundled plugins automatically.
  --skip-image-build    Don't build the agent apptainer image after install.
                        Default behavior runs `botainer image build agent-claude
                        --runtime apptainer` (takes 10-20 min the first time;
                        run on a login node OR inside `salloc` if your cluster
                        forbids heavy login-node work).
  --image-agent NAME    Which agent's image to build (default: agent-claude).
                        Use a comma-separated list for multiple: agent-claude,agent-codex.
                        Note: agent-codex is built from
                        plugins/agent-codex/agent-codex.def.
  -h, --help            Show this message

After install:
  1. (Optional) source ~/.bashrc to pick up the alias.
  2. Build the agent image (one-time, ~10-20 min):
       <alias> image build agent-claude --runtime apptainer
  3. Set up a project:
       cd /path/to/project
       <alias> init --agent claude --runtime apptainer
       <alias> auth login --shared --agent claude  # (or copy creds from a docker host)
  4. Submit:
       <alias> hpc setup --cluster=generic-slurm  # or a bundled profile
       <alias> hpc submit --dry-run        # inspect first
       <alias> hpc submit --yes            # real submission
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --editable) EDITABLE=1; shift ;;
    --alias) ALIAS_NAME="$2"; WRITE_RC=1; shift 2 ;;
    --skip-setup) SKIP_SETUP=1; shift ;;
    --skip-image-build) SKIP_IMAGE_BUILD=1; shift ;;
    --image-agent) IMAGE_AGENT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

echo ">> botainer v0.1.x HPC installer"
echo "   source:      $SOURCE"
echo "   target:      $TARGET"
echo "   state-dir:   $STATE_DIR"
echo "   install:     $( [[ $EDITABLE -eq 1 ]] && echo 'editable (pip -e)' || echo 'regular' )"
echo "   alias name:  $( [[ -n "$ALIAS_NAME" ]] && echo "$ALIAS_NAME (will write to shell rc)" || echo '(none — use full path)' )"
echo

# Refuse to clobber v0.0.x's state dir.
if [[ "$STATE_DIR" == "$HOME/.botainer" ]]; then
  echo "REFUSED: state-dir $STATE_DIR is v0.0.x's; refusing to clobber." >&2
  echo "  Pick a different --state-dir (default is ~/.botainer-v0_1)." >&2
  exit 3
fi

# Confirm source is a botainer clone.
if [[ ! -f "$SOURCE/pyproject.toml" ]]; then
  echo "REFUSED: $SOURCE doesn't look like a botainer source tree (no pyproject.toml)." >&2
  echo "  Pass --source <path-to-clone>." >&2
  exit 4
fi

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: no python3 found." >&2
  echo "  Module name varies by cluster — try 'module spider python' to" >&2
  echo "  find what your cluster calls it (Yale Grace uses 'Python/3.11')." >&2
  echo "  Once loaded, re-run install-hpc.sh." >&2
  exit 5
fi

# Pre-check apptainer presence BEFORE doing anything else. The image
# build step at the END of this script will refuse if apptainer is
# missing — bailing out NOW saves the user from running the cheap
# parts (venv, pip install, setup, alias) and then discovering the
# image didn't build at the bottom of a wall of preflight output.
#
# That confusion bit us on 2026-05-19; the script reported "WARNING"
# at the end and the user reasonably believed the install had
# succeeded. Refusing upfront is the fix.
if [[ "$SKIP_IMAGE_BUILD" -ne 1 ]]; then
  if ! command -v apptainer >/dev/null 2>&1 \
     && ! command -v singularity >/dev/null 2>&1; then
    # Detect: are we already on a compute node (in an allocation)?
    if [[ -n "${SLURM_JOB_ID:-}" ]]; then
      # In a Slurm allocation and apptainer STILL missing — this cluster's
      # setup is unusual and we don't know how to help. Genuine refuse.
      echo "REFUSED: apptainer/singularity not on PATH even though we" >&2
      echo "  appear to be inside a Slurm allocation (SLURM_JOB_ID=$SLURM_JOB_ID)." >&2
      echo "  Either this cluster requires 'module load apptainer' even on" >&2
      echo "  compute nodes (try 'module spider apptainer'), or apptainer is" >&2
      echo "  not installed on this site at all. Resolve, then re-run." >&2
      exit 7
    fi
    # LOGIN NODE + no apptainer (the common case on Yale Grace + many others
    # where apptainer is compute-node-only). Cluster-ease roadmap B6
    # (2026-06-13): the cheap parts of this script (venv, pip, setup, plugins)
    # work fine on a login node — refusing the WHOLE install because the
    # LAST step needs apptainer makes the first user action fail when most of
    # it could have succeeded. AUTO-DEFER the image build instead and print
    # the next-step prominently. Original behavior was "refuse upfront"
    # because the 2026-05-19 incident was a SILENT failure at the end of the
    # script; the fix is to be LOUD now AND at the end, not to refuse.
    echo "================================================================" >&2
    echo " HPC LOGIN NODE detected (no Slurm allocation, no apptainer on PATH)." >&2
    echo " On clusters like Yale Grace, apptainer is on compute nodes only." >&2
    echo "" >&2
    echo " Auto-deferring the image build: the cheap install steps (venv," >&2
    echo " pip, setup, plugins) will run now; the image build is skipped." >&2
    echo "" >&2
    echo " When this script finishes, build the image from inside salloc:" >&2
    echo "     salloc --mem=32G -c 2 -t 1:00:00        # on THIS (login) node" >&2
    # Full path, not the bare name. This banner prints BEFORE the venv exists
    # and before any shell rc is touched, so `botainer` is not on PATH for the
    # reader yet — telling them to type it hands them a command-not-found. The
    # end-of-script banner already got this right; this one did not.
    echo "     $TARGET/bin/botainer image build agent-claude --runtime apptainer" >&2
    echo "                                              # on the compute node" >&2
    echo "     exit                                     # back to the login node" >&2
    echo "" >&2
    echo " (Or re-run this script inside salloc to do it now: bash $0 $*)" >&2
    echo "================================================================" >&2
    SKIP_IMAGE_BUILD=1
    DEFERRED_IMAGE_BUILD=1
  fi
fi

mkdir -p "$TARGET/bin"
"$PY" -m venv "$TARGET/.venv"
# shellcheck disable=SC1090
source "$TARGET/.venv/bin/activate"
pip install --quiet --upgrade pip
if [[ $EDITABLE -eq 1 ]]; then
  pip install --quiet -e "$SOURCE"
else
  pip install --quiet "$SOURCE"
fi
ln -sf "$TARGET/.venv/bin/botainer" "$TARGET/bin/botainer"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

# Check apptainer presence (warn, don't fail; user may need module load).
if ! command -v apptainer >/dev/null 2>&1 && ! command -v singularity >/dev/null 2>&1; then
  echo "WARNING: neither 'apptainer' nor 'singularity' found on PATH." >&2
  echo "         You may need 'module load apptainer' (or similar) on this cluster." >&2
fi

# Write alias to shell rc if requested.
if [[ $WRITE_RC -eq 1 ]] && [[ -n "$ALIAS_NAME" ]]; then
  # Validate the alias name — alphanumeric + a few harmless chars only.
  if ! [[ "$ALIAS_NAME" =~ ^[A-Za-z][A-Za-z0-9_-]{0,31}$ ]]; then
    echo "REFUSED: --alias name $ALIAS_NAME contains characters that aren't safe for shell. Use [A-Za-z0-9_-]." >&2
    exit 6
  fi
  # Detect shell rc based on $SHELL (the user's LOGIN shell), not
  # whether ~/.zshrc exists. Many users have a stale ~/.zshrc from a
  # one-time experiment but actually run bash — writing the alias to
  # .zshrc in that case makes it invisible from their interactive
  # shell, which bit us on 2026-05-19.
  SHELL_NAME="$(basename "${SHELL:-/bin/bash}")"
  case "$SHELL_NAME" in
    zsh)   RC="$HOME/.zshrc" ;;
    bash)  RC="$HOME/.bashrc" ;;
    *)     RC="$HOME/.${SHELL_NAME}rc" ;;
  esac
  # If the chosen rc doesn't exist, fall back to .bashrc (the most
  # universal default) and warn.
  if [[ ! -f "$RC" ]]; then
    if [[ -f "$HOME/.bashrc" ]]; then
      echo "  (note: $RC doesn't exist; writing to .bashrc instead)" >&2
      RC="$HOME/.bashrc"
    fi
  fi
  MARKER_BEGIN="# >>> botainer v0.1.x parallel install (managed by install-hpc.sh) >>>"
  MARKER_END="# <<< botainer v0.1.x parallel install <<<"
  BLOCK="${MARKER_BEGIN}
export MY_BOTAINER=\"${STATE_DIR}\"
alias ${ALIAS_NAME}='MY_BOTAINER=\"${STATE_DIR}\" \"${TARGET}/bin/botainer\"'
${MARKER_END}"
  if grep -qF "$MARKER_BEGIN" "$RC" 2>/dev/null; then
    # Remove any existing block, then re-write (idempotent).
    awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
      $0==b {skip=1; next}
      $0==e {skip=0; next}
      !skip
    ' "$RC" > "$RC.tmp" && mv "$RC.tmp" "$RC"
  fi
  printf "\n%s\n" "$BLOCK" >> "$RC"
  echo
  echo "  ✓ wrote alias block to $RC (marker: $MARKER_BEGIN)"
fi

echo
echo ">> installed at $TARGET"
echo ">> state dir   $STATE_DIR"
if [[ -n "$ALIAS_NAME" ]]; then
  echo ">> alias       $ALIAS_NAME → MY_BOTAINER=$STATE_DIR $TARGET/bin/botainer"
  ALIAS_PREFIX="$ALIAS_NAME"
else
  echo ">> no alias written. Invoke as: $TARGET/bin/botainer"
fi

# Direct binary invocation (no shell alias needed for THIS script; the
# alias is for the user's interactive shell post-install).
# Task #220: was a string + eval — any backtick / semicolon / $() in
# $IMAGE_AGENT would execute as the user. Now an array; invoke via
# env+exec, never via eval.
BIN_EXEC=( "$TARGET/bin/botainer" )
BIN_ENV=( "MY_BOTAINER=$STATE_DIR" )

# ── Auto-run `botainer setup` (installs bundled plugins) ───────────
if [[ "$SKIP_SETUP" -eq 1 ]]; then
  echo ">> --skip-setup: NOT running 'botainer setup'."
  echo "   Run it yourself before submitting: $TARGET/bin/botainer setup"
else
  echo
  echo "── running 'botainer setup' (installs bundled plugins) ────────"
  env "${BIN_ENV[@]}" "${BIN_EXEC[@]}" setup || {
    echo "WARNING: 'botainer setup' exited non-zero. Inspect the output above." >&2
    echo "  You can re-run it manually: $TARGET/bin/botainer setup" >&2
  }
fi

# ── Auto-build the agent apptainer image ───────────────────────────
if [[ "$SKIP_IMAGE_BUILD" -eq 1 ]]; then
  if [[ "${DEFERRED_IMAGE_BUILD:-0}" -eq 1 ]]; then
    echo
    echo "================================================================"
    echo " >> IMAGE BUILD AUTO-DEFERRED (login node, no apptainer)."
    echo "    Setup, plugins, and venv are installed — the image is not yet built."
    echo "    Next step (build the image from inside a Slurm allocation):"
    echo
    echo "        salloc --mem=32G -c 2 -t 1:00:00"
    echo "        $TARGET/bin/botainer image build agent-claude --runtime apptainer"
    echo "        exit"
    echo "================================================================"
  else
    echo ">> --skip-image-build: NOT building agent images."
    echo "   Build later: $TARGET/bin/botainer image build agent-claude --runtime apptainer"
  fi
else
  echo
  echo "── building agent apptainer image(s): $IMAGE_AGENT ────────────"
  echo "   This takes 10-20 min the first time. To skip: --skip-image-build"
  echo
  # Iterate over comma-separated agents.
  IFS=',' read -ra AGENTS <<< "$IMAGE_AGENT"
  for a in "${AGENTS[@]}"; do
    a="${a# }"; a="${a% }"   # trim spaces
    [[ -z "$a" ]] && continue
    DEF_PATH="$SOURCE/plugins/$a/$a.def"
    if [[ ! -f "$DEF_PATH" ]]; then
      echo "  ✗ skipping $a: no .def file at $DEF_PATH"
      echo "    (author one at $DEF_PATH, or drop $a from --image-agent)"
      continue
    fi
    echo "  Building $a..."
    env "${BIN_ENV[@]}" "${BIN_EXEC[@]}" image build "$a" --runtime apptainer || {
      echo "WARNING: image build failed for $a. Inspect the output above." >&2
    }
  done
fi

if [[ -n "$ALIAS_NAME" ]]; then
  echo
  echo ">> Activate the alias in THIS shell: source $RC"
  ALIAS_PREFIX="$ALIAS_NAME"
else
  ALIAS_PREFIX="$TARGET/bin/botainer"
fi

echo
echo ">> Next steps (see docs/HPC-WORKFLOW.md for the full walkthrough):"
echo "    $ALIAS_PREFIX doctor                                   # verify env"
echo "    $ALIAS_PREFIX hpc setup --cluster=generic-slurm      # or a bundled profile"
echo "    cd /path/to/project"
echo "    $ALIAS_PREFIX init --agent claude --runtime apptainer"
echo "    $ALIAS_PREFIX hpc build agent-claude                   # build the .sif (needed before login)"
echo "    $ALIAS_PREFIX auth login --shared --agent claude       # OAuth (runs in the .sif via apptainer; NO docker needed)"
echo "    $ALIAS_PREFIX hpc submit --dry-run                     # inspect the sbatch script"
echo "    $ALIAS_PREFIX hpc submit --yes                         # actually submit"
