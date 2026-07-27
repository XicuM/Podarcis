"""research-mcp — FastMCP server for academic literature discovery and ingestion.

Provides typed async tools for:
  - Searching literature (academic-mcp for multi-provider search)
  - Download papers: fetches metadata, PDF, extracts text via markitdown,
    writes sources/ directory structure, and updates sources/state.json — all natively
    in async Python with httpx, no subprocess boundary.
  - CRUD operations on the ingestion queue (sources/state.json)

Set PROJECT_ROOT env var to the repository root.
"""
from __future__ import annotations

import asyncio
import datetime
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

def _resolve_sources_paths(root: Path) -> tuple[Path, Path]:
    """Return (state_path, sources_lit) based on sources_backend in config.yaml."""
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
        return root / "sources" / "state.json", root / "sources" / "literature"
    return root / "workspace" / "state.json", root / "workspace" / "literature"

_STATE_PATH, _SOURCES_LIT = _resolve_sources_paths(ROOT)

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

# Allowed outbound domains (runtime_gate policy)
_ALLOWED_DOMAINS = {
    "api.semanticscholar.org",
    "export.arxiv.org",
    "api.openalex.org",
    "eutils.ncbi.nlm.nih.gov",
    "www.ncbi.nlm.nih.gov",
    "googleapis.com",
    "api.unpaywall.org",
    "arxiv.org",
}

# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "research-mcp",
    instructions=(
        "Literature discovery and ingestion server. "
        "Use search_literature to find papers across multiple academic providers. "
        "Use download_paper to fetch, extract, and enqueue them (PDF → raw.md → metadata.md). "
        "Use queue_* tools to inspect and manage the ingestion queue. "
        "The queue state file location depends on sources_backend in .podarcis/config.yaml: "
        "'gdrive' → workspace/state.json, 'local' → sources/state.json."
    ),
)

# Global asyncio lock — shared by download_paper and all queue mutations
_state_lock = asyncio.Lock()

# ── httpx client factory ──────────────────────────────────────────────────────

def _make_client(**kwargs) -> httpx.AsyncClient:
    """Return a configured async httpx client with retries and a shared UA header."""
    transport = httpx.AsyncHTTPTransport(retries=3)
    headers = {"User-Agent": "Mozilla/5.0 (agentic-wiki research-mcp/1.0)"}
    if _API_KEY:
        headers["x-api-key"] = _API_KEY
    return httpx.AsyncClient(transport=transport, headers=headers, timeout=30, follow_redirects=True, **kwargs)


def _check_domain(url: str) -> None:
    """Enforce the outbound domain allow-list. Raises ValueError for disallowed hosts."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if not any(host == d or host.endswith("." + d) for d in _ALLOWED_DOMAINS):
        raise ValueError(
            f"Outbound request to '{host}' is blocked. "
            f"Allowed domains: {sorted(_ALLOWED_DOMAINS)}"
        )

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
            doi_url = work.get("doi") or ""
            doi = doi_url.replace("https://doi.org/", "").lower() or None
            return PaperMetadata(
                title=work.get("title") or "Unknown Title",
                abstract=abstract or "No abstract available.",
                authors=authors,
                year=work.get("publication_year") or "Unknown",
                pdf_url=(work.get("primary_location") or {}).get("pdf_url"),
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
                elif aid.get("idtype") == "pmc":
                    pmcid = aid.get("id")
            pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/" if pmcid else None
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


async def _fetch_unpaywall_pdf(doi: str) -> str | None:
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
        return None  # Unpaywall requires a valid contact email
    url = f"https://api.unpaywall.org/v2/{doi}?email={contact_email}"
    _check_domain(url)
    async with _make_client() as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            loc = data.get("best_oa_location") or {}
            return loc.get("url_for_pdf")
        except Exception:
            return None


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
        search_results = await search_literature(paper_id, limit=1)
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

async def _download_pdf(url: str, dest: Path) -> None:
    """Stream a PDF to disk with retries."""
    _check_domain(url)
    async with _make_client() as client:
        for attempt in range(5):
            try:
                async with client.stream("GET", url, headers={"Accept": "application/pdf"}) as resp:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        await asyncio.sleep(2 ** attempt + 1)
                        continue
                    resp.raise_for_status()
                    with dest.open("wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (429,) and exc.response.status_code < 500:
                    raise
                await asyncio.sleep(2 ** attempt + 1)
            except httpx.TransportError:
                await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to download PDF after 5 attempts: {url}")


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
        if not meta.pdf_url:
            # Try Unpaywall
            if meta.doi:
                meta.pdf_url = await _fetch_unpaywall_pdf(meta.doi)
        if not meta.pdf_url:
            shutil.rmtree(paper_dir, ignore_errors=True)
            raise ValueError(
                "No open-access PDF found via any provider. "
                "Please supply the PDF manually and use a local: paper_id."
            )
        await _download_pdf(meta.pdf_url, pdf_path)
        pdf_source_url = meta.pdf_url

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

    # ── Step 4: update sources/state.json + domain _index.md ─────────────────────────
    await ctx.report_progress(4, 4, "Updating manifest…")
    abstract_summary = (meta.abstract or "").replace("\n", " ").strip()[:200]
    if len(meta.abstract or "") > 200:
        abstract_summary += "..."

    async with _state_lock:
        _update_state_json(filename_base, domain, meta.title, abstract_summary, meta.year)
        _update_domain_index(filename_base, domain, meta.title, abstract_summary, meta.year)

    return {
        "status": "ingested",
        "paper_dir": str(paper_dir.relative_to(ROOT)),
        "files": ["original.pdf", "raw.md", "metadata.md"],
        "queued_for_ingest": True,
    }


def _update_state_json(filename_base, domain, title, abstract_summary, year):
    state = {"version": "1.0", "ingestion_queue": []}
    if _STATE_PATH.exists():
        try:
            state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    queue = state.setdefault("ingestion_queue", [])
    if not any(item.get("id") == filename_base for item in queue):
        queue.append({
            "id": filename_base,
            "type": "Literature",
            "path": str((_SOURCES_LIT / domain / filename_base / "raw.md").relative_to(ROOT)),
            "summary": f"{title} - {abstract_summary}",
            "enqueued_at": datetime.datetime.now().isoformat(),
            "status": "pending",
            "tags": [domain],
        })
        _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


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

@mcp.tool()
async def search_literature(
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
    """Search academic literature across multiple providers via academic-mcp.

    Returns a list of papers with title, authors, year, abstract, and provider IDs.
    Never fabricates results — if no papers are found, returns an empty list.
    """
    try:
        from academic_mcp import search as academic_search  # type: ignore[import]
        kwargs = {"query": query, "limit": limit}
        if provider != "all":
            kwargs["source"] = provider
        results = await academic_search(**kwargs)
        return results if isinstance(results, list) else []
    except ImportError:
        # academic-mcp not installed — fall back to Semantic Scholar directly
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={query}&limit={limit}"
            f"&fields=paperId,title,authors,year,abstract,openAccessPdf,externalIds"
        )
        _check_domain(url)
        async with _make_client() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json().get("data", [])


# ─────────────────────────────────────────────────────────────────────────────
# MCP Tools — Paper Download & Ingestion
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def download_paper(
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
      4. Write metadata.md and update sources/state.json + domain _index.md

    Returns paths to the created files. The agent should then git commit.
    Does NOT add failed papers to the manifest — halts and raises on any error.
    """
    # Sanitise filename_base
    if re.search(r"[/\\.]", filename_base):
        raise ValueError("filename_base must not contain '/', '\\', or '.' characters.")

    filename_base = filename_base.removesuffix(".pdf").removesuffix(".pdf.md")

    await ctx.report_progress(0, 4, "Fetching metadata…")
    meta = await _resolve_metadata(paper_id)

    return await _ingest_paper(ctx, paper_id, filename_base, domain, meta)


# ─────────────────────────────────────────────────────────────────────────────
# MCP Tools — Ingestion Queue (sources/state.json CRUD)
# ─────────────────────────────────────────────────────────────────────────────

def _read_state() -> dict:
    if not _STATE_PATH.exists():
        return {"version": "1.0", "ingestion_queue": []}
    return json.loads(_STATE_PATH.read_text(encoding="utf-8"))


def _write_state(state: dict) -> None:
    _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


@mcp.tool()
async def queue_list(
    status: Annotated[
        Literal["pending", "processing", "done", "all"],
        "Filter by status (default: all)",
    ] = "all",
) -> list[dict]:
    """List items in the ingestion queue from sources/state.json, optionally filtered by status."""
    async with _state_lock:
        state = _read_state()
    queue = state.get("ingestion_queue", [])
    if status == "all":
        return queue
    return [item for item in queue if item.get("status") == status]


@mcp.tool()
async def queue_enqueue(
    id: Annotated[str, "Unique identifier for this queue item"],
    type: Annotated[str, "Item type (e.g. 'Literature')"],
    path: Annotated[str, "Relative path to the raw source file"],
    summary: Annotated[str, "One-line summary of the source"],
    tags: Annotated[list[str], "Domain tags (e.g. ['nutrition', 'sleep'])"],
) -> dict:
    """Append an item to the ingestion queue in sources/state.json."""
    async with _state_lock:
        state = _read_state()
        queue = state.setdefault("ingestion_queue", [])
        if any(item.get("id") == id for item in queue):
            return {"status": "already_enqueued", "id": id}
        entry = {
            "id": id,
            "type": type,
            "path": path,
            "summary": summary,
            "enqueued_at": datetime.datetime.now().isoformat(),
            "status": "pending",
            "tags": tags,
        }
        queue.append(entry)
        _write_state(state)
    return {"status": "enqueued", "entry": entry}


@mcp.tool()
async def queue_dequeue(
    id: Annotated[str, "ID of the queue item to remove after successful ingestion"],
) -> dict:
    """Remove a completed item from the ingestion queue in sources/state.json."""
    async with _state_lock:
        state = _read_state()
        queue = state.get("ingestion_queue", [])
        removed = [item for item in queue if item.get("id") == id]
        if not removed:
            raise ValueError(f"No queue item with id '{id}' found.")
        state["ingestion_queue"] = [item for item in queue if item.get("id") != id]
        _write_state(state)
    return {"status": "dequeued", "removed": removed[0]}


# ─────────────────────────────────────────────────────────────────────────────
# Resources
# ─────────────────────────────────────────────────────────────────────────────

@mcp.resource("research://state")
def resource_state() -> str:
    """Live contents of sources/state.json (ingestion queue manifest)."""
    return _STATE_PATH.read_text(encoding="utf-8") if _STATE_PATH.exists() else "{}"


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
