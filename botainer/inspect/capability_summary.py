"""One-line capability summary printed at `botainer start` before launch.

Per sharp-edges F6 + prior-art review: surface the session's grants
(network, mounts, auth profile, plugins) before the user commits.
First-launch-per-project (or after image change) is a confirmation gate;
subsequent launches are informational only.

Per prior-art review: support --quiet (suppress) and --json (machine-parse)
via the `botainer start` CLI flags.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import click

from botainer.core.spec import SessionSpec
from botainer.state import dir as state_dir


@dataclass(frozen=True)
class SummaryGate:
    """Should the summary be confirmed (Y/n) or just printed?"""
    confirm: bool
    reason: str
    last_shown_image: str | None


def render_one_line(spec: SessionSpec) -> str:
    """One-line summary: Network: X | Mounts: ... | Auth: ... | Plugins: ..."""
    binds = spec.mount_plan.binds
    bind_summary = ", ".join(f"{b.target} ({b.mode.value})" for b in binds[:3])
    if len(binds) > 3:
        bind_summary += f", +{len(binds) - 3} more"
    plugins_summary = ", ".join(spec.plugins_enabled) if spec.plugins_enabled else "(none)"
    return (
        f"Network: {spec.network.mode.value} | "
        f"Mounts: {bind_summary} | "
        f"Auth: {spec.profile} | "
        f"Plugins: {plugins_summary}"
    )


def render_multiline(spec: SessionSpec) -> str:
    """Multi-line summary for confirmation gate (first launch or image change)."""
    lines = [
        f"Project:   {spec.project_root}",
        f"Session:   {spec.session_id}",
        f"Image:     {spec.image}",
        f"Network:   {spec.network.mode.value}",
    ]
    # Surface network risk loudly when the agent gets internet access.
    # Runtime matters: Docker can enforce `none`/allowlist; Apptainer shares
    # the host network namespace and CANNOT — so the advice differs (#37: don't
    # recommend `network.mode: none` on apptainer, where it's a no-op).
    is_apptainer = spec.runtime == "apptainer"
    if spec.network.mode.value == "internet":
        lines.append(
            "  ⚠ INTERNET MODE: the agent can fetch arbitrary URLs."
        )
        lines.append(
            "    This is convenient for `pip install` etc. but the agent"
        )
        lines.append(
            "    could also exfiltrate project data via HTTP POST."
        )
        if is_apptainer:
            lines.append(
                "    On Apptainer `network.mode: none` does NOT isolate the"
            )
            lines.append(
                "    container (it shares the host network namespace). Gate"
            )
            lines.append(
                "    at the cluster firewall / a host-side proxy, or run on a"
            )
            lines.append(
                "    compute node without outbound reach, for sensitive work."
            )
        else:
            lines.append(
                "    Switch to `network.mode: none` for sensitive work;"
            )
            lines.append(
                "    pre-install needed packages on the host with"
            )
            lines.append(
                "    `pip install --target ~/.botainer/state/<uuid>/packages/pip ...`"
            )
    elif spec.network.mode.value in (
        "endpoint-ip-allowlist",
        "api-only",
    ):
        # T3-6: tell the fail-closed TRUTH. allowlist/api-only is
        # REFUSED at COMPOSE for every runtime (composition.py #176 — the
        # iptables enforcement was retired), so a real session never reaches
        # this summary in allowlist mode. The old text ("best-effort", "reaches
        # the full internet today") implied the session RUNS with weak
        # enforcement; it does not — it refuses to start. Say that.
        lines.append(
            "  ⚠ allowlist mode (endpoint-ip-allowlist / api-only) is REFUSED"
        )
        lines.append(
            "    at v0.1.0 on every runtime — the session will NOT start. The"
        )
        lines.append(
            "    per-endpoint iptables enforcement was retired (#176). Use"
        )
        lines.append(
            "    `network.mode: internet` (+ gate egress at a host proxy /"
        )
        lines.append(
            "    cluster ACL) or `network.mode: none` for offline work."
        )
    elif spec.network.mode.value == "none" and is_apptainer:
        # T3-6: fail-closed TRUTH. `none` reads as "isolated" but
        # Apptainer shares the host network namespace, so it can't be enforced
        # — and the Apptainer adapter REFUSES it at launch
        # (adapters/apptainer.py::validate, called from composition.launch).
        # The old text ("treat the agent as having host network") implied the
        # session RUNS unisolated; it actually refuses to start. Say that.
        lines.append(
            "  ⚠ `network.mode: none` is REFUSED on Apptainer at v0.1.0 — the"
        )
        lines.append(
            "    session will NOT start (no namespace isolation is available;"
        )
        lines.append(
            "    apptainer exec inherits the host network namespace). Use Docker"
        )
        lines.append(
            "    for offline mode, or set `network.mode: internet` explicitly"
        )
        lines.append(
            "    and gate egress at the cluster firewall for sensitive work."
        )
    if spec.network.endpoints:
        lines.append(f"  endpoints: {', '.join(spec.network.endpoints)}")
    lines.append("Mounts:")
    for b in spec.mount_plan.binds:
        lines.append(f"  {b.source}  →  {b.target}  ({b.mode.value})")
    lines.append(f"Auth profile: {spec.profile}")
    # #53 / T0-2: disclose the agent's in-cage permission posture LOUDLY. When
    # `bypass` (the default), the agent runs with NO per-action permission
    # prompts — an operator MUST know the container is the only thing standing
    # between the agent and every mounted file / reachable network. This mirrors
    # v0.0.x's startup banner ("container is the sandbox, no per-action prompts").
    if spec.agent_permissions == "bypass":
        lines.append(
            "Permissions: BYPASS  (⚠ the agent runs UNATTENDED — no prompts)"
        )
        lines.append(
            "    The agent may run ANY command and edit ANY mounted file without"
        )
        lines.append(
            "    asking. The container (binds + network + §4 cage) is the ONLY"
        )
        lines.append(
            "    boundary. This is required for TTY-less batch; set"
        )
        lines.append(
            "    `agent_permissions: prompt` in .botainer/config.yaml to restore"
        )
        lines.append(
            "    per-action prompts (interactive/TTY sessions only)."
        )
    else:
        lines.append(
            "Permissions: PROMPT  (the agent asks before consequential actions;"
        )
        lines.append(
            "    needs a TTY — an unattended/batch job would DEADLOCK at the first"
        )
        lines.append(
            "    prompt. Use `agent_permissions: bypass` for batch.)"
        )
    # Make auth-mode unambiguous: proxy mode (real key on host, agent
    # sees an ephemeral token) vs mount mode (credentials mounted into
    # the container, agent can read them).
    # Re-audit round 4 (#5): use the SAME agent-family-agnostic predicate as
    # _auth_mode() / render_json / the HPC submit surface, so all consent
    # surfaces agree (was claude-only here — a codex-*-proxy session would have
    # missed the PROXY disclosure).
    if _auth_mode(spec) == "broker":
        # BROKER mode (T0-3 replacement): the real Anthropic credential stays
        # host-side in the broker daemon; the container holds ONLY a provably-
        # fake sentinel (BROKER-SENTINEL.…NOT-A-REAL-CREDENTIAL) and reaches
        # Anthropic through a per-session unix socket. The broker injects the
        # real Bearer on the outbound leg; a compromised agent can't read or
        # exfiltrate the credential. This is the SECURE posture — the positive
        # inverse of MOUNT — so the disclosure is reassurance, not a warning.
        lines.append(
            "  Auth mode: BROKER — your real Anthropic credential stays on the"
        )
        lines.append(
            "    host (in the broker daemon). The container holds only a fake"
        )
        lines.append(
            "    sentinel token and reaches Anthropic through a local broker"
        )
        lines.append(
            "    (unix socket on apptainer; loopback TCP on Docker Desktop) that"
        )
        lines.append(
            "    injects the real credential host-side. A compromised agent"
        )
        lines.append(
            "    cannot read or exfiltrate your credential. Token refresh (and"
        )
        lines.append(
            "    any rotation) happens host-side against botainer's own login"
        )
        lines.append(
            "    store — it never touches your native Claude Code credential."
        )
        lines.append(
            "    (Claude Code shows 'API Usage Billing' because it sees a custom"
        )
        lines.append(
            "    endpoint; the broker injects your subscription OAuth token, so"
        )
        lines.append(
            "    usage bills against your plan — verify in the Anthropic console.)"
        )
        if spec.runtime == "apptainer":
            # M1 (audit): the broker daemon runs where compose runs. On the
            # sbatch compose-at-submit path that is the LOGIN node, and its
            # socket/port is unreachable from the compute node. So broker mode is
            # same-node only until a compute-node-side daemon exists — works
            # under salloc / interactive apptainer, NOT the sbatch batch flow.
            #
            # HPC-parity audit (C2): this comment used to assert "the
            # cross-node bind check refuses it", which was FALSE for the TCP
            # broker (agent-codex-broker) — that check was bind-shaped and the
            # TCP rendezvous carries no bind, so the sbatch job composed
            # successfully and died on the compute node with ECONNREFUSED. It is
            # now true for both transports: _refuse_cross_node_binds check 3
            # refuses any env value naming a loopback host.
            lines.append(
                "    NOTE (HPC): broker mode needs compose+run on the SAME node —"
            )
            lines.append(
                "    use it under salloc/interactive apptainer. The sbatch batch"
            )
            lines.append(
                "    path can't reach a login-node broker yet (tracked follow-up)."
            )
    elif _auth_mode(spec) == "proxy":
        # Proxy mode is EXPERIMENTAL at v0.1.0. Two known gaps:
        #   1. The proxy reads from the PER-PROJECT credential file,
        #      not the shared-auth dir (per AUTH-PRODUCT-PLAN §2).
        #   2. There is NO refresh-on-401 controller. The proxy is a
        #      thin forwarder; if the upstream returns 401 the agent
        #      sees the 401 and the session must be re-auth'd. OAuth
        #      access tokens typically last hours, so this is a soft
        #      failure in interactive use, but it's a hard failure
        #      for long-running batch jobs.
        # See DN-009 for the plan.
        lines.append(
            "  Auth mode: PROXY  (⚠ NOT FUNCTIONAL at v0.1.0 — will refuse to start)"
        )
        lines.append(
            "    The design keeps your real Anthropic credentials on the host and"
        )
        lines.append(
            "    hands the container only an ephemeral token — but that token is"
        )
        lines.append(
            "    passed as ANTHROPIC_API_KEY, which botainer's credential-leak guard"
        )
        lines.append(
            "    refuses on principle, so a proxy session cannot start on ANY"
        )
        lines.append(
            "    runtime today. Use `--mode=shared` or `--mode=isolated` instead."
        )
        lines.append(
            "    (Making proxy functional needs a scoped leak-check exemption +"
        )
        lines.append(
            "     OAuth refresh-on-401 — tracked in internal design notes.)"
        )
        lines.append(
            "    Audit log (only written if you force it experimental):"
        )
        lines.append(
            f"      {spec.state_dir}/sessions/{spec.session_id}/proxy-audit.jsonl"
        )
    else:
        # MOUNT mode for ANY agent family (audit T11: was claude-only, so a
        # codex MOUNT session showed no warning). Detect the credential bind
        # regardless of agent (claude → /home/agent/.claude, codex →
        # /home/agent/.openai).
        _cred = _agent_cred_bind(spec)
        if _cred is not None:
            lines.append(
                "  Credential delivery: MOUNTED INTO THE CONTAINER — your real"
            )
            lines.append(
                f"    bound into the container at {_cred.target} (visible to the"
            )
            lines.append(
                "    visible to the agent. To keep them host-side, use a broker/proxy"
            )
            lines.append(
                "    for your agent family where one is available."
            )
    lines.append(f"Plugins:   {', '.join(spec.plugins_enabled) or '(none)'}")
    if spec.plugin_trust_warnings:
        # MEDIUM 9: never let trust-tier degradation be invisible.
        lines.append("")
        lines.append("⚠ PLUGIN TRUST WARNING — one or more enabled plugins are not first-party:")
        for name, tier, detail in spec.plugin_trust_warnings:
            lines.append(f"    {name}  ({tier})")
            lines.append(f"      {detail}")
        lines.append(
            "    If you didn't intend to enable modified or third-party plugins, "
            "disable them with `botainer plugin disable <name>` (AUDIT 2026-06-09: "
            "was `plugin uninstall`, which is not a real subcommand)."
        )
    if "nudge" in spec.plugins_enabled:
        lines.append(
            "  ⚠ nudge: screen -X stuff (host-side); anyone with shell"
            " access to this host can inject input into the agent's prompt"
        )
    if spec.entrypoint_wraps:
        wrap_names = [w[0] for w in spec.entrypoint_wraps]
        lines.append(f"Entrypoint wraps: {' → '.join(wrap_names)}")
    if spec.port_forwards:
        lines.append("Web ports forwarded:")
        for pf in spec.port_forwards:
            label = f" ({pf.label})" if pf.label else ""
            lines.append(
                f"  http://{pf.host_bind}:{pf.host_port}  →  "
                f"container:{pf.container_port}{label}"
            )
        lines.append(
            "  ⚠ any process with host shell access can reach these ports"
        )
    # Re-audit round 3 (#3/#4/#7): the consent re-confirm fingerprint
    # (_capability_fingerprint) keys on env-var NAMES, env-files, and
    # capabilities. If those weren't shown here, a change would force a
    # re-confirmation while the user saw an unchanged summary (couldn't tell WHAT
    # changed). Surface them so the displayed posture matches what the gate keys
    # on. Values are NOT shown (env values may be secret) — names only.
    if spec.env.values:
        lines.append(
            f"Env vars set ({len(spec.env.values)}): "
            f"{', '.join(sorted(spec.env.values.keys()))}"
        )
    if spec.env_files:
        lines.append("Env files (applied into the container):")
        for ef in spec.env_files:
            lines.append(f"  {ef}")
    if spec.capabilities:
        lines.append(
            # DEDUPED. Two grants of the same capability (e.g. two `mounts.extra`
            # entries) are two CapabilityDecls with one name, and this printed the
            # name twice — observed on a real Mac launch as
            # "env.values, mounts.extra, mounts.extra, mounts.workspace, network".
            # A repeated word in a security summary reads as a rendering bug and
            # makes the reader trust the rest of the line less.
            "Capabilities granted: "
            + ", ".join(sorted({c.name for c in spec.capabilities}))
        )
    # Audit T11 (DN-028 §7): if this is a git repo but the git plugin's
    # guarded `.git/hooks` read-only overlay is NOT active (plugin disabled or
    # mode:off), the agent has rw access to .git/hooks — and a hook it writes
    # runs on YOUR host as you the next time you `git` from outside the
    # container. The git plugin's own code deferred this to "the start banner";
    # surface it here.
    if _git_unprotected(spec):
        lines.append("")
        lines.append(
            "⚠ GIT UNPROTECTED — the agent has write access to .git/ (including"
        )
        lines.append(
            "    .git/hooks). A hook it installs runs on YOUR host as you the next"
        )
        lines.append(
            "    time you run git OUTSIDE this container. Enable the `git` plugin"
        )
        lines.append(
            "    (default mode `guarded`) for a read-only .git/hooks overlay, or"
        )
        lines.append(
            "    audit `ls -la .git/hooks/` after the session if you ran git on host."
        )

    # /scratch is bound so the agent has somewhere disposable to work — but the
    # user is never told the host directory behind it is likely to be DELETED
    # automatically. On the clusters botainer targets that is a scheduled
    # filesystem policy, not a person's decision, and the deletion is silent.
    # The agent is told (AGENT_HINTS); until now the user was not, at the one
    # moment they are actually reading. (User directive.)
    from botainer.inspect.agent_hints import scratch_purge_note
    _scratch_host, _purge_days = scratch_purge_note(spec)
    if _scratch_host:
        lines.append("")
        if _purge_days:
            lines.append(
                f"⚠ /scratch is AUTO-DELETED after ~{_purge_days} days by the cluster")
            lines.append(f"    host path: {_scratch_host}")
            lines.append(
                "    Anything the agent leaves there can disappear without warning."
            )
            lines.append(
                "    Results you want to keep belong in the project (/workspace)."
            )
        else:
            # The opposite failure, and the one that is true by default: NOTHING
            # deletes this. Announcing a purge that will not happen (the bug the
            # user caught on) is the worse error of the two, because
            # a user who believes it self-cleans never goes looking for the GBs.
            lines.append("• /scratch is for disposable work — nothing deletes it")
            lines.append(f"    host path: {_scratch_host}")
            lines.append(
                "    It is outside the project, so it is never committed — but no"
            )
            lines.append(
                "    purge runs either; it grows until you remove it. `botainer"
            )
            lines.append("    where` lists it with its size.")
    return "\n".join(lines)


def render_json(spec: SessionSpec) -> str:
    """Machine-readable summary."""
    return json.dumps(
        {
            "session_id": spec.session_id,
            "project_root": spec.project_root,
            "project_uuid": spec.project_uuid,
            "image": spec.image,
            "runtime": spec.runtime,
            "network_mode": spec.network.mode.value,
            "network_endpoints": list(spec.network.endpoints),
            "mounts": [
                {"source": b.source, "target": b.target, "mode": b.mode.value}
                for b in spec.mount_plan.binds
            ],
            "auth_profile": spec.profile,
            "plugins_enabled": list(spec.plugins_enabled),
            "entrypoint_wraps": [list(w) for w in spec.entrypoint_wraps],
            "nudge_enabled": "nudge" in spec.plugins_enabled,
            "port_forwards": [
                {
                    "container_port": pf.container_port,
                    "host_port": pf.host_port,
                    "host_bind": pf.host_bind,
                    "label": pf.label,
                }
                for pf in spec.port_forwards
            ],
            "auth_mode": _auth_mode(spec),
            # Re-audit: JSON consent surface parity with render_multiline —
            # git-protection state + the must-see fields (hook plugins run on
            # host, env var NAMES (values redacted), env-files, capabilities).
            "git_unprotected": _git_unprotected(spec),
            "env_var_names": sorted(spec.env.values.keys()),
            "env_files": list(spec.env_files),
            "host_hooks": [
                {"plugin": h.plugin, "when": h.when} for h in spec.hooks
            ],
            "capabilities": sorted(
                {c.name for c in spec.capabilities}
            ),
        },
        sort_keys=True,
    )


def _git_unprotected(spec: SessionSpec) -> bool:
    """True if the project is a git repo but the git plugin's read-only
    .git/hooks overlay is NOT active — the agent could install a host-executed
    git hook (DN-028 §7). Shared by render_multiline, render_json, and the
    HPC login-node confirmation (re-audit: was render_multiline-only)."""
    from pathlib import Path as _Path
    try:
        is_git = (_Path(spec.project_root) / ".git").exists()
    except OSError:
        is_git = False
    overlaid = any(
        b.target == "/workspace/.git/hooks" for b in spec.mount_plan.binds
    )
    return is_git and not overlaid


def _agent_cred_bind(spec: SessionSpec):
    """The agent-credential bind, if any, regardless of agent family. Agent
    plugins mount creds under /home/agent/.<family> (claude → .claude, codex →
    .openai). Audit T11: was claude-specific, so a codex MOUNT session reported
    auth 'none' despite mounting the real API key."""
    for b in spec.mount_plan.binds:
        if b.target.startswith("/home/agent/.") and b.target != "/home/agent/.config":
            return b
    return None


def _auth_mode(spec: SessionSpec) -> str:
    """Return 'broker', 'proxy', 'mount', or 'none' for tooling output
    (agent-agnostic)."""
    if any(p.startswith("agent-") and p.endswith("-broker") for p in spec.plugins_enabled):
        return "broker"
    if any(p.startswith("agent-") and p.endswith("-proxy") for p in spec.plugins_enabled):
        return "proxy"
    if _agent_cred_bind(spec) is not None:
        return "mount"
    return "none"


def _fingerprint_source(bind, spec) -> str:
    """The bind source as the CONSENT GATE should see it.

    A source that lives inside THIS session's own ephemeral directory carries a
    freshly-minted session id, so including it verbatim makes the fingerprint
    change on every launch — which fires the "capability set changed" gate when
    nothing changed, and trains the user to approve without reading.

    What the user is consenting to for such a bind is target + mode +
    provenance: "this session's own hints file, read-only, from the launcher".
    The launcher-chosen path is not part of that.

    Keyed on the PROPERTY (source is under `<state>/sessions/<this session>/`),
    not on a mode enum. The previous rule normalised only `mode == socket` and
    therefore missed the two `ro` per-session files — a third ephemeral bind in
    some other mode would have hit it again.
    """
    src = str(bind.source)
    sid = getattr(spec, "session_id", "") or ""
    if sid and f"/sessions/{sid}/" in src.replace("\\", "/"):
        # Keep the tail: /workspace/.botainer/AGENT_HINTS.md and .../AGENT_ACCESS.txt
        # must remain DISTINGUISHABLE from each other, or swapping which file is
        # bound where would not re-confirm.
        tail = src.replace("\\", "/").split(f"/sessions/{sid}/", 1)[1]
        return f"<this-session>/{tail}"
    if "socket" in bind.mode.value.lower():
        return "<ephemeral-socket>"
    return src


def _capability_fingerprint(spec: SessionSpec) -> str:
    """A stable hash of the SECURITY-RELEVANT capability surface the user
    consents to (audit T8). Keying the confirmation gate on the image digest
    ALONE let a capability CHANGE — flip network none→internet, add a bind,
    enable a plugin, switch auth proxy→mount — re-launch with NO re-confirmation
    while the image was unchanged. The gate now re-confirms when ANY of these
    change. Computed from the post-hook spec (the gate runs after hooks)."""
    import hashlib
    import json
    payload = {
        "image": spec.image,
        "runtime": spec.runtime,
        "network": spec.network.mode.value,
        "endpoints": sorted(spec.network.endpoints),
        # bind set: target+source+mode+provenance (the host→container exposure).
        # Audit L5: an ephemeral socket source (the broker's per-session
        # …/run/brk-<sid>.sock) would churn the fingerprint every launch →
        # consent fatigue (users reflexively --yes, eroding the gate). For a
        # socket bind the security-relevant surface is target+mode+provenance,
        # NOT the launcher-chosen ephemeral host path, so normalize its source.
        #: THE SAME PROBLEM, ONE BIND-MODE OVER. The rule above keyed
        # the normalisation on `mode == socket`, so it missed the other ephemeral
        # sources — the per-session files:
        #
        #   sessions/<sid>/AGENT_ACCESS.txt  ->  /workspace/.botainer/…  (ro)
        #   sessions/<sid>/AGENT_HINTS.md    ->  /workspace/.botainer/…  (ro)
        #
        # <sid> is minted per launch, so the fingerprint changed on EVERY launch
        # and the gate announced "capability set changed since last launch" when
        # nothing had. Measured on a second consecutive start with no config
        # change, and visible in a real cluster transcript.
        #
        # That is the exact consent-fatigue failure the comment above describes,
        # and it is worse than churn: the gate is the only thing between the user
        # and a REAL capability change (network none->internet, a new bind, a
        # switched auth mode). Firing every time teaches the user to press Enter
        # without reading, so the one real change goes through unread.
        #
        # Normalise on the PROPERTY (the source is inside this session's own
        # ephemeral dir) rather than on a mode enum, so a future ephemeral bind
        # in a third mode cannot reintroduce this.
        "binds": sorted(
            (b.target, _fingerprint_source(b, spec), b.mode.value, b.provenance.value)
            for b in spec.mount_plan.binds
        ),
        "env_names": sorted(spec.env.values.keys()),  # names only (values are secret)
        "env_files": list(spec.env_files),
        "plugins": sorted(spec.plugins_enabled),
        "capabilities": sorted((c.name, str(c.value)) for c in spec.capabilities),
        "entrypoint_wraps": [list(w) for w in spec.entrypoint_wraps],
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def determine_gate(spec: SessionSpec) -> SummaryGate:
    """Should the user be asked to confirm?

    Gate on first launch + whenever the CAPABILITY SET changes (audit T8) —
    not merely the image digest. Subsequent identical launches: print one-liner,
    don't gate. Tracks the capability fingerprint + image in the project meta.
    """
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    proj_paths = paths.for_project(spec.project_uuid)
    meta = state_dir.read_meta(proj_paths)
    fingerprint = _capability_fingerprint(spec)
    last_fp = meta.get("last_capability_summary_fingerprint")
    last_img = meta.get("last_capability_summary_image")
    if last_fp is None:
        # No fingerprint recorded (first launch, OR a pre-T8 meta that only has
        # the image). Re-confirm once, then the fingerprint is stored.
        reason = (
            "first launch of this project" if last_img is None
            else "capability set not yet confirmed under the current gate"
        )
        return SummaryGate(confirm=True, reason=reason, last_shown_image=last_img)
    if last_fp != fingerprint:
        if last_img != spec.image:
            reason = (
                f"image changed since last launch "
                f"({str(last_img)[:24]}… → {str(spec.image)[:24]}…)"
            )
        else:
            reason = (
                "capability set changed since last launch "
                "(network / binds / env / plugins / capabilities / entrypoint)"
            )
        return SummaryGate(confirm=True, reason=reason, last_shown_image=str(last_img))
    return SummaryGate(confirm=False, reason="", last_shown_image=str(last_img))


def record_shown(spec: SessionSpec) -> None:
    """After successful launch, remember the confirmed capability fingerprint
    (+ image, for the human-readable change reason)."""
    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    proj_paths = paths.for_project(spec.project_uuid)
    meta = state_dir.read_meta(proj_paths)
    meta["last_capability_summary_fingerprint"] = _capability_fingerprint(spec)
    meta["last_capability_summary_image"] = spec.image
    state_dir.write_meta(proj_paths, meta)


def print_and_maybe_confirm(
    spec: SessionSpec,
    *,
    quiet: bool = False,
    as_json: bool = False,
    auto_yes: bool = False,
) -> bool:
    """Print summary and (if gated) ask for confirmation.

    Returns True if the launch should proceed, False if user declined.

    Implementation-review HIGH 3: `--quiet` MUST NOT bypass the
    confirmation gate. It only suppresses the *informational* one-line
    summary on subsequent launches. The gate (first launch + after
    image change) is a security-critical user check; bypassing it via
    --quiet alone would let any script silently authorise a fresh
    project. To bypass the gate non-interactively, pass `--yes`
    (acknowledging you understand the capability grant).
    """
    gate = determine_gate(spec)

    # JSON mode: emit structured summary, then enforce gate the same way.
    if as_json:
        click.echo(render_json(spec))
        if gate.confirm and not auto_yes:
            click.secho(
                "refused: first launch (or image change) of this project; "
                "pass --yes to confirm non-interactively, or run interactively first.",
                fg="red",
                err=True,
            )
            return False
        if gate.confirm and auto_yes:
            record_shown(spec)
        return True

    if gate.confirm:
        if quiet and not auto_yes:
            # User asked for quiet but didn't pre-authorise. Refuse
            # rather than silently prompt or silently proceed.
            click.secho(
                "refused: first launch of this project requires confirmation; "
                "either run without --quiet (to see capability grant) or pass --yes.",
                fg="red",
                err=True,
            )
            return False
        if not quiet:
            click.secho("=" * 72, fg="cyan")
            click.secho("Confirm session launch:", fg="cyan", bold=True)
            click.secho(f"  Reason for confirmation: {gate.reason}", fg="cyan")
            click.secho("=" * 72, fg="cyan")
            click.echo(render_multiline(spec))
            click.secho("=" * 72, fg="cyan")
        if auto_yes:
            if not quiet:
                # Don't claim "--yes passed" — auto_yes is ALSO forced True on any
                # non-interactive path (a batch/compute-node job has no TTY to
                # answer the prompt; see submit.py `auto_yes=(args.yes or not
                # interactive)`). Say which.
                click.echo("(auto-confirmed without prompt — either --yes was "
                           "passed, or this is a non-interactive session with no "
                           "terminal to answer the confirmation)")
            record_shown(spec)
            return True
        try:
            answer = click.prompt("Launch this session? [y/N]", default="N", show_default=False)
        except (click.Abort, EOFError):
            return False
        if answer.strip().lower() in ("y", "yes"):
            record_shown(spec)
            return True
        return False

    # Subsequent-launch path (no gate). --quiet suppresses the one-line.
    if not quiet:
        click.secho(render_one_line(spec), fg="cyan")
    return True
