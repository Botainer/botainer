# Changelog

Notable changes to botainer, in [Keep a Changelog](https://keepachangelog.com/)
format. Versions follow [PEP 440](https://peps.python.org/pep-0440/), which is
what Python packaging actually enforces — `0.1.0a4`, not `0.1.0-alpha.4`.

This file records notable changes from the first public release onward.

## [0.1.0a5] — Unreleased candidate

Changes prepared since a4. This remains an alpha candidate; no a5 release tag
has been created for this candidate.

### Changed

- New projects default to `isolated` authentication for both agents. Existing
  selections are preserved.
- `botainer start --agent <name>` selects Claude or Codex within one project.
- Authentication profiles and cluster profiles have separate options:
  `--auth-profile` and `--cluster`.
- `hpc submit` accepts the same resource overrides as `start`.
- Cluster software roots can be declared directly without using a module system.
- `botainer where` reports storage locations and supports a machine-readable
  state-root query.
- One-session agent, authentication-mode and profile overrides disclose when they
  select a different history directory. Temporary overrides do not carry history;
  persistent changes offer a separate carry operation.
- [`docs/ROUGH-EDGES.md`](docs/ROUGH-EDGES.md) describes shipped limitations and
  available workarounds.

### Fixed

- Codex broker routing now writes the configuration used by the client. A short
  successful request run does not establish token renewal, all-model support or
  long-running stability.
- Apptainer session launches requesting NVIDIA GPUs include `--nv`; dispatched
  jobs and pool workers already included it. Automatic AMD selection is absent.
- Project mounts reject the state root, and non-selected agent credentials are
  excluded from the session.
- Setup no longer corrupts state-root initialization when allowing third-party
  plugins; copied projects no longer silently reuse the original identity.
- Credential diagnostics follow the selected agent family and avoid restoring
  stale credentials over a newer login.
- Shared-credential holders are detected and named at launch. The warning is
  advisory; it does not coordinate refreshes. Claude refresh-token conflicts
  have been observed. Codex shared-session overlap can work; its renewal behavior
  remains unverified.
- Ownership and cleanup checks reject mismatched session targets. Some broader
  persistent-session start, reconnect and stop work remains incomplete.
- Hot-job results preserve output and completion metadata. Interrupted handoff
  recovery preserves ownership so the same accepted request is not run twice.
- macOS system-path checks cover canonical aliases. Seven Python child-launch
  paths use isolated imports; this does not cover every child process.
- Distribution metadata omits local build paths and labels and records a digest
  of the staged file manifest.

### Known limitations

- Credential proxy mode is not qualified. Its launch path currently refuses;
  it is separate from the account-login broker.
- Authentication history carry still has recovery and transfer-integrity defects.
- Warm-pool resource-policy enforcement, worker import isolation, cancellation
  and status reporting have unresolved defects.
- The browser plugin remains Docker-only. Its viewer is a trust inversion
  described in [`SECURITY.md`](SECURITY.md).
- Automated logic tests and bounded runtime checks do not establish a general
  security guarantee or qualification across all clusters and client versions.

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
touch a real runtime. Runtime evidence for a4 was limited to macOS Docker and
a single Slurm cluster;
[the README](README.md#what-is-tested-and-what-is-not) breaks it down.
