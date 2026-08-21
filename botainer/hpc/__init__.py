"""HPC-specific pure helpers (no botainer-internal imports).

Modules here are intentionally dependency-light so they can be consumed by
BOTH the botainer package (e.g. core.composition) and the standalone
hpc-launcher host_helper scripts (which run under botainer's interpreter via
the plugin dispatcher's sys.executable, but follow a no-deep-import
convention). Keep everything in this package stdlib-only.
"""
