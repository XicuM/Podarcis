"""Unit tests for diagnostics-mcp server tools."""
import json
import pytest
from pathlib import Path

import sys
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import server


def test_diagnostics_mcp_tools(tmp_path, monkeypatch):
    """Test log_pain_point, get_pain_points, and clear_pain_points tools."""
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "DIAGNOSTICS_DIR", tmp_path / ".podarcis" / "diagnostics")
    monkeypatch.setattr(server, "PAIN_POINTS_FILE", tmp_path / ".podarcis" / "diagnostics" / "pain_points.jsonl")

    # 1. Log issue
    res = server.log_pain_point(
        category="command_failure",
        summary="Pytest command failed",
        details="Exit code 1",
        severity="high",
    )
    assert "Successfully logged pain point" in res

    # 2. Get active issues
    issues_str = server.get_pain_points()
    assert "Pytest command failed" in issues_str
    issues = json.loads(issues_str)
    assert len(issues) == 1
    assert issues[0]["category"] == "command_failure"

    # 3. Filter by category
    assert "No active platform pain points found" in server.get_pain_points(category="user_correction")

    # 4. Clear issues
    clear_res = server.clear_pain_points()
    assert "Marked 1 platform pain point(s) as resolved" in clear_res

    # 5. Verify empty
    assert "No active platform pain points found" in server.get_pain_points()


def test_diagnostics_sanitization(tmp_path, monkeypatch):
    """Test that sensitive user data, secrets, and home paths are redacted."""
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "DIAGNOSTICS_DIR", tmp_path / ".podarcis" / "diagnostics")
    monkeypatch.setattr(server, "PAIN_POINTS_FILE", tmp_path / ".podarcis" / "diagnostics" / "pain_points.jsonl")

    raw_summary = "Failed accessing /home/xicu/secret.key with api_key=sk-1234567890abcdef1234567890"
    raw_details = f"File in {tmp_path}/workspace/secret.md leaked user@example.com with Bearer abcdef1234567890abcdef1234567890"

    server.log_pain_point(
        category="execution_error",
        summary=raw_summary,
        details=raw_details,
    )

    issues = json.loads(server.get_pain_points())
    logged = issues[0]

    # Verify no raw sensitive data leaked
    assert "xicu" not in logged["summary"]
    assert "<HOME>" in logged["summary"]
    assert "sk-1234567890abcdef1234567890" not in logged["summary"]
    assert "[REDACTED_API_KEY]" in logged["summary"]
    assert "user@example.com" not in logged["details"]
    assert "[REDACTED_EMAIL]" in logged["details"]
    assert "[REDACTED_TOKEN]" in logged["details"]
    assert str(tmp_path) not in logged["details"]
    assert "<PROJECT_ROOT>" in logged["details"]

