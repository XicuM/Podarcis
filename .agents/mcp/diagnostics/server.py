"""diagnostics-mcp — FastMCP server for logging and querying platform pain points.

Provides tools for agents to log runtime friction, errors, and user corrections,
as well as retrieving logged issues for platform self-improvement.
Set PROJECT_ROOT env var to the repository root.
"""
from __future__ import annotations

import json
import os
import sys
import datetime
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP

# ── Path bootstrap ────────────────────────────────────────────────────────────

def _find_root() -> Path:
    env = os.environ.get("PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "AGENTS.md").exists():
            return parent
    raise RuntimeError("Cannot locate project root. Set the PROJECT_ROOT environment variable.")

ROOT = _find_root()
DIAGNOSTICS_DIR = ROOT / ".podarcis" / "diagnostics"
PAIN_POINTS_FILE = DIAGNOSTICS_DIR / "pain_points.jsonl"

def _ensure_dirs() -> None:
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    (DIAGNOSTICS_DIR / "sessions").mkdir(parents=True, exist_ok=True)

# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "diagnostics-mcp",
    instructions=(
        "Platform diagnostic logger and issue inspector. "
        "Call log_pain_point whenever you encounter tool errors, execution failures, user corrections, or results that fail to meet user expectations. "
        "Call get_pain_points to retrieve active issues when instructed to improve the platform."
    ),
)

# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def log_pain_point(
    category: Annotated[str, "Issue category: command_failure, execution_error, user_correction, or friction"],
    summary: Annotated[str, "Single-line summary of the pain point, user correction, or unmet expectation"],
    details: Annotated[str, "Optional detailed error traceback, output context, or user guidance"] = "",
    severity: Annotated[Literal["low", "medium", "high"], "Issue severity rating"] = "medium",
) -> str:
    """Log a runtime friction point, execution error, user correction, or unmet expectation to .podarcis/diagnostics/pain_points.jsonl."""
    _ensure_dirs()
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    issue_id = f"diag-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}"

    record = {
        "id": issue_id,
        "timestamp": timestamp,
        "category": category,
        "summary": summary,
        "details": details,
        "severity": severity,
        "resolved": False,
    }

    with open(PAIN_POINTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return f"Successfully logged pain point [{issue_id}]: {summary}"


@mcp.tool()
def get_pain_points(
    category: Annotated[str, "Optional category filter (e.g. command_failure, user_correction)"] = "",
) -> str:
    """Retrieve all active, unresolved platform pain points from .podarcis/diagnostics/."""
    _ensure_dirs()
    if not PAIN_POINTS_FILE.exists():
        return "No active platform pain points found."

    issues = []
    with open(PAIN_POINTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if not data.get("resolved", False):
                    if not category or data.get("category") == category:
                        issues.append(data)
            except json.JSONDecodeError:
                continue

    if not issues:
        return "No active platform pain points found."

    return json.dumps(issues, indent=2)


@mcp.tool()
def clear_pain_points() -> str:
    """Mark all recorded platform pain points as resolved."""
    _ensure_dirs()
    if not PAIN_POINTS_FILE.exists():
        return "No pain points to clear."

    count = 0
    updated_lines = []
    with open(PAIN_POINTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if not data.get("resolved", False):
                    data["resolved"] = True
                    count += 1
                updated_lines.append(json.dumps(data))
            except json.JSONDecodeError:
                continue

    with open(PAIN_POINTS_FILE, "w", encoding="utf-8") as f:
        for line in updated_lines:
            f.write(line + "\n")

    return f"Marked {count} platform pain point(s) as resolved."


if __name__ == "__main__":
    mcp.run()
