import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Resolve paths to import the harness scripts
harness_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if harness_path not in sys.path:
    sys.path.append(harness_path)

import runtime_gate

import core_io_helper

def test_approved_domains_network_gate():
    """Verify that pre-approved domains pass the network gate immediately."""
    for domain in runtime_gate.APPROVED_DOMAINS:
        url = f"https://{domain}/some/api/path?param=value"
        assert runtime_gate.check_permission("network", url) is True

def test_unapproved_domain_network_gate_interactive():
    """Verify that unapproved domains prompt user input and raise error on deny."""
    # Test denial
    with patch("sys.stdin.readline", return_value="n\n"), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdout.write") as mock_write:
        with pytest.raises(PermissionError) as exc_info:
            runtime_gate.check_permission("network", "https://unapproved-domain.com/data")
        assert "refused by user" in str(exc_info.value)
        mock_write.assert_called_once()

    # Test approval
    with patch("sys.stdin.readline", return_value="y\n"), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdout.write"):
        assert runtime_gate.check_permission("network", "https://unapproved-domain.com/data") is True

def test_unapproved_domain_non_interactive():
    """Verify that unapproved domains fail safe in non-interactive environments."""
    with patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(PermissionError) as exc_info:
            runtime_gate.check_permission("network", "https://unapproved-domain.com/data")
        assert "blocked in non-interactive environment" in str(exc_info.value)

def test_context_compaction_no_op_on_short_text():
    """Verify context compaction is a no-op when text size is below the limit."""
    short_text = "This is a short raw markdown source file."
    assert core_io_helper.compact_text(short_text, "dummy_path.md") == short_text

def test_context_compaction_large_text():
    """Verify context compaction prunes text, preserves frontmatter and footnotes/references."""
    frontmatter = "---\ntags: [test]\n---\n"
    intro = "Introduction segment of the paper."
    large_section = "\n# Technical Details\n" + "x" * 41000  # Will trigger pruning
    ref_section = "\n# Footnotes & References\n" + "[^1]: Semantic Scholar link.\n[^2]: arXiv paper."
    
    full_text = f"{frontmatter}{intro}{large_section}{ref_section}"
    compacted = core_io_helper.compact_text(full_text, "sources/literature/ai_agents/semaclaw/raw.md")
    
    assert "Context Compacted" in compacted
    assert "tags: [test]" in compacted  # Frontmatter preserved
    assert "Technical Details" in compacted  # Header preserved
    assert "Footnotes & References" in compacted  # References header preserved
    assert "[^1]: Semantic Scholar link." in compacted  # References preserved
    assert len(compacted) < len(full_text)
