"""wiki-mcp — FastMCP server for wiki querying and lint auditing.

Wraps the qmd CLI (BM25 + vector + LLM re-ranking) and lint scripts.
Set PROJECT_ROOT env var to the repository root.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated, Literal


from mcp.server.fastmcp import FastMCP

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
_WIKI_DIR = Path(__file__).resolve().parent
_VENV_PYTHON = ROOT / ".venv" / "bin" / "python"

# Add lint scripts to sys.path for direct import
for _p in (_WIKI_DIR,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Lint scripts are executed as subprocesses by path (see _run_script), not imported.

# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "wiki-mcp",
    instructions=(
        "Knowledge base querier and auditor for the agentic wiki. "
        "wiki_search finds documents, wiki_fetch batch-retrieves them, wiki_publish "
        "commits a synthesis, wiki_lint audits, and wiki_reindex rebuilds the search "
        "index. All wiki_* tools span wiki/, workspace/protocols/ and sources/literature/. "
        "repo_sync synchronises the configured workspace repositories."
    ),
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_qmd_enabled_in_config() -> bool:
    """Read engines.qmd from .podarcis/config.yaml."""
    yaml_path = ROOT / ".podarcis" / "config.yaml"
    if yaml_path.exists():
        try:
            import yaml
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            engines = data.get("engines", {})
            return bool(engines.get("qmd", False))
        except Exception:
            pass
    return False


def get_qmd_status() -> tuple[Literal["disabled", "enabled_ok", "enabled_broken"], str]:
    """Determine QMD engine state: disabled, enabled_ok, or enabled_broken."""
    env_flag = os.environ.get("ENABLE_QMD")
    if env_flag is not None:
        enabled = env_flag.lower() in ("true", "1", "yes")
    else:
        enabled = _is_qmd_enabled_in_config()

    if not enabled:
        return ("disabled", "QMD engine is disabled in .podarcis/config.yaml.")

    qmd_bin = shutil.which("qmd")
    if not qmd_bin:
        return ("enabled_broken", "'qmd' binary not found in PATH.")
    return ("enabled_ok", qmd_bin)


# Index-health cache: `qmd status` spawns a subprocess, too slow to run per search.
_QMD_HEALTH_TTL = 300.0
_qmd_health_cache: tuple[float, str | None] = (0.0, None)

_VECTORS_RE = re.compile(r"Vectors:\s+([\d,]+)\s+embedded", re.IGNORECASE)
_PENDING_RE = re.compile(r"Pending:\s+([\d,]+)\s+need embedding", re.IGNORECASE)
_UPDATED_RE = re.compile(r"Updated:\s+(\d+)([smhd])\s+ago", re.IGNORECASE)


def _parse_index_health(status_text: str) -> str | None:
    """Return a warning for index conditions qmd does not report itself, else None.

    Deliberately narrow. qmd already prints its own "N documents need embeddings"
    notice on vsearch and query, and that text reaches the caller through _qmd(),
    so re-detecting it here would only duplicate a warning the agent already sees.

    Two conditions qmd does NOT surface:
      * A stale index. A 25-day-old index answers queries with no warning at all,
        which is how this repo searched a month-old snapshot of the wiki without
        noticing (diag-1787825572).
      * Zero embeddings. qmd downgrades this to "for better results", but with no
        vectors at all semantic and hybrid search do not work — a severity worth
        restating, since the caller otherwise treats the results as vector-backed.
    """
    m = _VECTORS_RE.search(status_text)
    vectors = int(m.group(1).replace(",", "")) if m else None
    if vectors == 0:
        p = _PENDING_RE.search(status_text)
        pending = int(p.group(1).replace(",", "")) if p else 0
        return (
            f"QMD index has NO embeddings ({pending} documents pending). Semantic and "
            "hybrid search cannot work — results below are keyword-only. Run `qmd embed`."
        )

    u = _UPDATED_RE.search(status_text)
    if u:
        amount, unit = int(u.group(1)), u.group(2).lower()
        days = {"s": 0, "m": 0, "h": amount / 24.0, "d": amount}[unit]
        if days >= 7:
            return (
                f"QMD index was last updated {amount}{unit} ago and may not reflect "
                "recent edits — run `qmd update`."
            )

    return None


async def _qmd_index_warning() -> str | None:
    """Cached index-health warning, or None when the index is healthy."""
    global _qmd_health_cache
    now = time.monotonic()
    checked_at, cached = _qmd_health_cache
    if now - checked_at < _QMD_HEALTH_TTL:
        return cached
    try:
        text = await _qmd("status")
        warning = _parse_index_health(text)
    except Exception as exc:
        warning = f"QMD index health could not be determined ({exc})."
    _qmd_health_cache = (now, warning)
    return warning


async def _native_search(
    query: str,
    collection: str = "all",
    limit: int = 5,
) -> str:
    """Fast native keyword search using ripgrep or Python regex matching."""
    search_dirs = []
    if collection in ("wiki", "all"):
        search_dirs.append(ROOT / "wiki")
    if collection in ("protocols", "all"):
        search_dirs.append(ROOT / "workspace" / "protocols")
    if collection in ("sources", "all"):
        search_dirs.append(ROOT / "sources" / "literature")

    search_dirs = [d for d in search_dirs if d.exists()]
    if not search_dirs:
        return "No files found to search."

    rg_bin = shutil.which("rg")
    if rg_bin:
        cmd = [rg_bin, "-i", "-n", "-C", "1", "--no-heading", "--fixed-strings", query] + [str(d) for d in search_dirs]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        raw_output = stdout.decode("utf-8", errors="replace").strip()
        if raw_output:
            lines = raw_output.splitlines()
            return "\n".join(lines[: limit * 10])
        else:
            return f"No matches found for '{query}' in collection '{collection}'."
    else:
        import re
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        matches = []
        for sdir in search_dirs:
            for md_file in sdir.rglob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8", errors="replace")
                    for line_idx, line in enumerate(content.splitlines(), start=1):
                        if pattern.search(line):
                            rel_path = md_file.relative_to(ROOT)
                            matches.append(f"{rel_path}:{line_idx}:{line.strip()}")
                            if len(matches) >= limit * 5:
                                break
                except Exception:
                    continue
        if matches:
            return "\n".join(matches[: limit * 5])
        return f"No matches found for '{query}' in collection '{collection}'."


async def _qmd(
    *args: str,
    json_output: bool = False,
) -> str:
    """Run a qmd command from the project root and return stdout."""
    cmd = ["qmd", *args]
    if json_output:
        cmd.append("--json")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        raise RuntimeError(f"qmd {' '.join(args)} failed: {err}")
    return stdout.decode()


async def _run_script(script: str, *args: str) -> str:
    """Run a lint Python script and return its stdout."""
    proc = await asyncio.create_subprocess_exec(
        str(_VENV_PYTHON), str(_WIKI_DIR / script), *args,
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode()
    if stderr:
        output += f"\n---\n{stderr.decode()}"
    return output

# ─────────────────────────────────────────────────────────────────────────────
# Wiki Query Tools
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def wiki_search(
    query: Annotated[str, "The search query (natural language, keyword, or grep pattern)"],
    collection: Annotated[
        Literal["wiki", "protocols", "sources", "all"],
        "Restrict to a specific collection (default: all)",
    ] = "all",
    method: Annotated[
        Literal["hybrid", "semantic", "keyword"],
        "Search strategy to employ (default: hybrid)",
    ] = "hybrid",
    limit: Annotated[int, "Maximum number of results to return (default 5)"] = 5,
    min_score: Annotated[float, "Minimum relevance score threshold (0-1, default 0.0)"] = 0.0,
    hyde: Annotated[str | None, "Hypothetical document passage to search against (HyDE)"] = None,
    explain: Annotated[bool, "Whether to include retrieval score traces and rank breakdowns"] = False,
    no_rerank: Annotated[bool, "Skip LLM reranking for fast RRF/vector results"] = False,
) -> str:
    """Consolidated search tool: supports keyword (grep), semantic (vector), hybrid, and HyDE search strategies."""
    status, info = get_qmd_status()

    if status == "enabled_broken":
        warning = (
            "⚠️ WARNING: QMD Vector DB Engine is explicitly ENABLED in podarcis.yaml, "
            f"but QMD is unavailable ({info}).\n"
            "Falling back to Native Keyword Search mode.\n\n"
        )
        native_res = await _native_search(query, collection=collection, limit=limit)
        return warning + native_res

    if status == "disabled":
        prefix = ""
        if method in ("semantic", "hybrid") or hyde:
            prefix = "[Notice: QMD Vector DB engine is disabled in podarcis.yaml. Operating in Native Keyword Search mode.]\n\n"
        native_res = await _native_search(query, collection=collection, limit=limit)
        return prefix + native_res

    if hyde:
        query_doc = f"intent: {query}\nhyde: {hyde}\nlex: {query}"
        args = ["query", query_doc]
    elif method == "keyword":
        args = ["search", query]
    elif method == "semantic":
        args = ["vsearch", query, "-n", str(limit)]
    else:  # hybrid
        args = ["query", query]

    if collection != "all":
        args += ["-c", collection]

    if method == "hybrid" or hyde:
        if min_score > 0:
            args += ["--min-score", str(min_score)]

    if explain:
        args.append("--explain")
    if no_rerank:
        args.append("--no-rerank")

    try:
        result = await _qmd(*args)
        # A healthy binary does not imply a usable index — check before the caller
        # treats vector-backed results as authoritative.
        if method in ("semantic", "hybrid") or hyde:
            health = await _qmd_index_warning()
            if health:
                return f"⚠️ {health}\n\n{result}"
        return result
    except Exception as e:
        warning = (
            f"⚠️ WARNING: QMD Vector DB execution failed ({e}).\n"
            "Falling back to Native Keyword Search mode.\n\n"
        )
        native_res = await _native_search(query, collection=collection, limit=limit)
        return warning + native_res


@mcp.tool()
async def wiki_fetch(
    pattern: Annotated[
        str,
        "Glob pattern (e.g. 'wiki/nutrition/*.md') or relative file path pattern to batch fetch.",
    ],
    max_lines: Annotated[int, "Maximum lines to read per file (default 50)"] = 50,
    max_bytes: Annotated[int, "Skip files larger than N bytes (default 10240)"] = 10240,
) -> str:
    """Batch retrieve content snippets from multiple matching files across wiki, workspace, or sources."""
    status, _ = get_qmd_status()
    if status == "enabled_ok":
        try:
            return await _qmd("multi-get", pattern, "-l", str(max_lines), "--max-bytes", str(max_bytes))
        except Exception:
            pass

    # Native fallback for multi-get
    matches = list(ROOT.glob(pattern)) if "*" in pattern or "?" in pattern else [ROOT / pattern]
    matches = [m for m in matches if m.is_file()]
    if not matches:
        return f"No files matched pattern: {pattern}"

    outputs = []
    for f in matches[:20]:  # Limit to 20 matching files max
        try:
            size = f.stat().st_size
            if size > max_bytes:
                outputs.append(f"=== File: {f.relative_to(ROOT)} (Skipped: {size} bytes > max {max_bytes}) ===")
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            snippet = "\n".join(lines[:max_lines])
            trunc = f"\n[... truncated {len(lines) - max_lines} more lines]" if len(lines) > max_lines else ""
            outputs.append(f"=== File: {f.relative_to(ROOT)} ===\n{snippet}{trunc}")
        except Exception as e:
            outputs.append(f"=== File: {f.relative_to(ROOT)} (Error: {e}) ===")
    return "\n\n".join(outputs)


@mcp.tool()
async def wiki_reindex() -> str:
    """Rebuild the qmd semantic index and refresh collection context summaries."""
    status, info = get_qmd_status()
    if status == "disabled":
        return "[Notice: QMD Vector DB engine is disabled in podarcis.yaml. Index update skipped.]"
    if status == "enabled_broken":
        return (
            "⚠️ WARNING: QMD Vector DB Engine is ENABLED in podarcis.yaml, "
            f"but QMD is unavailable ({info}). Index update skipped."
        )
    try:
        out = await _qmd("update")

        ctx_count = 0
        for search_dir in ["wiki", "workspace/protocols", "sources/literature"]:
            dir_path = ROOT / search_dir
            if not dir_path.exists():
                continue
            for index_file in dir_path.rglob("_index.md"):
                rel_dir = index_file.parent.relative_to(ROOT)
                try:
                    txt = index_file.read_text(encoding="utf-8")
                    summary_line = ""
                    for line in txt.splitlines():
                        if line.startswith("rationale:") or line.startswith("title:"):
                            summary_line = line.split(":", 1)[1].strip(" \"'")
                            break
                        elif line.startswith("# "):
                            summary_line = line[2:].strip()
                            break
                    if summary_line:
                        await _qmd("context", "add", str(rel_dir), summary_line)
                        ctx_count += 1
                except Exception:
                    continue
        if ctx_count > 0:
            out += f"\n✓ Synced context summaries for {ctx_count} folder(s)."
        return out
    except Exception as e:
        return f"⚠️ WARNING: QMD Index update failed ({e})."


@mcp.tool()
async def wiki_publish(
    queue_id: Annotated[str, "The source ID being synthesized (e.g., 'smith_2023_protein_synthesis') — must match a `[^queue_id]:` footnote in `content` so `literature_status` picks up the citation and reports this source as 'done'."],
    wiki_path: Annotated[str, "Target file path to write the synthesis (relative to PROJECT_ROOT, e.g. 'wiki/nutrition/protein.md')"],
    content: Annotated[str, "Markdown content to write to the wiki file"],
    category: Annotated[str, "YAML frontmatter category (e.g., 'nutrition')"],
    rationale: Annotated[str, "YAML frontmatter rationale sentence explaining the page design"],
    related: Annotated[list[str], "List of related internal markdown link paths"],
    title: Annotated[str, "Title of the wiki page"],
) -> str:
    """Atomic transaction tool: Writes wiki page with standard frontmatter, updates search index,
    and runs link audits. There is no separate queue to mark 'done' — as long as `content`
    contains a `[^queue_id]:` footnote, `literature_status` will report this source's
    status as 'done' on its next call, derived live from the citation."""
    target_file = ROOT / wiki_path

    # 1. Ensure target directory exists
    target_file.parent.mkdir(parents=True, exist_ok=True)

    # 2. Add standardized YAML frontmatter if not present in content
    if not content.strip().startswith("---"):
        related_str = "\n".join([f"  - \"{r}\"" for r in related])
        frontmatter = (
            f"---\n"
            f"title: \"{title}\"\n"
            f"category: \"{category}\"\n"
            f"related:\n{related_str}\n"
            f"rationale: \"{rationale}\"\n"
            f"---\n\n"
        )
        content = frontmatter + content

    # 3. Write content to the target file
    try:
        target_file.write_text(content, encoding="utf-8")
    except Exception as e:
        return f"Error writing wiki file: {e}"

    # 4. Sanity-check that the wiki page actually cites queue_id — otherwise
    #    literature_status will keep reporting this source as 'pending' after this call,
    #    which would silently defeat the point of calling this tool.
    queue_id_cited = bool(re.search(rf'^\[\^{re.escape(queue_id)}\]:', content, re.MULTILINE))

    # 5. Rebuild search index (if QMD active)
    index_res = ""
    status, info = get_qmd_status()
    if status == "enabled_ok":
        try:
            index_res = await _qmd("update")
        except Exception as e:
            index_res = f"Index update warning: {e}"
    elif status == "enabled_broken":
        index_res = f"⚠️ WARNING: QMD Vector DB Engine is ENABLED in podarcis.yaml, but QMD is unavailable ({info}). Index update skipped."
    else:
        index_res = "[Notice: QMD Vector DB engine is disabled in podarcis.yaml. Index update skipped.]"

    # 6. Run link audits on target directory
    audit_res = ""
    try:
        audit_res = await _run_script("check_links.py", str(target_file.parent))
    except Exception as e:
        audit_res = f"Link checker error: {e}"

    queue_note = (
        f"✓ '{queue_id}' is cited — literature_status will report it as 'done'."
        if queue_id_cited else
        f"⚠️ WARNING: content has no '[^{queue_id}]:' footnote — "
        f"literature_status will still report '{queue_id}' as 'pending'."
    )
    res_summary = (
        f"✓ Successfully wrote wiki page to: {wiki_path}\n"
        f"{queue_note}\n"
        f"--- Index Update Output ---\n{index_res}\n"
        f"--- Link Auditor Output ---\n{audit_res}"
    )
    return res_summary



# ─────────────────────────────────────────────────────────────────────────────
# Workspace Synchronization & Repository Tools
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def repo_sync(
    action: Annotated[
        str,
        "Action to perform: 'pull' (sync/pull remotes and gdrive), 'push' (push local commits to remotes), or 'all'.",
    ] = "pull",
    auto_commit: Annotated[
        bool,
        "Automatically commit uncommitted local changes before pushing (only applicable for action='push' or 'all').",
    ] = False,
    message: Annotated[
        str,
        "Commit message for auto_commit.",
    ] = "chore: sync workspace changes",
) -> str:
    """Synchronize all configured workspace repositories (clones missing repos, pulls remote origins, ingests Google Drive deltas, and updates backend configs)."""
    podarcis_dir = ROOT / ".podarcis"
    if str(podarcis_dir) not in sys.path:
        sys.path.insert(0, str(podarcis_dir))
    from repos import sync_repos_full, push_repos, get_repo_status
    from components import sync_all_backends

    results = {}
    if action in ("pull", "all"):
        sync_all_backends(ROOT)
        results["pull"] = sync_repos_full(ROOT)
    if action in ("push", "all"):
        results["push"] = push_repos(ROOT, auto_commit=auto_commit, message=message)

    results["status"] = get_repo_status(ROOT)
    return json.dumps(results, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Lint / Audit Tools
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def wiki_lint(
    scope_path: Annotated[
        str,
        "Directory or file to audit (relative to PROJECT_ROOT, e.g. 'wiki/' or 'wiki/nutrition/').",
    ],
    fix: Annotated[
        bool,
        "Whether to automatically repair fixable YAML syntax errors (such as unquoted colons in frontmatter).",
    ] = False,
) -> str:
    """Check for broken links, missing/unused footnotes, YAML/frontmatter syntax & schema errors, directory bloat, and page length."""
    path = ROOT / scope_path
    args = [str(path)]
    if fix:
        args.append("--fix")
    return await _run_script("check_links.py", *args)




# ─────────────────────────────────────────────────────────────────────────────
# Resources
# ─────────────────────────────────────────────────────────────────────────────

@mcp.resource("wiki://collections/wiki")
def resource_wiki_index() -> str:
    """Directory listing of all pages in the wiki collection."""
    wiki_dir = ROOT / "wiki"
    files = sorted(wiki_dir.rglob("*.md"))
    lines = [f"# Wiki Collection ({len(files)} pages)\n"]
    for f in files:
        rel = f.relative_to(ROOT)
        lines.append(f"- [{rel}]({rel})")
    return "\n".join(lines)


@mcp.resource("wiki://collections/protocols")
def resource_protocols_index() -> str:
    """Directory listing of all pages in the protocols collection."""
    proto_dir = ROOT / "workspace" / "protocols"
    files = sorted(proto_dir.rglob("*.md")) if proto_dir.exists() else []
    lines = [f"# Protocols Collection ({len(files)} pages)\n"]
    for f in files:
        rel = f.relative_to(ROOT)
        lines.append(f"- [{rel}]({rel})")
    return "\n".join(lines)


@mcp.resource("wiki://collections/sources")
def resource_sources_index() -> str:
    """Directory listing of all pages in the sources/literature collection."""
    src_dir = ROOT / "sources" / "literature"
    files = sorted(src_dir.rglob("*.md")) if src_dir.exists() else []
    lines = [f"# Sources Collection ({len(files)} pages)\n"]
    for f in files:
        rel = f.relative_to(ROOT)
        lines.append(f"- [{rel}]({rel})")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
