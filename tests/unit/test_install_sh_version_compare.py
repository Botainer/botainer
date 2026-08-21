"""Task #222: install.sh Python version compare must use numeric semantics.

Was: `[[ "$PY_VER" < "3.10" ]]` — bash string compare. "3.10" sorts
BEFORE "3.9" lexicographically, so a Python 3.10 install would be
falsely rejected as "< 3.10".

Now: defer the comparison to Python itself via
`python -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 4)'`.
"""

from __future__ import annotations

from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[2] / "tools" / "pkg" / "install.sh"


def test_install_sh_does_not_use_lexicographic_compare() -> None:
    """The broken pattern `[[ "$PY_VER" < "3.10" ]]` must not appear."""
    src = INSTALL_SH.read_text()
    assert '[[ "$PY_VER" < "3.10" ]]' not in src
    assert "[[ \"$PY_VER\" < \"3.10\" ]]" not in src


def test_install_sh_uses_python_version_info_check() -> None:
    """Compare uses sys.version_info tuple — Python's own semver-aware logic."""
    src = INSTALL_SH.read_text()
    assert "sys.version_info >= (3, 10)" in src
