# Example configurations

Drop-in `.botainer/config.yaml` for common scenarios. Copy the file
you want into your project's `.botainer/config.yaml` and tweak.

| File | Use case |
|---|---|
| `minimal.yaml` | Bare bones — just an agent, no internet, no extras. |
| `daily-dev.yaml` | Recommended starting point: internet, nudge, web-ports. |
| `secure.yaml` | Proxy mode, no internet, network locked down. |
| `hpc-slurm.yaml` | HPC: Slurm + Apptainer + module loading. |
| `notebook.yaml` | Jupyter/Streamlit/Gradio forwarding for data-science work. |

Each example has comments explaining the trade-offs.
