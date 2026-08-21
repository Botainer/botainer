"""Task #240: IdentityChangeRefused must inherit Refused so the
@handle_refusals decorator catches it.

Was `class IdentityChangeRefused(Exception)`. The decorator caught it
explicitly by name, but any code path raising it BEFORE reaching a
decorated CLI command (e.g., import-time identity check that surfaces
later) bypassed the named handler and dumped a traceback.
"""

from __future__ import annotations

from botainer.core.identity import IdentityChangeRefused
from botainer.core.refusal import RefusalCategory, Refused


def test_identity_change_refused_is_refused_subclass() -> None:
    """Catching Refused must also catch IdentityChangeRefused."""
    assert issubclass(IdentityChangeRefused, Refused)


def test_identity_change_refused_carries_category() -> None:
    exc = IdentityChangeRefused("user aborted")
    assert exc.category == RefusalCategory.IDENTITY_CHANGE_REFUSED
    assert exc.args[0] == "user aborted"


def test_identity_change_refused_default_message() -> None:
    exc = IdentityChangeRefused()
    assert exc.category == RefusalCategory.IDENTITY_CHANGE_REFUSED
    assert exc.args[0] == ""


def test_identity_change_refused_caught_as_refused() -> None:
    try:
        raise IdentityChangeRefused("test")
    except Refused as exc:
        assert exc.category == RefusalCategory.IDENTITY_CHANGE_REFUSED
