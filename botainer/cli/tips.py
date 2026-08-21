"""`botainer tips` — show the full list of user tips.

One tip is shown as a footer after each command; this command is the clear place
to browse them all (grouped by source: core + each installed plugin's own tips)."""
from __future__ import annotations

from collections import OrderedDict

import click


@click.command("tips")
def tips() -> None:
    """Show all botainer tips (one is shown after each command you run).

    Tips come from botainer core plus every installed plugin's own list
    (`contributes.user_tips`). Silence the per-command footer with
    BOTAINER_NO_TIPS=1.
    """
    from botainer.tips import collect_tips

    all_tips = collect_tips(enabled=None)  # None → base + every installed plugin
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    for t in all_tips:
        groups.setdefault(t.source, []).append(t.text)

    n = 1
    for source, texts in groups.items():
        label = "botainer (core)" if source == "botainer" else f"plugin: {source}"
        click.echo(click.style(f"\n{label}", bold=True))
        for text in texts:
            click.echo(f"  {n:>2}. {text}")
            n += 1

    click.echo(
        "\nOne of these is shown after each command (on a terminal). "
        "Silence: BOTAINER_NO_TIPS=1."
    )
