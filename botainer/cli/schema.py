"""`botainer schema <kind>` — emit JSON Schemas for IDE / LLM use.

Schemas help editors validate .botainer/config.yaml and policy.yaml as
you type, and let LLMs generate valid configs without grepping the
source code.

Available schemas:
  config     — project .botainer/config.yaml
  policy     — host-wide policy.yaml
  plugin     — plugin manifest (botainer-plugin.yaml)
  session    — emitted SessionSpec (what `botainer inspect --json` produces)
"""

from __future__ import annotations

import json

import click


@click.group("schema", invoke_without_command=True)
@click.pass_context
def schema(ctx: click.Context) -> None:
    """Emit JSON Schemas for IDE / LLM use."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@schema.command("config")
@click.option("--pretty", is_flag=True, help="Indent for readability.")
def config_schema(pretty: bool) -> None:
    """Emit the JSON Schema for .botainer/config.yaml.

    Save as e.g. .vscode/schemas/botainer-config.json and reference from
    YAML language server (.vscode/settings.json yaml.schemas) to get
    autocomplete + validation in your editor.
    """
    from botainer.core.config import ProjectConfig
    _emit(ProjectConfig.model_json_schema(), pretty)


@schema.command("policy")
@click.option("--pretty", is_flag=True, help="Indent for readability.")
def policy_schema(pretty: bool) -> None:
    """Emit the JSON Schema for the host policy.yaml."""
    from botainer.core.policy import SitePolicy
    _emit(SitePolicy.model_json_schema(), pretty)


@schema.command("plugin")
@click.option("--pretty", is_flag=True, help="Indent for readability.")
def plugin_schema(pretty: bool) -> None:
    """Emit the JSON Schema for a plugin manifest (botainer-plugin.yaml)."""
    from botainer.plugins.manifest import PluginManifest
    _emit(PluginManifest.model_json_schema(), pretty)


@schema.command("session")
@click.option("--pretty", is_flag=True, help="Indent for readability.")
def session_schema(pretty: bool) -> None:
    """Emit the JSON Schema for a SessionSpec (what `botainer inspect --json` produces)."""
    from botainer.core.spec import SessionSpec
    _emit(SessionSpec.model_json_schema(), pretty)


def _emit(schema_dict: dict[str, object], pretty: bool) -> None:
    if pretty:
        click.echo(json.dumps(schema_dict, indent=2, sort_keys=True))
    else:
        click.echo(json.dumps(schema_dict, sort_keys=True))
