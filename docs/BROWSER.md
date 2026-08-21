# Giving the agent a web browser

botainer can give the agent a **real web browser** — open pages, click, type,
read the page as text, take screenshots. Optionally you can watch it live and
take over, which is how you log a site in for the agent.

> **How far this has been tested.** The headless browser and the viewer are
> exercised by the test suite and the viewer has been run end-to-end on docker.
> **Neither has been run on a real cluster.** On HPC, treat the first run as a
> shakedown.

## Do you need this?

If a session has internet, Claude can already **fetch** a page without this
plugin — say *"go read this page"* and it will pull the text over HTTP. For a
one-off read, that is enough. What it cannot do without the plugin is drive a
real browser. The cage has no root and no `apt`, and under apptainer `HOME` is
unwritable, so a browser cannot be installed at run time — it has to be in the
image. Anything needing JavaScript, a login, or a click needs this plugin and an
image build.

The `browser` **plugin** adds three things over "just ask":

1. **Real browser tools** (navigate / click / type / read-as-structured-text)
   instead of Claude hand-writing throwaway scripts — more reliable for multi-step
   web tasks (forms, logins, flows).
2. **A visible, opt-in capability** — you (and the session's capability summary)
   know the agent has a browser, and you control it. Without the plugin, ad-hoc
   browsing just happens, invisibly.
3. **The container quirks are pre-solved** (the no-sandbox flag the cage needs, a
   persistent Chromium cache in `/packages`).

Rule of thumb: casual page-reading → just ask. Repeated / reliable / interactive
web work → enable the plugin.

## Turn it on

The plugin is **off by default**. In your project's `.botainer/config.yaml`:

```yaml
network:
  mode: internet          # a browser needs internet
plugins_enabled:
  - agent-claude
  - browser
```

Then:
1. `botainer setup` (once, to install the bundled plugin), and
2. **build the agent image** — `botainer image build agent-claude` (add
   `--runtime apptainer` on HPC). Chromium, its system libraries and the MCP
   server are baked in at build time. Browser support adds ~400 MB and a few
   minutes to the build. If you already built the image before enabling the
   plugin, build it again: the browser stack is not added to an existing image.
3. start a session.

If the `browser` tool is missing from the agent's toolset, it is almost always
step 2 — the image was built without browser support.

## Using it (what to say to Claude)

- *"Open example.com and tell me the page title."*
- *"Go to <url>, click the 'Sign in' link, and read what's on the next page."*
- *"Take a screenshot of the current page."* ← this is how you **see** what it sees.

If the agent says the `browser` tool is missing, the image was built without
browser support — build it again (`botainer image build agent-claude`). Do NOT
ask the agent to `npx playwright install` / `pip install playwright` /
apt-install a browser: the cage has no root and no `apt`, and on apptainer HOME
is unwritable, so those fail by design. Everything comes from the image.

## Logging a site in for the agent

Two ways, pick whichever fits — **both are supported**:

1. **Manual login via the live viewer** (below): turn on `viewer: true`, open the
   window, and log in yourself while the agent waits. Simple; you see exactly what's
   happening. (This is the classic flow; it stays supported.)
2. **Credential handoff** (`botainer plugin browser login <url>`): log in on **your
   own machine** with `npx playwright codegen --save-storage=.botainer/browser-auth.json <url>`,
   then set `plugins.browser.storage_state: .botainer/browser-auth.json` — the
   agent's browser starts already logged in. **Your password never enters the
   container** (an in-container login is keyloggable), and you don't need the viewer.

Run `botainer plugin browser login <url>` for the exact steps. Keep the saved
session file **secret** (it's a live login) and **out of git** (add it to
`.gitignore`). The agent ends up with the logged-in session either way — handoff
just keeps your *password* off the untrusted container. Some sites bind a session to
the browser (device/DPoP/mTLS) and won't transfer; if so, use the viewer to log in
manually.

## Seeing the browser

By default it runs **headless — there is no window.** Two ways to see it:

- **Screenshots (always available):** ask Claude to take a screenshot; you get the
  image of exactly what the page looks like. On-demand, not live video. Good enough
  for "let me see what it's looking at."
- **The live watch-and-log-in viewer (opt-in):** a real window you can WATCH and
  CLICK — e.g. to log into a site yourself, then hand control back to the agent.
  You and the agent drive the SAME browser.

> ⚠️ **A file the browser produced is untrusted input to YOUR machine.**
> Screenshots, saved pages, PDFs and downloads are bytes chosen by a web page the
> container visited, written by a container botainer treats as hostile. Opening
> one on your host runs your host's image decoder, PDF reader or browser over
> attacker-influenced content — the same trust inversion as the viewer below,
> just quieter, because nothing prints a warning when you double-click a
> screenshot. This is not a reason never to look at them; it is a reason to open
> them the way you would open an email attachment from a stranger: in a viewer
> you would not mind losing, never by handing the path to a tool that executes
> what it reads.

### Turning on the live viewer

Add `viewer: true` under the plugin in `.botainer/config.yaml`:

```yaml
network:
  mode: internet
plugins_enabled:
  - agent-claude
  - browser
plugins:
  browser:
    viewer: true          # start the watchable viewer for this session
```

There is **no extra image**. The screen-sharing tools (Xvfb, x11vnc, noVNC,
websockify) are in the agent image alongside Chromium.

Install the gateway extra once on the machine you watch from, then start it:

```bash
pip install 'botainer[gateway]'
botainer plugin browser gateway
```

**Keep that command running.** The viewer page is served by your own machine, by
this process — the page loads only while it is up. It opens a throwaway browser
window and prints the URL so you can reopen it in any private window meanwhile.
Ctrl-C, and the browser shows "unable to connect": expected, not a bug.

The screen is **blank until the agent opens its first page** — Chromium starts
on the first browser action, so an empty canvas at first is normal.

On **HPC** it prints an `ssh -L` line to run on your laptop (optionally via your
login node with `-J`). There is no open port on the compute node; access goes
through a `0700` socket reachable only via your own SSH forward.

### What the viewer does and does not protect

The container runs a VNC server and nothing else — an authenticated pixel and
input stream, on a loopback publish (docker) or a private `0700` socket (HPC).
**Your machine serves the viewer page**: a vendored, version-pinned noVNC under
a strict Content-Security-Policy, with a per-run token and a throwaway browser
profile. So your browser never executes code the container chose. Clipboard is
disabled both ways.

What remains: **the pixels are still the agent's browser.** A page it visited
can try to phish you visually — a fake login box on your screen is still a fake
login box. Treat what you see as untrusted content, because it is.

One caveat on the per-run token: it rides the WebSocket path. On docker
websockify enforces it (TokenFile); on HPC the unix-socket transport has no
TokenFile, so there the token is **advisory** and the `0700` socket permissions
are the actual gate. Either way the transport is the first line of defence:
loopback-only on a laptop, a node-local socket behind your SSH forward on a
cluster. Keep that forward private. The HPC path is wired but has not been run
on a real cluster.

### `viewer_mode: legacy` — deprecated, and why

Before the gateway existed, the container served the noVNC page itself and your
browser loaded it. That is a **trust inversion**: your browser runs code chosen
by the sandbox you are supervising, so nothing configured server-side is a
boundary, and a compromised agent could go after your clipboard, your
`127.0.0.1` services, or your browser itself.

It is still reachable as `viewer_mode: legacy`, purely as an escape hatch if the
gateway will not run for you. **It is deprecated and will be removed.** If you
use it, treat it as a trust decision: a throwaway browser profile, decline every
clipboard and file prompt, never a native VNC client. `botainer plugin browser
watch` prints the warning each time.

If the viewer stack can't come up (e.g. the agent image was built without it),
the session **refuses to start** rather than silently downgrading you to an
unsupervised headless agent — with a message telling you to build the agent image
again or set `viewer: false`.


## Notes for the agent (also in AGENT_HINTS)

- The browser is headless; the human can't see it unless you screenshot.
- Prefer reading the page as text / accessibility snapshot over screenshots —
  cheaper and usually enough. Screenshot when the human asks to *see* it.
- It runs inside the container cage with `--no-sandbox` (Chrome's own sandbox
  can't start there). Don't visit untrusted pages you wouldn't want executing in
  this container.
- If the live viewer is on, a human may be watching and can take over. On a login
  wall or any human-only step, **don't guess credentials** — ask the human to run
  `botainer plugin browser watch`, log in, and hand back.
