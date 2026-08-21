#!/usr/bin/env bash
# Move one of botainer's bulky per-project components off the state root.
#
#   --component scratch    bulk intermediates          BYTE-heavy
#   --component packages   pip/npm/conda installs      INODE-heavy  <-- the sneaky one
#   --component home       the container's HOME cache  both
#
# WHY YOU NEED THIS. `<MY_BOTAINER>/state/<uuid>/` holds three things with three
# different lifetimes on one filesystem, and on a cluster that filesystem is a
# quota-limited $HOME. Two different quotas break here, for different reasons:
#
#   bytes   -> scratch and home fill it.  `du` shows you coming.
#   inodes  -> packages fills it, and `du` shows almost nothing. A conda env is
#              a few GB but hundreds of thousands of files; a 500,000-file home
#              cap dies to one of them. Every session then fails with
#              `[Errno 122] Disk quota exceeded` writing a tiny metadata file.
#
# Moving `scratch` does NOT fix an inode problem — that is the byte-heavy one.
# If you are out of FILES, move `packages`, and probably `home` too.
#
# This script moves the DATA. Pointing botainer at the new location is a config
# change (printed at the end) — the script does not edit your config for you,
# because where each component belongs is a site decision.
#
# SAFE BY CONSTRUCTION:
#   • dry-run unless you pass --apply
#   • copies, verifies the file count, then renames the original aside — it
#     NEVER deletes your data; you remove the `.relocated` copy yourself
#   • refuses if the destination exists and is not empty
#   • moves ONE named component. data/, sessions/, meta.json — your credentials
#     and project identity — are never touched and never move. That is
#     deliberate: relocating the whole state root would put the shared OAuth
#     credential on group-visible space.
#
# Usage:
#   tools/pkg/relocate-storage.sh --component packages --dest /project/$USER/bpkgs
#   tools/pkg/relocate-storage.sh --component packages --dest ... --apply
#   tools/pkg/relocate-storage.sh --component packages --cleanup
#
# --component defaults to `scratch` for compatibility with the older
# the older relocate-scratch.sh, which this replaces.
set -euo pipefail

STATE_ROOT="${MY_BOTAINER:-$HOME/.botainer}"
DEST=""; APPLY=0; CLEANUP=0; ASIDES=""; COMPONENT="scratch"; MOVED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --component) COMPONENT="${2:?--component needs scratch|packages|home}"; shift 2 ;;
    --dest) DEST="${2:?--dest needs a path}"; shift 2 ;;
    --state-root) STATE_ROOT="${2:?}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --cleanup) CLEANUP=1; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
case "$COMPONENT" in
  scratch|packages|home) ;;
  *) echo "ERROR: --component must be scratch, packages or home (got '$COMPONENT')" >&2
     echo "       A typo here would silently find no directories and report" >&2
     echo "       'nothing to move', which reads exactly like success." >&2
     exit 2 ;;
esac

if [ "$CLEANUP" = 1 ]; then
  # Remove the `.relocated` originals this script left behind, after you have
  # confirmed the new location works. Lists them and asks before removing:
  # this is the only destructive path in the script and it is opt-in twice.
  found=0
  for a in "$STATE_ROOT"/state/*/"$COMPONENT".relocated*; do
    [ -e "$a" ] || continue
    found=1
    echo "  $(du -sh "$a" 2>/dev/null | cut -f1)  $a"
  done
  if [ "$found" = 0 ]; then
    echo "Nothing to clean up — no $COMPONENT.relocated directories under $STATE_ROOT/state/."
    exit 0
  fi
  echo
  printf 'Delete all of the above? This cannot be undone. [y/N] '
  read -r ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "Nothing deleted."; exit 0 ;;
  esac
  for a in "$STATE_ROOT"/state/*/"$COMPONENT".relocated*; do
    [ -e "$a" ] || continue
    rm -rf "$a" && echo "removed $a"
  done
  exit 0
fi
[ -n "$DEST" ] || { echo "ERROR: --dest is required (or --cleanup)" >&2; exit 2; }
[ -d "$STATE_ROOT/state" ] || { echo "ERROR: no state dir at $STATE_ROOT/state" >&2; exit 2; }

echo "component  : $COMPONENT"
echo "state root : $STATE_ROOT"
echo "destination: $DEST"
[ "$APPLY" = 1 ] && echo "mode       : APPLY (data will move)" || echo "mode       : DRY RUN (nothing will change)"
echo

total=0
for pdir in "$STATE_ROOT"/state/*/; do
  uuid="$(basename "$pdir")"
  pdir="${pdir%/}"
  src="$pdir/$COMPONENT"
  [ -d "$src" ] || continue
  size="$(du -sk "$src" 2>/dev/null | cut -f1)"; size="${size:-0}"
  human="$(( size / 1024 )) MiB"
  dst="$DEST/$uuid"
  total=$(( total + size ))

  if [ -e "$dst" ] && [ -n "$(ls -A "$dst" 2>/dev/null)" ]; then
    echo "SKIP  $uuid  destination exists and is not empty: $dst" >&2
    continue
  fi
  echo "MOVE  $uuid  $human"
  echo "        from $src"
  echo "        to   $dst"
  if [ "$APPLY" = 1 ]; then
    mkdir -p "$dst"
    # copy-verify-remove, never a bare mv: a partial mv across filesystems on a
    # cluster leaves you with neither copy.
    cp -a "$src/." "$dst/" 2>/dev/null || true
    src_n="$(find "$src" -type f | wc -l)"; dst_n="$(find "$dst" -type f | wc -l)"
    if [ "$src_n" != "$dst_n" ]; then
      echo "        REFUSED: copied $dst_n of $src_n files — original left intact" >&2
      continue
    fi
    # NEVER delete the user's data. The original is renamed aside; you delete
    # it yourself once you have confirmed the move. A migration script that
    # removes data has exactly one chance to be right, and no way to apologise.
    aside="${src}.relocated"
    if [ -e "$aside" ]; then aside="${src}.relocated.$$"; fi
    mv "$src" "$aside"
    echo "        done ($dst_n files verified)"
    echo "        original kept at: $aside"
    MOVED=$(( MOVED + 1 ))
    ASIDES="${ASIDES}${aside}"$'\n' 
  fi
done

echo
echo "total $COMPONENT on the state root: $(( total / 1024 )) MiB"
if [ "$APPLY" != 1 ]; then
  echo
  echo "DRY RUN — nothing changed. Re-run with --apply to move the data."
fi
if [ "$APPLY" = 1 ] && [ "$MOVED" = 0 ]; then
  # Nothing was found to move. Almost always: it has already been done. Saying
  # "WHAT TO DO NEXT ... this script kept every one of them" here would be a
  # lie about a run that did nothing, and the user cannot tell the difference.
  echo
  echo "Nothing to move — no $COMPONENT directory under $STATE_ROOT/state/*/."
  echo "If you already ran this, that is expected. Check the setting took:"
  echo "    botainer inspect | grep /$COMPONENT"
fi
if [ "$APPLY" = 1 ] && [ "$MOVED" != 0 ]; then
  echo
  echo "═══ WHAT TO DO NEXT — three steps, in this order ═══════════════"
  echo
  echo "1. TELL BOTAINER where $COMPONENT lives now. Edit:"
  echo
  echo "       $STATE_ROOT/cluster.yaml"
  echo
  echo "   and set (create the \`$COMPONENT:\` block if it is not there):"
  echo
  echo "       $COMPONENT:"
  echo "         template: $DEST"
  echo
  echo "2. CHECK IT TOOK EFFECT — in any project directory:"
  echo
  echo "       botainer inspect | grep /$COMPONENT"
  echo
  echo "   The source must read  $DEST/<uuid>"
  echo "   If it still shows the old path under $STATE_ROOT/state/, stop:"
  echo "   the config did not take and step 3 would delete your only copy."
  echo
  echo "3. ONLY THEN delete the originals. This script kept every one of"
  echo "   them; nothing has been destroyed. To remove them all:"
  echo
  echo "       $0 --component $COMPONENT --state-root '$STATE_ROOT' --cleanup"
  echo
  echo "   or by hand, one per project:"
  if [ -n "${ASIDES:-}" ]; then
    printf '%s' "$ASIDES" | while IFS= read -r a; do
      [ -n "$a" ] && echo "       rm -rf '$a'"
    done
  fi
  echo
fi
