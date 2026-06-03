"""
CostAgent: Claude-powered agent for AWS/GCP cost analysis.

Uses claude-sonnet-4-6 with tool use to:
  - read_file      : read any local file
  - fetch_cost_csv : parse an AWS Cost Explorer CSV export
  - write_report   : persist the final markdown savings report
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import anthropic

from .analyzer import CostAnalyzer

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are a cloud cost optimization expert specialising in AWS and GCP.
You have access to three tools:

  1. fetch_cost_csv  — parse an AWS Cost Explorer CSV export and return
     a structured JSON summary (services, tags, anomalies, waste score).
  2. read_file       — read any local text file (configs, previous reports…).
  3. write_report    — write a Markdown savings report to disk.

When asked to analyse a billing report you MUST:
  a. Call fetch_cost_csv to obtain the structured data.
  b. Reason carefully about:
       • Idle / unused resources (zero or near-zero utilisation).
       • Right-sizing opportunities (oversized instance families).
       • Reserved-instance / savings-plan candidates (steady-state workloads).
       • Cost anomalies (sudden spikes vs. the rolling average).
  c. Produce a concrete, prioritised Markdown savings report and call
     write_report to persist it.

Be specific: name the service, the account/tag, the current spend, the
estimated saving, and the recommended action. Avoid vague advice.
"""

# ── tool schemas ─────────────────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read the full text content of a local file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "fetch_cost_csv",
        "description": (
            "Parse an AWS Cost Explorer CSV export located at `path`. "
            "Returns a JSON object with keys: services (list), total_cost, "
            "by_service (dict), by_tag (dict), anomalies (list), waste_score (float 0-10)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the AWS Cost Explorer CSV file.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_report",
        "description": "Write a Markdown savings report to a local file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Destination file path for the report.",
                },
                "content": {
                    "type": "string",
                    "description": "Full Markdown content of the savings report.",
                },
            },
            "required": ["path", "content"],
        },
    },
]


# ── tool execution ────────────────────────────────────────────────────────────

def _execute_tool(name: str, tool_input: dict[str, Any]) -> str:
    """Dispatch a tool call and return the result as a string."""
    if name == "read_file":
        file_path = Path(tool_input["path"])
        if not file_path.exists():
            return json.dumps({"error": f"File not found: {file_path}"})
        return file_path.read_text(encoding="utf-8")

    elif name == "fetch_cost_csv":
        csv_path = tool_input["path"]
        analyzer = CostAnalyzer(csv_path)
        summary = analyzer.analyze()
        return json.dumps(summary, indent=2)

    elif name == "write_report":
        report_path = Path(tool_input["path"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(tool_input["content"], encoding="utf-8")
        return json.dumps({"status": "ok", "path": str(report_path)})

    return json.dumps({"error": f"Unknown tool: {name}"})


# ── main agent class ──────────────────────────────────────────────────────────

class CostAgent:
    """
    Agentic loop that drives Claude to analyse cloud costs and emit a
    savings report.

    Usage::

        agent = CostAgent()
        report_text = agent.analyze(
            cost_report="billing.csv",
            output="savings-report.md",
        )
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    # ------------------------------------------------------------------
    def analyze(
        self,
        cost_report: str,
        output: str = "savings-report.md",
        *,
        verbose: bool = False,
    ) -> str:
        """
        Run the full cost-analysis agentic loop.

        Parameters
        ----------
        cost_report:
            Path to an AWS Cost Explorer CSV export.
        output:
            Destination path for the Markdown savings report.
        verbose:
            If True, stream thinking/tool-call details to stdout.

        Returns
        -------
        The final assistant text (summary / confirmation).
        """
        user_message = (
            f"Please analyse the AWS billing report at `{cost_report}` and "
            f"write a prioritised savings report to `{output}`.  "
            "Identify idle resources, right-sizing opportunities, and "
            "reserved-instance candidates.  Be specific with numbers."
        )

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message}
        ]

        if verbose:
            print(f"[cost-agent] Starting analysis of {cost_report}")

        # ── agentic loop ──────────────────────────────────────────────
        while True:
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=8192,
                system=SYSTEM_PROMPT,
                tools=TOOLS,  # type: ignore[arg-type]
                messages=messages,
            )

            # Append the full assistant response to history
            messages.append({"role": "assistant", "content": response.content})

            if verbose:
                for block in response.content:
                    if hasattr(block, "text"):
                        print(f"[assistant] {block.text[:200]}…")
                    elif block.type == "tool_use":
                        print(f"[tool_use ] {block.name}({json.dumps(block.input)[:120]})")

            # Check stop reason
            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                # Unexpected stop — surface to caller
                break

            # Execute every tool call in this turn
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                result_text = _execute_tool(block.name, block.input)  # type: ignore[arg-type]

                if verbose:
                    print(f"[tool_result] {block.name} → {result_text[:120]}…")

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        # Extract final text
        final_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                final_text += block.text

        return final_text

    # ------------------------------------------------------------------
    def estimate_savings(self, cost_report: str) -> dict[str, Any]:
        """
        Quick estimate of potential savings without writing a full report.

        Returns a dict with keys: total_cost, potential_saving,
        waste_score, top_opportunities.
        """
        analyzer = CostAnalyzer(cost_report)
        summary = analyzer.analyze()

        # Use Claude to interpret the numbers and rank opportunities
        prompt = (
            "Given this cost summary in JSON, estimate the top 5 savings "
            "opportunities as a JSON array with fields: service, issue, "
            "estimated_saving_usd, priority (high/medium/low).\n\n"
            f"{json.dumps(summary, indent=2)}"
        )

        response = self._client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system="You are a cloud cost optimisation expert.  Reply ONLY with valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        )

        raw = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw += block.text

        # Strip markdown fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]

        opportunities = json.loads(raw)
        return {
            "total_cost": summary.get("total_cost", 0.0),
            "waste_score": summary.get("waste_score", 0.0),
            "potential_saving": sum(
                op.get("estimated_saving_usd", 0) for op in opportunities
            ),
            "top_opportunities": opportunities,
        }
