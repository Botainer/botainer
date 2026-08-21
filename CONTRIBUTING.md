# Contributing to botainer

Thanks for looking. Bug reports, docs fixes, cluster profiles, tests and code
are all welcome — but read the next section first, because how this repository
is published affects how a contribution reaches a release.

## How this repository works

**This is a published mirror.** Development happens in a private repository and
each release is exported here as a single commit. That has two consequences
worth knowing before you spend time:

- **A merge here is not the end of the journey.** Your change has to be adopted
  into the private repository before it appears in the next release. The export
  refuses to run until that has happened, so nothing gets silently reverted —
  but it does mean review can be slower than you would expect from the diff
  size.
- **Rebases and force-pushes on `main` do not happen**, and neither does
  history rewriting. Releases are ordinary commits on top.

If that sounds like friction: it is, and it is temporary. Ask in an issue
before starting anything large, and the answer will include whether the shape
of the thing makes the round trip easy or awkward.

**Issues are the low-friction path** and are genuinely useful: a cluster profile
that does not match your site, a document that misled you, a refusal message
that did not tell you what to do. Those need no agreement, no round trip, and
they are where most of the value is at this stage.

## Contributor License Agreement

Contributions require agreeing to the [Contributor License Agreement](CLA.md),
adapted from the Apache Software Foundation's Individual CLA v2.2.

**There is no bot.** Post this as a comment on your pull request:

> I have read the Botainer Contributor License Agreement v1.0
> (sha256 0dd37a0ac872d332) and I hereby sign it

The digest is there so the record says *which text* you agreed to — not just
that you agreed to something. If [`CLA.md`](CLA.md) changes, that is a new
version with a new digest, and it applies only to contributions submitted after
you accept it (CLA §10). Your comment is the record: it is timestamped by
GitHub, attributed to your account, and not something this project can quietly
revise.

Signing is checked by hand. It is not automated because an automated CLA gate
that nobody is maintaining is worse than a manual check that actually happens.

If you're contributing on your employer's time or equipment, please make sure
you're authorized to sign (CLA §4).



## AI-assisted contributions

Welcome — this project itself is written with AI assistance. Three rules:

1. You submit each contribution as your own, under the CLA, and you are
   accountable for it — including where it came from.
2. Don't submit AI output that reproduces identifiable third-party code;
   disclose any known third-party material per CLA §§5 and 7.
3. Maintainers may decline contributions.

## Ground rules

- Open an issue before large changes so we can discuss the approach.
- Match the existing code style; include tests where behavior changes.
- Third-party code you didn't write must be flagged in the pull request with
  its source and license (CLA §§5 and 7) — don't paste unattributed code.
- By project policy, runtime dependencies must be permissively licensed
  (MIT/BSD/ISC/Apache-2.0) or weak-copyleft (LGPL/MPL); no GPL/AGPL runtime
  dependencies.

## License

botainer's own code is released under [Apache-2.0](LICENSE), and every public
release — including your contribution — is distributed under that license. The
distribution also carries third-party components under their own terms (see
[`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md)); that does not affect your
contribution.
