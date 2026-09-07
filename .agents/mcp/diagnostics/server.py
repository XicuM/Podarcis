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
        "Call diagnostics_log whenever you encounter tool errors, execution failures, user corrections, or results that fail to meet user expectations. "
        "Call diagnostics_list to retrieve active issues when instructed to improve the platform, "
        "Resolving them is the operator job, via `podarcis diagnose --resolve <id>`."
    ),
)

_DIAG_DIR = Path(__file__).resolve().parent
if str(_DIAG_DIR) not in sys.path:
    sys.path.insert(0, str(_DIAG_DIR))
from sanitizer import sanitize_text


def _read_records() -> list[dict]:
    """Every pain point record, oldest first.

    A malformed line raises rather than being skipped: diagnostics_resolve
    rewrites the whole file from this list, so swallowing a bad line would
    silently delete it.
    """
    lines = PAIN_POINTS_FILE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def diagnostics_log(
    category: Annotated[str, "Issue category: command_failure, execution_error, user_correction, or friction"],
    summary: Annotated[str, "Single-line summary of the pain point, user correction, or unmet expectation"],
    details: Annotated[str, "Optional detailed error traceback, output context, or user guidance"] = "",
    severity: Annotated[Literal["low", "medium", "high"], "Issue severity rating"] = "medium",
) -> str:
    """Log a runtime friction point, execution error, user correction, or unmet expectation to .podarcis/diagnostics/pain_points.jsonl."""
    _ensure_dirs()
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    issue_id = f"diag-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}"

    # Redact sensitive data, secrets, and absolute user paths before persisting
    sanitized_summary = sanitize_text(summary, root_dir=ROOT)
    sanitized_details = sanitize_text(details, root_dir=ROOT)

    record = {
        "id": issue_id,
        "timestamp": timestamp,
        "category": category,
        "summary": sanitized_summary,
        "details": sanitized_details,
        "severity": severity,
        "resolved": False,
    }

    with open(PAIN_POINTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return f"Successfully logged pain point [{issue_id}]: {sanitized_summary}"


@mcp.tool()
def diagnostics_list(
    category: Annotated[str, "Optional category filter (e.g. command_failure, user_correction)"] = "",
) -> str:
    """Retrieve all active, unresolved platform pain points from .podarcis/diagnostics/."""
    _ensure_dirs()
    if not PAIN_POINTS_FILE.exists():
        return "No active platform pain points found."

    issues = [
        r for r in _read_records()
        if not r["resolved"] and category in ("", r["category"])
    ]
    return json.dumps(issues, indent=2) if issues else "No active platform pain points found."


if __name__ == "__main__":
    mcp.run()
