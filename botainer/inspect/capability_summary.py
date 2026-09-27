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
import textwrap
from dataclasses import dataclass

import click

from botainer.core import agent_permissions
from botainer.core.spec import BindMode, SessionSpec
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


def render_multiline(spec: SessionSpec, *, on_sbatch_path: bool = False) -> str:
    """Multi-line summary for confirmation gate (first launch or image change).

    `on_sbatch_path` is passed by `hpc submit`, which renders this same block on
    the login node. It is a fact only the CALLER has: the runtime cannot supply
    it, because `botainer start --runtime apptainer` inside an `salloc` is the
    same runtime and NOT the sbatch path. Branching on `spec.runtime` told that
    user to "use `botainer start --runtime apptainer` inside an salloc" — what
    they were already doing — and never told them broker works for them.
    """
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
    # WHAT THIS BANNER MAY SAY. Only what BOTAINER did — which flags it
    # appended, and what it pinned. It may NOT assert what the agent will then
    # do, because botainer does not control that and saying so has been wrong
    # in both directions:
    #
    #   The old PROMPT branch said "the agent asks before consequential actions
    #   ... an unattended/batch job would DEADLOCK at the first prompt". In that
    #   mode botainer appends nothing, so both halves were claims about someone
    #   else's binary — and the deadlock warning is the half that matters on a
    #   cluster, because it may say a batch job will hang when it will not.
    #
    #   The old BYPASS branch — the one nearly everyone sees, since `bypass` is
    #   the default — ended "set `agent_permissions: prompt` ... to restore
    #   per-action prompts". On codex that instruction does not restore prompts;
    #   it produces an agent whose every command fails, because codex's own
    #   sandbox cannot start in a container. The default banner handed the
    #   reader a remedy that broke their session. Same class as #130 (shipped
    #   output naming commands that do not exist), one step worse, because this
    #   command exists and runs.
    #
    # Both branches also assumed the vocabulary was two words. It is not: any
    # value other than `bypass` fell into the PROMPT branch, so `acceptEdits`,
    # `plan`, `dontAsk` and `never` would all have been announced as "the agent
    # asks before consequential actions".
    # The user's OWN word is what gets printed — `prompt` stays `prompt` on
    # screen for someone who wrote it — while lookups fold it onto `default`.
    # The gloss then corrects the misnomer in place, which is better than
    # silently renaming their setting back at them.
    _perm = spec.agent_permissions
    _fam = _agent_family_for(spec)
    _argv = agent_permissions.argv_for(_fam, _perm)
    _gloss = agent_permissions.gloss_for(_fam, _perm)
    _tier = agent_permissions.tier_of(_fam, _perm)

    if _tier == agent_permissions.TIER_BOTAINER_OFF:
        lines.append(
            f"Permissions: {_perm}  (⚠ the agent runs UNATTENDED — nothing will ask)"
        )
        lines.append(
            "    botainer turned the agent's own permission system OFF. It may run"
        )
        lines.append(
            "    ANY command and edit ANY mounted file without asking. The container"
        )
        lines.append(
            "    (binds + network + §4 cage) is the ONLY boundary. This is required"
        )
        lines.append(
            "    for TTY-less batch — there is no terminal to answer a prompt on."
        )
    else:
        lines.append(f"Permissions: {_perm}")
        if _gloss:
            lines.append(f"    {_gloss}.")
        if agent_permissions.canonical(_perm) == agent_permissions.CANONICAL_DEFAULT:
            lines.append(
                "    botainer is NOT choosing whether you are asked. Whether the"
            )
            lines.append(
                "    agent prompts, and about what, is the agent's own decision —"
            )
            lines.append(
                "    botainer cannot promise it on the agent's behalf. If this is a"
            )
            lines.append(
                "    batch job with no TTY, check the agent's own default before"
            )
            lines.append(
                "    relying on it; `bypass` is the setting that guarantees no prompt."
            )
    # State the argv, always. It is the one thing botainer knows for certain,
    # it is short, and it is what makes the two paragraphs above checkable by
    # the person reading them rather than taken on trust.
    if _argv:
        lines.append(f"    botainer appended: {' '.join(_argv)}")
        if _fam == "openai":
            lines.append(
                "    (the --sandbox value is PINNED: codex's own sandbox cannot"
            )
            lines.append(
                "     start inside a container. The container is the boundary.)"
            )
    else:
        lines.append("    botainer appended: nothing")
    # Make auth-mode unambiguous: proxy mode (real key on host, agent
    # sees an ephemeral token) vs mount mode (credentials mounted into
    # the container, agent can read them).
    # Re-audit round 4 (#5): use the SAME agent-family-agnostic predicate as
    # _auth_mode() / render_json / the HPC submit surface, so all consent
    # surfaces agree (was claude-only here — a codex-*-proxy session would have
    # missed the PROXY disclosure).
    # COMPANIONS: when more than one agent family is enabled they can be in
    # DIFFERENT modes, and the blocks below speak in the singular ("your
    # credential"). Naming each family first is what stops a broker-mode
    # reassurance from being read as covering a mount-mode companion whose real
    # key is bind-mounted and readable in the same container. Silent when only
    # one family is enabled — the blocks below already say it, and a line that
    # fires on every ordinary session is scenery.
    _fam_modes = auth_modes_by_family(spec)
    if len(_fam_modes) > 1:
        lines.append("  Auth modes differ by agent — this session has more "
                     "than one enabled:")
        for _fam, _mode in sorted(_fam_modes.items()):
            lines.append(f"    {_fam:<12s} {_mode}")
        if len(set(_fam_modes.values())) > 1:
            lines.append(
                "    They are NOT the same posture. What is said below about "
                "one of these"
            )
            lines.append(
                "    credentials does not carry to the others — read the "
                "per-mode notes."
            )

    if _auth_mode(spec) == "broker":
        # BROKER mode (T0-3 replacement): the real Anthropic credential stays
        # host-side in the broker daemon; the container holds ONLY a provably-
        # fake sentinel (BROKER-SENTINEL.…NOT-A-REAL-CREDENTIAL) and reaches
        # Anthropic through a per-session unix socket. The broker injects the
        # real Bearer on the outbound leg. So for THIS family, a compromised
        # agent has no credential to read — the positive inverse of MOUNT.
        #
        # SCOPE, and it is the whole of CRITICAL-1: that holds for the family
        # in broker mode and says nothing about any other family in the same
        # session. A companion in mount mode has its real key bind-mounted and
        # readable in the same container. The absolute form of this sentence
        # used to be PRINTED here; it is not, and must not come back. Anything
        # reassuring said in this block is about one credential, and the
        # per-family block above is what tells the user which.
        # Name the provider and native client for the credential being described.
        # Both agent names (codex/claude) and provider-family names
        # (openai/anthropic) occur in the mode map, so handle both spellings.
        _broker_fams = sorted(f for f, m in _fam_modes.items() if m == "broker")
        _one = _broker_fams[0] if len(_broker_fams) == 1 else ""
        _vendor = {"anthropic": "Anthropic", "claude": "Anthropic",
                   "openai": "OpenAI", "codex": "OpenAI"}.get(_one, "")
        _client = {"anthropic": "Claude Code", "claude": "Claude Code",
                   "openai": "codex", "codex": "codex"}.get(_one, "")
        # Whole phrases, not spliced nouns: the first version produced "your
        # real your agent provider's credential" and "your native native agent
        # credential" in the fallback.
        _cred_phrase = f"your real {_vendor} credential" if _vendor else (
            "your real agent credential")
        _reach_phrase = _vendor if _vendor else "the provider"
        _native_phrase = (f"your native {_client} credential" if _client else
                          "your native agent credential")
        lines.append(
            f"  Auth mode: BROKER — {_cred_phrase} stays on the"
        )
        lines.append(
            "    host (in the broker daemon). The container holds only a fake"
        )
        lines.append(
            f"    sentinel token and reaches {_reach_phrase} through a local broker"
        )
        lines.append(
            "    (unix socket on apptainer; loopback TCP on Docker Desktop) that"
        )
        lines.append(
            "    injects the real credential host-side. Token refresh (and"
        )
        lines.append(
            "    any rotation) happens host-side against botainer's own login"
        )
        lines.append(
            f"    store — it never touches {_native_phrase}."
        )
        if _one in ("anthropic", "claude"):
            # MEASURED for Claude Code only. There is no verified equivalent
            # for codex, and inventing one would be the same defect in the
            # other direction — so the other families get no billing sentence
            # rather than a plausible guess.
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
        #      not the shared-auth dir (per internal design note DN-040 §2).
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
            "    The design keeps your real provider credentials on the host and"
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
            # `--mode` is not an option of `start` — `botainer start --mode=shared`
            # answers "Error: No such option: --mode". Name the two spellings
            # that exist: the persistent switch, and the one-session override.
            "    runtime today. Switch with `botainer auth use shared`"
        )
        lines.append(
            "    (or `isolated`); `botainer start --auth-mode shared` does it"
        )
        lines.append(
            "    for one session only."
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
            # THIS PARAGRAPH WAS GARBLED, in the block a user reads to decide
            # whether to launch: "— your real" ended mid-sentence, "(visible to
            # the" never closed, and "visible to the agent." repeated the half
            # it had already lost. Rendered from a post-hook spec to see it,
            # because the credential bind is a pre_session contribution and so
            # is absent from the compose-time plan.
            #
            # It states FACTS and POINTS, and renders the bind's ACTUAL mode
            # rather than assuming: in shared mode the mount is rw because the
            # agent must be able to save a refreshed token, which also means it
            # can overwrite the credential every shared-mode project uses.
            _rw = _cred.mode is BindMode.RW
            # WHO SHARES IT. Derived from the spec, not from the mode name: the
            # host-wide store is bound only in shared mode, so its presence is
            # what makes "every project that shares this login" true rather
            # than assumed. The refuting review asked for this scope and it is
            # measurable, so it is stated instead of hedged.
            _host_wide = any(
                "/shared-auth" in b.source for b in spec.mount_plan.binds)
            _scope = ("every project that shares this login runs as"
                      if _host_wide else "this project's sessions run as")
            lines.append("  Credential delivery: MOUNTED INTO THE CONTAINER")
            lines.append(
                f"    Your real credential file is bound at {_cred.target} "
                f"({_cred.mode.value}), so anything"
            )
            # ONE STRING, WRAPPED MECHANICALLY. The review showed why: with the
            # consequence split across two appended lines, deleting the
            # continuation left "…changes the account every" dangling — the
            # exact garble this replaced — and every test still passed. The
            # sentence now exists once and `textwrap` decides the line breaks,
            # so there is no second line for an editor to lose.
            _consequence = (
                "the agent runs can read it"
                + (f" — and overwrite it, which changes the account {_scope}."
                   if _rw else ".")
            )
            lines.extend(textwrap.wrap(
                _consequence, width=74,
                initial_indent="    ", subsequent_indent="    "))
            # WHERE the host-side alternative actually works. `hpc submit`
            # renders this same block (submit.py's consent screen), and the
            # sbatch path REFUSES broker mode at compose — after the broker
            # hook has already started and refreshed, which can rotate the
            # refresh token and log other holders out. So on apptainer this
            # must not print `auth use broker`: following it costs a rotation
            # and still does not launch. (Refuting review, 2026-09-13.)
            if on_sbatch_path:
                lines.append(
                    "    Broker mode keeps the token host-side but the sbatch "
                    "path cannot reach a"
                )
                lines.append(
                    "    login-node broker: use `botainer start --runtime "
                    "apptainer` inside an salloc."
                )
            else:
                lines.append(
                    "    A host-side alternative for some agents: `botainer "
                    "auth use broker`"
                )
                lines.append(
                    "    (needs a broker variant installed for your agent "
                    "family)."
                )
            lines.append(
                "    What each auth mode does and does not keep out: "
                "docs/CAPABILITY-SURFACE.md."
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

    # Report the host directory behind /scratch and any applicable purge
    # policy at launch. The purge note must follow the actual bind source;
    # a cluster profile alone does not establish that this directory is purged.
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
            # deletes this. Announcing a purge that will not happen misleads:
            #
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


def _agent_family_for(spec: SessionSpec) -> str | None:
    """Which vendor's permission vocabulary applies to this session.

    A `SessionSpec` does NOT carry the primary agent name — only
    `plugins_enabled` and the resolved `agent_permissions`. So the family is
    derived from the enabled agent plugin, the same mapping compose uses
    (`composition._agent_family`).

    SORTED, not first-found. `plugins_enabled` order is not a guarantee, and
    #120's guard means exactly one agent plugin is enabled today — so sorting
    costs nothing now and makes the answer deterministic if companions (#172)
    ever enable two. If two families are ever enabled, this returns one of them
    and the banner would be describing the primary's posture for both; that is
    a known limit of a single scalar `agent_permissions`, recorded in the
    permission design rather than papered over here.
    """
    for p in sorted(spec.plugins_enabled):
        if p.startswith("agent-claude"):
            return "anthropic"
        if p.startswith("agent-codex"):
            return "openai"
    return None


def auth_modes_by_family(spec: SessionSpec) -> dict[str, str]:
    """Every enabled agent family and the mode it uses. Order-stable.

    WHY THIS EXISTS, and why `_auth_mode` below must never be used to decide
    what to REASSURE the user about (CRITICAL-1, companion-agents audit).

    `_auth_mode` is FIRST-MATCH across the whole session: one
    `agent-<x>-broker` anywhere in plugins_enabled makes it answer "broker".
    That is fine as a single label for tooling. It is wrong as the basis for a
    consent-gate verdict, because the moment a session runs a COMPANION —
    which is the point of enabling two agent families — the two families can be
    in different modes. A broker-mode primary alongside a mount-mode companion
    answered "broker", and the gate then printed "A compromised agent cannot
    read or exfiltrate your credential" while the companion's real key sat
    bind-mounted and readable in the same container.

    An absolute safety verdict that is true of one credential and false of
    another in the same session is exactly the class of statement this project
    refuses to make at a consent chokepoint.

    Plugin names are `agent-<family>` or `agent-<family>-<mode>`.
    """
    modes: dict[str, str] = {}
    for p in spec.plugins_enabled:
        if not p.startswith("agent-"):
            continue
        rest = p[len("agent-"):]
        for suffix in ("broker", "proxy", "shared"):
            if rest.endswith("-" + suffix):
                modes[rest[: -(len(suffix) + 1)]] = suffix
                break
        else:
            # No mode suffix → the isolated/mount plugin for that family.
            modes.setdefault(rest, "mount")
    return modes


def _auth_mode(spec: SessionSpec) -> str:
    """A single session-level label for tooling output (agent-agnostic).

    FIRST-MATCH, and deliberately so: `render_json` and the HPC submit surface
    want one string. Do NOT use it to decide a reassurance — see
    `auth_modes_by_family` for why, and use that instead.
    """
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
        # nothing had. Consecutive starts with unchanged capabilities must not
        # produce a difference solely because the session ID changed.
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


def _echo_warnings(lines, *, err: bool = False) -> None:
    """Print a warning block, or nothing at all if there is none.

    A credential collision can arise on a routine launch after the first-launch
    confirmation has already been recorded. Print its warning on every path,
    independently of that confirmation gate.
    """
    for line in lines or ():
        if not line:
            click.echo("", err=err)
        elif line.startswith("⚠"):
            click.secho(line, fg="red", bold=True, err=err)
        elif line.startswith("?"):
            click.secho(line, fg="yellow", err=err)
        else:
            click.secho(line, fg="yellow", err=err)


def print_and_maybe_confirm(
    spec: SessionSpec,
    *,
    quiet: bool = False,
    as_json: bool = False,
    pre_authorised: bool = False,
    interactive: bool = True,
    extra_warnings=(),
    on_sbatch_path: bool,
) -> bool:
    """Print summary and (if gated) ask for confirmation.

    Returns True if the launch should proceed, False if user declined.

    `on_sbatch_path` is REQUIRED, with no default, deliberately: it is a fact
    only the caller has (the runtime cannot supply it — `start --runtime
    apptainer` inside an salloc is the same runtime and not the sbatch path),
    and the credential paragraph's host-side advice is WRONG for the other
    answer. A default would make omission silent; a required keyword makes it a
    TypeError. Same discipline as `run_hook(agent_writable_roots=…)`.

    TAKES THE TWO FACTS, NOT ONE MERGED FLAG. This used to take `auto_yes`,
    which callers computed as `args.yes or not interactive` — conflating two
    things with DIFFERENT consequences:

      * `pre_authorised` — the user passed `--yes`. They authorised the grant
        without seeing it, which IS consent, so the fingerprint is recorded.
      * `interactive` — whether there is a terminal to answer a prompt. A
        compute-node job or `hpc submit --mode=here` has none, so the summary
        can only be SHOWN. Being shown something is not consenting to it.

    Both suppress the prompt, so the old single flag looked sufficient. Only
    one of them may record consent. With them merged, a show-only
    `--mode=here` or `attach` called `record_shown` and satisfied the
    first-launch gate for a user who was never asked — so the NEXT interactive
    `hpc submit` skipped the confirmation entirely, and a FAILED here/attach
    burned the gate for a session that never ran.

    A previous change saw half of this: the message below says "either --yes
    was passed, or this is a non-interactive session", because claiming
    "--yes passed" was wrong. It fixed what the user is TOLD and left what the
    code DOES. Passing the facts separately makes the conflation
    unrepresentable rather than merely corrected.

    `extra_warnings` is a pre-rendered block from the caller (start.py passes
    the credential-holder census, #215). It is printed on all three paths —
    JSON, gated, and ungated — because the ungated path is the daily one. In
    JSON mode it goes to STDERR so stdout stays parseable.

    Implementation-review HIGH 3: `--quiet` MUST NOT bypass the
    confirmation gate. It only suppresses the *informational* one-line
    summary on subsequent launches. The gate (first launch + after
    image change) is a security-critical user check; bypassing it via
    --quiet alone would let any script silently authorise a fresh
    project. To bypass the gate non-interactively, pass `--yes`
    (acknowledging you understand the capability grant).
    """
    gate = determine_gate(spec)
    # Suppress the prompt for either reason; record consent for only one.
    auto_yes = pre_authorised or not interactive

    # JSON mode: emit structured summary, then enforce gate the same way.
    if as_json:
        click.echo(render_json(spec))
        _echo_warnings(extra_warnings, err=True)
        if gate.confirm and not auto_yes:
            click.secho(
                "refused: first launch (or image change) of this project; "
                "pass --yes to confirm non-interactively, or run interactively first.",
                fg="red",
                err=True,
            )
            return False
        if gate.confirm and pre_authorised:
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
            click.echo(render_multiline(spec, on_sbatch_path=on_sbatch_path))
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
            _echo_warnings(extra_warnings)
            if pre_authorised:
                record_shown(spec)
            return True
        _echo_warnings(extra_warnings)
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
    # NOT gated on `quiet`. --quiet exists to drop the informational one-liner
    # on a project you launch every day; a warning that another session is
    # about to invalidate your login is not informational, and the daily
    # launch is exactly when it happens.
    _echo_warnings(extra_warnings)
    return True
