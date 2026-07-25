"""wiki-mcp — FastMCP server for wiki querying and lint auditing.

Wraps the qmd CLI (BM25 + vector + LLM re-ranking) and lint scripts.
Set PROJECT_ROOT env var to the repository root.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
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

# Import lint scripts directly (they expose importable functions)
import check_links   # noqa: E402

# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "wiki-mcp",
    instructions=(
        "Knowledge base querier and auditor for the agentic wiki. "
        "Use wiki_* tools to search or retrieve documents, lint_* tools for audits. "
        "All wiki_* tools query across wiki/ and workspace/protocols/."
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
        if not (ROOT / "workspace" / "protocols").exists():
            search_dirs.append(ROOT / "user" / "protocols")
    if collection in ("sources", "all"):
        search_dirs.append(ROOT / "sources" / "literature")

    search_dirs = [d for d in search_dirs if d.exists()]
    if not search_dirs:
        return "No files found to search."

    rg_bin = shutil.which("rg")
    if rg_bin:
        cmd = [rg_bin, "-i", "-n", "-C", "1", "--no-heading", query] + [str(d) for d in search_dirs]
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
    """Run a qmd command from the wiki MCP directory (where qmd.yml lives) and return stdout."""
    cmd = ["qmd", *args]
    if json_output:
        cmd.append("--json")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(_WIKI_DIR),
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
        Literal["wiki", "protocols", "all"],
        "Restrict to a specific collection (default: all)",
    ] = "all",
    method: Annotated[
        Literal["hybrid", "semantic", "keyword"],
        "Search strategy to employ (default: hybrid)",
    ] = "hybrid",
    limit: Annotated[int, "Maximum number of results to return (default 5)"] = 5,
    min_score: Annotated[float, "Minimum relevance score threshold (0-1, default 0.0)"] = 0.0,
) -> str:
    """Consolidated search tool: supports keyword (grep), semantic (vector), and hybrid strategies."""
    status, info = get_qmd_status()

    if status == "enabled_broken":
        warning = (
            "⚠️ WARNING: QMD Vector DB Engine is explicitly ENABLED in podarcis.yaml, "
            f"but QMD is unavailable ({info}).\n"
            "Please install @tobilu/qmd via npm or run 'python tui/setup.py'.\n"
            "Falling back to Native Keyword Search mode.\n\n"
        )
        native_res = await _native_search(query, collection=collection, limit=limit)
        return warning + native_res

    if status == "disabled":
        prefix = ""
        if method in ("semantic", "hybrid"):
            prefix = "[Notice: QMD Vector DB engine is disabled in podarcis.yaml. Operating in Native Keyword Search mode.]\n\n"
        native_res = await _native_search(query, collection=collection, limit=limit)
        return prefix + native_res

    if method == "keyword":
        args = ["search", query]
        if collection != "all":
            args += ["-c", collection]
    elif method == "semantic":
        args = ["vsearch", query, "-n", str(limit)]
        if collection != "all":
            args += ["-c", collection]
    else:  # hybrid
        args = ["query", query]
        if collection != "all":
            args += ["-c", collection]
        if min_score > 0:
            args += ["--min-score", str(min_score)]
    try:
        return await _qmd(*args)
    except Exception as e:
        warning = (
            f"⚠️ WARNING: QMD Vector DB execution failed ({e}).\n"
            "Falling back to Native Keyword Search mode.\n\n"
        )
        native_res = await _native_search(query, collection=collection, limit=limit)
        return warning + native_res


@mcp.tool()
async def wiki_vsearch(
    query: Annotated[str, "Natural-language semantic query"],
    n: Annotated[int, "Number of results to return (default 5)"] = 5,
    collection: Annotated[
        Literal["wiki", "protocols", "sources", "all"],
        "Restrict to a specific collection (default: all)",
    ] = "all",
) -> str:
    """Semantic (vector) search (deprecated: use wiki_search with method='semantic' instead)."""
    return await wiki_search(query, collection=collection, method="semantic", limit=n)


@mcp.tool()
async def wiki_query(
    query: Annotated[str, "Query string — hybrid BM25 + vector + LLM re-ranking"],
    collection: Annotated[
        Literal["wiki", "protocols", "sources", "all"],
        "Restrict to a specific collection (default: all)",
    ] = "all",
    min_score: Annotated[float, "Minimum relevance score threshold (0–1, default 0.0)"] = 0.0,
) -> str:
    """Best-quality hybrid search (deprecated: use wiki_search with method='hybrid' instead)."""
    return await wiki_search(query, collection=collection, method="hybrid", min_score=min_score)


@mcp.tool()
async def wiki_get(
    path: Annotated[
        str,
        "Relative path or filename (e.g. 'sargantana_core.md' or 'wiki/riscv_cores/sargantana_core.md').",
    ],
) -> str:
    """Retrieve the full content of a specific document by its relative path or filename. Auto-resolves basenames if unique."""
    resolved_path = Path(path)
    if resolved_path.is_absolute():
        try:
            resolved_path = resolved_path.relative_to(ROOT)
        except ValueError:
            pass

    full_target = ROOT / resolved_path
    if full_target.is_file():
        try:
            return full_target.read_text(encoding="utf-8")
        except Exception as e:
            return f"Error reading file '{path}': {e}"

    # Search under wiki/, user/protocols/, workspace/protocols/, sources/literature/
    name_query = resolved_path.name
    matches = []
    for search_dir in ["wiki", "workspace/protocols", "user/protocols", "sources/literature"]:
        dir_path = ROOT / search_dir
        if dir_path.exists():
            matches.extend(list(dir_path.rglob(f"*{name_query}*")))

    matches = sorted(list(set([m for m in matches if m.is_file()])))

    if len(matches) == 1:
        try:
            return matches[0].read_text(encoding="utf-8")
        except Exception as e:
            return f"Error reading file '{matches[0].relative_to(ROOT)}': {e}"
    elif len(matches) > 1:
        options = "\n".join([f"- {m.relative_to(ROOT)}" for m in matches])
        return f"Error: Multiple files matched '{path}'. Please specify the exact path:\n{options}"
    else:
        return f"Error: File '{path}' not found."


@mcp.tool()
async def wiki_update_index() -> str:
    """Rebuild the qmd semantic index after adding or modifying wiki/source files."""
    status, info = get_qmd_status()
    if status == "disabled":
        return "[Notice: QMD Vector DB engine is disabled in podarcis.yaml. Index update skipped.]"
    if status == "enabled_broken":
        return (
            "⚠️ WARNING: QMD Vector DB Engine is ENABLED in podarcis.yaml, "
            f"but QMD is unavailable ({info}). Index update skipped."
        )
    try:
        return await _qmd("update")
    except Exception as e:
        return f"⚠️ WARNING: QMD Index update failed ({e})."


@mcp.tool()
async def complete_source_synthesis(
    queue_id: Annotated[str, "The ID of the enqueued item (e.g., 'smith_2023_protein_synthesis')"],
    wiki_path: Annotated[str, "Target file path to write the synthesis (relative to PROJECT_ROOT, e.g. 'wiki/nutrition/protein.md')"],
    content: Annotated[str, "Markdown content to write to the wiki file"],
    category: Annotated[str, "YAML frontmatter category (e.g., 'nutrition')"],
    rationale: Annotated[str, "YAML frontmatter rationale sentence explaining the page design"],
    related: Annotated[list[str], "List of related internal markdown link paths"],
    title: Annotated[str, "Title of the wiki page"],
) -> str:
    """Atomic transaction tool: Writes wiki page with standard frontmatter, marks the queue item as done, updates search index, and runs link audits."""
    import datetime
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

    # 4. Update state.json (mark queue item status as 'done')
    state_path = ROOT / "state.json"
    queue_updated = False
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            queue = state.setdefault("ingestion_queue", [])
            for item in queue:
                if item.get("id") == queue_id:
                    item["status"] = "done"
                    item["completed_at"] = datetime.datetime.now().isoformat()
                    queue_updated = True
                    break
            if queue_updated:
                state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as e:
            return f"Wiki file written, but failed to update state.json queue: {e}"

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

    res_summary = (
        f"✓ Successfully wrote wiki page to: {wiki_path}\n"
        f"✓ Queue status for '{queue_id}' updated to 'done': {queue_updated}\n"
        f"--- Index Update Output ---\n{index_res}\n"
        f"--- Link Auditor Output ---\n{audit_res}"
    )
    return res_summary



# ─────────────────────────────────────────────────────────────────────────────
# Lint / Audit Tools
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def lint_check_links(
    scope_path: Annotated[
        str,
        "Directory or file to audit (relative to PROJECT_ROOT, e.g. 'wiki/' or 'wiki/nutrition/').",
    ],
) -> str:
    """Check for broken links, missing/unused footnotes, directory bloat, and page length."""
    path = ROOT / scope_path
    return await _run_script("check_links.py", str(path))




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
    proto_dir = ROOT / "user" / "protocols"
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
