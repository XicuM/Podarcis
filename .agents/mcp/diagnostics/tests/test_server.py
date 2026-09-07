"""Unit tests for diagnostics-mcp server tools."""
import json
import pytest
from pathlib import Path

import importlib.util
SERVER_DIR = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("diagnostics_mcp_server", SERVER_DIR / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def test_diagnostics_mcp_tools(tmp_path, monkeypatch):
    """Test diagnostics_log, diagnostics_list, and diagnostics_resolve tools."""
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "DIAGNOSTICS_DIR", tmp_path / ".podarcis" / "diagnostics")
    monkeypatch.setattr(server, "PAIN_POINTS_FILE", tmp_path / ".podarcis" / "diagnostics" / "pain_points.jsonl")

    # 1. Log issue
    res = server.diagnostics_log(
        category="command_failure",
        summary="Pytest command failed",
        details="Exit code 1",
        severity="high",
    )
    assert "Successfully logged pain point" in res

    # 2. Get active issues
    issues_str = server.diagnostics_list()
    assert "Pytest command failed" in issues_str
    issues = json.loads(issues_str)
    assert len(issues) == 1
    assert issues[0]["category"] == "command_failure"

    # 3. Filter by category
    assert "No active platform pain points found" in server.diagnostics_list(category="user_correction")

    # 4. Resolution is a CLI action now: see test_diagnose.py.


def test_diagnostics_sanitization(tmp_path, monkeypatch):
    """Test that sensitive user data, secrets, and home paths are redacted."""
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "DIAGNOSTICS_DIR", tmp_path / ".podarcis" / "diagnostics")
    monkeypatch.setattr(server, "PAIN_POINTS_FILE", tmp_path / ".podarcis" / "diagnostics" / "pain_points.jsonl")

    raw_summary = "Failed accessing /home/xicu/secret.key with api_key=sk-1234567890abcdef1234567890"
    raw_details = f"File in {tmp_path}/workspace/secret.md leaked user@example.com with Bearer abcdef1234567890abcdef1234567890"

    server.diagnostics_log(
        category="execution_error",
        summary=raw_summary,
        details=raw_details,
    )

    issues = json.loads(server.diagnostics_list())
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



def test_malformed_line_raises_rather_than_vanishing(tmp_path, monkeypatch):
    """A corrupt line must raise, not be silently skipped on read."""
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "DIAGNOSTICS_DIR", tmp_path / ".podarcis" / "diagnostics")
    pp = tmp_path / ".podarcis" / "diagnostics" / "pain_points.jsonl"
    monkeypatch.setattr(server, "PAIN_POINTS_FILE", pp)

    server.diagnostics_log(category="friction", summary="real issue")
    with open(pp, "a", encoding="utf-8") as f:
        f.write("{not json at all\n")

    with pytest.raises(json.JSONDecodeError):
        server.diagnostics_list()
    assert "{not json at all" in pp.read_text(encoding="utf-8")
