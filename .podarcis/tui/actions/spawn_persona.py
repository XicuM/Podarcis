'''``podarcis wiki persona NAME``: split an agent pane and prompt with current.md.'''

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from podarcis.console import console
from podarcis.herdr.layout import panes_by_label, wait_for_shell
from podarcis.tui import PANE_AGENT, PERSONAS, SESSION_NAME
from podarcis.tui.context import read_snippet
from podarcis.tui.deps import resolve_deps, resolve_harness
from podarcis.tui.root import WikiRootError, find_wiki_root
from podarcis.tui.server import HerdrSession, session_sock
from podarcis.tui.session import in_wiki_session


def persona_start_argv(name: str, kind: str, pane_id: str) -> list[str]:
    argv = ['agent', 'start', name, '--kind', kind, '--pane', pane_id]
    if kind == 'claude':
        argv += ['--', '--agent', name]
    return argv


def persona_prompt_text(name: str, kind: str, snippet: str) -> str:
    snippet = (snippet or '').strip()
    if kind == 'opencode':
        if snippet:
            return f'@{name}\n\n{snippet}'
        return f'@{name}'
    return snippet


def _pane_id(result: dict) -> str:
    pane = result.get('pane')
    if isinstance(pane, dict) and pane.get('pane_id'):
        return str(pane['pane_id'])
    pane_id = result.get('pane_id')
    if pane_id:
        return str(pane_id)
    raise RuntimeError(f'herdr response has no pane_id: {result!r}')


def spawn_persona(
    session: HerdrSession,
    name: str,
    *,
    kind: str,
    wiki_root: Path,
    snippet: str | None = None,
) -> list[list[str]]:
    '''Split the agent pane, start the persona, prompt with current.md. Returns CLI argv log.'''
    labels = panes_by_label(session.cli)
    calls: list[list[str]] = []
    text = persona_prompt_text(name, kind, snippet if snippet is not None else read_snippet(wiki_root))

    if name in labels:
        pane_id = labels[name]
        if text.strip():
            prompt = ['agent', 'prompt', name, text]
            calls.append(prompt)
            session.cli(*prompt)
        return calls

    agent_id = labels.get(PANE_AGENT)
    if not agent_id:
        raise RuntimeError('agent pane not found; launch `podarcis wiki` first.')
    split = ['pane', 'split', agent_id, '--direction', 'down', '--no-focus']
    calls.append(split)
    created = session.cli(*split)
    pane_id = _pane_id(created)
    rename = ['pane', 'rename', pane_id, name]
    calls.append(rename)
    session.cli(*rename)
    wait_for_shell(session.cli, pane_id)

    start = persona_start_argv(name, kind, pane_id)
    calls.append(start)
    session.cli(*start)
    if text.strip():
        prompt = ['agent', 'prompt', name, text]
        calls.append(prompt)
        session.cli(*prompt)
    return calls


def cmd_wiki_persona(args: argparse.Namespace) -> int:
    try:
        wiki_root = find_wiki_root(explicit=getattr(args, 'root', None))
    except WikiRootError as exc:
        console.print(f'[bold red]Error:[/bold red] {getattr(exc, "message", str(exc))}')
        return 1
    name = (getattr(args, 'name', None) or '').strip()
    if name not in PERSONAS:
        console.print(
            '[bold red]Error:[/bold red] unknown persona '
            f'"{name}". Choose from: {", ".join(PERSONAS)}.'
        )
        return 1

    deps = resolve_deps(wiki_root)
    if not deps.herdr:
        console.print(f'[bold red]Error:[/bold red] {deps.herdr_missing_message}')
        return 1
    if not session_sock().exists():
        console.print(
            '[bold red]Error:[/bold red] herdr session podarcis is not running; '
            'launch `podarcis wiki` first.'
        )
        return 1
    kind = resolve_harness(wiki_root) or deps.harness
    if not kind:
        console.print('[bold red]Error:[/bold red] no agent harness found (install opencode or claude).')
        return 1

    env = os.environ.copy()
    env['HERDR_SESSION'] = SESSION_NAME
    env['PROJECT_ROOT'] = str(wiki_root)
    session = HerdrSession(deps.herdr, env=env, sock=session_sock())
    try:
        spawn_persona(session, name, kind=kind, wiki_root=wiki_root)
    except RuntimeError as exc:
        console.print(f'[bold red]Error:[/bold red] {exc}')
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    if not in_wiki_session():
        return 0
    argv = list(sys.argv[1:] if argv is None else argv)
    name = argv[0] if argv else os.environ.get('PODARCIS_PERSONA', '')
    ns = argparse.Namespace(name=name, root=os.environ.get('PROJECT_ROOT'))
    return cmd_wiki_persona(ns)


if __name__ == '__main__':
    raise SystemExit(main())
