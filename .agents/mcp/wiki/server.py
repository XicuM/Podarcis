"""wiki-mcp — FastMCP server for wiki querying and lint auditing.

Wraps the qmd CLI (BM25 + vector + LLM re-ranking) and `podarcis lint`.
Set PROJECT_ROOT env var to the repository root.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import Annotated, Literal


from mcp.server.fastmcp import FastMCP

# ── Path bootstrap ────────────────────────────────────────────────────────────

def _find_root() -> Path:
    env = os.environ.get("PROJECT_ROOT") or os.environ.get("PODARCIS_PROJECT") or os.environ.get("PODARCIS_ROOT")
    if env:
        return Path(env).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "AGENTS.md").exists():
            return parent
    raise RuntimeError(
        "Cannot locate project root. Set the PROJECT_ROOT environment variable."
    )

ROOT = _find_root()

# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "wiki-mcp",
    instructions=(
        "Knowledge base querier and auditor for the agentic wiki. "
        "wiki_search finds documents, wiki_fetch batch-retrieves them, wiki_publish "
        "commits a synthesis, wiki_lint audits, and wiki_reindex rebuilds the search "
        "index. All wiki_* tools span wiki/, workspace/protocols/ and sources/literature/."
    ),
)

# ── Helpers ───────────────────────────────────────────────────────────────────

QMD_ABSENT = (
    "semantic search needs the 'qmd' binary on PATH — install it, "
    "or set engines.qmd: true in .podarcis/config.yaml."
)
QMD_OFF = "QMD engine is off (engines.qmd: false in .podarcis/config.yaml)."


def _qmd_config_flag() -> bool | None:
    """`engines.qmd` from .podarcis/config.yaml, or None when the key is absent."""
    yaml_path = ROOT / ".podarcis" / "config.yaml"
    if yaml_path.exists():
        try:
            import yaml
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            raw = (data.get("engines") or {}).get("qmd")
            return None if raw is None else bool(raw)
        except Exception:
            pass
    return None


def get_qmd_status() -> tuple[Literal["disabled", "enabled_ok", "enabled_broken"], str]:
    """Determine QMD engine state: disabled, enabled_ok, or enabled_broken.

    An absent `engines.qmd` is not a decision the user made, so it follows the
    binary rather than defaulting to off and then blaming a config line nobody
    wrote. An explicit value, from the key or from $ENABLE_QMD, always wins.
    Mirrors `podarcis.search.qmd_status`.
    """
    env_flag = os.environ.get("ENABLE_QMD")
    if env_flag is not None:
        enabled: bool | None = env_flag.lower() in ("true", "1", "yes")
    else:
        enabled = _qmd_config_flag()

    if enabled is False:
        return ("disabled", QMD_OFF)

    qmd_bin = shutil.which("qmd")
    if not qmd_bin:
        # Nobody asked for it and it is not installed: nothing is broken.
        return ("enabled_broken", "'qmd' binary not found in PATH.") if enabled else ("disabled", QMD_ABSENT)
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


def _rel(path: Path) -> str:
    """Root-relative path, the way every other message here names a file."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _conflict_path(target: Path) -> Path:
    """First free `<stem>.conflict-<n><ext>` beside `target`.

    Same name the front-end's editor uses, so a conflict looks the same
    whichever side produced it, and the page lands in the collection where the
    linter and the index will both see it rather than somewhere it can be
    forgotten.
    """
    n = 1
    while (candidate := target.with_name(f"{target.stem}.conflict-{n}{target.suffix}")).exists():
        n += 1
    return candidate


async def _run_lint(*args: str) -> str:
    """Run `podarcis lint` and return its output.

    The linter is Rust and lives in the `podarcis` binary; this used to invoke
    a `check_links.py` sitting next to this file, one of two identical copies.
    A build that is missing says so, because a lint whose failure is
    indistinguishable from a lint that never ran is not a check.
    """
    try:
        from podarcis.audit import podarcis_bin
        binary = podarcis_bin(ROOT)
    except (FileNotFoundError, ImportError) as err:
        # Imported here, not at module scope: this server is otherwise
        # standalone, and a missing engine package should degrade to a legible
        # message from one tool rather than failing the whole module to load.
        return f"Link checker unavailable: {err}"
    proc = await asyncio.create_subprocess_exec(
        binary, "lint", *args,
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
            prefix = f"[Notice: {info} Operating in Native Keyword Search mode.]\n\n"
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
        return f"[Notice: {info} Index update skipped.]"
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

    # 3. Write content to the target file, keeping whatever was there.
    #
    #    This used to be a bare write_text onto the path, which made the tool a
    #    silent replace: a page a human had just edited, or one another agent
    #    wrote a minute earlier, was gone with nothing to recover it from. The
    #    front-end's editor refuses that write and parks the other version as
    #    `<name>.conflict-<n>.md`; this does the same, from the other side of
    #    the same file. Publishing is allowed to win — it just is not allowed
    #    to destroy.
    kept = None
    try:
        if target_file.is_file():
            existing = target_file.read_text(encoding="utf-8")
            if existing != content:
                kept = _conflict_path(target_file)
                kept.write_text(existing, encoding="utf-8")
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
        index_res = f"[Notice: {info} Index update skipped.]"

    # 6. Run link audits on target directory
    audit_res = ""
    try:
        audit_res = await _run_lint(str(target_file.parent))
    except Exception as e:
        audit_res = f"Link checker error: {e}"

    queue_note = (
        f"✓ '{queue_id}' is cited — literature_status will report it as 'done'."
        if queue_id_cited else
        f"⚠️ WARNING: content has no '[^{queue_id}]:' footnote — "
        f"literature_status will still report '{queue_id}' as 'pending'."
    )
    # A preserved copy nobody is told about is no better than a lost one: this
    # has to be an instruction, because the page now has a duplicate that the
    # linter counts and the index will serve.
    conflict_note = (
        f"\n⚠️ '{wiki_path}' already existed with different content. It was NOT discarded — "
        f"the previous version is now {_rel(kept)}. Read both, merge them into {wiki_path}, "
        f"and delete the copy. Leaving it is a duplicate page in the collection.\n"
        if kept else ""
    )
    res_summary = (
        f"✓ Successfully wrote wiki page to: {wiki_path}\n"
        f"{conflict_note}"
        f"{queue_note}\n"
        f"--- Index Update Output ---\n{index_res}\n"
        f"--- Link Auditor Output ---\n{audit_res}"
    )
    return res_summary



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
    return await _run_lint(*args)




# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
