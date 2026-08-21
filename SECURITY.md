# Security policy

botainer runs AI coding agents inside containers, and its threat model treats
the agent as untrusted — prompt injection, a compromised model, a hostile page
the agent visited. Reports that the cage does not hold are the reports that
matter most here.

Read this first, though: **botainer is at v0.1.0a4, an alpha, and it is not a
complete security product.**  What follows is how to report a
problem, not a claim that there are few of them.

## Reporting a vulnerability

**Please don't open a public issue for a security problem.** Use GitHub's
private vulnerability reporting — the "Report a vulnerability" button on this
repository's Security tab. That reaches the maintainer without the report being
public first.

Useful to include: the version, what the agent (or a user) was able to do that
it should not have been, and a reproduction if you have one. An exact command
and the resulting file listing beats a description.

**No response-time promise.** This is a one-maintainer project with no security
team behind it, and a promise about acknowledgement times would be a promise
nobody is on call to keep. Reports are read. If one is serious and you have not
heard back, sending a follow-up is reasonable and welcome.

## What counts

The contract is [`docs/CAPABILITY-SURFACE.md`](docs/CAPABILITY-SURFACE.md). It
states what a session exposes: every bind, every environment variable that
crosses the boundary, every capability grant and every refusal. **A way to
exceed what that document grants is a vulnerability.** Concretely, that includes
a caged agent escaping the bound subtree, reaching a host resource that was
never granted, persisting code the host will later execute, or reading another
project's credentials.

A place where the document itself is WRONG — where it describes a protection the
implementation does not have — is also a vulnerability, and a more valuable
report than most, because everything else is reasoned about on top of it.

## What does not count

Documented trade-offs, made with eyes open and written down where they apply:

- **The agent runs without per-action permission prompts inside the cage.** That
  is the design (§4ab): the container is the boundary, not the prompt.
- **The browser viewer is a trust inversion.** Your browser connects to a server
  the untrusted container controls, so nothing configured server-side is a
  boundary. This is stated at the connect point and throughout
  [`docs/BROWSER.md`](docs/BROWSER.md), and gateway mode exists as the opt-in
  fix.
- **`network.mode: none` is not enforceable under apptainer**, which shares the
  host network namespace. botainer refuses that combination rather than pretend.
- **Mount-based auth modes put a real credential inside the container**, where a
  compromised agent can read and overwrite it. Broker mode is the mode where it
  cannot. The capability summary says which one is in force at every launch.

Reporting one of these is not wasted effort — if a documented trade-off is worse
than documented, or reachable in a way the documentation does not describe, that
is a real finding. It just is not news that the trade-off exists.

## Supported versions

Only the current alpha. There is no maintained older line and no backport path:
fixes land on the newest version.

| Version | Supported |
|---|---|
| 0.1.0a4 | yes |
| anything older | no |
