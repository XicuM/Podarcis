'''Merge overlay keybindings into the live herdr session config.'''

from __future__ import annotations

import re
import sys
from pathlib import Path

SEARCH_MODULE = 'podarcis.tui.actions.search_overlay'
LINT_MODULE = 'podarcis.tui.actions.lint_overlay'
SYNC_MODULE = 'podarcis.tui.actions.sync'
COMMIT_MODULE = 'podarcis.tui.actions.commit'
SYNC_ACTION = 'podarcis.wiki.sync'

_KEY_RE = re.compile(r'(?m)^\s*key\s*=\s*["\']([^"\']+)["\']')


def plugin_manifest() -> Path | None:
    from podarcis.tui.server import package_herdr_dir
    path = package_herdr_dir() / 'herdr-plugin.toml'
    return path if path.is_file() else None


def _toml_str(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _python_cmd(module: str, python_bin: str) -> str:
    return f'{python_bin} -m {module}'


def overlay_key_blocks(*, plugin_linked: bool | None = None, python_bin: str = 'python3') -> list[dict]:
    '''The four overlay bindings. Never ``prefix+c`` (new tab).'''
    if plugin_linked is None:
        plugin_linked = plugin_manifest() is not None
    if plugin_linked:
        sync = {
            'key': 'prefix+shift+s',
            'type': 'plugin_action',
            'command': SYNC_ACTION,
            'description': 'wiki repo sync',
        }
    else:
        sync = {
            'key': 'prefix+shift+s',
            'type': 'popup',
            'command': _python_cmd(SYNC_MODULE, python_bin),
            'description': 'wiki repo sync',
        }
    return [
        {
            'key': 'prefix+/',
            'type': 'popup',
            'command': _python_cmd(SEARCH_MODULE, python_bin),
            'description': 'wiki search',
            'width': '80%',
            'height': 20,
        },
        {
            'key': 'prefix+shift+l',
            'type': 'popup',
            'command': _python_cmd(LINT_MODULE, python_bin),
            'description': 'wiki lint',
            'width': '80%',
            'height': 20,
        },
        sync,
        {
            'key': 'prefix+shift+c',
            'type': 'popup',
            'command': _python_cmd(COMMIT_MODULE, python_bin),
            'description': 'lint-gated commit',
            'width': '80%',
            'height': 16,
        },
    ]


def format_key_block(spec: dict) -> str:
    lines = [
        '[[keys.command]]',
        f'key = {_toml_str(spec["key"])}',
        f'type = {_toml_str(spec["type"])}',
        f'command = {_toml_str(spec["command"])}',
        f'description = {_toml_str(spec["description"])}',
    ]
    if spec.get('width') is not None:
        lines.append(f'width = {_toml_str(str(spec["width"]))}')
    if spec.get('height') is not None:
        lines.append(f'height = {int(spec["height"])}')
    return '\n'.join(lines)


def present_keys(text: str) -> set[str]:
    return set(_KEY_RE.findall(text or ''))


def merge_overlay_keys(
    dest: Path,
    *,
    python_bin: str | None = None,
    plugin_linked: bool | None = None,
) -> list[str]:
    '''Append missing ``[[keys.command]]`` blocks. Idempotent. Never duplicates.'''
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    existing = present_keys(text)
    py = python_bin or sys.executable or 'python3'
    added: list[str] = []
    chunks: list[str] = []
    for spec in overlay_key_blocks(plugin_linked=plugin_linked, python_bin=py):
        if spec['key'] in existing:
            continue
        chunks.append(format_key_block(spec))
        added.append(spec['key'])
        existing.add(spec['key'])
    if not chunks:
        return []
    body = text.rstrip()
    extra = '\n\n' + '\n\n'.join(chunks) + '\n'
    path.write_text((body + extra) if body else extra.lstrip(), encoding='utf-8')
    return added
