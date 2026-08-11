'''Main entrypoint for Podarcis Gateway MCP Server.

Provides a unified MCP server instance exposing tools, resources, and prompts
for Podarcis node capabilities based on .podarcis/config.yaml.
'''
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import anyio

podarcis_dir = Path(__file__).resolve().parent.parent
root_dir = podarcis_dir.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(podarcis_dir) not in sys.path:
    sys.path.insert(0, str(podarcis_dir))

from mcp.server.fastmcp import FastMCP
from podarcis.gateway.router import sync_gateway
from podarcis.gateway.watcher import ConfigWatcher

logger = logging.getLogger('podarcis.gateway.server')

def _find_root() -> Path:
    env = os.environ.get("PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "AGENTS.md").exists():
            return parent
    return Path.cwd().resolve()

def create_gateway(root: Path, config_path: Path | None = None, port: int = 8000) -> tuple[FastMCP, ConfigWatcher]:
    '''Construct and initialize the Podarcis FastMCP Gateway server.'''
    mcp = FastMCP(
        "podarcis",
        instructions=(
            "Podarcis Gateway MCP server. Unified node capability pack providing "
            "wiki searching & auditing, literature discovery & paper ingestion, "
            "nutrition menumaker, financial calculations, platform diagnostics, "
            "skills, and subagent persona prompts."
        ),
        port=port,
    )

    sync_gateway(mcp, root, config_path)
    watcher = ConfigWatcher(mcp, root, config_path)
    return mcp, watcher

async def _run_server() -> int:
    '''Async entrypoint for podarcis-mcp command.'''
    parser = argparse.ArgumentParser(description="Podarcis Gateway MCP Server")
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    parser.add_argument("--transport", choices=["stdio", "http", "sse"], default="stdio", help="Transport mode")
    parser.add_argument("--port", type=int, default=9090, help="Port for HTTP/SSE transport")
    args = parser.parse_args()

    root = _find_root()
    cfg_path = Path(args.config).resolve() if args.config else None

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    mcp, watcher = create_gateway(root, cfg_path, port=args.port)

    try:
        if args.transport == "stdio":
            watcher.start()
            await mcp.run_stdio_async()
        elif args.transport == "sse":
            watcher.start()
            await mcp.run_sse_async()
        elif args.transport == "http":
            watcher.start()
            await mcp.run_streamable_http_async()
    except KeyboardInterrupt:
        logger.info("Gateway server shutting down.")
    finally:
        watcher.stop()

    return 0

def main() -> int:
    return anyio.run(_run_server)

if __name__ == "__main__":
    sys.exit(main())
