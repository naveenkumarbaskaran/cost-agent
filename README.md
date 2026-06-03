# cost-agent-ai

A Claude-powered CLI agent that analyses AWS Cost Explorer billing exports,
identifies waste, and produces a prioritised Markdown savings report.

## Features

- **Full AI analysis** — Claude drives a tool-use agentic loop to read your
  billing CSV, reason about idle resources, right-sizing opportunities, and
  Reserved-Instance candidates, then writes a concrete savings report.
- **Quick savings estimate** — Local CSV parsing + Claude ranking of the top
  5 opportunities, no report file required.
- **Local inspection** — Parse and display a billing CSV entirely offline
  (no API key needed).
- **Rich terminal UI** — Coloured tables, spinners, and panels via
  [Rich](https://github.com/Textualize/rich).

## Installation

```bash
pip install cost-agent-ai
```

Or from source:

```bash
git clone https://github.com/your-org/cost-agent-ai
cd cost-agent-ai
pip install -e .
```

## Prerequisites

1. An [Anthropic API key](https://console.anthropic.com/).
2. An AWS Cost Explorer CSV export (see below).

Export the API key:

```bash
export ANTHROPIC_API_KEY="sk-ant-…"
```

## Getting an AWS Cost Explorer CSV

1. Open the [AWS Cost Explorer](https://console.aws.amazon.com/cost-management/home#/custom).
2. Set your desired date range (e.g. last 30 days).
3. Group by **Service** and optionally by a tag (e.g. `Environment`).
4. Click **Download CSV**.

## Usage

### Full Analysis

```bash
cost-agent analyze --cost-report billing.csv --output savings-report.md
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--cost-report` | *(required)* | Path to the AWS Cost Explorer CSV export |
| `--output` | `savings-report.md` | Destination for the Markdown savings report |
| `--verbose` / `-v` | off | Stream tool-call details to stdout |

### Quick Savings Estimate

```bash
cost-agent estimate-savings --cost-report billing.csv
```

Prints a table of the top 5 savings opportunities with estimated dollar
values and priority ratings. Add `--json-output` for machine-readable JSON.

### Local CSV Inspection (no API key)

```bash
cost-agent inspect --cost-report billing.csv
```

Parses the CSV locally and prints:

- Spend breakdown by service
- Detected cost anomalies (z-score based)
- Idle resource flags
- Reserved-Instance candidates
- Waste score (0–10)

## What the Agent Analyses

| Category | What it looks for |
|---|---|
| **Idle resources** | Services with days of near-zero spend relative to their median |
| **Right-sizing** | Services whose spend profile suggests oversized instances |
| **RI / Savings Plan candidates** | EC2, RDS, ElastiCache, Redshift with steady-state spend |
| **Cost anomalies** | Daily spend spikes with z-score above 2.5 |

## Example Output

```
╭────────────── Cost Summary ──────────────╮
│ Total spend:   $4,821.34                  │
│ Waste score:   7.2 / 10                   │
│ Est. savings:  $1,340.00                  │
╰───────────────────────────────────────────╯

┌─────────────────────────────────────────────────────────────────┐
│ Top Savings Opportunities                                        │
├───┬───────────────┬──────────────────────────┬──────────┬───────┤
│ # │ Service       │ Issue                    │ Est.     │ Pri.  │
├───┼───────────────┼──────────────────────────┼──────────┼───────┤
│ 1 │ Amazon EC2    │ 3 idle instances detected │ $620     │ HIGH  │
│ 2 │ Amazon RDS    │ RI candidate (35% saving) │ $420     │ HIGH  │
│ 3 │ Amazon S3     │ Cost spike on 2024-11-03  │ $180     │ MED   │
│ 4 │ AWS Lambda    │ Unused scheduled invocs   │  $80     │ MED   │
│ 5 │ Amazon ECR    │ Untagged images           │  $40     │ LOW   │
└───┴───────────────┴──────────────────────────┴──────────┴───────┘
```

## Architecture

```
cost_agent/
├── __init__.py        Package exports
├── agent.py           CostAgent — Anthropic SDK agentic loop
│                        tools: read_file, fetch_cost_csv, write_report
├── analyzer.py        CostAnalyzer — pure-Python CSV parsing
│                        groups by service/tag, detects anomalies,
│                        idle resources, RI candidates, waste score
└── cli.py             Click CLI — analyze / estimate-savings / inspect
```

### How the Agent Loop Works

1. User invokes `cost-agent analyze`.
2. `CostAgent.analyze()` sends a task description to `claude-sonnet-4-6`.
3. Claude calls `fetch_cost_csv` → `CostAnalyzer.analyze()` returns JSON.
4. Claude reasons over the JSON and may call `read_file` for extra context.
5. Claude calls `write_report` with the full Markdown savings report.
6. The loop ends when Claude produces `stop_reason="end_turn"`.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
mypy cost_agent/
```

## Configuration

| Environment Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (required for `analyze` and `estimate-savings`) |

## License

MIT
