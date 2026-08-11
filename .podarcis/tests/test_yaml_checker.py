import os
import tempfile
import pytest
from pathlib import Path

from check_links import check_file, check_yaml_file, try_fix_unquoted_colons

def test_try_fix_unquoted_colons():
    fm = "title: Protocol: Social Life\ncategory: relationships\nrationale: test"
    fixed, changed = try_fix_unquoted_colons(fm)
    assert changed is True
    assert 'title: "Protocol: Social Life"' in fixed

def test_check_yaml_file_valid(tmp_path):
    f = tmp_path / "valid.yaml"
    f.write_text("name: Podarcis\nversion: 1.0\n", encoding="utf-8")
    res = check_yaml_file(str(f))
    assert res.get("yaml_errors") == []

def test_check_yaml_file_invalid(tmp_path):
    f = tmp_path / "invalid.yaml"
    f.write_text("name: Podarcis\n  version: : 1.0\n", encoding="utf-8")
    res = check_yaml_file(str(f))
    assert len(res.get("yaml_errors")) > 0
    assert "Invalid YAML syntax" in res["yaml_errors"][0]

def test_check_file_frontmatter_validation(tmp_path):
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    f = wiki_dir / "note.md"
    f.write_text("---\ntitle: \"Test Note\"\ntype: concept\ncategory: test\nrationale: rationale text\n---\nBody text\n", encoding="utf-8")
    
    res = check_file(str(f))
    assert res.get("yaml_errors") == []
    assert res.get("missing_frontmatter") == []

def test_check_file_frontmatter_unquoted_colon_auto_fix(tmp_path):
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    f = wiki_dir / "note.md"
    f.write_text("---\ntitle: Protocol: State Transitions\ntype: protocol\ncategory: psychology\nrationale: test\n---\nBody text\n", encoding="utf-8")
    
    res = check_file(str(f), do_fix=True)
    updated_content = f.read_text(encoding="utf-8")
    assert 'title: "Protocol: State Transitions"' in updated_content
