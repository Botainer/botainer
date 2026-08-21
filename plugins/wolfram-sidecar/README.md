# wolfram-sidecar — host-side wolframscript proxy

⚠️ **LESS SAFE BY DESIGN** (task #61): this plugin runs wolframscript
**on the host as the user** in response to in-container agent requests.
The three sandbox layers (string filter, kernel-init, sandbox-exec)
reduce the blast radius but do not eliminate the trust transfer.
Compare to in-container alternatives: NO host fs access, NO host
process spawn, NO host network capability. The reason this plugin
exists in spite of that trade is that Wolfram licensing forces
host-only execution for many license types.

If you don't need wolframscript, DO NOT enable this plugin. If you
do enable it, treat the in-container agent as having read/write/exec
on your host wolframscript installation and anything wolframscript
itself can reach (its kernel cache, ~/.WolframEngine, etc.).

Lets in-container agents run Wolfram language code by forwarding
requests to **wolframscript on the host**, with three layers of
sandbox protection. Mac-only by default (uses `sandbox-exec`); a
Linux opt-in path runs with reduced isolation.

## How it differs from v0.0.x's wolfram-proxy

| | v0.0.x | v0.1.x (this plugin) |
|---|---|---|
| Transport | TCP `127.0.0.1:51839` | **unix-domain socket** bound into the container |
| Container reach | `host.docker.internal` (Docker), N/A on Apptainer | bind-mount; works on both runtimes |
| Config required | `host_ports: [51839]` or `network: full` | nothing — the socket bind is enough |
| Lifecycle | one shared host daemon, started by hand | **per-session** proxy; spawned by pre_session hook, killed by post_session |
| Auth | per-launch token mounted ro into container | per-session token in the session scratch, bound ro at `/run/wolfram-token` |
| Sandbox layers | string filter + kernel-init + sandbox-exec | unchanged — all three layers preserved verbatim |

The security model is identical to v0.0.x's. The transport change
just removes two long-standing operational requirements (the
`host_ports` carve-out and the dependency on
`host.docker.internal`).

## Setup

### macOS (recommended path)

1. Install Mathematica or wolframscript on your Mac and **activate**
   it (paid Wolfram One, free Wolfram Engine for Developers, etc.).
2. In your project's `.botainer/config.yaml`, enable the plugin:
   ```yaml
   plugins_enabled:
     - agent-claude     # or whichever agent
     - git
     - wolfram-sidecar
   ```
3. (Optional) tune the plugin via `plugins.wolfram-sidecar`:
   ```yaml
   plugins:
     wolfram-sidecar:
       wolframscript_path: wolframscript     # or absolute path
       timeout_seconds: 300                  # global cap
       max_request_bytes: 1048576            # 1 MiB
       require_sandbox: true                 # sandbox-exec required
   ```
4. Start the session:
   ```sh
   botainer start
   ```
   The pre_session hook spawns the proxy in the background, mints a
   per-launch token, and binds three things into the container:
   - `<session>/wolfram-proxy.sock` → `/run/wolfram-proxy.sock`
   - `<session>/wolfram-token` → `/run/wolfram-token` (ro)
   - `<plugin>/client.py` → `/usr/local/bin/wolframscript` (ro)
5. Inside the container, use `wolframscript` normally:
   ```sh
   wolframscript -code "Integrate[x^2, x]"
   # => x^3/3
   wolframscript --proxy-timeout 10 -code "Factor[x^4 - 1]"
   wolframscript -code "NIntegrate[Sin[x^2], {x, 0, 100}]"
   ```

When the session ends (`botainer stop` or container exit), the
post_session hook SIGTERMs the proxy and unlinks the socket + token
files.

### Linux (opt-in; reduced isolation)

`sandbox-exec` is macOS-only, so on Linux the kernel-init Wolfram
sandbox is the only OS-layer defense (plus the string filter). To
opt in:

```yaml
plugins:
  wolfram-sidecar:
    require_sandbox: false
```

The proxy will start with a loud warning in stderr noting that only
the kernel sandbox is active. **Do not use this on a multi-tenant
host with sensitive data outside the container.**

## Sandbox layers (defense in depth)

### Layer 1 — string filter

Before invoking wolframscript, the proxy refuses requests whose argv
contains any of an extensive denylist:

- Process / shell: `Run[`, `RunProcess[`, `StartProcess[`,
  `SystemOpen[`, `SystemShell[`, plus the `@`-prefix and space-form
  bypasses (`Run @ "..."`, `Run [`).
- Loader / library: `Install[`, `LibraryFunctionLoad[`, `LibraryLink`.
- Network: `URLFetch[`, `URLRead[`, `Import["http`, `SendMail[`,
  `CloudDeploy[`, …
- File write: `Export[`, `Put[`, `DeleteFile[`, `OpenWrite[`,
  `WriteString[`, …
- File read: `Get[`, `ReadString[`, `FilePrint[`, `Get @`, `Get [`.
- Dynamic eval (bypass vectors): `ToExpression[`, `Symbol[`,
  `FromCharacterCode[`, `StringJoin[`, `Names[`.
- External evaluators: `JLink`, `NETLink`, `ExternalEvaluate[`,
  `StartExternalSession[`.

`-file <path>` is refused outright (path input is unbounded; the
filter can't catch what's inside the file).

This is best-effort — Wolfram's metaprogramming surface is large
enough that no static denylist defeats a determined adversary. It's
a fast first check, not the final defense.

### Layer 2 — kernel-init sandbox (`sandbox/wolfram-sandbox-init.wl`)

A Wolfram script that runs BEFORE user code in every `-code`
invocation. It `Unprotect`s, redefines as a refusal that returns
`$Failed`, then `Protect`s + `Locked`s every dangerous function:
`Run`, `RunProcess`, `StartProcess`, `SystemOpen`, `Export`, `Put`,
`OpenWrite`, `DeleteFile`, `URLFetch`, `SendMail`, `CloudDeploy`,
`ExternalEvaluate`, plus the file-read functions.

This defeats `ToExpression["Run[\"id\"]"]`-style bypasses that
defeat the Layer-1 string filter, because the resulting `Run`
symbol is the locked, refusal-returning version.

### Layer 3 — macOS sandbox-exec (`sandbox/wolfram-sandbox.sb`)

The wolframscript process runs inside a sandbox profile that:

**Denies read** of: `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.claude`,
`~/.anthropic`, `~/Documents`, `~/Desktop`, `~/Downloads`,
`~/Dropbox` (and corporate-named Dropbox folders), `~/Library/Keychains`,
browser data (Chrome, Firefox, Safari, Arc), `~/Library/Mail`,
`~/Library/Messages`, `1Password`, `/etc/sudoers`, `/etc/master.passwd`,
`/etc/ssh`, `/var/db/dslocal`, `/Volumes`, `/Network`, shell history
files, calendars, contacts, app-managed token dirs (Slack, VS Code,
Notion, Anthropic, OpenAI).

**Allows write** to: `/tmp`, `/private/var/tmp`, `/dev`,
`~/Library/Mathematica`, `~/Library/Wolfram`,
`~/Library/Caches/Wolfram`, and Wolfram-prefixed log dirs.
**Nothing else** — by construction this blocks every macOS
persistence vector (LaunchAgents, LaunchDaemons, `/Applications`,
`~/.zshrc`, login scripts) and every user-data write target
(Documents, Desktop, Dropbox, source repos).

The profile is the one shipped in v0.0.12+ (reviewed by a security
SME); copied verbatim into `sandbox/wolfram-sandbox.sb`.

## Wire protocol

Request (JSON, sent over unix socket):
```json
{
  "args": ["-code", "Integrate[x^2, x]"],
  "auth": "<per-launch-token-from-/run/wolfram-token>",
  "timeout": 60
}
```

Response (JSON):
```json
{"returncode": 0, "stdout": "x^3/3\n", "stderr": ""}
```

`timeout` is optional; if absent the proxy uses the global
`timeout_seconds` config. If present, it's capped at the global
limit (so an in-container client can't ask for hours).

The in-container `wolframscript` (binding `client.py` at
`/usr/local/bin/wolframscript`) handles the protocol transparently
— users just run `wolframscript -code "..."`.

## Threat model

**Defends against:**
- Compromised in-container agent reading host files via Wolfram's
  filesystem APIs. (Layer 2 + Layer 3.)
- Compromised in-container agent exfiltrating data via
  `URLFetch[]`, `SendMail[]`, `CloudDeploy[]`. (Layer 1 + Layer 2.)
- Compromised in-container agent persisting on the host via
  LaunchAgents, `.zshrc`, etc. (Layer 3 write allowlist.)
- Other host processes reaching the proxy: the socket is mode 0600
  in a session-scratch dir, and the per-launch token must match.

**Does NOT defend against:**
- A compromised host process (the proxy and wolframscript run as
  your user). The container is bounded; the host isn't.
- Side-channel leaks via prompt content (the agent can encode data
  in math expressions that the proxy faithfully forwards).
- A bug in the agent image that bypasses `wolframscript` and
  talks to the socket directly — possible, but no different from
  any other bind-mount-into-container plugin.
- Sufficiently clever Wolfram metaprogramming. The kernel-init
  defense covers known bypass classes; new ones may exist.

## Configuration reference

| Key | Default | Effect |
|---|---|---|
| `wolframscript_path` | `wolframscript` | Path to binary on host |
| `timeout_seconds` | `300` | Global per-request cap |
| `max_request_bytes` | `1048576` | Refuse-above-this body size |
| `require_sandbox` | `true` | Refuse to start without sandbox-exec |

## Files in this plugin

```
plugins/wolfram-sidecar/
├── botainer-plugin.yaml         # manifest
├── README.md                    # this file
├── proxy.py                     # host-side daemon (unix-socket)
├── client.py                    # in-container wolframscript shim
├── sandbox/
│   ├── wolfram-sandbox.sb       # macOS sandbox-exec profile
│   └── wolfram-sandbox-init.wl  # kernel-level Wolfram sandbox
└── hooks/
    ├── pre_session.py           # spawn proxy, mint token, contribute binds
    └── post_session.py          # kill proxy, unlink socket + token
```

## See also

- the v0.0.x host proxy (development repository only) — the v0.0.x
  source this plugin ports from. Same security model, TCP transport.
- `docs/CAPABILITY-SURFACE.md` — the plugin capability contract (wolfram-sidecar
  was the worked example for the host_helper pattern).
- the v0.0.x README §"Wolframscript" (development repository only) — v0.0.x user docs;
  the threat model description applies to v0.1.x verbatim.
