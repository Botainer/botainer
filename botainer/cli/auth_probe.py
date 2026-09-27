"""`botainer auth rotation-probe` — does a refresh KILL the old refresh token?

THE QUESTION, and why it is the only one left. We measured  that a
refresh returns a NEW refresh token. We did NOT measure whether the OLD one
stops working, and those are different claims — many OAuth servers rotate while
leaving the previous token usable, precisely so a client that dies mid-refresh
is not locked out. The whole architecture turns on this one bit:

    old tokens stay valid  -> today's design is fine; concurrent sessions work;
                              only the symlink bug needs fixing.
    old tokens are killed  -> mount mode cannot do concurrency at any effort,
                              and the host must become the sole refresher.

`tests/unit/test_auth_concurrency_model.py` models both worlds and shows the
designs diverging on exactly this bit. This command settles it by observation,
which is the only thing that can.

This diagnostic makes two token-endpoint calls and exits. It does not proxy
model requests or run a daemon. Credential-refresh diagnostics require explicit
consent because refreshing a token can change authentication state, even without
a model request.

The endpoint and client_id are PINNED, never read from config, for the same
reason the broker pins them: the durable refresh token is sent to that URL, so a
hostile `.botainer/config.yaml` must not be able to redirect it. They are pinned
PER AGENT FAMILY, in the credential registry that also fixes which filename to
read — not as constants in this module, which is where they used to live. That
placement was the bug: `--agent` accepted any family while the endpoint could
only ever be Anthropic's, so what stood between a codex token and the Anthropic
token endpoint was the fact that the filenames happened not to match. This
command supports ANTHROPIC only, and now says so by name instead of by failing
to find a file.

SAFETY, because this deliberately rotates a live credential:
  - a backup is written BEFORE any network call, and its path is printed;
  - every successful refresh is persisted IMMEDIATELY and atomically, so the
    newest working token is always on disk before the next call is attempted;
  - the only unavoidable window is a crash between a 200 response and the write
    to disk, which is one fsync wide and is stated plainly rather than hidden.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import click

from botainer.cli._refusal_handler import handle_refusals
from botainer.state import dir as state_dir


def _post_refresh(refresh_token: str, *, endpoint: str, client_id: str,
                  timeout: float = 20.0) -> tuple[int, dict]:
    """One refresh call. Returns (status, body). Never raises on HTTP status.

    `endpoint` AND `client_id` ARE REQUIRED KEYWORD ARGUMENTS, and that is the
    fix, not a style choice. They used to be module constants pinned to
    Anthropic while the command's `--agent` accepted any family — so the pair
    that decides WHERE a durable refresh token is sent had no connection to the
    family whose token it is. Measured: a `.credentials.json` planted under
    `agent-codex` had its token handed to the Anthropic token endpoint. Nothing
    checked; the lookup had simply never found a file before, because codex's
    real credential is named `auth.json`.

    Making these required means a caller cannot reach this function without
    naming a family first, and the only source of the pair is the per-family
    registry that also fixes the filename. Omitting them is a `TypeError`, not a
    silent default — the same unforgettable-enforcement shape as
    `run_hook(agent_writable_roots=…)`.

    Still PINNED, never read from config: the values live in the registry, so a
    hostile `.botainer/config.yaml` cannot redirect a refresh token.
    """
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode()
    req = urllib.request.Request(
        endpoint, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            body = {}
        return e.code, body


def _write_credential_atomically(path: Path, doc: dict) -> None:
    """Persist before doing anything else. A rotated token that never reaches
    disk is a locked-out user."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cred-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib_suppress():
            os.unlink(tmp)
        raise


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def _apply(doc: dict, body: dict, oauth_key: str) -> dict:
    """Merge a refresh response back into the credential.

    `oauth_key` is REQUIRED for the same reason `_post_refresh`'s endpoint
    is: this used to hardcode `claudeAiOauth`, so a non-claude credential
    would have grown a claude-shaped block beside its real one rather than
    being updated. Today only anthropic reaches here, and that is enforced
    above rather than assumed here.
    """
    blk = doc.setdefault(oauth_key, {})
    blk["accessToken"] = body.get("access_token", blk.get("accessToken"))
    if body.get("refresh_token"):
        blk["refreshToken"] = body["refresh_token"]
    if body.get("expires_in"):
        blk["expiresAt"] = int((time.time() + float(body["expires_in"])) * 1000)
    return doc


@click.command("rotation-probe")
@click.option("--agent", default="agent-claude", show_default=True)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@handle_refusals
def rotation_probe(agent: str, yes: bool) -> None:
    """Settle whether a refresh INVALIDATES the previous refresh token.

    Makes two live calls to the token endpoint with your credential. Rotates it
    (twice, if old tokens turn out to still work). Always writes the newest
    working token back before continuing.
    """
    from botainer.cli.auth_doctor import (
        _cred_shape,
        _normalise_agent,
        _oauth_block,
        _read_json,
        _refresh_of,
        _refuse_unknown_agent,
    )

    # THE FAMILY DECIDES EVERYTHING, AND IT DECIDES IT FIRST. Endpoint,
    # client id, credential filename and OAuth block key all come out of one
    # registry entry, so they cannot disagree with each other.
    _refuse_unknown_agent(agent)
    family = _normalise_agent(agent)          # `codex` and `agent-codex` both work
    shape = _cred_shape(family)
    endpoint, client_id = shape["token_endpoint"], shape["oauth_client_id"]

    # REFUSE BY NAME, NOT BY FAILING TO FIND A FILE. This is the dead-end loop
    # the queue row is about: `--agent agent-codex` used to look for CLAUDE's
    # `.credentials.json`, miss (codex's file is `auth.json`, in the same
    # directory), and print "Run `botainer auth login` first". A codex user who
    # WAS logged in ran the login, it succeeded, and the probe refused
    # identically. Saying what is actually true costs one branch.
    if not endpoint or not client_id:
        click.secho(
            f"refused: rotation-probe cannot probe {family}.", fg="red",
            err=True)
        click.echo(
            "  This command settles an ANTHROPIC OAuth question by making real\n"
            "  refresh calls, and there is no refresh route implemented for this\n"
            "  agent family. It is not that you are logged out — logging in\n"
            "  again will not change this answer.")
        click.echo("  `botainer auth doctor --agent "
                   f"{family[len('agent-'):]}` reports that family's state.")
        raise SystemExit(2)

    paths = state_dir.ensure_user_state_dir(create_if_missing=False)
    root = paths.root
    # Filename from the SAME entry as the endpoint — see `_post_refresh`.
    cred = root / "shared-auth" / family / shape["filenames"][0]
    if not cred.exists():
        click.secho(f"refused: no credential at {cred}", fg="red", err=True)
        click.echo("  Run `botainer auth login` first.")
        raise SystemExit(2)
    doc = _read_json(cred) or {}
    # `_refresh_of` reads either spelling; the block key is the family's own.
    r0 = _refresh_of(_oauth_block(doc, shape["oauth_key"]))
    if not r0:
        click.secho("refused: that credential holds no refresh token.",
                    fg="red", err=True)
        raise SystemExit(2)

    click.secho("This probe uses your REAL credential.", fg="yellow", bold=True)
    click.echo(f"  It sends up to two refresh requests to {endpoint}")
    click.echo("  and ROTATES your token (the new one is saved each time).")
    click.echo("  It does not proxy API traffic and starts no daemon.")
    click.echo("\n  Worst case: if it crashes between a response and the disk")
    click.echo("  write, you re-run `botainer auth login`. A backup is made.")
    if not yes and not click.confirm("\nProceed?", default=False):
        click.echo("aborted — nothing was sent.")
        return

    backup = root / f"rotation-probe-backup-{int(time.time())}.json"
    # ROW-88 SHAPE: `backup.write_text(...)` took the umask, so the backup
    # landed 0644 — world-readable, holding a live refresh token — while
    # the sibling `rotation-test arm` gets 0600 because `shutil.copy2`
    # preserves the credential's mode. Measured: with the source at 0600
    # the backup was STILL 0644, so the mode was umask-derived and not
    # inherited. Routing through the atomic writer takes the mode from
    # `mkstemp` (0600) instead of from a chmod a future caller can forget.
    _write_credential_atomically(backup, doc)
    click.echo(f"\nbackup: {backup}")

    # ---- call 1: rotate, and PERSIST before doing anything else ----
    click.echo("call 1: refreshing with the current token ...")
    status, body = _post_refresh(r0, endpoint=endpoint, client_id=client_id)
    if status != 200 or not body.get("access_token"):
        click.secho(f"\nrefresh FAILED (status {status}).", fg="red", bold=True)
        click.echo(f"  {str(body)[:300]}")
        click.echo("\n  Nothing was rotated; your credential is untouched.")
        click.echo("  If this says the token is invalid, you are simply logged")
        click.echo("  out — run `botainer auth login`.")
        raise SystemExit(1)
    r1 = body.get("refresh_token") or ""
    _write_credential_atomically(cred, _apply(doc, body, shape["oauth_key"]))
    click.secho("  ok — new token saved.", fg="green")

    if not r1:
        click.secho("\nRESULT: NO ROTATION AT ALL.", fg="green", bold=True)
        click.echo("  The server returned no new refresh token, so the original")
        click.echo("  stays in use. Multi-holder is not a problem at all.")
        _record(root, "no-rotation", endpoint)
        return
    if r1 == r0:
        click.secho("\nRESULT: NO ROTATION.", fg="green", bold=True)
        click.echo("  The same refresh token came back. Multi-holder is fine.")
        _record(root, "no-rotation", endpoint)
        return

    # ---- call 2: THE question — is the OLD token still accepted? ----
    click.echo("call 2: retrying with the OLD token ...")
    status2, body2 = _post_refresh(r0, endpoint=endpoint, client_id=client_id)
    if status2 == 200 and body2.get("access_token"):
        # It worked, so we now hold a THIRD token; persist it immediately or the
        # one we saved a moment ago may itself have just been superseded.
        _write_credential_atomically(
            cred, _apply(_read_json(cred) or {}, body2, shape["oauth_key"]))
        click.secho("\nRESULT: ROTATION WITHOUT INVALIDATION.", fg="green", bold=True)
        click.echo(
            "  A refresh mints a new token, but the OLD one still works.\n"
            "  => Multiple holders can coexist. Concurrent sessions are fine,\n"
            "     and the only real defect is the symlink bug that silently\n"
            "     splits a project off from the shared store.")
        _record(root, "rotation-without-invalidation", endpoint)
        return

    click.secho("\nRESULT: ROTATION WITH INVALIDATION.", fg="red", bold=True)
    click.echo(f"  The old token was rejected (status {status2}).")
    click.echo(
        "  => Exactly one holder can be valid at a time. Concurrent sessions\n"
        "     in mount mode CANNOT work at any effort, because each container\n"
        "     refreshing kills the others. The host must become the sole\n"
        "     refresher and hand containers access tokens only.")
    _record(root, "rotation-with-invalidation", endpoint)


def _record(root: Path, verdict: str, endpoint: str) -> None:
    """Persist the finding WITH ITS DATE, so drift is detectable later.

    An external service's behaviour is not a fact we own — it can change with
    no notice and no version number. Recording when we last observed it is the
    difference between "we know" and "we knew once".
    """
    path = root / "external-facts.json"
    try:
        facts = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        facts = {}
    facts.setdefault("oauth_refresh_behaviour", []).append({
        "verdict": verdict,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": endpoint,
    })
    path.write_text(json.dumps(facts, indent=2), encoding="utf-8")
    click.echo(f"\nrecorded in {path}")
    click.echo("  Re-run periodically: this is someone else's server and the")
    click.echo("  answer can change without warning.")
