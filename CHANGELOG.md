# Changelog

Notable changes to botainer, in [Keep a Changelog](https://keepachangelog.com/)
format. Versions follow [PEP 440](https://peps.python.org/pep-0440/), which is
what Python packaging actually enforces — `0.1.0a4`, not `0.1.0-alpha.4`.

This file starts at the first public release. botainer was developed privately
before that, and nothing here reaches back further: a history of changes to
something nobody outside could run is noise, not information.

## [0.1.0a4] — 2026-08-26

First public release. An **alpha**: usable, incomplete, and specific about
which parts are which.

### What works

- Runs Claude Code or Codex inside a Docker (laptop) or Apptainer (HPC)
  container, with the bind set, environment and capability grants composed from
  config and stated up front at every launch.
- Subscription OAuth login in three modes — `shared`, `isolated`, and `broker`,
  where the real token never enters the container.
- Slurm: submit a session as a batch job, attach to a running one, and let the
  caged agent request further jobs through a directional mailbox that never
  gives it an uncaged path.
- Fourteen bundled plugins, each declaring what it contributes. Two are on
  after `botainer init` (the Claude agent and protected git mode); the rest
  are opt-in per project.
- ~50 cluster profiles, each stamped with how far it was actually verified —
  hardware-tested, probe-derived, or transcribed from a vendor's documentation.

### What does not

- **API keys and the credential proxy.** Code exists for both; neither works.
  Subscription login is the only supported route.
- **The codex broker.** Implemented, never successfully run anywhere.
- **The browser plugin on a cluster.** Docker only so far.

### Known trade-offs

Documented rather than fixed, and listed in
[`SECURITY.md`](SECURITY.md#what-does-not-count):
the agent runs without per-action prompts inside the cage; the browser viewer is
a trust inversion; `network.mode: none` cannot be enforced under apptainer, so
botainer refuses it there instead of pretending.

### Testing, plainly

~2,400 automated tests verify what botainer *decides* — binds, environment,
argv, refusals — and they compute that without launching a container. Only three
touch a real runtime. One maintainer uses it daily on one Mac and repeatedly on
one Slurm cluster. That is the whole evidence base;
[the README](README.md#what-is-tested-and-what-is-not) breaks it down.
