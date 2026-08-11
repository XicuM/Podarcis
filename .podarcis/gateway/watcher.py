'''Config watcher and hot-reload engine for Podarcis Gateway.

Monitors .podarcis/config.yaml for changes and updates registered capabilities dynamically.
'''
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from podarcis.gateway.router import sync_gateway

logger = logging.getLogger('podarcis.gateway.watcher')

class ConfigWatcher:
    '''Monitors config file mtime and triggers dynamic re-syncing.'''

    def __init__(self, mcp: Any, root: Path, config_path: Path | None = None, interval: float = 2.0) -> None:
        self.mcp = mcp
        self.root = root
        self.config_path = config_path or (root / '.podarcis' / 'config.yaml')
        self.interval = interval
        self._last_mtime: float = 0.0
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        '''Start background watcher loop.'''
        if self._running:
            return
        self._running = True
        self._last_mtime = self._get_mtime()
        self._task = asyncio.create_task(self._watch_loop())

    def stop(self) -> None:
        '''Stop background watcher loop.'''
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    def _get_mtime(self) -> float:
        try:
            return self.config_path.stat().st_mtime if self.config_path.exists() else 0.0
        except Exception:
            return 0.0

    async def _watch_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.interval)
            current_mtime = self._get_mtime()
            if current_mtime != self._last_mtime:
                self._last_mtime = current_mtime
                logger.info(f"Config file change detected: {self.config_path.name}")
                try:
                    result = sync_gateway(self.mcp, self.root, self.config_path)
                    if result.get('changed'):
                        logger.info("Gateway capabilities changed. Hot-reload triggered.")
                except Exception as e:
                    logger.error(f"Error re-syncing gateway config: {e}")
