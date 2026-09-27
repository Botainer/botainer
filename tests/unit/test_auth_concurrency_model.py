"""Model alternative refresh-token semantics before integrating a design.

These tests do not establish provider behavior. A changed refresh token and an
invalidated old token are different outcomes. A server can issue a new token
while leaving the old one usable, allowing recovery after an interrupted refresh.

Each test states whether older tokens remain valid. Comparing the same designs
under both assumptions exposes their concurrency requirements without touching
live credentials.
"""
from __future__ import annotations

import itertools

import pytest


class FakeAuthServer:
    """Minimal OAuth server model with explicit old-token invalidation.

    Every test chooses the behavior rather than inheriting an assumption about
    a real provider.
    """

    def __init__(self, *, invalidate_old: bool) -> None:
        self.invalidate_old = invalidate_old
        self._ids = itertools.count(1)
        first = next(self._ids)
        self.valid_refresh = {f"R{first}"}
        self.issued_access: set[str] = set()
        self.start_token = f"R{first}"

    def refresh(self, refresh_token: str) -> tuple[str, str]:
        """-> (access, refresh). Raises if the token is not accepted."""
        if refresh_token not in self.valid_refresh:
            raise PermissionError(f"refresh token {refresh_token} rejected")
        n = next(self._ids)
        new_refresh, new_access = f"R{n}", f"A{n}"
        if self.invalidate_old:
            self.valid_refresh.discard(refresh_token)
        self.valid_refresh.add(new_refresh)
        self.issued_access.add(new_access)
        return new_access, new_refresh

    def call_api(self, access_token: str) -> bool:
        """Access tokens are BEARER tokens: valid until they expire, and NOT
        revoked when some other holder refreshes. This is the property the
        host-side design leans on, and it is why 'rotation breaks everything'
        was too strong."""
        return access_token in self.issued_access


# --------------------------------------------------------------------------
# Design A — TODAY. Every container holds the refresh token and refreshes
# for itself.
# --------------------------------------------------------------------------

def _design_a_session(server: FakeAuthServer, holder: dict) -> bool:
    """A session that needs a refresh, holding its own refresh token."""
    try:
        access, new_refresh = server.refresh(holder["refresh"])
    except PermissionError:
        return False
    holder["refresh"] = new_refresh
    return server.call_api(access)


def test_todays_design_survives_concurrency_IF_old_tokens_stay_valid() -> None:
    """The good case for the status quo, and the reason the open question
    matters: if the server does not invalidate, two holders coexist happily."""
    server = FakeAuthServer(invalidate_old=False)
    a = {"refresh": server.start_token}
    b = {"refresh": server.start_token}          # same login, two copies
    assert _design_a_session(server, a)
    assert _design_a_session(server, b), "second holder should still work"


def test_todays_design_BREAKS_under_concurrency_if_old_tokens_are_invalidated() -> None:
    """The bad case — and the one the user's symptom looks like."""
    server = FakeAuthServer(invalidate_old=True)
    a = {"refresh": server.start_token}
    b = {"refresh": server.start_token}
    assert _design_a_session(server, a)
    assert not _design_a_session(server, b), (
        "expected the second holder to be locked out once the first refreshed")


def test_todays_design_is_fine_SEQUENTIALLY_even_with_invalidation() -> None:
    """Why shared mode works day-to-day despite the above: one holder at a
    time, each handing the newest token to the next. This is exactly what
    pre_session/post_session reconcile achieves — and why the symlink bug,
    which creates a SECOND holder, is what actually breaks it."""
    server = FakeAuthServer(invalidate_old=True)
    shared = {"refresh": server.start_token}
    for _ in range(5):
        assert _design_a_session(server, shared), "sequential use must survive"


# --------------------------------------------------------------------------
# Design B — HOST-SIDE REFRESH. The host is the only holder of the refresh
# token; containers receive access tokens only and never refresh.
# --------------------------------------------------------------------------

class HostRefresher:
    """Sole holder. Hands out access tokens; never gives out the refresh token."""

    def __init__(self, server: FakeAuthServer) -> None:
        self._server = server
        self._refresh = server.start_token

    def access_token_for_a_new_session(self) -> str:
        access, new_refresh = self._server.refresh(self._refresh)
        self._refresh = new_refresh          # only this object ever rotates
        return access


def test_host_side_refresh_survives_concurrency_under_EITHER_behaviour() -> None:
    """THE result this file exists for.

    Because exactly one object ever calls refresh, invalidation of old refresh
    tokens is irrelevant — there is never a second holder to invalidate. And
    because access tokens are bearer tokens, every session issued one keeps
    working after later refreshes.
    """
    for invalidate in (True, False):
        server = FakeAuthServer(invalidate_old=invalidate)
        host = HostRefresher(server)
        sessions = [host.access_token_for_a_new_session() for _ in range(5)]
        assert len(set(sessions)) == 5, "each session got its own access token"
        for tok in sessions:
            assert server.call_api(tok), (
                f"a session issued earlier stopped working (invalidate_old="
                f"{invalidate}) — access tokens are supposed to be bearer "
                f"tokens unaffected by later refreshes")


def test_host_side_refresh_never_exposes_the_refresh_token() -> None:
    """The security half. Whatever reaches the container must not be usable to
    mint further tokens, or a prompt-injected agent regains what the design
    exists to take away."""
    server = FakeAuthServer(invalidate_old=True)
    host = HostRefresher(server)
    handed_out = {host.access_token_for_a_new_session() for _ in range(3)}
    assert not (handed_out & server.valid_refresh), (
        "a refresh token leaked into what the container receives")


# --------------------------------------------------------------------------
# Design C — ISOLATED. Each project performs its own login, so each has an
# independent refresh-token chain.
# --------------------------------------------------------------------------

def test_isolated_logins_are_unaffected_by_each_others_refreshes() -> None:
    """The workaround available TODAY, at the cost of one login per project.

    Modelled as separate servers because separate logins are separate grants —
    which is the assumption to check against reality (see the docstring at the
    end of this file), not something proven here.
    """
    for invalidate in (True, False):
        one = FakeAuthServer(invalidate_old=invalidate)
        two = FakeAuthServer(invalidate_old=invalidate)
        a = {"refresh": one.start_token}
        b = {"refresh": two.start_token}
        assert _design_a_session(one, a)
        assert _design_a_session(two, b), "independent grants must not interfere"


# --------------------------------------------------------------------------
# What the model says to go and measure.
# --------------------------------------------------------------------------

def test_the_discriminating_experiment_is_identified() -> None:
    """A model earns its keep by naming the cheapest question that resolves it.

    Design A's fate hangs entirely on `invalidate_old`, and the two tests above
    show it going both ways on that single bit. Design B does not depend on it
    at all. So:

      - If old refresh tokens STAY valid  -> today's design is fine; fix only
        the symlink bug and concurrency needs nothing.
      - If old refresh tokens are KILLED  -> today's design cannot do
        concurrency at any effort, and Design B is the fix.

    The experiment: refresh once, keep the OLD token, then try to refresh with
    the OLD one again. Accepted => no invalidation. Rejected => invalidation.

    That call spends the user's credential against the real auth server, which
    they have said must be explicit and consented (re brokers and
    "anything else... to rotate keys"). So it belongs behind a command with a
    confirmation prompt, not in this suite, and NOT as a side effect of
    anything else.
    """
    killed = FakeAuthServer(invalidate_old=True)
    old = killed.start_token
    killed.refresh(old)
    with pytest.raises(PermissionError):
        killed.refresh(old)

    survives = FakeAuthServer(invalidate_old=False)
    old = survives.start_token
    survives.refresh(old)
    survives.refresh(old)      # must not raise
