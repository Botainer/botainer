# agent-claude-proxy — host-side credential proxy for Claude Code

> **⚠ NOT FUNCTIONAL at v0.1.0 — a proxy session refuses to start.**
> This plugin's design is sound, but it does not run yet on ANY runtime:
> - **Blocker (T0-3 / #59):** the proxy hands the agent an ephemeral
>   token via `ANTHROPIC_API_KEY`, but botainer's credential-leak guard
>   (`core/credential_leak_check`) refuses *any* pre_session env var named
>   `ANTHROPIC_API_KEY` on principle — so a plugin can't smuggle a real
>   key to the agent. The proxy's mechanism and that guard directly
>   contradict, so composition refuses the session. The hook now fails
>   fast and says so (set `BOTAINER_PROXY_EXPERIMENTAL=1` to
>   force the spawn path for development — you must ALSO patch the leak
>   check to get anywhere).
> - Even once unblocked: **no refresh-on-401** (token expiry mid-session
>   needs re-`auth login`), and **OAuth credential files**
>   (`{"claudeAiOauth": {...}}`, what `claude /login` writes for Claude
>   Pro/Max) are not parsed — only **API-key** files.
>
> **Use Mode A (`botainer auth use isolated`) or Mode B (`botainer auth
> use shared`) for all real work today.** The work to make proxy
> functional (a scoped leak-check exemption for the proxy-minted token +
> OAuth refresh-on-401) is tracked in
> botainer's internal design notes.
>
> **If you're on AWS Bedrock or Google Vertex AI** for Claude access,
> our proxy is **not needed** — Bedrock/Vertex use IAM credentials
> at the TLS layer that aren't in the container by default. Just
> set `ANTHROPIC_BASE_URL` (or `CLAUDE_CODE_USE_BEDROCK=1` /
> `ANTHROPIC_VERTEX_PROJECT_ID`) in your project env and don't
> mount `~/.aws` / `~/.config/gcloud`. See
> the Bedrock/Vertex note in the top-level README for the full picture.

Keeps your real Anthropic API key on the host. The container only sees
an ephemeral session token. If the container is compromised mid-session,
the attacker gets a useless token, not your real key.

## Setup

Add to `.botainer/config.yaml`:

```yaml
plugins_enabled:
  - agent-claude
  - agent-claude-proxy        # ← enables proxy mode

plugins:
  agent-claude-proxy:
    upstream: "https://api.anthropic.com"
    max_request_size_bytes: 1048576    # 1 MiB
    rate_limit_rps: 60
    redact_request_bodies: false       # keep request bodies in audit log
```

When enabled:

- agent-claude's pre_session credential bind is **skipped** (mutually
  exclusive with mount mode).
- A host-side proxy listens on a unix socket at
  `${state_dir}/sessions/<sid>/anthropic-proxy.sock`.
- The container sees `ANTHROPIC_API_KEY=<ephemeral-token>` and
  `ANTHROPIC_BASE_URL=http+unix:///run/anthropic-proxy.sock`.
- Every request is logged to `proxy-audit.jsonl` (mode 0600).

## Threat model

- **Compromised container can't exfil the API key.** The container sees
  only the ephemeral token. The token is validated by the proxy with
  `secrets.compare_digest` (constant-time).
- **Stale shell env vars can't redirect the proxy.** All proxy env
  vars come from the plugin config; `BOTAINER_PROXY_UPSTREAM=...` in
  your shell is ignored.
- **Upstream URL is allowlisted.** Only `api.anthropic.com` (HTTPS),
  `localhost`, `127.0.0.1`, `::1` (testing). Refuses attacker-controlled
  hosts and the AWS metadata service.
- **Per-request size + rate-limit caps** prevent body-bombing and
  request flooding.

## What it doesn't protect against

- A compromised proxy process on the host (it has the real key).
  Mitigation: run as your user only; no setuid; small attack surface.
- A user-tampered installed plugin tree (trust verification warns at
  start; refuses are a v0.1.x hardening).
- Side-channel leaks via prompt content. The agent can still encode
  data into prompts, which the proxy faithfully forwards.

## Verifying it's active

```sh
botainer inspect | grep "Auth mode"
# Expected: "Auth mode: PROXY — your real Anthropic credentials stay on the host."
```

Or `botainer doctor --auth-only` shows the current auth mode + any
issues.

See DN-034 for the full design.
