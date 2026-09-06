"""Tests for research-mcp ingestion pipeline: path resolution, text sanitization,
domain validation, and derived synthesis status.

All tests operate on temp directories without importing the full server module
(which has FastMCP/httpx dependencies), except where the module is loaded directly
via importlib for end-to-end pipeline tests.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

import pytest
import yaml


# ═══════════════════════════════════════════════════════════════════════════════
# Backend-aware path resolution (mirrors server._resolve_sources_lit)
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_sources_lit(root: Path) -> Path:
    """Replicate server._resolve_sources_lit logic for testing."""
    pod_yaml = root / ".podarcis" / "config.yaml"
    backend = "gdrive"
    if pod_yaml.exists():
        try:
            data = yaml.safe_load(pod_yaml.read_text(encoding="utf-8")) or {}
            backend = (data.get("repositories", {}) or {}).get("sources", "gdrive")
        except Exception:
            pass
    if backend != "gdrive":
        return root / "sources" / "literature"
    return root / "workspace" / "literature"


def test_resolve_paths_gdrive():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        podarcis = root / ".podarcis"
        podarcis.mkdir()
        (podarcis / "config.yaml").write_text(
            yaml.safe_dump({"repositories": {"sources": "gdrive"}}),
            encoding="utf-8",
        )
        sources_lit = _resolve_sources_lit(root)
        assert "workspace" in str(sources_lit)


def test_resolve_paths_local():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        podarcis = root / ".podarcis"
        podarcis.mkdir()
        (podarcis / "config.yaml").write_text(
            yaml.safe_dump({"repositories": {"sources": "local"}}),
            encoding="utf-8",
        )
        sources_lit = _resolve_sources_lit(root)
        assert "sources" in str(sources_lit)


def test_resolve_paths_git_url():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        podarcis = root / ".podarcis"
        podarcis.mkdir()
        (podarcis / "config.yaml").write_text(
            yaml.safe_dump(
                {"repositories": {"sources": "git@gitlab.com:foo/sources.git"}}
            ),
            encoding="utf-8",
        )
        sources_lit = _resolve_sources_lit(root)
        assert "sources" in str(sources_lit)


def test_resolve_paths_default_when_no_config():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sources_lit = _resolve_sources_lit(root)
        assert "workspace" in str(sources_lit)


# ═══════════════════════════════════════════════════════════════════════════════
# Text sanitization (mirrors server._sanitize)
# ═══════════════════════════════════════════════════════════════════════════════

def _sanitize(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def test_sanitize_collapses_spaces():
    assert _sanitize("hello     world") == "hello world"


def test_sanitize_collapses_newlines():
    text = "line1\n\n\n\n\nline2\n\n\nline3"
    result = _sanitize(text)
    # 5 newlines → 2, 3 newlines → 2: line1\n\nline2\n\nline3 = 4 newlines total
    assert result.count("\n") == 4


def test_sanitize_trims():
    assert _sanitize("  hello  \n") == "hello"


def test_sanitize_preserves_single_newline():
    assert _sanitize("line1\nline2") == "line1\nline2"


# ═══════════════════════════════════════════════════════════════════════════════
# Domain validation (mirrors server._check_domain)
# ═══════════════════════════════════════════════════════════════════════════════

_ALLOWED_DOMAINS = {
    'semanticscholar.org', 'arxiv.org', 'openalex.org', 'nih.gov', 'ncbi.nlm.nih.gov',
    'nature.com', 'science.org', 'pnas.org', 'cell.com', 'frontiersin.org', 'plos.org',
    'biorxiv.org', 'medrxiv.org', 'sciencedirect.com', 'royalsocietypublishing.org',
    'wiley.com', 'springer.com', 'mdpi.com', 'tandfonline.com', 'apa.org', 'sagepub.com',
    'oup.com', 'bmj.com', 'jamanetwork.com', 'thelancet.com', 'cambridge.org', 'jstor.org',
    'unpaywall.org', 'zenodo.org', 'osf.io', 'figshare.com', 'ssrn.com', 'archive.org',
    'googleapis.com', 'crossref.org', 'doi.org',
}

_ALLOWED_TLDS = ('.edu', '.ac.uk', '.gov', '.gov.uk', '.org.uk', '.edu.au')


def _check_domain(url: str) -> None:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or '').lower()
    if (
        any(host == d or host.endswith('.' + d) for d in _ALLOWED_DOMAINS)
        or host.endswith(_ALLOWED_TLDS)
    ):
        return
    raise ValueError(f"Outbound request to '{host}' is blocked by research policy.")


def test_check_domain_allows_semantic_scholar():
    _check_domain('https://api.semanticscholar.org/graph/v1/paper/123')


def test_check_domain_allows_arxiv():
    _check_domain('http://export.arxiv.org/api/query?id=1234.5678')


def test_check_domain_allows_openalex():
    _check_domain('https://api.openalex.org/works/W123')


def test_check_domain_allows_pubmed():
    _check_domain('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi')
    _check_domain('https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/')


def test_check_domain_allows_publishers():
    _check_domain('https://royalsocietypublishing.org/doi/pdf/10.1098/rstb.2016.0206')
    _check_domain('https://onlinelibrary.wiley.com/doi/pdf/10.1111/j.1467.x')
    _check_domain('https://link.springer.com/content/pdf/10.1007/s123.pdf')
    _check_domain('https://www.nature.com/articles/s41598-021-90968-z.pdf')


def test_check_domain_allows_academic_tlds():
    _check_domain('https://repository.upenn.edu/articles/12345/pdf')
    _check_domain('https://eprints.ox.ac.uk/id/eprint/123/paper.pdf')
    _check_domain('https://digitalcommons.unl.edu/psychology/123')


def test_check_domain_allows_google_books():
    _check_domain('https://www.googleapis.com/books/v1/volumes/abc')


def test_check_domain_allows_unpaywall():
    _check_domain('https://api.unpaywall.org/v2/10.1234/foo')


def test_check_domain_blocks_unknown():
    with pytest.raises(ValueError, match='blocked'):
        _check_domain('https://evil.example.com/steal')


def test_check_domain_blocks_no_host():
    with pytest.raises(ValueError, match='blocked'):
        _check_domain('not-a-url')


# ═══════════════════════════════════════════════════════════════════════════════
# Derived synthesis status (mirrors server._cited_source_ids / _synthesis_status)
#
# Replaces the old sources/state.json queue: instead of a hand-maintained "status"
# field that has to be kept in sync by enqueue/dequeue calls (and in practice
# drifted stale — an audit found 73/82 real entries still "pending" despite
# already being cited in wiki/), status is derived live from whether the source's
# id appears as a `[^id]:` footnote definition anywhere in wiki/ or workspace/.
# ═══════════════════════════════════════════════════════════════════════════════

_FOOTNOTE_DEF_RE = re.compile(r'^\[\^([A-Za-z0-9_\-]+)\]:', re.MULTILINE)


def _cited_source_ids(root: Path) -> set:
    """Replicate server._cited_source_ids logic for testing."""
    cited = set()
    for base_name in ("wiki", "workspace"):
        base = root / base_name
        if not base.exists():
            continue
        for md_file in base.rglob("*.md"):
            cited.update(_FOOTNOTE_DEF_RE.findall(md_file.read_text(encoding="utf-8")))
    return cited


def _synthesis_status(root: Path, sources_lit: Path) -> list:
    """Replicate server._synthesis_status logic for testing."""
    if not sources_lit.exists():
        return []
    cited = _cited_source_ids(root)
    items = []
    for meta_path in sorted(sources_lit.rglob("metadata.md")):
        paper_dir = meta_path.parent
        if not (paper_dir / "raw.md").exists() or not (paper_dir / "original.pdf").exists():
            continue
        source_id = paper_dir.name
        items.append({"id": source_id, "status": "done" if source_id in cited else "pending"})
    return items


def _make_source(sources_lit: Path, domain: str, source_id: str, full: bool = True):
    """Create a fake ingested source directory under sources_lit/domain/source_id/."""
    paper_dir = sources_lit / domain / source_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.md").write_text(f'---\ntitle: "{source_id}"\n---\n', encoding="utf-8")
    if full:
        (paper_dir / "raw.md").write_text("body", encoding="utf-8")
        (paper_dir / "original.pdf").write_bytes(b"%PDF-1.4")
    return paper_dir


def test_cited_source_ids_finds_footnote_in_wiki():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wiki = root / "wiki"
        wiki.mkdir()
        (wiki / "page.md").write_text(
            "Some claim[^smith_2024].\n\n[^smith_2024]: [Smith (2024).](../sources/x.md)\n",
            encoding="utf-8",
        )
        assert _cited_source_ids(root) == {"smith_2024"}


def test_cited_source_ids_ignores_inline_reference_marker():
    """An inline [^id] reference with no matching definition line must not count as cited —
    only the `[^id]:` *definition* is the OKF-required citation."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wiki = root / "wiki"
        wiki.mkdir()
        (wiki / "page.md").write_text("Some claim[^smith_2024], no definition below.\n", encoding="utf-8")
        assert _cited_source_ids(root) == set()


def test_cited_source_ids_checks_workspace_too():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "protocol.md").write_text(
            "[^doe_2023]: [Doe (2023).](../wiki/x.md)\n", encoding="utf-8"
        )
        assert _cited_source_ids(root) == {"doe_2023"}


def test_synthesis_status_pending_when_uncited():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sources_lit = root / "sources" / "literature"
        _make_source(sources_lit, "testing", "uncited_paper")

        items = _synthesis_status(root, sources_lit)
        assert len(items) == 1
        assert items[0] == {"id": "uncited_paper", "status": "pending"}


def test_synthesis_status_done_when_cited():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sources_lit = root / "sources" / "literature"
        _make_source(sources_lit, "testing", "cited_paper")
        wiki = root / "wiki"
        wiki.mkdir()
        (wiki / "page.md").write_text("[^cited_paper]: [Cited.](../sources/x.md)\n", encoding="utf-8")

        items = _synthesis_status(root, sources_lit)
        assert items == [{"id": "cited_paper", "status": "done"}]


def test_synthesis_status_excludes_partial_directories():
    """A directory missing original.pdf or raw.md is not a fully-ingested source
    (CLAUDE.md §No Stubs) and must not show up at all, pending or done."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sources_lit = root / "sources" / "literature"
        _make_source(sources_lit, "testing", "stub_paper", full=False)

        assert _synthesis_status(root, sources_lit) == []


def test_synthesis_status_stays_correct_without_any_manual_bookkeeping():
    """The core guarantee this replaces state.json for: citing a source in wiki/
    flips its status on the very next call, with nothing else to update anywhere."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sources_lit = root / "sources" / "literature"
        _make_source(sources_lit, "testing", "flips_paper")

        assert _synthesis_status(root, sources_lit)[0]["status"] == "pending"

        wiki = root / "wiki"
        wiki.mkdir()
        (wiki / "page.md").write_text("[^flips_paper]: [Cited.](../sources/x.md)\n", encoding="utf-8")

        assert _synthesis_status(root, sources_lit)[0]["status"] == "done"


def test_synthesis_status_filter_by_status():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sources_lit = root / "sources" / "literature"
        _make_source(sources_lit, "testing", "a")
        _make_source(sources_lit, "testing", "b")
        _make_source(sources_lit, "testing", "c")
        wiki = root / "wiki"
        wiki.mkdir()
        (wiki / "page.md").write_text("[^b]: [Cited.](../sources/x.md)\n", encoding="utf-8")

        items = _synthesis_status(root, sources_lit)
        pending = [i for i in items if i["status"] == "pending"]
        done = [i for i in items if i["status"] == "done"]
        assert {i["id"] for i in pending} == {"a", "c"}
        assert {i["id"] for i in done} == {"b"}


# ═══════════════════════════════════════════════════════════════════════════════
# Domain _index.md update logic
# ═══════════════════════════════════════════════════════════════════════════════

def test_domain_index_creation():
    with tempfile.TemporaryDirectory() as tmp:
        lit_dir = Path(tmp) / "sources" / "literature"
        domain_dir = lit_dir / "testing"
        domain_dir.mkdir(parents=True)

        index_path = domain_dir / "_index.md"
        assert not index_path.exists()

        # First ingestion: creates index with header
        existing = ""
        if not existing:
            existing = "# Testing Literature\n\n"
        entry = "- [paper1/raw.md](paper1/raw.md) - Test Paper - Abstract. (2025) #testing\n"
        if "paper1" not in existing:
            existing += entry
        index_path.write_text(existing, encoding="utf-8")

        assert index_path.exists()
        content = index_path.read_text()
        assert "Testing Literature" in content
        assert "paper1/raw.md" in content


def test_domain_index_appends_entries():
    with tempfile.TemporaryDirectory() as tmp:
        lit_dir = Path(tmp) / "sources" / "literature"
        domain_dir = lit_dir / "testing"
        domain_dir.mkdir(parents=True)

        index_path = domain_dir / "_index.md"
        index_path.write_text(
            "# Testing Literature\n\n"
            "- [paper1/raw.md](paper1/raw.md) - Paper One - Abstract. (2024) #testing\n"
        )

        # Append second entry
        existing = index_path.read_text()
        entry2 = "- [paper2/raw.md](paper2/raw.md) - Paper Two - Abstract. (2025) #testing\n"
        if "paper2" not in existing:
            if not existing.endswith("\n"):
                existing += "\n"
            existing += entry2
        index_path.write_text(existing)

        content = index_path.read_text()
        assert content.count("\n- [") == 2


def test_domain_index_idempotent():
    """Same paper should not be appended twice."""
    with tempfile.TemporaryDirectory() as tmp:
        lit_dir = Path(tmp) / "sources" / "literature"
        domain_dir = lit_dir / "testing"
        domain_dir.mkdir(parents=True)

        index_path = domain_dir / "_index.md"
        index_path.write_text(
            "# Testing Literature\n\n"
            "- [paper1/raw.md](paper1/raw.md) - Paper One. (2024) #testing\n"
        )

        # Try to add again — the real _update_domain_index checks filename_base not in existing
        existing = index_path.read_text()
        if "paper1/raw.md" not in existing:
            existing += "- [paper1/raw.md](paper1/raw.md) - Paper One. (2024) #testing\n"
        index_path.write_text(existing)

        content = index_path.read_text()
        # idempotent: the entry for paper1/raw.md should appear exactly once
        assert content.count("- [paper1/raw.md](paper1/raw.md)") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-End Ingestion & CLI Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.anyio
async def test_ingest_paper_full_lifecycle(tmp_path, monkeypatch):
    """Test full _ingest_paper pipeline with mocked PDF and metadata."""
    import importlib.util
    from unittest.mock import AsyncMock

    server_path = Path(__file__).resolve().parent.parent / 'server.py'
    spec = importlib.util.spec_from_file_location('research_mcp_test_mod', server_path)
    res_server = importlib.util.module_from_spec(spec)
    sys.modules['research_mcp_test_mod'] = res_server
    spec.loader.exec_module(res_server)

    sources_lit = tmp_path / 'sources' / 'literature'
    sources_lit.mkdir(parents=True)

    monkeypatch.setattr(res_server, 'ROOT', tmp_path)
    monkeypatch.setattr(res_server, '_SOURCES_LIT', sources_lit)

    async def mock_download_pdf(url, dest, doi=None):
        dest.write_text('Mock PDF Content', encoding='utf-8')

    monkeypatch.setattr(res_server, '_download_pdf', mock_download_pdf)

    class MockMarkItDown:
        def convert(self, path):
            class MockResult:
                text_content = 'Extracted full text from research paper on assertiveness and agency.'
            return MockResult()

    monkeypatch.setattr(res_server, 'MarkItDown', MockMarkItDown)

    ctx = AsyncMock()
    meta = res_server.PaperMetadata(
        title='Empirical Study on Interpersonal Agency',
        abstract='An abstract exploring communion and agency balance.',
        authors=[{'name': 'Jane Doe'}, {'name': 'John Smith'}],
        year=2024,
        pdf_url='https://api.semanticscholar.org/paper.pdf',
        doi='10.1000/182',
    )

    result = await res_server._ingest_paper(
        ctx=ctx,
        paper_id='DOI:10.1000/182',
        filename_base='doe_2024_agency',
        domain='social_and_behavioral/psychology',
        meta=meta,
    )

    assert result['status'] == 'ingested'
    assert result['queued_for_ingest'] is False  # status is derived, not tracked here
    paper_dir = sources_lit / 'social_and_behavioral' / 'psychology' / 'doe_2024_agency'
    assert (paper_dir / 'original.pdf').exists()
    assert (paper_dir / 'raw.md').exists()
    assert (paper_dir / 'metadata.md').exists()

    raw_content = (paper_dir / 'raw.md').read_text()
    assert 'Extracted full text from research paper' in raw_content
    meta_content = (paper_dir / 'metadata.md').read_text()
    assert 'Jane Doe' in meta_content

    # No state.json anywhere — synthesis status is derived from wiki/ citations, not tracked here.
    assert not (tmp_path / 'sources' / 'state.json').exists()
    status = res_server._synthesis_status(tmp_path, sources_lit)
    assert status == [{
        'id': 'doe_2024_agency',
        'title': 'Empirical Study on Interpersonal Agency',
        'domain': 'social_and_behavioral/psychology',
        'path': 'sources/literature/social_and_behavioral/psychology/doe_2024_agency/metadata.md',
        'status': 'pending',
    }]


def test_cli_research_ingest_with_mock(tmp_path, monkeypatch):
    """Test CLI cmd_research ingest flow with mocked metadata and async context."""
    from unittest.mock import AsyncMock
    from podarcis import cli
    import argparse

    mock_res = {
        'success': True,
        'paper_dir': str(tmp_path / 'paper_dir'),
        'files': ['original.pdf', 'raw.md', 'metadata.md'],
    }

    class MockResearchServer:
        @staticmethod
        async def _resolve_metadata(paper_id):
            class Meta:
                title = 'Sample Interpersonal Paper'
            return Meta()

        @staticmethod
        async def _ingest_paper(ctx, paper_id, filename_base, domain, meta):
            return mock_res

    monkeypatch.setitem(sys.modules, 'research_mcp_server', MockResearchServer)

    args = argparse.Namespace(
        research_action='ingest',
        paper_id='openalex:W12345',
        domain='social_and_behavioral/psychology',
        name='sample_2024_paper',
        json=False,
    )

    exit_code = cli.cmd_research(args)
    assert exit_code == 0
