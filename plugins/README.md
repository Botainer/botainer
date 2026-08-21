# Bundled plugins

Each subdirectory is a first-party plugin shipped with botainer. The
launcher discovers them under `plugins/<name>/` (dev) or
`<package>/_builtin_plugins/<name>/` (installed wheel).

Run `botainer plugin list --available` to see them all with one-line
descriptions. Run `botainer plugin info <name>` for any plugin's
manifest contents + config schema.

| Plugin | Role | Default? |
|---|---|---|
| `agent-claude` | Claude Code agent (per-project credentials) | default |
| `agent-claude-proxy` | Host-side credential proxy (real key stays on host) | opt-in |
| `git` | Protected-mode git (filtered config, disposable .git/config) | default |
| `hpc-launcher` | Slurm submission helper for Apptainer | opt-in (HPC) |
| `hpc-modules` | `module load` env capture + bind into container | opt-in (HPC) |
| `nudge` | Host-side screen-based input injection (`botainer nudge "text"`) | opt-in |
| `web-ports` | Forward TCP ports to host (Jupyter / Streamlit / Gradio) | opt-in |

## Authoring third-party plugins

The plugin manifest schema is at `botainer.plugins.manifest.PluginManifest`.
Run `botainer schema plugin --pretty` for the JSON Schema. Examples in
this directory are good starting points; copy one, edit, install with
`botainer plugin add file:///path/to/your-plugin/`.

Critical contract: plugin contributions go through `check_envelope`
against your manifest's `mount_target_prefixes`. Stay within what you
declare; the launcher refuses contributions outside.

## Trust model

The launcher ships with `trusted_plugins.lock` recording the SHA-256
tree hash of each bundled plugin. This lock is the integrity foundation,
kept fresh on every plugin-tree change (a pre-commit gate enforces it).

Note (AUDIT 2026-06-09 H9): the launcher does **NOT** re-hash and compare
the installed tree at every `botainer start`. A launcher-written runtime
hash is the wrong integrity boundary — an attacker with filesystem write to
the installed plugin dir also has write to the lock, and editable/dev
installs drift the hash, producing chronic false positives. The shipped lock
is instead the basis for **install-time** verification (wheel-signature
verification, v0.2), which is the correct boundary. `verify_plugin()` and the
lock infrastructure are retained for that path.
