# Third-party licenses

botainer's own source is Apache-2.0 (see `LICENSE`, and `NOTICE` for the
copyright). A botainer **distribution is not purely Apache-2.0**: it bundles third-party code under other licenses. This file is the
notice that goes with it.

Two kinds of third-party code are distinguished below, because the obligations
differ:

- **Bundled** — the source is copied into this repository and ships inside the
  sdist, the wheel and the `build-distrib.sh` tarball. Its license travels with
  the distribution and binds anyone who redistributes botainer.
- **Dependencies** — named in `pyproject.toml`, fetched by `pip` at install
  time. Not redistributed by botainer; listed here for your awareness only.

---

## Bundled

### noVNC — Mozilla Public License 2.0

| | |
|---|---|
| Upstream | https://github.com/novnc/noVNC |
| Version | 1.7.0 (pinned) |
| Location | `plugins/browser/gateway_web/novnc/core/**/*.js` |
| License | MPL-2.0 — full text at `licenses/MPL-2.0.txt`; upstream's own notice at `plugins/browser/gateway_web/novnc/LICENSE.txt`; authors at `.../novnc/AUTHORS` |
| Modified by botainer? | **No.** The tree is vendored byte-for-byte from upstream. |

This is the VNC client that botainer's laptop-side gateway
(`botainer plugin browser gateway`) serves to your browser. It is vendored — not
fetched at runtime — so the client code your browser executes never depends on a
network fetch or on anything the untrusted container serves. Provenance, the
verification performed at vendor time, and the re-vendor procedure are in
`plugins/browser/gateway_web/PROVENANCE.md`.

MPL-2.0 is a **file-level** copyleft: it covers the noVNC files themselves. It
does not extend to botainer's own Apache-2.0 source that sits alongside them.
If you modify any file under `novnc/`, MPL-2.0 §3 requires you to make that
modified file available under MPL-2.0 — which is a second reason the vendoring
procedure says *re-vendor, do not edit*.

Vendored subset: `core/` (the RFB client) plus `vendor/pako/`, which is the
complete import closure of `core/rfb.js`. Upstream's `app/` UI, HTML, CSS, fonts
and images — the parts under BSD-2-Clause, OFL-1.1 and CC-BY-SA — are **not**
vendored; botainer serves its own minimal page (`gateway_web/vnc.html` +
`app.js`, MIT, botainer's own). So the only upstream licenses that actually
apply to what ships are MPL-2.0 and, for pako, MIT.

### pako — MIT

| | |
|---|---|
| Upstream | https://github.com/nodeca/pako |
| Location | `plugins/browser/gateway_web/novnc/vendor/pako/` |
| License | MIT — text at `plugins/browser/gateway_web/novnc/vendor/pako/LICENSE` |
| Copyright | (C) 2014-2016 by Vitaly Puzrin and Andrei Tuputcyn |

Ships as part of the noVNC vendored tree (noVNC's zlib inflate/deflate),
under its own MIT license rather than noVNC's MPL-2.0.

---

## Dependencies (installed by pip, not redistributed)

Required:

| Package | License |
|---|---|
| pydantic | MIT |
| PyYAML | MIT |
| jsonschema | MIT |
| click | BSD-3-Clause |

Optional extras:

| Package | Extra | License |
|---|---|---|
| websockify | `botainer[gateway]` | LGPL-3.0 (as declared upstream) |
| pytest, hypothesis, mypy, ruff | `botainer[dev]` | MIT / MPL-2.0 (hypothesis) / MIT / MIT |

**websockify** deserves a note because it is LGPL. It is *not* vendored: pip
installs it only if you ask for the `gateway` extra, and botainer runs it as a
**separate process** (`subprocess.Popen`), never as a linked or imported
library — see `plugins/browser/commands/gateway.py`. LGPL-3.0's relinking
obligation attaches to combined works; spawning a separate program is not one.
botainer's own license is unaffected.

---

## Container images are not covered here

`botainer image build` builds Docker/Apptainer images from the recipes in
`plugins/*/`. Those recipes install a base OS, Node.js, `uv`, Chromium, the
agent CLIs and so on **on your machine, at build time**, from their own
upstream sources. botainer does not redistribute any of it, and this file makes
no claim about those licenses. If you redistribute a built image, its contents
are yours to audit.

---

*If you spot an attribution error here, it is a bug — please report it. Getting
this wrong harms the upstream authors, which is the opposite of the intent.*
