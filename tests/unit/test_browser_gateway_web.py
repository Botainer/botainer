"""The vendored gateway web assets (Track B) — structural invariants.

These assertions encode WHY the laptop-served page is trustworthy: pinned
upstream noVNC (pure JS, no WASM), a minimal custom page with no inline
script/style (so `strict_csp()` needs no nonces), no runtime JSON fetch, and
no clipboard wiring. If any of these breaks, the CSP in gateway.py::strict_csp
is no longer known-correct — re-derive it (see gateway_web/PROVENANCE.md)."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "plugins/browser/gateway_web"
_GW = Path(__file__).resolve().parents[2] / "plugins/browser/commands/gateway.py"
_spec = importlib.util.spec_from_file_location("browser_gateway", _GW)
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)  # type: ignore[union-attr]


def test_vendored_tree_complete():
    assert (_WEB / "vnc.html").is_file()
    assert (_WEB / "app.js").is_file()
    assert (_WEB / "app.css").is_file()
    assert (_WEB / "PROVENANCE.md").is_file()
    assert (_WEB / "novnc/core/rfb.js").is_file()
    assert (_WEB / "novnc/vendor/pako/lib/zlib/inflate.js").is_file()
    assert (_WEB / "novnc/LICENSE.txt").is_file()      # MPL-2.0 rides along


def test_novnc_is_pure_js_no_wasm():
    # Upstream noVNC decodes in pure JS (browser-sandboxed). A .wasm appearing
    # here means the bundle changed character (e.g. KasmVNC fork) and the CSP
    # (img-src data:, no blob:) must be re-derived — refuse via this test.
    files = [p for p in (_WEB / "novnc").rglob("*") if p.is_file()]
    assert files, "vendored novnc tree is empty?"
    non_js = {p.suffix for p in files} - {".js", ".txt", ".md", ""}
    assert not non_js, f"unexpected file types in vendored novnc: {non_js}"
    assert not [p for p in files if p.suffix == ".wasm"]


def test_page_has_no_inline_script_or_style():
    html = (_WEB / "vnc.html").read_text(encoding="utf-8")
    # Every <script> must be src=-only (no body), so script-src 'self' suffices.
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", html, re.S):
        assert 'src="' in m.group(0), "script tag without src"
        assert m.group(1).strip() == "", "inline script body found"
    assert "<style" not in html, "inline <style> block found"
    assert not re.search(r'\bstyle="', html), "inline style attribute found"
    assert not re.search(r"\bon[a-z]+=", html), "inline event handler found"


def test_page_fetches_no_defaults_json():
    # The stock noVNC UI fetches defaults.json/mandatory.json at runtime; the
    # minimal page must not (frozen behavior, CSP needs no connect-src http).
    for name in ("vnc.html", "app.js"):
        text = (_WEB / name).read_text(encoding="utf-8")
        assert "defaults.json" not in text
        assert "mandatory.json" not in text
        assert "fetch(" not in text
        assert "XMLHttpRequest" not in text


def test_app_js_wires_no_clipboard():
    # B4 default: NO clipboard in either direction. The exfiltration direction
    # (ServerCutText -> user clipboard) must stay unwired in the trusted page.
    js = (_WEB / "app.js").read_text(encoding="utf-8")
    assert "navigator.clipboard" not in js
    assert "addEventListener('clipboard'" not in js
    assert 'addEventListener("clipboard"' not in js


def test_app_js_imports_only_the_vendored_client():
    js = (_WEB / "app.js").read_text(encoding="utf-8")
    imports = re.findall(r"^import .* from '(.*)';", js, re.M)
    assert imports == ["./novnc/core/rfb.js"]


def test_pin_matches_provenance():
    prov = (_WEB / "PROVENANCE.md").read_text(encoding="utf-8")
    assert gw.NOVNC_PIN in prov
    assert gw.NOVNC_GITHUB_TARBALL_SHA256 in prov
    assert gw.NOVNC_NPM_INTEGRITY.replace("\n", "") in prov.replace("\n", " ").replace(" ", "")
