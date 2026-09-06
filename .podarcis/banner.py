'''Header banner and lore rendering for the TUI application.

The banner answers three questions and deliberately no others: what is costing
context, what is scheduled to run, and whether the knowledge repos are in sync.
Skills and personas appear nowhere: the harness loads only their one-line
description until something invokes them, so there is nothing to toggle and
nothing to budget.
'''

# Standard library imports
import random
from itertools import zip_longest
from pathlib import Path

# Local imports
from common import load_one_liners, load_version_info, get_config_value
from components import discover_components, get_enabled_mcp_servers
from console import console
from repos import get_repo_status
from rich.cells import cell_len
from rich.text import Text

BORDER_STYLE = 'bold #29b8db'
ACCENT = '#29b8db'

LEFT_W, COL_GAP, RIGHT_W = 26, 4, 38
INNER_W = LEFT_W + COL_GAP + RIGHT_W

_FRONTEND_DISPLAY = {'vscode': 'VSCode', 'obsidian': 'Obsidian'}


def _pad(text: str, width: int) -> str:
    '''Pad or trim to an exact terminal cell width, honouring Braille/Unicode widths.'''
    while cell_len(text) > width:
        text = text[:-1]
    return text + ' ' * (width - cell_len(text))


def _fmt_schedule(expr: str, width: int = 14) -> str:
    '''Compact a systemd OnCalendar expression for display.

    `Sun *-*-* 03:00:00` -> `Sun 03:00`, `*-*-* 03:00:00` -> `daily 03:00`.
    The raw form overflows the status column and breaks the box border.
    '''
    raw = (expr or '').strip()
    if not raw:
        return ''
    toks = raw.split()
    time = ''
    if ':' in toks[-1]:
        time = ':'.join(toks[-1].split(':')[:2])
        toks = toks[:-1]
    day = ' '.join(t for t in toks if t != '*-*-*')
    label = f'{day or ("daily" if time else "")} {time}'.strip() or raw
    return label if len(label) <= width else label[: width - 1] + '…'


def _row(content: Text | str, style: str = '') -> None:
    '''Print one bordered banner row.'''
    row = Text('│ ', style=BORDER_STYLE)
    if isinstance(content, Text):
        row.append(content)
    else:
        row.append(_pad(content, INNER_W), style=style)
    row.append(' │', style=BORDER_STYLE)
    console.print(row)


def _centered(t: Text, width: int) -> Text:
    pad = max(0, width - cell_len(t.plain))
    left = pad // 2
    return Text(' ' * left) + t + Text(' ' * (pad - left))


def _subtitle(root_dir: Path) -> Text:
    frontend = _FRONTEND_DISPLAY.get(get_config_value(root_dir, 'frontend'), 'No frontend')
    path_str = str(root_dir).replace(str(Path.home()), '~')
    return (Text()
        .append(frontend, style='white')
        .append(' · ', style=ACCENT)
        .append(path_str, style='white')
    )


def _print_header(root_dir: Path, splash: str | None) -> None:
    '''Render the title border, splash one-liner, and frontend/path subtitle.

    Shared by the project and install banners so the two cannot drift.
    '''
    version, date_str = load_version_info(root_dir)
    title = f' Podarcis — The Research Agent v{version} ({date_str}) '
    dashes = max(4, (INNER_W + 2) - len(title))

    console.print(Text()
        .append('╭', style=BORDER_STYLE)
        .append('─' * (dashes // 2), style=ACCENT)
        .append(title, style=f'bold white on {ACCENT}')
        .append('─' * (dashes - dashes // 2), style=ACCENT)
        .append('╮', style=BORDER_STYLE)
    )

    splash_text = splash or random.choice(load_one_liners(root_dir))
    _row(f'★ {splash_text} ★'.center(INNER_W), style='italic dim')
    _row(_centered(_subtitle(root_dir), INNER_W))


def _status_rows(root_dir: Path) -> list[tuple]:
    '''Build the right-hand status rows: MCP tool budget, then scheduled jobs.'''
    from jobs import discover_jobs

    mcp_servers, _, _ = discover_components(root_dir)
    enabled_mcp = get_enabled_mcp_servers(root_dir)
    jobs = discover_jobs(root_dir)

    live = {k: v for k, v in mcp_servers.items() if k in enabled_mcp}
    total_tk = sum(v.get('tokens', 0) for v in live.values())

    # Tool modules are ordered by what they cost: the budget is the point.
    modules = sorted(mcp_servers, key=lambda k: -mcp_servers[k].get('tokens', 0))

    rows: list[tuple] = [
        ('header', f'MCP tools ({len(live)}/{len(mcp_servers)})', f'{total_tk:,} tk'),
        *[
            ('item', k.removesuffix('-mcp'), f'{mcp_servers[k].get("tokens", 0):,} tk',
             k in enabled_mcp)
            for k in modules
        ],
    ]

    if jobs:
        job_on = sum(1 for v in jobs.values() if v.get('enabled'))
        rows += [
            ('empty',),
            ('header', f'Jobs ({job_on}/{len(jobs)})', ''),
            *[
                ('item', j, _fmt_schedule(jobs[j].get('schedule', '')), jobs[j]['enabled'])
                for j in sorted(jobs)
            ],
        ]

    return rows


def _render_status(row: tuple, width: int = RIGHT_W) -> Text:
    '''Format one right-column row: label on the left, value right-aligned.'''
    kind = row[0]
    if kind == 'empty':
        return Text(' ' * width)

    label, value = row[1], str(row[2] or '')
    cell = Text()
    if kind == 'header':
        prefix, label_style = '', BORDER_STYLE
    else:
        prefix, label_style = ('● ' if row[3] else '○ '), 'bold white'
        cell.append(prefix, style='bold green' if row[3] else 'bold red')

    avail = width - len(prefix) - (len(value) + 2 if value else 0)
    label = label if len(label) <= avail else label[: max(avail - 1, 0)] + '…'
    cell.append(label, style=label_style)
    gap = width - len(prefix) - len(label) - len(value)
    cell.append(' ' * max(1, gap))
    cell.append(value, style='dim white')
    return cell

_REPO_STATE = {
    'synced':         ('synced',  'green'),
    'modified':       (None,      'yellow'),   # label carries the change count
    'ahead':          (None,      ACCENT),
    'behind':         (None,      'yellow'),
    'missing':        ('missing', 'bold red'),
    'gdrive_managed': ('gdrive',  'dim white'),
    'ready':          ('ready',   'dim white'),
}

NAME_W, BRANCH_W, STATE_W = 11, 9, 13


def _short_remote(url: str) -> str:
    '''Trim a git remote to the part that identifies it.'''
    if not url or url in ('local', 'gdrive'):
        return url or 'local'
    for prefix in ('git@github.com:', 'https://github.com/', 'git@', 'https://'):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    return url.removesuffix('.git')


def _repo_state(info: dict) -> tuple[str, str]:
    '''Human-readable tracking state and its style for one repository.'''
    status = info.get('status', 'ready')
    label, style = _REPO_STATE.get(status, ('ready', 'dim white'))
    if label is None:
        if status == 'modified':
            n = info.get('changes', 0)
            label = f'{n} change' + ('' if n == 1 else 's')
        else:
            label = f'{"↑" if status == "ahead" else "↓"}{info.get(status, 0)}'
    # A dirty tree can also be out of step with its upstream; say both.
    if status == 'modified':
        if info.get('ahead'): label += f' ↑{info["ahead"]}'
        if info.get('behind'): label += f' ↓{info["behind"]}'
    return label, style


def _repo_rows(root_dir: Path) -> list[Text]:
    '''Render one tracking row per configured repository.

    Costs a handful of local `git` calls per render via get_repo_status; that is
    the price of showing live state instead of the static configured URL.
    '''
    rows = []
    for info in get_repo_status(root_dir):
        label, style = _repo_state(info)
        rows.append(Text()
            .append('  ' + _pad(info['repo'], NAME_W), style=f'bold {ACCENT}')
            .append(_pad(info.get('branch') or '—', BRANCH_W), style='white')
            .append(_pad(label, STATE_W), style=style)
            .append(
                _pad(_short_remote(info.get('url', '')), INNER_W - 2 - NAME_W - BRANCH_W - STATE_W),
                style='dim white',
            )
        )
    return rows


def display_project_banner(root_dir: Path, splash: str | None = None) -> None:
    '''Render side-by-side logo and gateway/job status header box.'''
    logo_path = Path(__file__).resolve().parent/'logo.txt'
    logo_lines = ([
        l.replace('⠀', ' ').rstrip()
        for l in logo_path.read_text('utf-8').splitlines()
    ] if logo_path.exists() else [])

    _print_header(root_dir, splash)
    _row('')

    rows = _status_rows(root_dir)
    for logo_line, row in zip_longest(logo_lines, rows, fillvalue=None):
        _row(Text()
            .append(_pad(logo_line or '', LEFT_W), style=ACCENT)
            .append(' ' * COL_GAP)
            .append(_render_status(row or ('empty',)))
        )

    _row('')
    for repo_row in _repo_rows(root_dir):
        _row(repo_row)

    console.print(Text('╰' + '─' * (INNER_W + 2) + '╯', style=BORDER_STYLE))


def display_install_banner(root_dir: Path, splash: str | None = None) -> None:
    '''Render clean minimal installation header box.'''
    _print_header(root_dir, splash)
    console.print(Text('╰' + '─' * (INNER_W + 2) + '╯', style=BORDER_STYLE))
