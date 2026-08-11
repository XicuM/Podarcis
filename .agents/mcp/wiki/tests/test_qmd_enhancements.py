import asyncio
import importlib.util
from pathlib import Path
import pytest

_SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
_spec = importlib.util.spec_from_file_location("wiki_server_mod", _SERVER_PATH)
wiki_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wiki_server)

def test_wiki_get_line_slicing(tmp_path, monkeypatch):
    """Verify wiki_get slices document lines correctly."""
    async def _run():
        monkeypatch.setattr(wiki_server, "ROOT", tmp_path)

        test_file = tmp_path / "wiki" / "test_doc.md"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        content_lines = [f"Line {i}" for i in range(1, 21)]
        test_file.write_text("\n".join(content_lines), encoding="utf-8")

        # Test full fetch
        res_full = await wiki_server.wiki_get("wiki/test_doc.md")
        assert "Line 1" in res_full
        assert "Line 20" in res_full

        # Test sliced fetch (lines 5 to 8)
        res_sliced = await wiki_server.wiki_get("wiki/test_doc.md", start_line=5, num_lines=4)
        assert "[Lines 5-8 of 20]" in res_sliced
        assert "Line 5" in res_sliced
        assert "Line 8" in res_sliced
        assert "Line 4" not in res_sliced
        assert "Line 9" not in res_sliced

    asyncio.run(_run())


def test_wiki_search_hyde_and_explain(tmp_path, monkeypatch):
    """Verify wiki_search constructs structured query document when hyde or explain flags are set."""
    async def _run():
        monkeypatch.setattr(wiki_server, "ROOT", tmp_path)
        monkeypatch.setattr(wiki_server, "get_qmd_status", lambda: ("enabled_ok", "/bin/qmd"))

        captured_args = []

        async def mock_qmd(*args, json_output=False):
            captured_args.extend(args)
            return "mock QMD search result"

        monkeypatch.setattr(wiki_server, "_qmd", mock_qmd)

        # Search with HyDE and explain
        res = await wiki_server.wiki_search("creatine BBB", hyde="Creatine crosses BBB via SLC6A8", explain=True, no_rerank=True)

        assert res == "mock QMD search result"
        assert any("intent: creatine BBB" in a for a in captured_args)
        assert any("hyde: Creatine crosses BBB via SLC6A8" in a for a in captured_args)
        assert "--explain" in captured_args
        assert "--no-rerank" in captured_args

    asyncio.run(_run())


def test_wiki_multi_get_fallback(tmp_path, monkeypatch):
    """Verify wiki_multi_get retrieves snippets across files."""
    async def _run():
        monkeypatch.setattr(wiki_server, "ROOT", tmp_path)
        monkeypatch.setattr(wiki_server, "get_qmd_status", lambda: ("disabled", "Disabled"))

        (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
        f1 = tmp_path / "wiki" / "doc1.md"
        f2 = tmp_path / "wiki" / "doc2.md"

        f1.write_text("# Doc 1\nContent of doc 1", encoding="utf-8")
        f2.write_text("# Doc 2\nContent of doc 2", encoding="utf-8")

        res = await wiki_server.wiki_multi_get("wiki/*.md", max_lines=10)
        assert "=== File: wiki/doc1.md ===" in res
        assert "=== File: wiki/doc2.md ===" in res
        assert "# Doc 1" in res
        assert "# Doc 2" in res

    asyncio.run(_run())
