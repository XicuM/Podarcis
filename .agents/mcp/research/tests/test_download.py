"""Tests for research-mcp PDF acquisition: mirror fallthrough, content validation,
and diagnosable failures.

Regression coverage for the download failures logged in
.podarcis/diagnostics/pain_points.jsonl:
  - diag-1788654043: MDPI 403 bot-wall reported as "...: None" with no detail.
  - diag-1788654016 / diag-1788654161: legitimate OA repositories rejected by the
    publisher allow-list before any request was made.
  - diag-1787071266: a failed download left an empty stub directory behind.

Network is never touched: httpx.AsyncClient.stream is patched.
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\ntrailer\n%%EOF\n"
HTML_BYTES = b"<!doctype html><html><body>Just a moment...</body></html>"


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes, content_type: str):
        self.status_code = status_code
        self._body = body
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("raise_for_status should not fire in these tests")

    async def aiter_bytes(self, chunk_size: int = 65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


def _patch_stream(monkeypatch, responses: dict, calls: list):
    """Route each URL to a canned (status, body, content-type) response."""

    @asynccontextmanager
    async def fake_stream(self, method, url, **kwargs):
        calls.append(url)
        status, body, ctype = responses.get(url, (404, b"", "text/html"))
        yield _FakeResponse(status, body, ctype)

    monkeypatch.setattr(server.httpx.AsyncClient, "stream", fake_stream)
    monkeypatch.setattr(server, "_fetch_unpaywall_pdfs", _no_unpaywall)


async def _no_unpaywall(doi):
    return []


# ═══════════════════════════════════════════════════════════════════════════════
# Mirror fallthrough
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.anyio
async def test_falls_through_to_unpaywall_mirror_on_403(monkeypatch, tmp_path):
    """A 403 bot-wall on the primary URL must not fail the download when a mirror exists."""
    walled = "https://www.mdpi.com/2076-328X/15/7/864/pdf"
    mirror = "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/pdf/"
    calls: list[str] = []

    @asynccontextmanager
    async def fake_stream(self, method, url, **kwargs):
        calls.append(url)
        if url == mirror:
            yield _FakeResponse(200, PDF_BYTES, "application/pdf")
        else:
            yield _FakeResponse(403, HTML_BYTES, "text/html")

    async def fake_unpaywall(doi):
        return [mirror]

    monkeypatch.setattr(server.httpx.AsyncClient, "stream", fake_stream)
    monkeypatch.setattr(server, "_fetch_unpaywall_pdfs", fake_unpaywall)

    dest = tmp_path / "original.pdf"
    used = await server._download_pdf(walled, dest, doi="10.3390/bs15070864")

    assert used == mirror
    assert dest.read_bytes() == PDF_BYTES
    assert walled in calls, "primary URL should still be attempted first"


@pytest.mark.anyio
async def test_institutional_repository_host_is_not_blocked(monkeypatch, tmp_path):
    """OA repositories outside the publisher allow-list must be reachable (diag-1788654016)."""
    url = "https://biblio.ugent.be/publication/4108324/file/4108327.pdf"
    calls: list[str] = []
    _patch_stream(monkeypatch, {url: (200, PDF_BYTES, "application/pdf")}, calls)

    dest = tmp_path / "original.pdf"
    used = await server._download_pdf(url, dest)

    assert used == url
    assert dest.read_bytes() == PDF_BYTES


# ═══════════════════════════════════════════════════════════════════════════════
# Content validation
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.anyio
async def test_html_landing_page_is_never_saved_as_pdf(monkeypatch, tmp_path):
    """A 200 response that is not a PDF must be rejected, not written to original.pdf."""
    url = "https://example.edu/paper"
    calls: list[str] = []
    _patch_stream(monkeypatch, {url: (200, HTML_BYTES, "text/html")}, calls)

    dest = tmp_path / "original.pdf"
    with pytest.raises(RuntimeError) as exc:
        await server._download_pdf(url, dest)

    assert not dest.exists()
    assert "not a PDF" in str(exc.value)
    assert "text/html" in str(exc.value)


@pytest.mark.anyio
async def test_non_https_candidate_is_skipped(monkeypatch, tmp_path):
    url = "http://insecure.edu/paper.pdf"
    calls: list[str] = []
    _patch_stream(monkeypatch, {}, calls)

    with pytest.raises(RuntimeError) as exc:
        await server._download_pdf(url, tmp_path / "original.pdf")

    assert calls == [], "no request should be issued for a non-https candidate"
    assert "not https" in str(exc.value)


# ═══════════════════════════════════════════════════════════════════════════════
# Diagnosable failures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.anyio
async def test_failure_message_reports_status_codes_not_none(monkeypatch, tmp_path):
    """Regression for diag-1788654043: the error said '...: None' and was unactionable."""
    url = "https://www.mdpi.com/2076-328X/15/7/864/pdf"
    calls: list[str] = []
    _patch_stream(monkeypatch, {url: (403, HTML_BYTES, "text/html")}, calls)

    with pytest.raises(RuntimeError) as exc:
        await server._download_pdf(url, tmp_path / "original.pdf")

    message = str(exc.value)
    assert "None" not in message
    assert "HTTP 403" in message
    assert url in message


@pytest.mark.anyio
async def test_missing_playwright_is_reported_not_swallowed(monkeypatch, tmp_path):
    """The headless-browser fallback is optional; its absence must be visible."""
    url = "https://www.mdpi.com/paper/pdf"
    calls: list[str] = []
    _patch_stream(monkeypatch, {url: (403, HTML_BYTES, "text/html")}, calls)

    async def no_playwright(u, d):
        raise ImportError("No module named 'playwright'")

    monkeypatch.setattr(server, "_download_pdf_playwright", no_playwright)

    with pytest.raises(RuntimeError) as exc:
        await server._download_pdf(url, tmp_path / "original.pdf")

    assert "playwright not installed" in str(exc.value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://pmc.ncbi.nlm.nih.gov/articles/PMC12292508/",
         "https://pmc.ncbi.nlm.nih.gov/articles/PMC12292508/pdf/"),
        ("https://www.ncbi.nlm.nih.gov/pmc/articles/12292508",
         "https://pmc.ncbi.nlm.nih.gov/articles/PMC12292508/pdf/"),
        # Already a PDF path — left alone.
        ("https://pmc.ncbi.nlm.nih.gov/articles/PMC12292508/pdf/",
         "https://pmc.ncbi.nlm.nih.gov/articles/PMC12292508/pdf/"),
        # Unrelated hosts pass through untouched.
        ("https://arxiv.org/pdf/1706.03762", "https://arxiv.org/pdf/1706.03762"),
        (None, None),
    ],
)
def test_normalize_pmc_landing_page_to_pdf(raw, expected):
    """Unpaywall returns PMC article URLs whose PDF sits one segment deeper."""
    assert server._normalize_pdf_url(raw) == expected


@pytest.mark.anyio
async def test_playwright_never_renders_html_as_pdf(monkeypatch, tmp_path):
    """page.pdf() would turn a Cloudflare challenge or 404 into byte-valid fake evidence."""

    class _Resp:
        ok = True
        headers = {"content-type": "text/html; charset=utf-8"}

        async def body(self):
            return HTML_BYTES

    class _Page:
        async def goto(self, url, **kw):
            return _Resp()

    class _Context:
        async def new_page(self):
            return _Page()

    class _Browser:
        async def new_context(self, **kw):
            return _Context()

        async def close(self):
            pass

    class _Chromium:
        async def launch(self, **kw):
            return _Browser()

    class _PW:
        chromium = _Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import types

    fake_mod = types.ModuleType("playwright.async_api")
    fake_mod.async_playwright = lambda: _PW()
    pkg = types.ModuleType("playwright")
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_mod)

    dest = tmp_path / "original.pdf"
    got = await server._download_pdf_playwright("https://www.mdpi.com/paper/pdf", dest)

    assert got is False
    assert not dest.exists(), "an HTML page must never be written to original.pdf"


@pytest.mark.anyio
async def test_unpaywall_collects_all_mirrors(monkeypatch):
    """best_oa_location and every oa_locations entry are returned, deduped, best first."""
    payload = {
        "best_oa_location": {"url_for_pdf": "https://a.example.edu/1.pdf"},
        "oa_locations": [
            {"url_for_pdf": "https://a.example.edu/1.pdf"},  # duplicate
            {"url_for_pdf": "https://b.example.edu/2.pdf"},
            {"url": "https://c.example.edu/landing"},
        ],
    }

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return _Resp()

    monkeypatch.setattr(server, "_make_client", lambda **kw: _Client())
    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.edu")

    urls = await server._fetch_unpaywall_pdfs("10.1234/abcd")

    assert urls == [
        "https://a.example.edu/1.pdf",
        "https://b.example.edu/2.pdf",
        "https://c.example.edu/landing",
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# arXiv support & regression for diag-1789556328
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.anyio
async def test_download_pdf_upgrades_http_arxiv_url(monkeypatch, tmp_path):
    """Semantic Scholar returns http://arxiv.org/pdf/... which must be upgraded to https."""
    http_url = "http://arxiv.org/pdf/2305.16291"
    https_url = "https://arxiv.org/pdf/2305.16291"
    calls: list[str] = []
    _patch_stream(monkeypatch, {https_url: (200, PDF_BYTES, "application/pdf")}, calls)

    dest = tmp_path / "original.pdf"
    used = await server._download_pdf(http_url, dest)

    assert used == https_url
    assert dest.read_bytes() == PDF_BYTES
    assert https_url in calls


@pytest.mark.anyio
async def test_download_pdf_with_arxiv_doi(monkeypatch, tmp_path):
    """arXiv DOIs (10.48550/arXiv.NNNN) resolve to direct PDF links."""
    arxiv_pdf = "https://arxiv.org/pdf/2404.16130.pdf"
    calls: list[str] = []
    _patch_stream(monkeypatch, {arxiv_pdf: (200, PDF_BYTES, "application/pdf")}, calls)

    dest = tmp_path / "original.pdf"
    used = await server._download_pdf(None, dest, doi="10.48550/arXiv.2404.16130")

    assert used == arxiv_pdf
    assert dest.read_bytes() == PDF_BYTES
    assert arxiv_pdf in calls


@pytest.mark.anyio
async def test_download_pdf_with_arxiv_id(monkeypatch, tmp_path):
    """Direct arXiv ID constructs https://arxiv.org/pdf/<id>.pdf candidate."""
    arxiv_pdf = "https://arxiv.org/pdf/2501.13956.pdf"
    calls: list[str] = []
    _patch_stream(monkeypatch, {arxiv_pdf: (200, PDF_BYTES, "application/pdf")}, calls)

    dest = tmp_path / "original.pdf"
    used = await server._download_pdf(None, dest, arxiv_id="2501.13956")

    assert used == arxiv_pdf
    assert dest.read_bytes() == PDF_BYTES
    assert arxiv_pdf in calls


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://arxiv.org/pdf/2305.16291", "https://arxiv.org/pdf/2305.16291"),
        ("http://arxiv.org/abs/2305.16291", "https://arxiv.org/pdf/2305.16291"),
        ("https://arxiv.org/abs/2404.16130", "https://arxiv.org/pdf/2404.16130"),
        ("https://arxiv.org/pdf/2404.16130", "https://arxiv.org/pdf/2404.16130"),
        ("https://arxiv.org/pdf/2404.16130.pdf", "https://arxiv.org/pdf/2404.16130.pdf"),
    ],
)
def test_normalize_arxiv_url(raw, expected):
    """arXiv URLs are upgraded to https and rewritten from /abs/ to /pdf/."""
    assert server._normalize_pdf_url(raw) == expected


@pytest.mark.anyio
async def test_resolve_metadata_bare_arxiv_id(monkeypatch):
    """Bare arXiv IDs like '2305.16291' must be recognized without hitting search."""
    calls: list[str] = []

    async def mock_fetch_semantic_scholar(paper_id):
        calls.append(paper_id)
        return server.PaperMetadata(
            title="Voyager",
            abstract="Voyager paper abstract",
            year=2023,
            url=f"https://arxiv.org/abs/{paper_id.removeprefix('arXiv:')}",
            arxiv_id=paper_id.removeprefix("arXiv:"),
        )

    monkeypatch.setattr(server, "_fetch_semantic_scholar", mock_fetch_semantic_scholar)

    meta = await server._resolve_metadata("2305.16291")
    assert meta.title == "Voyager"
    assert meta.arxiv_id == "2305.16291"
    assert meta.pdf_url == "https://arxiv.org/pdf/2305.16291.pdf"
    assert meta.doi == "10.48550/arXiv.2305.16291"
    assert calls == ["arXiv:2305.16291"]


@pytest.mark.anyio
async def test_resolve_metadata_arxiv_doi(monkeypatch):
    """arXiv DOIs (e.g. 10.48550/arXiv.2404.16130) normalize to arXiv IDs."""
    calls: list[str] = []

    async def mock_fetch_semantic_scholar(paper_id):
        calls.append(paper_id)
        return server.PaperMetadata(
            title="GraphRAG",
            abstract="GraphRAG abstract",
            year=2024,
            arxiv_id="2404.16130",
        )

    monkeypatch.setattr(server, "_fetch_semantic_scholar", mock_fetch_semantic_scholar)

    meta = await server._resolve_metadata("10.48550/arXiv.2404.16130")
    assert meta.title == "GraphRAG"
    assert meta.arxiv_id == "2404.16130"
    assert meta.pdf_url == "https://arxiv.org/pdf/2404.16130.pdf"
    assert calls == ["arXiv:2404.16130"]


# ═══════════════════════════════════════════════════════════════════════════════
# UTF-8 sanitization & regression for diag-1789670728
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.anyio
async def test_ingest_paper_sanitizes_utf8_and_warns(monkeypatch, tmp_path):
    """markitdown output with null bytes or control chars must be sanitized and warned."""
    class _FakeContext:
        def __init__(self):
            self.warnings = []

        async def report_progress(self, *a, **kw):
            pass

        async def warning(self, msg: str, **kw):
            self.warnings.append(msg)

    class _FakeResult:
        text_content = "Reflective RAG\x00 with null\x03 bytes and\ud800 surrogates"

    class _FakeMarkItDown:
        def convert(self, path):
            return _FakeResult()

    monkeypatch.setattr(server, "_SOURCES_LIT", tmp_path / "sources" / "literature")
    monkeypatch.setattr(server, "MarkItDown", _FakeMarkItDown)

    dummy_pdf = tmp_path / "test.pdf"
    dummy_pdf.write_bytes(PDF_BYTES)

    meta = server.PaperMetadata(
        title="Test Paper",
        abstract="Abstract",
        year=2026,
        local_path=str(dummy_pdf),
    )
    ctx = _FakeContext()

    with pytest.warns(UserWarning, match="Stripped 3 binary/control character"):
        await server._ingest_paper(
            ctx,
            "local:test.pdf",
            "test_paper_2026",
            "technology_and_systems",
            meta,
        )

    raw_path = tmp_path / "sources" / "literature" / "technology_and_systems" / "test_paper_2026" / "raw.md"
    assert raw_path.exists()
    raw_bytes = raw_path.read_bytes()
    assert b"\x00" not in raw_bytes
    assert b"\x03" not in raw_bytes

    raw_text = raw_path.read_text(encoding="utf-8")
    assert "Reflective RAG with null bytes" in raw_text

    assert len(ctx.warnings) == 1
    assert "Stripped 3 binary/control character(s)" in ctx.warnings[0]


