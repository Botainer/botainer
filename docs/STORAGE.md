# Where botainer puts things

Four locations, three of which you never think about. The fourth — `/scratch` —
is worth five minutes now because it is awkward to change later.

---

## 1. The short version

| inside the container | on your machine | what it is |
|---|---|---|
| `/workspace` | your project directory | your code. Read-write. What you deliver. |
| `/home/user` | `$MY_BOTAINER/state/<uuid>/home` | the container's HOME: tool caches, agent config |
| `/packages` | `$MY_BOTAINER/state/<uuid>/packages` | pip/npm installs, kept between sessions |
| `/scratch` | `$MY_BOTAINER/state/<uuid>/scratch` **by default** | bulk, disposable working files |

Three of those four can be moved to another filesystem: `/scratch`,
`/packages` and `/home/user`. Your credentials and project identity cannot —
that is deliberate, see §2.

`$MY_BOTAINER` defaults to `~/.botainer`. `<uuid>` is the project's id — one per
project, so projects never share these.

**On a laptop the defaults are fine.** Read §2 only if you are on HPC, or if
your home directory has a quota you care about.

---

## 2. Why `/scratch` is different, and why it matters on HPC

Everything in the table above except `/scratch` is **small and must survive**:
your credentials, your project identity, your installed plugins. Losing them is
not "re-download something", it is "this project no longer exists and the agent
refuses to start".

`/scratch` is the opposite: **large and genuinely disposable.** It is where the
agent puts a 40 GB intermediate BAM, an unpacked dataset, a build tree.

On a cluster those two want different filesystems:

```
  $HOME            small quota, backed up, never purged   ← state belongs here
  the scratch FS   huge, fast, AUTO-PURGED (often ~60d)   ← scratch belongs here
```

If you put everything on `$HOME`, the agent's bulk files eat your quota. If you
put everything on scratch, **a purge takes your credentials with it** and you
lose the project.

So botainer lets you move `/scratch` alone, leaving the rest on `$HOME`.

---

## 2b. Two different quotas, and they break for different reasons

Clusters limit **two** things, and they are not interchangeable:

```
  bytes   how much data      -> /scratch and /home/user are the big ones
  inodes  how many FILES     -> /packages is the killer, and it is not obvious
```

`/packages` (pip, npm, conda) is only a few GB but is **hundreds of thousands
of tiny files**. A home directory with a 500,000-file cap is exhausted by one
conda environment while `du` still shows plenty of room. When that happens
every session dies with:

```
OSError: [Errno 122] Disk quota exceeded
```

Check both. On most clusters `quota -s` shows a "files" column; some sites have
their own command (`getquota`, `mmlsquota`).

**Moving `/scratch` does not help an inode problem** — scratch is the byte-heavy
one. If you are out of *files*, move `/packages`, and probably `/home/user` too
(it holds `~/.cache` for pip and npm).

## 3. Moving `/scratch`, `/packages` and `/home/user` (HPC)

### 3a. Tell botainer where it goes

In `$MY_BOTAINER/cluster.yaml` (create the blocks you need; all are optional
and independent):

```yaml
scratch:
  template: "/scratch/${USER}/botainer"        # bulk data — byte-heavy
packages:
  template: "/project/${USER}/botainer-pkgs"   # pip/npm/conda — INODE-heavy
home:
  template: "/project/${USER}/botainer-home"   # tool caches — both
```

Set only the ones you need. Anything you leave out stays under the state root,
exactly as before.

botainer appends the project uuid, so with two projects you get:

```
/scratch/you/botainer/edb09384-c0f4-434f-ac51-ee1bcc16fb8f
/scratch/you/botainer/94cf4c69-552c-4751-af19-2e4d1a8e466d
```

**One setting, host-wide.** Every project gets its own subdirectory
automatically — you do not configure this per project. (There is no per-project
override today; if you need one, say so.)

The template may use environment variables — `${USER}` is the common one. It
**must** expand to an absolute path with nothing left unresolved. If it does
not, botainer prints a warning and falls back to the default location rather
than guessing.

### 3b. Check it took effect

In any project directory:

```sh
botainer inspect | grep /scratch
```

The source must be your new path. If it still shows
`$MY_BOTAINER/state/<uuid>/scratch`, the setting did not take — look for the
warning botainer printed.

### 3c. Moving data you already have

If you have been using botainer already, your scratch data is on the old path.
To move it:

```sh
# --component is scratch | packages | home. Dry run first, always.
tools/pkg/relocate-storage.sh --component packages --dest /project/$USER/bpkgs
tools/pkg/relocate-storage.sh --component packages --dest /project/$USER/bpkgs --apply
```

It copies, verifies file counts, then **renames the original aside** — it never
deletes anything. After you have confirmed §3b, remove the leftovers:

```sh
tools/pkg/relocate-storage.sh --component packages --cleanup   # lists them, asks once
```

**Order matters:** move the data, then set the config, then verify, then clean
up. Setting the config first is not destructive — it just points `/scratch` at
an empty directory while your files sit where they were.

---

## 4. Which path should I use?

There is no universal answer; it is per site. What your cluster calls scratch is
in its documentation, usually as "scratch", "work" or "flash". Common shapes:

```
/scratch/$USER                    many university clusters
/<site-fs>/scratch/$USER          sites with a named parallel filesystem
/pscratch/sd/<initial>/$USER      some DOE facilities
/fs/scratch/<project-code>        sites that bill scratch to a project
```

Your site's own documentation is authoritative. `botainer hpc info` shows the
template botainer would use for your cluster, if it ships a profile for it.

If botainer ships a profile for your cluster (`botainer hpc info` shows which),
its `scratch.template` is a starting point taken from the site's public docs —
**not something anyone verified on your account.** Check it exists before
relying on it:

```sh
ls -ld /scratch/$USER        # substitute YOUR site's scratch path
```

Some templates contain a value only you know — `${PROJECT}`, `${PROJECT_CODE}`,
`${LRZ_HASH}`. Those will not resolve until you either export the variable or
replace it with the literal value in your `cluster.yaml`.

---

## 5. What lives where, in full

```
$MY_BOTAINER/                          default ~/.botainer
├── cluster.yaml                       your cluster profile (incl. scratch.template)
├── policy.yaml                        host-wide policy
├── plugins/                           installed plugins
├── images/                            built .sif images (HPC)
├── shared-auth/agent-<name>/          host-wide credential, shared auth mode
├── hpc-job-outputs/<uuid>/            Slurm logs. NEVER bound into a container.
├── hpc-jobs/<uuid>/                   dispatched-job mailbox. Also never bound.
└── state/<uuid>/                      one per project
    ├── meta.json                      project identity — losing this loses the project
    ├── data/                          per-agent credential + plugin state
    ├── sessions/                      per-session records
    ├── locks/
    ├── packages/    → /packages       ┐ these three, and ONLY these three,
    ├── scratch/     → /scratch        ├ can be moved to another filesystem
    └── home/        → /home/user      ┘ (§3). Everything above stays put.
```

**One rule:** the small, precious things — your login (`shared-auth/`), each
project's identity (`meta.json`) and credentials (`data/`) — never move. The
big, rebuildable things can. That is deliberate: relocating the whole root
would put your credentials on whatever volume you picked, which on a cluster is
usually group-visible project space.

`images/` is the exception that is NOT yet fixable: `.sif` files are 2-6 GB each
and stay wherever the state root is. If your quota problem is BYTES rather than
file count, that is the one this does not solve yet.

`hpc-job-outputs/` and `hpc-jobs/run/` sit outside `state/<uuid>/` deliberately:
they are written by uncaged host processes, so they must be somewhere the caged
agent has no write path to.

---

## 6. Cleaning up

- `botainer where` lists a project's directories with sizes.
- Deleting a project's `scratch/` is always safe. It is disposable by
  definition, and the agent is told so.
- Deleting `packages/` is safe; the next session reinstalls.
- **Do not delete `data/`, `meta.json` or `sessions/`.** That is the project's
  identity and your credentials.
- There is no `botainer rm` yet. Removing a project means deleting its
  `state/<uuid>/` by hand.

## 7. What botainer does NOT do

Stated so you do not go looking:

- **It does not purge `/scratch`.** Nothing in botainer deletes it. Whether
  anything else does depends entirely on the filesystem you put it on — a
  cluster scratch volume usually purges on a schedule; your laptop never will.
- **It does not use `$SLURM_TMPDIR`** or any node-local disk. Fast node-local
  scratch is not wired at v0.1.
- **`hpc setup` does not create or verify** your scratch directory today. It
  prints the template; making it interactive is a tracked improvement.
