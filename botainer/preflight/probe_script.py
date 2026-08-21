"""The in-container self-test probe — a launcher-owned POSIX-sh constant.

`PROBE_SH` runs INSIDE the session container (delivered via argv, not a bind:
`entrypoint_wraps = (("sh","-c",PROBE_SH,"botainer-probe",*probe_args),)`), so
the probe run's mount plan + capability surface are byte-identical to a real
session — the thing under test isn't perturbed by the test.

Contract:
  - Pure POSIX sh (works in the agent images' /bin/sh and any third-party
    image with /bin/sh; python3 used ONLY for the optional TCP prober, which
    skips if absent).
  - NO shell-construction from untrusted input: PROBE_SH is a constant; all
    variable data arrives as positional parameters ("$@"), each a
    "<check>\x1f<kind>\x1f<path>" triple (build_probe_plan). Field split is by
    parameter expansion on the 0x1f separator — never eval of the spec.
  - Emits one JSON line per check between BOTAINER-SELFTEST-V1-BEGIN/END, then
    /proc/self/mounts between BOTAINER-PROC-MOUNTS-BEGIN/END. ALWAYS exits 0 —
    verdicts live in the JSON; a nonzero exit means exec/launch breakage
    (runner maps that to a runtime error, not a check failure).
  - NEVER prints file contents, env values, or credentials — only enum names,
    pass/fail/skip, and a sanitized short reason (quotes/backslashes/control
    chars stripped).
  - Test hooks: $BOTAINER_PROBE_STATUS / $BOTAINER_PROBE_MOUNTS override the
    /proc paths so unit tests feed canned files.

Design authored + shell reviewed via verified Fable-5 subagents
(wf_fecc8dc3-aa7).
"""

from __future__ import annotations

PROBE_SH = r'''
US=$(printf '\037')
SB="BOTAINER-SELFTEST-V1-BEGIN"; SE="BOTAINER-SELFTEST-V1-END"
MB="BOTAINER-PROC-MOUNTS-BEGIN"; ME="BOTAINER-PROC-MOUNTS-END"
STATUS="${BOTAINER_PROBE_STATUS:-/proc/self/status}"
MOUNTS="${BOTAINER_PROBE_MOUNTS:-/proc/self/mounts}"

san() { printf '%s' "$1" | tr -d '"\\' | tr -d '\000-\037'; }
# emit reads $path (the current probe's target, set in the loop below) as the
# "target" field so the runner can match each result to a specific planned
# probe by (check, target) IDENTITY — two targets sharing a check enum (e.g.
# two RO binds → data-ro) are then distinguishable (Codex Priority-A MEDIUM).
emit() { printf '{"check":"%s","result":"%s","detail":"%s","target":"%s"}\n' "$1" "$2" "$(san "$3")" "$(san "${path-}")"; }

echo "$SB"
for spec in "$@"; do
  check=${spec%%"$US"*}
  rest=${spec#*"$US"}
  kind=${rest%%"$US"*}
  path=${rest#*"$US"}
  case "$kind" in
    write_rw)
      f="$path/.botainer-selftest.$$"
      if ( : > "$f" ) 2>/dev/null; then rm -f "$f" 2>/dev/null; emit "$check" pass "";
      else emit "$check" fail "write refused at $path"; fi ;;
    write_ro)
      if [ -d "$path" ]; then
        f="$path/.botainer-selftest.$$"
        if ( : > "$f" ) 2>/dev/null; then rm -f "$f" 2>/dev/null; emit "$check" fail "dir writable (expected RO)";
        else emit "$check" pass ""; fi
      elif [ -e "$path" ]; then
        if ( : >> "$path" ) 2>/dev/null; then emit "$check" fail "file writable (expected RO)";
        else emit "$check" pass ""; fi
      else emit "$check" fail "expected-RO path missing"; fi ;;
    capeff)
      v=$(sed -n 's/^CapEff:[[:space:]]*//p' "$STATUS" 2>/dev/null)
      vs=$(printf '%s' "$v" | sed 's/^0*//')
      if [ -z "$v" ]; then emit "$check" skip "no CapEff field";
      elif [ -z "$vs" ]; then emit "$check" pass "";
      else emit "$check" fail "CapEff nonzero"; fi ;;
    nonewprivs)
      v=$(sed -n 's/^NoNewPrivs:[[:space:]]*//p' "$STATUS" 2>/dev/null)
      if [ -z "$v" ]; then emit "$check" skip "no NoNewPrivs field";
      elif [ "$v" = "1" ]; then emit "$check" pass "";
      else emit "$check" fail "NoNewPrivs not set"; fi ;;
    shadow)
      # Use the shell builtin `[ -r ]` (access(2)) rather than `cat`: a hostile
      # or minimal image may lack cat, and `if cat ...` puts command-not-found
      # in the ELSE (pass) branch — reporting shadow-safe when we never tested
      # (Fable-5 review MEDIUM-5). `[` is a POSIX builtin, always present. A
      # readable shadow (world-readable, or running as root) → fail.
      if [ ! -e /etc/shadow ]; then emit "$check" pass "no /etc/shadow";
      elif [ -r /etc/shadow ]; then emit "$check" fail "/etc/shadow readable";
      else emit "$check" pass ""; fi ;;
    sshdir)
      cnt=0
      for d in "$HOME/.ssh" /root/.ssh; do
        [ -d "$d" ] || continue
        # globs: normal, dot-not-dotdot (.[!.]*), and dotdot-prefixed (..?*)
        # so a key hidden as ..id_rsa is still counted (L7).
        for e in "$d"/* "$d"/.[!.]* "$d"/..?*; do [ -e "$e" ] && cnt=$((cnt+1)); done
      done
      if [ "$cnt" -gt 0 ]; then emit "$check" fail "ssh file(s) present";
      else emit "$check" pass ""; fi ;;
    home)
      # $HOME must equal the path the launcher promised AND be writable.
      # Apptainer silently refuses `--env HOME=`, so a mounted-but-unpointed-at
      # home looks fine from the host and is broken inside (§4bo). Both halves
      # matter: the right path that is read-only is just as useless as the
      # wrong path.
      if [ "${HOME-}" != "$path" ]; then
        emit "$check" fail "HOME is not the promised path"
      elif [ ! -d "$HOME" ]; then
        emit "$check" fail "HOME is not a directory"
      elif touch "$HOME/.botainer-home-probe" 2>/dev/null; then
        rm -f "$HOME/.botainer-home-probe" 2>/dev/null
        emit "$check" pass ""
      else
        emit "$check" fail "HOME not writable"
      fi ;;
    env_unset)
      bad=""
      for v in BASH_ENV LD_PRELOAD LD_AUDIT ENV PROMPT_COMMAND; do
        eval "val=\${$v-}"
        [ -n "$val" ] && bad="$bad $v"
      done
      if [ -n "$bad" ]; then emit "$check" fail "exec-injection env set:$bad";
      else emit "$check" pass ""; fi ;;
    tcp_must_fail)
      host=${path%%:*}; port=${path#*:}
      if command -v python3 >/dev/null 2>&1; then
        if python3 -c 'import socket,sys
s=socket.socket(); s.settimeout(3)
try:
    s.connect((sys.argv[1], int(sys.argv[2]))); sys.exit(0)
except Exception:
    sys.exit(1)' "$host" "$port" >/dev/null 2>&1; then
          emit "$check" fail "egress reachable (expected none)";
        else emit "$check" pass ""; fi
      else emit "$check" skip "no python3 for network probe"; fi ;;
    *) emit "$check" skip "unknown probe kind"; ;;
  esac
done
echo "$SE"
echo "$MB"
cat "$MOUNTS" 2>/dev/null || true
echo "$ME"
exit 0
'''


def render_probe_args(spec) -> list[str]:
    """The full `sh -c` entrypoint tail for the probe: ['sh','-c',PROBE_SH,
    'botainer-probe', *directed-probe-args]. Ready to use as an
    entrypoint_wrap tuple."""
    from botainer.preflight.checks import build_probe_plan
    return ["sh", "-c", PROBE_SH, "botainer-probe", *build_probe_plan(spec)]
