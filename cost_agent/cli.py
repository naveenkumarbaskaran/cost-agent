"""
CLI entry point for cost-agent.

Commands
--------
  analyze         Run full Claude-powered analysis and write a savings report.
  estimate-savings  Quick savings estimate (no report written).

Examples
--------
  cost-agent analyze --cost-report billing.csv --output savings-report.md
  cost-agent estimate-savings --cost-report billing.csv
"""

from __future__ import annotations

import json
import sys

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

from .agent import CostAgent
from .analyzer import CostAnalyzer

console = Console()


# ── helpers ───────────────────────────────────────────────────────────────────

def _require_api_key() -> None:
    """Exit with a helpful message if the API key is missing."""
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[bold red]Error:[/] ANTHROPIC_API_KEY environment variable is not set.\n"
            "Export it before running:  export ANTHROPIC_API_KEY=sk-ant-…"
        )
        sys.exit(1)


# ── CLI group ─────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(package_name="cost-agent-ai")
def cli() -> None:
    """cost-agent — Cloud cost optimisation powered by Claude AI."""


# ── analyze command ───────────────────────────────────────────────────────────

@cli.command("analyze")
@click.option(
    "--cost-report",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to the AWS Cost Explorer CSV export.",
)
@click.option(
    "--output",
    default="savings-report.md",
    show_default=True,
    help="Destination path for the Markdown savings report.",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Stream tool-call details to stdout.",
)
def analyze(
    cost_report: str,
    output: str,
    verbose: bool,
) -> None:
    """
    Analyse an AWS Cost Explorer billing export and write a prioritised
    savings report.

    Claude reads the CSV, identifies idle resources, right-sizing
    opportunities, and reserved-instance candidates, then writes
    a concrete Markdown report.
    """
    _require_api_key()

    with console.status(
        f"[bold green]Analysing {cost_report} with Claude…[/]", spinner="dots"
    ):
        agent = CostAgent()
        result = agent.analyze(cost_report, output=output, verbose=verbose)

    console.print()
    console.print(
        Panel(
            result[:600] + ("…" if len(result) > 600 else ""),
            title="[bold cyan]Claude's Summary[/]",
            border_style="cyan",
        )
    )
    console.print(f"\n[bold green]✓[/] Savings report written to [bold]{output}[/]")


# ── estimate-savings command ──────────────────────────────────────────────────

@cli.command("estimate-savings")
@click.option(
    "--cost-report",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to the AWS Cost Explorer CSV export.",
)
@click.option(
    "--json-output",
    is_flag=True,
    default=False,
    help="Print raw JSON instead of the formatted table.",
)
def estimate_savings(cost_report: str, json_output: bool) -> None:
    """
    Quickly estimate potential savings without running the full analysis.

    Parses the CSV locally, then asks Claude to rank the top opportunities.
    No report file is written.
    """
    _require_api_key()

    with console.status(
        "[bold green]Estimating savings…[/]", spinner="dots"
    ):
        agent = CostAgent()
        result = agent.estimate_savings(cost_report)

    if json_output:
        click.echo(json.dumps(result, indent=2))
        return

    # ── render a rich table ───────────────────────────────────────────
    console.print()
    console.print(
        Panel(
            f"[bold]Total spend:[/]  ${result['total_cost']:,.2f}\n"
            f"[bold]Waste score:[/]  {result['waste_score']:.1f} / 10\n"
            f"[bold]Est. savings:[/] ${result['potential_saving']:,.2f}",
            title="[bold cyan]Cost Summary[/]",
            border_style="cyan",
        )
    )

    table = Table(
        title="Top Savings Opportunities",
        show_header=True,
        header_style="bold magenta",
        border_style="dim",
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Service", style="bold")
    table.add_column("Issue")
    table.add_column("Est. Saving (USD)", justify="right", style="green")
    table.add_column("Priority", justify="center")

    priority_style = {"high": "bold red", "medium": "yellow", "low": "dim"}

    for idx, opp in enumerate(result.get("top_opportunities", []), start=1):
        saving = opp.get("estimated_saving_usd", 0)
        priority = str(opp.get("priority", "-")).lower()
        style = priority_style.get(priority, "")
        table.add_row(
            str(idx),
            str(opp.get("service", "—")),
            str(opp.get("issue", "—")),
            f"${saving:,.0f}",
            f"[{style}]{priority.upper()}[/]" if style else priority.upper(),
        )

    console.print(table)


# ── local CSV inspection command (bonus, no Claude required) ──────────────────

@cli.command("inspect")
@click.option(
    "--cost-report",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to the AWS Cost Explorer CSV export.",
)
def inspect(cost_report: str) -> None:
    """
    Inspect a billing CSV locally (no Claude / API key required).

    Prints a service breakdown, anomalies, idle resources, and waste score.
    """
    analyzer = CostAnalyzer(cost_report)

    with console.status("[bold green]Parsing CSV…[/]", spinner="dots"):
        summary = analyzer.analyze()

    console.print()
    console.print(
        Panel(
            f"[bold]Total cost:[/]  ${summary['total_cost']:,.2f}\n"
            f"[bold]Services:[/]    {len(summary['services'])}\n"
            f"[bold]Anomalies:[/]   {len(summary['anomalies'])}\n"
            f"[bold]Idle flags:[/]  {len(summary['idle_resources'])}\n"
            f"[bold]RI candidates:[/] {len(summary['ri_candidates'])}\n"
            f"[bold]Waste score:[/] {summary['waste_score']:.1f} / 10",
            title=f"[bold cyan]Inspection: {cost_report}[/]",
            border_style="cyan",
        )
    )

    if summary["by_service"]:
        tbl = Table("Service", "Total Cost (USD)", title="Spend by Service",
                    show_header=True, header_style="bold magenta")
        for svc, cost in list(summary["by_service"].items())[:15]:
            tbl.add_row(svc, f"${cost:,.2f}")
        console.print(tbl)

    if summary["anomalies"]:
        console.print("\n[bold yellow]Anomalies detected:[/]")
        for a in summary["anomalies"][:5]:
            rprint(
                f"  • [red]{a['service']}[/] on {a['date']}: "
                f"${a['cost']:.2f} (z={a['zscore']:.1f}, mean=${a['mean_daily_cost']:.2f})"
            )

    if summary["ri_candidates"]:
        console.print("\n[bold yellow]Reserved Instance candidates:[/]")
        for r in summary["ri_candidates"][:5]:
            rprint(
                f"  • [cyan]{r['service']}[/] — "
                f"save ~${r['estimated_monthly_saving']:.0f}/mo"
            )


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    cli()


if __name__ == "__main__":
    main()
