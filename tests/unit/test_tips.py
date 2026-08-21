"""Unit tests for the user-facing tips system (botainer/tips.py) + the manifest
`contributes.user_tips` field."""
from __future__ import annotations

from pathlib import Path

import pytest

from botainer import tips as tips_mod
from botainer.tips import Tip

_ROOT = Path(__file__).resolve().parents[2]

# Drift tripwire (Fable/user): every core tip is categorized here so
# ADDING or materially changing one forces a conscious drift decision. "usage" =
# discoverability/how-to (no behavior claim to drift). "factual" = asserts botainer
# behavior → MUST also have a claims-vs-impl.tsv row (a `botainer/tips.py` line) so
# the claim can't silently drift from the code (how "/home doesn't persist" shipped
# wrong before). Keys are distinctive substrings; each must match exactly one tip.
_CORE_TIP_DRIFT = {
    "See all tips": "usage",
    "doctor` checks your host": "usage",
    "inspect` previews a session before you start it": "factual",
    "Files the agent writes land": "factual",
    "permission prompts OFF": "factual",
    "network.mode: internet` gives": "factual",
    "Both /packages and the container's home": "factual",
    "status` and `botainer list`": "usage",
    "inspect` and `botainer access` show what a session can reach": "factual",
    "hpc jobs-doctor": "usage",
    "Rebuilt a plugin or changed its manifest": "usage",
}


def test_every_core_tip_has_a_drift_decision():
    for tip in tips_mod.BASE_TIPS:
        hits = [k for k in _CORE_TIP_DRIFT if k in tip]
        assert len(hits) == 1, (
            f"core tip needs exactly one drift category (got {hits}): {tip!r}\n"
            "→ you added/changed a tip: add it to _CORE_TIP_DRIFT ('usage'|'factual'); "
            "if 'factual', ALSO add a claims-vs-impl.tsv row (tips section) so the "
            "claim can't drift from the code.")
    for key in _CORE_TIP_DRIFT:                                  # no stale keys
        assert any(key in t for t in tips_mod.BASE_TIPS), f"stale drift key: {key!r}"
    # The companion check — every 'factual' tip has a claims-vs-impl drift row —
    # lives in a maintainer-side suite that does not ship. It reads a baseline
    # under tools/dev/, absent from every distribution, so keeping it here made
    # the EXPORTED tree fail its own suite. This half is pure product logic and
    # stays public, where a reader can see the rule being enforced.


# ───────────────────────── collection + aggregation ─────────────────────────
def test_base_tips_present_and_nonempty():
    base = tips_mod._base_tips()
    assert base and all(isinstance(t, Tip) and t.source == "botainer" for t in base)
    assert all(t.text.strip() for t in base)


def test_collect_merges_plugin_tips(monkeypatch):
    monkeypatch.setattr(
        tips_mod, "_plugin_tips", lambda only: [Tip("browser tip", "browser")])
    out = tips_mod.collect_tips(enabled={"browser"})
    assert Tip("browser tip", "browser") in out
    assert any(t.source == "botainer" for t in out)          # base always included


def test_plugin_tips_never_raises_on_broken_lookup(monkeypatch):
    # If the plugin machinery blows up, _plugin_tips must degrade to [] silently
    # so tips can never break the CLI.
    def boom():
        raise RuntimeError("plugin dir exploded")
    monkeypatch.setattr("botainer.plugins.lifecycle.list_installed", boom)
    assert tips_mod._plugin_tips(None) == []


# ───────────────────────── sequential rotation ─────────────────────────
def test_rotation_is_sequential_and_wraps(monkeypatch, tmp_path):
    idx_file = tmp_path / "tips-index"
    monkeypatch.setattr(tips_mod, "_index_file", lambda: idx_file)
    seen = [tips_mod._next_index(3) for _ in range(7)]
    assert seen == [0, 1, 2, 0, 1, 2, 0]          # cycles, no repeats within a cycle


def test_rotation_falls_back_to_zero_without_state(monkeypatch):
    monkeypatch.setattr(tips_mod, "_index_file", lambda: None)
    assert tips_mod._next_index(5) == 0
    assert tips_mod._next_index(0) == 0            # empty pool guard


def test_select_tip_empty_is_none():
    assert tips_mod.select_tip([]) is None


# ───────────────────────── footer gating + rendering ─────────────────────────
def test_footer_suppressed_by_env(monkeypatch, capsys):
    monkeypatch.setenv("BOTAINER_NO_TIPS", "1")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    tips_mod.print_tip_footer(enabled=set())
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("val,suppressed", [
    ("1", True), ("true", True), ("yes", True), ("on", True), ("anything", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
])
def test_no_tips_env_truthiness(monkeypatch, val, suppressed):
    # BOTAINER_NO_TIPS=0 must NOT suppress (Fable-5 L5).
    monkeypatch.setenv("BOTAINER_NO_TIPS", val)
    assert tips_mod.tips_suppressed() is suppressed


def test_footer_suppressed_when_not_a_tty(monkeypatch, capsys):
    monkeypatch.delenv("BOTAINER_NO_TIPS", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    tips_mod.print_tip_footer(enabled=set())
    assert capsys.readouterr().err == ""


def test_footer_prints_on_tty(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("BOTAINER_NO_TIPS", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr(tips_mod, "_index_file", lambda: tmp_path / "i")
    monkeypatch.setattr(tips_mod, "collect_tips",
                        lambda enabled: [Tip("hello tip", "botainer")])
    tips_mod.print_tip_footer(enabled=set())
    err = capsys.readouterr().err
    assert "hello tip" in err and "botainer tips" in err


def test_maybe_tip_footer_gating(monkeypatch):
    """The main() wrapper's footer gate: skip on error exit, help/version, bare,
    and `tips` itself; fire on a normal successful command (Fable-5 coverage gap)."""
    from botainer.cli import main as m
    calls: list = []
    monkeypatch.setattr("botainer.tips.print_tip_footer",
                        lambda enabled: calls.append(enabled))
    m._maybe_tip_footer(1, ["status"])       # nonzero exit → skip
    m._maybe_tip_footer(0, ["tips"])         # the tips command itself → skip
    m._maybe_tip_footer(0, ["--help"])       # help → skip
    m._maybe_tip_footer(0, ["-h", "start"])  # help flag anywhere → skip
    m._maybe_tip_footer(0, ["--version"])    # version → skip
    m._maybe_tip_footer(0, [])               # bare (help already shown) → skip
    assert calls == []
    m._maybe_tip_footer(0, ["status"])       # normal success → fires
    assert calls == [None]                   # None → core + all installed plugins


def test_footer_tags_plugin_source():
    line = tips_mod.render_footer(Tip("do the thing", "browser"), color=False)
    assert "[browser]" in line and "do the thing" in line


def test_footer_no_ansi_when_color_off():
    line = tips_mod.render_footer(Tip("x", "botainer"), color=False)
    assert "\x1b[" not in line


# ───────────────────────── manifest user_tips validation ─────────────────────────
def test_manifest_user_tips_valid():
    from botainer.plugins.manifest import ContributesDecl
    c = ContributesDecl(user_tips=["a useful tip", "another"])
    assert c.user_tips == ["a useful tip", "another"]


@pytest.mark.parametrize("bad", [
    ["", ],                        # empty
    ["  "],                        # blank
    ["has \x1b[31m escape"],       # C0 escape
    ["c1 csi \x9b31m"],            # C1 control (Fable-5 L1)
    ["del \x7f here"],             # DEL
    ["bidi ‮ override"],      # Cf format char (spoofing)
    ["x" * 301],                   # too long
])
def test_manifest_user_tips_rejects_bad(bad):
    from botainer.plugins.manifest import ContributesDecl
    with pytest.raises(Exception):
        ContributesDecl(user_tips=bad)


def test_manifest_user_tips_allows_tab_and_unicode():
    from botainer.plugins.manifest import ContributesDecl
    c = ContributesDecl(user_tips=["ok\ttabbed", "unicode café ✓ fine"])
    assert len(c.user_tips) == 2


# ── the RULE, on BOTH surfaces ──

def test_no_shipped_tip_renders_a_safety_verdict():
    """The RULE at the top of botainer/tips.py, enforced.

    IT WAS PROSE UNTIL. The rule came from the maintainer catching
    two shipped safety verdicts by hand on, and for the next 19 days
    nothing checked it: `_validate_user_tips` verified non-empty, charset and
    length, with the rule written in a comment directly above it. A plugin tip
    reading "The viewer is SAFER … and PROTECTS your clipboard — it is secure."
    was accepted unmodified.

    THIS WALKS BOTH SURFACES, WHICH IS THE POINT. The drift gate that already
    existed walks BASE_TIPS only — and BOTH of the maintainer's catches were in
    plugins/browser/botainer-plugin.yaml. A check covering only the half where
    the incident did not happen is scenery.

    Known limits, so nobody mistakes this for a guarantee: it matches literal
    words. "redundant", "unnecessary", "you don't need to worry about" and
    "handled for you" are verdicts it misses. The structural fix — a typed Tip
    with a required, resolvable pointer, so a verdict has nowhere to sit — is
    described in tips.py's _VERDICT_WORDS block and is not done.
    """
    import yaml
    offenders = []
    for tip in tips_mod._base_tips():
        hits = tips_mod.verdict_words_in(tip.text)
        if hits:
            offenders.append(f"  BASE_TIPS {hits}: {tip.text}")
    for manifest in sorted((_ROOT / "plugins").glob("*/botainer-plugin.yaml")):
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        for t in (data.get("contributes") or {}).get("user_tips") or []:
            hits = tips_mod.verdict_words_in(t)
            if hits:
                offenders.append(f"  {manifest.parent.name} {hits}: {t}")
    assert not offenders, (
        "these tips render a comparative or absolute SAFETY VERDICT. A tip may "
        "state a FACT and POINT at the security surface; it may not adjudicate "
        "safety, because a one-liner cannot carry the caveats and so misleads "
        "(botainer/tips.py RULE, user-corrected twice):\n" + "\n".join(offenders)
    )
