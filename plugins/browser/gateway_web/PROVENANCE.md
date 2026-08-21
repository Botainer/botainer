# Vendored noVNC — provenance + verification (Track B gateway)

The `novnc/` tree here is the TRUSTED viewer client the laptop-side gateway
serves to the user's browser (`botainer plugin browser gateway`). It is vendored
— frozen in git, served read-only — precisely so the client code the user's
browser runs never depends on a network fetch or on anything the untrusted
container serves. Do not edit files under `novnc/`; re-vendor instead (below).

## What this is

- **Project:** noVNC — https://github.com/novnc/noVNC (MPL-2.0; `novnc/LICENSE.txt`)
- **Version:** 1.7.0 (upstream stable, released 2026-04-28)
- **Vendored subset:** `core/` (the RFB client, ES modules) + `vendor/pako/`
  (zlib, imported by `core/inflator.js`/`deflator.js`) + `LICENSE.txt` + `AUTHORS`.
  That is the complete import closure of `core/rfb.js` — verified by grepping
  every `import` in `core/` (only `./…` and `../vendor/pako/…` targets).
- **Deliberately NOT vendored:** upstream `app/` UI, `vnc.html`, `vnc_lite.html`,
  `defaults.json`, `mandatory.json`, `tests/`, `utils/`, `po/`. The gateway serves
  its own minimal page (`../vnc.html` + `../app.js`) with no inline script and no
  runtime JSON fetch, so the strict CSP needs no nonces and the page is
  iframe-sandbox-friendly.

## Verification performed at vendor time (2026-07-14, host session)

Two independent distribution channels were fetched and cross-checked:

1. GitHub tag tarball `https://github.com/novnc/noVNC/archive/refs/tags/v1.7.0.tar.gz`
   - SHA-256: `b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6`
   - (NB GitHub regenerates tag archives on the fly; the *compression* wrapper is
     not contractually byte-stable, so treat this hash as a record of what was
     fetched, not as a re-download oracle. The npm integrity below IS immutable.)
2. npm tarball `https://registry.npmjs.org/@novnc/novnc/-/novnc-1.7.0.tgz`
   - registry `dist.integrity` (immutable):
     `sha512-ucEJOx4T2avIRCleodk7YobZj5O2Ga2AeLfQ69A/yjG9HHba2+PDgwSkN3FttrmG+70ZGx21sElNFouK13RzyA==`
   - recomputed locally from the downloaded bytes: MATCHED.
- `diff -r` of `core/` and `vendor/` between the two channels: **identical**.
- The tree vendored here is the GitHub-tag copy of that identical content.

## Security audit notes (drive the gateway CSP — `gateway.strict_csp()`)

- **Pure-JS decoders, no WASM anywhere** (`core/decoders/*.js`; the h264 decoder
  uses the browser-native WebCodecs API). This is UPSTREAM noVNC, not the
  KasmVNC WASM fork → CSP `img-src 'self' data:` (data:, NOT blob:) is correct.
- Grep-audited `core/` + `vendor/`: no `eval(`, no `new Function`, no
  `new Worker`, no `importScripts`, no `URL.createObjectURL`, no `blob:`.
  Cursors render via `canvas.toDataURL()` → CSS `cursor: url(data:image/…)`
  (hence `img-src data:`); styles are set via CSSOM properties (not inline
  `style=` attributes), so `style-src 'self'` holds.
- Version ≥ 0.6.2 closes CVE-2017-18635 (the only in-threat-model
  noVNC-client CVE — DOM-XSS via innerHTML).

## Re-vendor procedure (version bump)

1. Pick the new upstream stable tag; fetch BOTH the GitHub tag tarball and the
   npm tarball; verify the npm `dist.integrity`; `diff -r` `core/` + `vendor/`
   between channels — refuse on any mismatch.
2. Re-run the security grep audit above; re-confirm no `.wasm`/non-JS files
   (`find core vendor -type f ! -name '*.js'` should list only pako's
   LICENSE/README) — if decoders stop being pure JS, the CSP in
   `plugins/browser/commands/gateway.py::strict_csp()` must be re-derived.
3. Replace `novnc/core` + `novnc/vendor/pako` + `LICENSE.txt` + `AUTHORS`
   wholesale; update `NOVNC_PIN` + the hashes in
   `plugins/browser/commands/gateway.py` and THIS file; regen the plugin
   trust-lock; run `tests/unit/test_browser_gateway_web.py`.
