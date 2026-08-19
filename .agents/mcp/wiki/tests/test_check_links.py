import sys
import os
import pytest

# Add wiki MCP directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import check_links

def test_check_links_link_parsing():
    # Test internal helper in check_links if any
    assert hasattr(check_links, "check_file")


def test_positional_footnotes_flagged(tmp_path):
    p = tmp_path / "numeric.md"
    p.write_text("---\ntitle: T\ntype: concept\ncategory: test\nrationale: t\n---\nClaim.[^1] Another.[^2]\n\n[^1]: Foo\n[^2]: Bar\n")
    res = check_links.check_file(str(p))
    assert res.get("positional_footnotes") == ["1", "2"]


def test_named_footnotes_not_flagged(tmp_path):
    p = tmp_path / "named.md"
    p.write_text("---\ntitle: T\ntype: concept\ncategory: test\nrationale: t\n---\nClaim.[^smith2024]\n\n[^smith2024]: Smith\n")
    res = check_links.check_file(str(p))
    assert res.get("positional_footnotes") == []
