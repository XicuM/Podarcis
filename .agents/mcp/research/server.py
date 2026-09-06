"""research-mcp — FastMCP server for academic literature discovery and ingestion.

Provides typed async tools for:
  - Searching literature (academic-mcp for multi-provider search)
  - Download papers: fetches metadata, PDF, extracts text via markitdown, and writes
    sources/ directory structure — all natively in async Python with httpx, no
    subprocess boundary.
  - Inspecting synthesis status of ingested sources, derived live from disk (see
    `literature_status` / `_synthesis_status`) rather than from a hand-maintained manifest.

Set PROJECT_ROOT env var to the repository root.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP, Context
from markitdown import MarkItDown

# ── Path bootstrap ────────────────────────────────────────────────────────────

def _find_root() -> Path:
    env = os.environ.get("PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "AGENTS.md").exists():
            return parent
    raise RuntimeError(
        "Cannot locate project root. Set the PROJECT_ROOT environment variable."
    )

ROOT = _find_root()

def _resolve_sources_lit(root: Path) -> Path:
    """Return the literature sources directory based on sources_backend in config.yaml."""
    pod_yaml = root / ".podarcis" / "config.yaml"
    backend = "gdrive"
    if pod_yaml.exists():
        try:
            import yaml
            with open(pod_yaml, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            backend = data.get("sources_backend", "gdrive")
        except Exception:
            pass
    if backend == "local":
        return root / "sources" / "literature"
    return root / "workspace" / "literature"

_SOURCES_LIT = _resolve_sources_lit(ROOT)

# API key (Semantic Scholar) — optional, loaded from .podarcis/config.yaml or environment
def _load_api_key(root: Path) -> str:
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    if key:
        return key
    pod_yaml = root / ".podarcis" / "config.yaml"
    if pod_yaml.exists():
        try:
            import yaml
            with open(pod_yaml, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict):
                    apis = data.get("apis", {})
                    if isinstance(apis, dict) and apis.get("semantic_scholar_api_key"):
                        val = str(apis["semantic_scholar_api_key"]).strip()
                        if val and val != "your_api_key_here":
                            return val
        except Exception:
            pass
    return ""

_API_KEY: str = _load_api_key(ROOT)

# Allowed outbound domains & TLDs for academic literature & repositories
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

# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    'research-mcp',
    instructions=(
        'Literature discovery and ingestion server. '
        'Use literature_search to find papers across multiple academic providers. '
        'Use literature_download to fetch, extract, and ingest them (PDF → raw.md → metadata.md). '
        'Use literature_status to check synthesis status — it is derived live by checking whether '
        'each source id is cited (as a `[^id]:` footnote) anywhere in wiki/ or workspace/, '
        'not read from a manifest, so it can never drift stale. '
        'The literature directory location depends on sources_backend in .podarcis/config.yaml: '
        '\'gdrive\' → workspace/literature/, \'local\' → sources/literature/.'
    ),
)

# Guards concurrent writes to a shared domain _index.md from parallel downloads.
_index_lock = asyncio.Lock()

# ── httpx client factory ──────────────────────────────────────────────────────

def _make_client(**kwargs) -> httpx.AsyncClient:
    """Return a configured async httpx client with retries and a shared UA header."""
    transport = httpx.AsyncHTTPTransport(retries=3)
    headers = {'User-Agent': 'Mozilla/5.0 (agentic-wiki research-mcp/1.0)'}
    if _API_KEY:
        headers['x-api-key'] = _API_KEY
    return httpx.AsyncClient(transport=transport, headers=headers, timeout=60, follow_redirects=True, **kwargs)


def _check_domain(url: str) -> None:
    """Enforce outbound domain allow-list for academic sources."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or '').lower()
    if (
        any(host == d or host.endswith('.' + d) for d in _ALLOWED_DOMAINS)
        or host.endswith(_ALLOWED_TLDS)
    ):
        return
    raise ValueError(f"Outbound request to '{host}' is blocked by research policy.")

# ─────────────────────────────────────────────────────────────────────────────
# Metadata dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PaperMetadata:
    title: str = "Unknown Title"
    abstract: str = "No abstract available."
    authors: list[dict] = field(default_factory=list)
    year: int | str = "Unknown"
    pdf_url: str | None = None
    doi: str | None = None
    url: str | None = None
    publication_types: list[str] = field(default_factory=list)
    local_path: str | None = None   # set for local: paper IDs

    def authors_str(self) -> str:
        return json.dumps([a.get("name", "") for a in self.authors])

# ─────────────────────────────────────────────────────────────────────────────
# Metadata fetchers (one per provider, all async)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_semantic_scholar(paper_id: str) -> PaperMetadata | None:
    fields = "title,authors,year,abstract,publicationTypes,openAccessPdf,externalIds,url"
    url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}?fields={fields}"
    _check_domain(url)
    async with _make_client() as client:
        for attempt in range(5):
            try:
                resp = await client.get(url)
                if resp.status_code == 429 or resp.status_code >= 500:
                    await asyncio.sleep(2 ** attempt + 1)
                    continue
                resp.raise_for_status()
                data = resp.json()
                pdf_info = data.get("openAccessPdf") or {}
                ext = data.get("externalIds") or {}
                return PaperMetadata(
                    title=data.get("title") or "Unknown Title",
                    abstract=data.get("abstract") or "No abstract available.",
                    authors=data.get("authors") or [],
                    year=data.get("year") or "Unknown",
                    pdf_url=pdf_info.get("url"),
                    doi=ext.get("DOI"),
                    url=data.get("url"),
                    publication_types=data.get("publicationTypes") or [],
                )
            except httpx.HTTPError:
                await asyncio.sleep(2 ** attempt)
    return None


async def _fetch_arxiv(arxiv_id: str) -> PaperMetadata | None:
    raw = arxiv_id.removeprefix("arXiv:")
    url = f"http://export.arxiv.org/api/query?id_list={raw}"
    _check_domain(url)
    async with _make_client() as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entry = root.find("atom:entry", ns)
            if entry is None:
                return None
            title = (entry.findtext("atom:title", namespaces=ns) or "").replace("\n", " ").strip()
            summary = (entry.findtext("atom:summary", namespaces=ns) or "").replace("\n", " ").strip()
            year_text = entry.findtext("atom:published", namespaces=ns) or ""
            year = int(year_text[:4]) if year_text[:4].isdigit() else "Unknown"
            authors = [
                {"name": a.findtext("atom:name", namespaces=ns)}
                for a in entry.findall("atom:author", ns)
            ]
            pdf_url = next(
                (
                    lnk.get("href")
                    for lnk in entry.findall("atom:link", ns)
                    if lnk.get("title") == "pdf" or lnk.get("type") == "application/pdf"
                ),
                None,
            )
            return PaperMetadata(
                title=title,
                abstract=summary,
                authors=authors,
                year=year,
                pdf_url=pdf_url,
                url=f"https://arxiv.org/abs/{raw}",
                publication_types=["preprint"],
            )
        except Exception:
            return None


async def _fetch_openalex(work_id: str) -> PaperMetadata | None:
    raw = work_id.removeprefix("openalex:")
    url = f"https://api.openalex.org/works/{raw}"
    _check_domain(url)
    async with _make_client() as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            work = resp.json()
            # Reconstruct abstract from inverted index
            abstract = ""
            inv = work.get("abstract_inverted_index") or {}
            if inv:
                pos_word = {pos: w for w, positions in inv.items() for pos in positions}
                abstract = " ".join(pos_word.get(i, "") for i in range(max(pos_word) + 1))
            authors = [
                {"name": a.get("author", {}).get("display_name")}
                for a in work.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ]
            best_oa = work.get("best_oa_location") or {}
            primary_loc = work.get("primary_location") or {}
            oa_info = work.get("open_access") or {}
            pdf_url = (
                best_oa.get("pdf_url")
                or primary_loc.get("pdf_url")
                or best_oa.get("landing_page_url")
                or oa_info.get("oa_url")
            )
            doi_url = work.get("doi") or ""
            doi = doi_url.replace("https://doi.org/", "").lower() or None
            return PaperMetadata(
                title=work.get("title") or "Unknown Title",
                abstract=abstract or "No abstract available.",
                authors=authors,
                year=work.get("publication_year") or "Unknown",
                pdf_url=pdf_url,
                doi=doi,
                url=work.get("doi") or work.get("id"),
                publication_types=[work.get("type", "journal-article")],
            )
        except Exception:
            return None


async def _fetch_pubmed(pmid: str) -> PaperMetadata | None:
    raw = pmid.removeprefix("pmid:").removeprefix("pmcid:")
    summary_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        f"?db=pubmed&id={raw}&retmode=json"
    )
    _check_domain(summary_url)
    async with _make_client() as client:
        try:
            resp = await client.get(summary_url)
            resp.raise_for_status()
            data = resp.json()
            item = (data.get("result") or {}).get(raw)
            if not item:
                return None
            title = item.get("title") or "Unknown Title"
            authors = [{"name": a["name"]} for a in item.get("authors", []) if a.get("name")]
            pubdate = item.get("pubdate", "")
            year_m = re.search(r"\d{4}", pubdate)
            year = int(year_m.group()) if year_m else "Unknown"
            doi, pmcid = None, None
            for aid in item.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("id")
                elif aid.get("idtype") in ("pmc", "pmcid"):
                    pmcid = aid.get("id")
            if pmcid:
                clean_pmc = pmcid.replace("pmc-id:", "").strip()
                if not clean_pmc.upper().startswith("PMC"):
                    clean_pmc = f"PMC{clean_pmc}"
                pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{clean_pmc}/pdf/"
            else:
                pdf_url = None
            # Fetch abstract
            abstract = ""
            fetch_url = (
                f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
                f"?db=pubmed&id={raw}&retmode=xml"
            )
            try:
                r2 = await client.get(fetch_url)
                root = ET.fromstring(r2.text)
                abstract_elem = root.find(".//Abstract")
                if abstract_elem is not None:
                    abstract = " ".join(
                        t.text for t in abstract_elem.findall(".//AbstractText") if t.text
                    )
            except Exception:
                pass
            return PaperMetadata(
                title=title,
                abstract=abstract or "No abstract available.",
                authors=authors,
                year=year,
                pdf_url=pdf_url,
                doi=doi,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{raw}/",
                publication_types=["journal-article"],
            )
        except Exception:
            return None


async def _fetch_google_books(volume_id: str) -> PaperMetadata | None:
    raw = volume_id.removeprefix("googlebooks:")
    url = f"https://www.googleapis.com/books/v1/volumes/{raw}"
    _check_domain(url)
    async with _make_client() as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            item = resp.json()
            vi = item.get("volumeInfo", {})
            year_m = re.search(r"\d{4}", vi.get("publishedDate", ""))
            isbn = next(
                (i["identifier"] for i in vi.get("industryIdentifiers", [])
                 if i.get("type") in ("ISBN_13", "ISBN_10")),
                None,
            )
            pdf_info = item.get("accessInfo", {}).get("pdf", {})
            pdf_url = pdf_info.get("downloadLink") if pdf_info.get("isAvailable") else None
            return PaperMetadata(
                title=vi.get("title") or "Unknown Book Title",
                abstract=vi.get("description") or "No description available.",
                authors=[{"name": a} for a in vi.get("authors", [])],
                year=int(year_m.group()) if year_m else "Unknown",
                pdf_url=pdf_url,
                doi=None,
                url=vi.get("infoLink") or vi.get("previewLink"),
                publication_types=["book"],
            )
        except Exception:
            return None


async def _fetch_unpaywall_pdfs(doi: str) -> list[str]:
    """Return every open-access PDF mirror Unpaywall knows for a DOI, best first.

    Publishers behind bot walls (MDPI, Springer, ScienceDirect) routinely 403 the
    `best_oa_location`, while a PMC or institutional-repository mirror in
    `oa_locations` serves the same PDF freely. Returning the full list lets the
    downloader fall through to a mirror instead of failing the whole ingestion.
    """
    contact_email = os.environ.get("UNPAYWALL_EMAIL", "")
    if not contact_email:
        # Fallback: read from .podarcis/config.yaml
        pod_yaml = ROOT / ".podarcis" / "config.yaml"
        if pod_yaml.exists():
            try:
                import yaml
                with open(pod_yaml, "r", encoding="utf-8") as _f:
                    _cfg = yaml.safe_load(_f) or {}
                contact_email = (_cfg.get("apis") or {}).get("unpaywall_email", "")
            except Exception:
                pass
    if not contact_email:
        return []  # Unpaywall requires a valid contact email
    url = f"https://api.unpaywall.org/v2/{doi}?email={contact_email}"
    _check_domain(url)
    async with _make_client() as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []

    urls: list[str] = []
    locations = [data.get("best_oa_location") or {}] + list(data.get("oa_locations") or [])
    for loc in locations:
        for key in ("url_for_pdf", "url"):
            candidate = _normalize_pdf_url((loc or {}).get(key))
            if candidate and candidate not in urls:
                urls.append(candidate)
    return urls


_PMC_LANDING_RE = re.compile(
    r"^https?://(?:www\.)?(?:pmc\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pmc)/articles/(PMC)?(\d+)/?$",
    re.IGNORECASE,
)


def _normalize_pdf_url(url: str | None) -> str | None:
    """Rewrite known landing-page URLs to their direct PDF path.

    Unpaywall frequently returns a PMC *article* URL in `url` where the PDF lives
    one segment deeper. Fetching the landing page yields HTML, which the content
    check correctly rejects — losing an otherwise freely available paper.
    """
    if not url:
        return url
    m = _PMC_LANDING_RE.match(url.strip())
    if m:
        return f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{m.group(2)}/pdf/"
    return url


async def _fetch_local(paper_id: str) -> PaperMetadata | None:
    """Read a local PDF, extract text, and attempt DOI-based metadata lookup."""
    pdf_path = Path(paper_id.removeprefix("local:"))
    if not pdf_path.exists():
        raise FileNotFoundError(f"Local PDF not found: {pdf_path}")
    md = MarkItDown()
    result = md.convert(str(pdf_path))
    text = _sanitize(result.text_content)
    doi_m = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, re.IGNORECASE)
    doi = doi_m.group().rstrip(".,") if doi_m else None
    if doi:
        meta = await _fetch_semantic_scholar(f"DOI:{doi}")
        if meta:
            meta.local_path = str(pdf_path)
            return meta
    # Fallback: minimal metadata from filename + text
    abstract_m = re.search(
        r"abstract\s*(.*?)\s*(?:1\.?\s+introduction|introduction|keywords|background)",
        text, re.IGNORECASE | re.DOTALL,
    )
    abstract = ""
    if abstract_m:
        abstract = re.sub(r"\s+", " ", abstract_m.group(1).strip())[:1000]
    return PaperMetadata(
        title=pdf_path.stem,
        abstract=abstract or "Metadata extraction fallback from PDF.",
        year="Unknown",
        doi=doi,
        url="local-file",
        publication_types=["local-pdf"],
        local_path=str(pdf_path),
    )


def _sanitize(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def _resolve_metadata(paper_id: str) -> PaperMetadata:
    """Resolve paper ID (accepts URLs, raw DOIs, search queries, or provider prefixes), then fetch metadata."""
    paper_id = paper_id.strip()
    
    # 1. Resolve URLs
    if paper_id.startswith("http://") or paper_id.startswith("https://"):
        if "arxiv.org/abs/" in paper_id or "arxiv.org/pdf/" in paper_id:
            m = re.search(r"arxiv.org/(?:abs|pdf)/([\d.]+)(?:\.pdf)?", paper_id)
            if m:
                paper_id = f"arXiv:{m.group(1)}"
        elif "pubmed.ncbi.nlm.nih.gov/" in paper_id:
            m = re.search(r"pubmed.ncbi.nlm.nih.gov/(\d+)", paper_id)
            if m:
                paper_id = f"pmid:{m.group(1)}"
        elif "openalex.org/" in paper_id:
            m = re.search(r"openalex.org/(?:works/)?(W\d+)", paper_id)
            if m:
                paper_id = f"openalex:{m.group(1)}"
        elif "doi.org/" in paper_id:
            m = re.search(r"doi.org/(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", paper_id, re.IGNORECASE)
            if m:
                paper_id = f"DOI:{m.group(1)}"
                
    # 2. Check if raw DOI
    if re.match(r"^10\.\d{4,9}/", paper_id):
        paper_id = f"DOI:{paper_id}"

    # 3. If paper_id has no prefix and is not a 40-char hex string (Semantic Scholar ID),
    # treat it as a search query
    is_hex_id = bool(re.match(r"^[0-9a-fA-F]{40}$", paper_id))
    has_prefix = ":" in paper_id
    
    if not has_prefix and not is_hex_id:
        print(f"Title or search query detected: '{paper_id}'. Searching literature...")
        search_results = await literature_search(paper_id, limit=1)
        if search_results:
            first_paper = search_results[0]
            resolved_id = first_paper.get("paperId")
            if not resolved_id:
                ext = first_paper.get("externalIds") or {}
                if ext.get("DOI"):
                    resolved_id = f"DOI:{ext.get('DOI')}"
                elif ext.get("ArXiv"):
                    resolved_id = f"arXiv:{ext.get('ArXiv')}"
                elif ext.get("PubMed"):
                    resolved_id = f"pmid:{ext.get('PubMed')}"
            if resolved_id:
                print(f"Resolved query '{paper_id}' to paper ID '{resolved_id}'")
                paper_id = resolved_id
            else:
                raise ValueError(f"Found search results for '{paper_id}', but could not resolve a valid provider ID.")
        else:
            raise ValueError(f"Could not find any papers matching the query: '{paper_id}'")

    # 4. Dispatch to the correct metadata fetcher based on paper_id prefix
    if paper_id.startswith("openalex:"):
        meta = await _fetch_openalex(paper_id)
    elif paper_id.startswith("pmid:") or paper_id.startswith("pmcid:"):
        meta = await _fetch_pubmed(paper_id)
    elif paper_id.startswith("googlebooks:"):
        meta = await _fetch_google_books(paper_id)
    elif paper_id.startswith("local:"):
        meta = await _fetch_local(paper_id)
    elif paper_id.startswith("arXiv:"):
        meta = await _fetch_semantic_scholar(paper_id)
        if not meta:
            meta = await _fetch_arxiv(paper_id)
    else:
        # Raw Semantic Scholar hash or DOI:xxx
        meta = await _fetch_semantic_scholar(paper_id)

    if meta is None:
        raise ValueError(f"Could not fetch metadata for '{paper_id}' from any provider.")

    # arXiv fallback for PDF URL
    if (
        not meta.local_path
        and not meta.pdf_url
        and (paper_id.startswith("arXiv:") or "arxiv" in (meta.url or "").lower())
    ):
        arxiv_id = paper_id if paper_id.startswith("arXiv:") else None
        if not arxiv_id and meta.url:
            m = re.search(r"arxiv.org/abs/([\d.]+)", meta.url)
            if m:
                arxiv_id = m.group(1)
        if arxiv_id:
            ax = await _fetch_arxiv(arxiv_id)
            if ax and ax.pdf_url:
                meta.pdf_url = ax.pdf_url
                meta.abstract = meta.abstract or ax.abstract

    return meta


# ─────────────────────────────────────────────────────────────────────────────
# Core ingestion logic
# ─────────────────────────────────────────────────────────────────────────────

def _publisher_oa_url(doi: str) -> str | None:
    """Try to construct a publisher-native open-access PDF URL from a DOI."""
    if not doi:
        return None
    doi = doi.removeprefix("DOI:").removeprefix("doi:")
    article_id = doi.split("/", 1)[-1] if "/" in doi else doi
    # Nature family journals — https://www.nature.com/articles/<id>.pdf
    if any(d in doi.lower() for d in ("10.1038/", "10.1037/")):
        return f"https://www.nature.com/articles/{article_id}.pdf"
    # Science journals — https://www.science.org/doi/pdf/<doi>
    if doi.startswith("10.1126/"):
        return f"https://www.science.org/doi/pdf/{doi}"
    # PNAS — https://www.pnas.org/doi/pdf/<doi>
    if doi.startswith("10.1073/"):
        return f"https://www.pnas.org/doi/pdf/{doi}"
    # Cell Press (open-access)
    if "10.1016/j.cell" in doi.lower() or "10.1016/j.celrep" in doi.lower():
        return f"https://www.cell.com/article/{''.join(doi.split('/', 1)[1:])}/pdf"
    # Frontiers — fully OA
    if "10.3389/" in doi:
        return f"https://www.frontiersin.org/articles/{doi}/pdf"
    # PLOS — fully OA
    if "10.1371/" in doi:
        return f"https://journals.plos.org/plosone/article/file?id={doi}&type=printable"
    return None


_PLAYWRIGHT_HINT = (
    "headless-browser fallback unavailable (playwright not installed — "
    "`uv pip install 'podarcis[browser]' && playwright install chromium`)"
)


async def _download_pdf_playwright(url: str, dest: Path) -> bool:
    """Attempt PDF download via headless browser (handles Cloudflare challenges).

    Raises ImportError when playwright is absent so the caller can report the
    missing fallback instead of silently swallowing it.
    """
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = await context.new_page()
            # Navigate to the PDF URL; Cloudflare will auto-redirect through JS challenge
            response = await page.goto(url, wait_until="networkidle", timeout=30000)
            if response is None or not response.ok:
                return False
            # Only ever accept a PDF the server actually served. Rendering the page
            # with page.pdf() is deliberately NOT done: it turns a Cloudflare
            # challenge, a 404, or a paywall notice into a byte-valid PDF that
            # passes every downstream check and enters sources/ as fabricated
            # evidence. A missed download is recoverable; a fake source is not.
            if "application/pdf" not in response.headers.get("content-type", ""):
                return False
            pdf_bytes = await response.body()
            if not pdf_bytes.lstrip()[:5].startswith(b"%PDF-"):
                return False
            dest.write_bytes(pdf_bytes)
            return True
        finally:
            await browser.close()
    return False


async def _download_pdf(url: str | None, dest: Path, doi: str | None = None) -> str:
    """Stream a PDF to disk with retries; return the URL that actually served it.

    Falls back to Playwright on 403/bot-block.

    Candidates are tried in order: the publisher-native OA URL derived from the DOI,
    the resolver-supplied URL, then every Unpaywall mirror. Every candidate here is
    vetted by an OA resolver (Semantic Scholar / OpenAlex / Unpaywall / arXiv), so the
    publisher allow-list that guards the API endpoints is deliberately not applied —
    it cannot enumerate the long tail of institutional repositories and was rejecting
    legitimate open-access hosts. HTTPS and PDF magic bytes are enforced instead,
    which is a stronger guarantee than a hostname list: it verifies the content.
    """
    publisher_url = _publisher_oa_url(doi) if doi else None
    urls_to_try: list[str] = []
    for candidate in (publisher_url, url):
        if candidate and candidate not in urls_to_try:
            urls_to_try.append(candidate)
    if doi:
        for mirror in await _fetch_unpaywall_pdfs(doi):
            if mirror not in urls_to_try:
                urls_to_try.append(mirror)

    attempts: list[str] = []
    async with _make_client() as client:
        for try_url in urls_to_try:
            if not try_url.lower().startswith("https://"):
                attempts.append(f"{try_url} → skipped (not https)")
                continue
            for attempt in range(3):
                try:
                    async with client.stream(
                        "GET", try_url,
                        headers={
                            "Accept": "application/pdf",
                            "Referer": "https://scholar.google.com/",
                        },
                    ) as resp:
                        if resp.status_code in (403, 404):
                            attempts.append(f"{try_url} → HTTP {resp.status_code}")
                            break  # try next URL
                        if resp.status_code == 429 or resp.status_code >= 500:
                            await asyncio.sleep(2 ** attempt + 1)
                            continue
                        resp.raise_for_status()
                        body = bytearray()
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            body.extend(chunk)
                    if not bytes(body).lstrip()[:5].startswith(b"%PDF-"):
                        ctype = resp.headers.get("content-type", "unknown")
                        attempts.append(
                            f"{try_url} → HTTP 200 but not a PDF "
                            f"(content-type: {ctype}, {len(body)} bytes — likely a landing page)"
                        )
                        break  # try next URL rather than saving HTML as original.pdf
                    dest.write_bytes(bytes(body))
                    return try_url  # success
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    attempts.append(f"{try_url} → HTTP {code}")
                    if code != 429 and code < 500:
                        break
                    await asyncio.sleep(2 ** attempt + 1)
                except httpx.TransportError as exc:
                    attempts.append(f"{try_url} → {type(exc).__name__}: {exc}")
                    await asyncio.sleep(2 ** attempt)

    # ── httpx failed on all URLs → try Playwright fallback ───────────────────
    playwright_missing = False
    for try_url in urls_to_try:
        try:
            if await _download_pdf_playwright(try_url, dest):
                if dest.exists() and dest.read_bytes().lstrip()[:5].startswith(b"%PDF-"):
                    return try_url
                attempts.append(f"{try_url} → playwright returned a non-PDF")
                dest.unlink(missing_ok=True)
        except ImportError:
            playwright_missing = True
            break
        except Exception as exc:
            attempts.append(f"{try_url} → playwright: {type(exc).__name__}: {exc}")

    detail = "; ".join(attempts) or "no candidate URLs available"
    suffix = f". Note: {_PLAYWRIGHT_HINT}" if playwright_missing else ""
    raise RuntimeError(f"Failed to download PDF. Tried {len(urls_to_try)} URL(s): {detail}{suffix}")


async def _ingest_paper(
    ctx: Context,
    paper_id: str,
    filename_base: str,
    domain: str,
    meta: PaperMetadata,
) -> dict:
    """Write sources/ directory structure and return a result summary.

    Directory layout:
      sources/literature/<domain>/<filename_base>/
        original.pdf
        raw.md        (markitdown extraction, YAML front-matter)
        metadata.md   (bibliographic summary)
    """
    paper_dir = _SOURCES_LIT / domain / filename_base
    paper_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = paper_dir / "original.pdf"
    raw_path = paper_dir / "raw.md"
    meta_path = paper_dir / "metadata.md"

    # ── Step 1: acquire PDF ──────────────────────────────────────────────────
    await ctx.report_progress(1, 4, "Acquiring PDF…")
    is_local = bool(meta.local_path)
    if is_local:
        shutil.copy2(meta.local_path, pdf_path)
        pdf_source_url = f"file://{meta.local_path}"
    else:
        if not meta.pdf_url and meta.doi:
            # Try Unpaywall — take the best mirror; _download_pdf tries the rest.
            mirrors = await _fetch_unpaywall_pdfs(meta.doi)
            meta.pdf_url = mirrors[0] if mirrors else None
        if not meta.pdf_url and not meta.doi:
            shutil.rmtree(paper_dir, ignore_errors=True)
            raise ValueError(
                "No open-access PDF found via any provider. "
                "Please supply the PDF manually and use a local: paper_id."
            )
        try:
            pdf_source_url = await _download_pdf(meta.pdf_url, pdf_path, doi=meta.doi)
        except Exception:
            # Never leave a half-built stub behind — a directory without both
            # original.pdf and raw.md is not an ingested source (CLAUDE.md §No Stubs).
            shutil.rmtree(paper_dir, ignore_errors=True)
            raise

    # ── Step 2: extract text ─────────────────────────────────────────────────
    await ctx.report_progress(2, 4, "Extracting text with markitdown…")
    md_converter = MarkItDown()
    try:
        result = md_converter.convert(str(pdf_path))
        body = _sanitize(result.text_content)
    except Exception as exc:
        shutil.rmtree(paper_dir, ignore_errors=True)
        raise RuntimeError(f"PDF text extraction failed: {exc}") from exc

    if not body:
        shutil.rmtree(paper_dir, ignore_errors=True)
        raise RuntimeError("PDF extraction returned empty text. The PDF may be scanned/image-only.")

    raw_path.write_text(
        f"---\nsource_url: {pdf_source_url}\nstatus: raw\n---\n\n{body}\n",
        encoding="utf-8",
    )

    # ── Step 3: write metadata.md ────────────────────────────────────────────
    await ctx.report_progress(3, 4, "Writing metadata…")
    safe_title = (meta.title or "Unknown Title").replace('"', "'")
    meta_path.write_text(
        f'---\ntitle: "{safe_title}"\nsource: "[[raw.md]]"\nraw_pdf: "[[original.pdf]]"\n'
        f"tags: [literature-summary]\nauthors: {meta.authors_str()}\n"
        f"year: {meta.year}\npaper_type: {json.dumps(meta.publication_types)}\n---\n"
        f"# {safe_title}\n\n## Abstract\n{meta.abstract}\n",
        encoding="utf-8",
    )

    # ── Step 4: update the domain _index.md ──────────────────────────────────
    await ctx.report_progress(4, 4, "Updating domain index…")
    abstract_summary = (meta.abstract or "").replace("\n", " ").strip()[:200]
    if len(meta.abstract or "") > 200:
        abstract_summary += "..."

    async with _index_lock:
        _update_domain_index(filename_base, domain, meta.title, abstract_summary, meta.year)

    rel_paper_dir = str(paper_dir.relative_to(ROOT)) if paper_dir.is_relative_to(ROOT) else str(paper_dir)
    return {
        'status': 'ingested',
        'paper_dir': rel_paper_dir,
        'files': ['original.pdf', 'raw.md', 'metadata.md'],
        'queued_for_ingest': False,  # synthesis status is now derived live — see literature_status
    }


def _update_domain_index(filename_base, domain, title, abstract_summary, year):
    index_dir = _SOURCES_LIT / domain
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / "_index.md"
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    if not existing:
        existing = f"# {domain.replace('_', ' ').title()} Literature\n\n"
    if filename_base not in existing:
        entry = (
            f"- [{filename_base}/raw.md]({filename_base}/raw.md)"
            f" - {title} - {abstract_summary}. ({year}) #{domain}\n"
        )
        if not existing.endswith("\n"):
            existing += "\n"
        existing += entry
        index_path.write_text(existing, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# MCP Tools — Literature Search
# ─────────────────────────────────────────────────────────────────────────────

async def _search_semanticscholar_direct(query: str, limit: int = 5) -> list[dict]:
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/search"
        f"?query={query}&limit={limit}"
        f"&fields=paperId,title,authors,year,abstract,openAccessPdf,externalIds"
    )
    _check_domain(url)
    async with _make_client() as client:
        for attempt in range(3):
            try:
                resp = await client.get(url)
                if resp.status_code == 429:
                    await asyncio.sleep(2 ** attempt + 1)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", []) or []
            except Exception:
                await asyncio.sleep(1)
    return []


async def _search_openalex_direct(query: str, limit: int = 5) -> list[dict]:
    url = f"https://api.openalex.org/works?search={query}&per-page={limit}"
    _check_domain(url)
    async with _make_client() as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("results", []):
                inv = item.get("abstract_inverted_index") or {}
                abstract = ""
                if inv:
                    pos_word = {pos: w for w, positions in inv.items() for pos in positions}
                    abstract = " ".join(pos_word.get(i, "") for i in range(max(pos_word) + 1))
                authors = [
                    {"name": a.get("author", {}).get("display_name")}
                    for a in item.get("authorships", [])
                    if a.get("author", {}).get("display_name")
                ]
                doi_url = item.get("doi") or ""
                doi = doi_url.replace("https://doi.org/", "").lower() or None
                work_id = item.get("id", "").split("/")[-1]
                results.append({
                    "paperId": f"openalex:{work_id}",
                    "title": item.get("title") or "Unknown Title",
                    "abstract": abstract or "No abstract available.",
                    "authors": authors,
                    "year": item.get("publication_year") or "Unknown",
                    "openAccessPdf": (item.get("primary_location") or {}).get("pdf_url"),
                    "externalIds": {"DOI": doi, "OpenAlex": item.get("id")},
                })
            return results
        except Exception:
            return []


async def _search_pubmed_direct(query: str, limit: int = 5) -> list[dict]:
    search_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={query}&retmode=json&retmax={limit}"
    _check_domain(search_url)
    async with _make_client() as client:
        try:
            resp = await client.get(search_url)
            resp.raise_for_status()
            id_list = (resp.json().get("esearchresult") or {}).get("idlist", [])
            if not id_list:
                return []
            results = []
            for pmid in id_list:
                meta = await _fetch_pubmed(pmid)
                if meta:
                    results.append({
                        "paperId": f"pmid:{pmid}",
                        "title": meta.title,
                        "abstract": meta.abstract,
                        "authors": meta.authors,
                        "year": meta.year,
                        "openAccessPdf": meta.pdf_url,
                        "externalIds": {"PubMed": pmid, "DOI": meta.doi},
                    })
            return results
        except Exception:
            return []


@mcp.tool()
async def literature_search(
    query: Annotated[str, "Search query (title keywords, topic, author name, etc.)"],
    limit: Annotated[int, "Maximum number of results to return (default 5)"] = 5,
    provider: Annotated[
        Literal[
            "all", "pubmed", "openalex", "arxiv", "semanticscholar",
            "googlebooks", "biorxiv", "crossref",
        ],
        "Restrict search to a single provider (default: all)",
    ] = "all",
) -> list[dict]:
    """Search academic literature across multiple providers via academic-mcp or native API fallbacks.

    Returns a list of papers with title, authors, year, abstract, and provider IDs.
    Never fabricates results — if no papers are found, returns an empty list.
    """
    try:
        from academic_mcp import search as academic_search  # type: ignore[import]
        kwargs = {"query": query, "limit": limit}
        if provider != "all":
            kwargs["source"] = provider
        results = await academic_search(**kwargs)
        if isinstance(results, list) and results:
            return results
    except Exception:
        pass

    if provider == "openalex":
        return await _search_openalex_direct(query, limit)
    elif provider == "pubmed":
        return await _search_pubmed_direct(query, limit)
    elif provider == "semanticscholar":
        return await _search_semanticscholar_direct(query, limit)

    # Default "all": Try Semantic Scholar -> OpenAlex -> PubMed
    results = await _search_semanticscholar_direct(query, limit)
    if not results:
        results = await _search_openalex_direct(query, limit)
    if not results:
        results = await _search_pubmed_direct(query, limit)

    return results



# ─────────────────────────────────────────────────────────────────────────────
# MCP Tools — Paper Download & Ingestion
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def literature_download(
    ctx: Context,
    paper_id: Annotated[
        str,
        "Paper identifier. Accepted formats: "
        "'openalex:<W…>', 'pmid:<id>', 'pmcid:<id>', 'googlebooks:<id>', 'arXiv:<id>', "
        "'DOI:<10.xxx/yyy>', 'https://doi.org/…', 'https://arxiv.org/abs/…', "
        "'https://pubmed.ncbi.nlm.nih.gov/<id>/', 'https://openalex.org/W…', "
        "'local:</absolute/path/to/file.pdf>', or a raw 40-char Semantic Scholar hash. "
        "Plain text titles or keywords are also accepted and will trigger an automatic search.",
    ],
    filename_base: Annotated[
        str,
        "snake_case base name for the output directory (e.g. 'smith_2023_protein_synthesis'). "
        "Must not contain slashes, backslashes, or dots.",
    ],
    domain: Annotated[
        str,
        "Subdomain folder under sources/literature/ (e.g. 'nutrition', 'sleep', 'finance'). "
        "Use a short lowercase identifier consistent with existing wiki categories.",
    ],
) -> dict:
    """Download and ingest an academic paper into the sources/ directory structure.

    Workflow (with progress notifications):
      1. Fetch metadata from the appropriate provider
      2. Download the open-access PDF (with Unpaywall fallback)
      3. Extract text via markitdown → raw.md
      4. Write metadata.md and update the domain _index.md

    Returns paths to the created files. The agent should then git commit.
    On any error, the partial directory is removed rather than left as a stub — see
    CLAUDE.md §No Stubs. Synthesis status is not tracked here; check it later with
    `literature_status`, which derives it live from wiki/workspace citations.
    """
    # Sanitise filename_base
    if re.search(r"[/\\.]", filename_base):
        raise ValueError("filename_base must not contain '/', '\\', or '.' characters.")

    filename_base = filename_base.removesuffix(".pdf").removesuffix(".pdf.md")

    await ctx.report_progress(0, 4, "Fetching metadata…")
    meta = await _resolve_metadata(paper_id)

    return await _ingest_paper(ctx, paper_id, filename_base, domain, meta)


# ─────────────────────────────────────────────────────────────────────────────
# MCP Tools — Synthesis Status (derived live from disk, no manifest)
# ─────────────────────────────────────────────────────────────────────────────
#
# There used to be a hand-maintained sources/state.json manifest here, with enqueue/
# dequeue tools mutating a "status" field. In practice the dequeue step was never
# reliably exercised: an audit found 73/82 entries still marked "pending" even
# though many were already cited in wiki/ — including sources cited for months.
# A manually-updated status field can only drift from the truth; it can't be kept
# in sync by construction. So instead of a manifest, "has this source been
# synthesized" is answered directly from the one place that can't lie about it:
# whether wiki/ (or workspace/) actually cites the source's id as a footnote.

_FOOTNOTE_DEF_RE = re.compile(r'^\[\^([A-Za-z0-9_\-]+)\]:', re.MULTILINE)
_META_TITLE_RE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.MULTILINE)


def _cited_source_ids(root: Path) -> set[str]:
    """Scan wiki/ and workspace/ for footnote definitions (`[^id]:`) whose label is a
    source id — the OKF spec requires footnote labels to equal `sources[].id`
    (CLAUDE.md §Footnote Formatting), so this is a direct, un-gameable citation check."""
    cited: set[str] = set()
    for base_name in ("wiki", "workspace"):
        base = root / base_name
        if not base.exists():
            continue
        for md_file in base.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            cited.update(_FOOTNOTE_DEF_RE.findall(text))
    return cited


def _read_meta_title(meta_path: Path) -> str:
    try:
        text = meta_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = _META_TITLE_RE.search(text)
    return m.group(1) if m else ""


def _synthesis_status(root: Path, sources_lit: Path) -> list[dict]:
    """Return one entry per fully-ingested literature source (both original.pdf and
    raw.md present — a partial directory is not a source, per CLAUDE.md §No Stubs),
    with status derived live via `_cited_source_ids` rather than read from a manifest."""
    if not sources_lit.exists():
        return []
    cited = _cited_source_ids(root)
    items = []
    for meta_path in sorted(sources_lit.rglob("metadata.md")):
        paper_dir = meta_path.parent
        if not (paper_dir / "raw.md").exists() or not (paper_dir / "original.pdf").exists():
            continue
        source_id = paper_dir.name
        domain = str(paper_dir.parent.relative_to(sources_lit))
        rel_path = str(meta_path.relative_to(root)) if meta_path.is_relative_to(root) else str(meta_path)
        items.append({
            "id": source_id,
            "title": _read_meta_title(meta_path),
            "domain": domain,
            "path": rel_path,
            "status": "done" if source_id in cited else "pending",
        })
    return items


@mcp.tool()
async def literature_status(
    status: Annotated[
        Literal["pending", "done", "all"],
        "Filter by synthesis status (default: all)",
    ] = "all",
) -> list[dict]:
    """List ingested literature sources with synthesis status, derived live by checking
    whether each source's id is cited as a `[^id]:` footnote anywhere in wiki/ or
    workspace/. There is no separate manifest — status can never drift stale, because
    it is recomputed from the citation graph on every call."""
    items = _synthesis_status(ROOT, _SOURCES_LIT)
    if status == "all":
        return items
    return [item for item in items if item["status"] == status]


# ─────────────────────────────────────────────────────────────────────────────
# Resources
# ─────────────────────────────────────────────────────────────────────────────

@mcp.resource("research://sources/index")
def resource_sources_index() -> str:
    """Live contents of sources/_index.md (master source catalogue)."""
    p = ROOT / "sources" / "_index.md"
    return p.read_text(encoding="utf-8") if p.exists() else "# Sources\n\n(empty)"


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
