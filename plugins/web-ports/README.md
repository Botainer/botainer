# web-ports — forward container ports to host

When the agent runs `jupyter lab` or `streamlit run` inside the
container, you want to open it in your host browser. `web-ports`
declares `docker -p` flags so localhost:8888 reaches the container.

## Setup

```yaml
plugins_enabled:
  - agent-claude
  - git
  - web-ports

plugins:
  web-ports:
    ports:
      - 8888                                          # forwards 8888 ↔ 8888
      - {container: 7860, host: 7860, label: "gradio"}
      - {container: 8501, host: 8501, label: "streamlit"}
```

The capability summary at launch shows:

```
Web ports forwarded:
  http://127.0.0.1:8888 → container:8888
  http://127.0.0.1:7860 → container:7860 (gradio)
  http://127.0.0.1:8501 → container:8501 (streamlit)
  ⚠ any process with host shell access can reach these ports
```

## Inside the container

Bind your web server to `0.0.0.0` (not `127.0.0.1`), because the
container's loopback isn't reachable from the host:

```sh
jupyter lab --ip 0.0.0.0 --no-browser --port 8888
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

## Security

- Default bind is loopback only (`127.0.0.1`). Anyone with shell access
  to your host can reach the port; not other hosts on the network.
- `host_bind: 0.0.0.0` is **refused**. So are `::`, empty string, `0`,
  bare hostnames, and `*` — all of which Docker would treat as "all
  interfaces."
- Apptainer doesn't support port forwarding directly; the web-ports
  plugin refuses on apptainer runtime. For HPC, use `ssh -L 8888:...`
  from your laptop to the login node.

See DN-038 for the full design.
