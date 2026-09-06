import asyncio
import importlib.util
from pathlib import Path
import pytest

_SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
_spec = importlib.util.spec_from_file_location("wiki_server_mod", _SERVER_PATH)
wiki_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wiki_server)


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
    """Verify wiki_fetch retrieves snippets across files."""
    async def _run():
        monkeypatch.setattr(wiki_server, "ROOT", tmp_path)
        monkeypatch.setattr(wiki_server, "get_qmd_status", lambda: ("disabled", "Disabled"))

        (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
        f1 = tmp_path / "wiki" / "doc1.md"
        f2 = tmp_path / "wiki" / "doc2.md"

        f1.write_text("# Doc 1\nContent of doc 1", encoding="utf-8")
        f2.write_text("# Doc 2\nContent of doc 2", encoding="utf-8")

        res = await wiki_server.wiki_fetch("wiki/*.md", max_lines=10)
        assert "=== File: wiki/doc1.md ===" in res
        assert "=== File: wiki/doc2.md ===" in res
        assert "# Doc 1" in res
        assert "# Doc 2" in res

    asyncio.run(_run())
