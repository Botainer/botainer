# Profiles: three different things with one name

botainer says "profile" about three unrelated axes. They came from three
different features at three different times, and nothing ever reconciled the
vocabulary — so `--profile` on one command and `--profile` on another select
completely different objects, and `hpc.py` alone uses the identifier
`profile_name` for two of them.

This page settles the names. It is a glossary, not a plan: everything below
already exists.

## The three axes

| axis | what it names | the question it answers | where it lives |
|---|---|---|---|
| **auth profile** | this agent's history + settings, and — in ISOLATED mode only — a credential slot | *which of my logins?* | `<creds-dir>/profiles/<name>/` |
| **cluster profile** | declared facts about a machine | *what machine am I on?* | `cluster_profiles/<name>.yaml` |
| **job profile** | a named resource-request shape | *how big a job?* | `job_profiles:` in `.botainer/config.yaml` |

> **One exception, and it is the mode the HPC guide teaches.** In **shared**
> mode the auth profile selects this agent's history and settings but NOT a
> credential: shared mode holds one host-wide login at
> `shared-auth/agent-<agent>/`, which every project links to. There is no
> `profiles/<name>/` credential under it, so `botainer auth login --shared
> --auth-profile work` is **refused** — logging in under a second profile name
> would overwrite the first account's token rather than sitting beside it. For
> side-by-side logins use `auth use isolated`. NOT broker: broker mode has
> no login of its own and reads this same shared credential, so it is not
> an alternative here.

They are orthogonal. A single dispatched job uses all three at once: your
`work` auth profile, the `generic-slurm` cluster profile, and the `big` job
profile. Nothing about one implies anything about another.

### auth profile — *which of my logins?*

One agent, several accounts: personal and work, or two ChatGPT plans. The
profile name is a directory component under the credential store, which is why
it is restricted to `^[a-z][a-z0-9_-]{0,31}$` — it is interpolated into paths
that get `chmod 0o700`.

    botainer auth login --auth-profile work
    botainer start --auth-profile work

**It moves your history with it.** An agent's notes, todos and session list
live beside the credential — the profile name is a path component of the
agent's config dir — so switching auth profile changes which set you see.
botainer notices the switch and OFFERS to carry them across; it does not do it
silently, and it does not do it without asking. Non-interactively it says what
it found and proceeds. Either way the coupling is real and worth knowing before
you name a second profile.

### cluster profile — *what machine am I on?*

Partitions, time limits, whether `$TMPDIR` is node-local, how to bootstrap
Lmod. Bundled profiles assert facts about machines the botainer authors cannot
reach, so each carries a verification record saying who checked it and when.

    botainer hpc setup --cluster generic-slurm

A wrong cluster profile is silent until someone submits a job.

### job profile — *how big a job?*

A named bundle of resource requests — partition, cpus, memory, time, gpus.
Defined per-project in `.botainer/config.yaml`, bounded by whatever ceilings
the cluster profile and the site policy declare.

This is the axis you do NOT select from your shell, and that is the point of
it: the agent picks a job profile from inside the container, by name, and the
name is the only thing it gets to choose. It cannot ask for resources you did
not pre-approve.

    # the agent, inside the cage
    botainer-job profiles
    botainer-job submit big ./run.sh

    # you, on the host, seeing exactly what `big` would be allowed to do
    botainer hpc jobs-explain big

`hpc submit` takes individual `--partition` / `--cpus` / `--gpus` overrides
rather than a profile name; it is the direct route, not the delegated one.

## Which flag selects which

`--auth-profile` and `--cluster` are the canonical spellings — they are what
`--help` shows and what belongs in a script someone else has to read. A bare
`--profile` still works everywhere it used to, and is ambiguous ACROSS commands
by construction: it means the auth axis on `auth login` and the cluster axis on
`hpc setup`. Nothing can make one word mean both correctly, so the qualified
name is the one to use.

| command | axis | canonical | also accepted |
|---|---|---|---|
| `auth login` | auth | `--auth-profile` | `--profile` |
| `start`, `inspect`, `dry-run` | auth | `--auth-profile` | — |
| `hpc setup` | cluster | `--cluster` | `--profile` |
| `hpc jobs-explain` | job | positional argument | — |

`start` had `--auth-profile` before this page existed. That was not a style
choice: `start` needs to name the auth axis unambiguously, and `--profile` was
already taken by the cluster axis. The workaround came first; this page is the
rule it implied.

## What is NOT settled

The word is consistent at the surface a user types. It is not yet consistent
everywhere inside the code: `hpc.py` still uses a plain `profile_name` in the
warm-pool and sbatch internals. Those are all the JOB axis and unambiguous in
context — the collision that mattered (one identifier naming the cluster axis
in `setup` and the job axis in `jobs_explain`, in one file) is gone. Renaming
the rest would change `sbatch_submit`'s keyword, which tests call directly, so
it is churn with a breakage risk and no reader benefit. Recorded here rather
than left for someone to rediscover.
