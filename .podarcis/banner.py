'''Header banner and lore rendering for the TUI application.'''

# Standard library imports
import random
from itertools import zip_longest
from pathlib import Path

# Local imports
from common import load_one_liners, load_version_info, get_config_value
from components import discover_components, get_enabled_mcp_servers
from console import console
from repos import get_repo_names, get_repo_url
from rich.text import Text

BORDER_STYLE = 'bold #29b8db'


def _center_text(t: Text, width: int) -> Text:
    padding = max(0, width - len(t))
    left = padding // 2
    right = padding - left
    result = Text(' ' * left)
    result.append(t)
    result.append(' ' * right)
    return result


_FRONTEND_DISPLAY = {'vscode': 'VSCode', 'obsidian': 'Obsidian'}
_HARNESS_DISPLAY = {'opencode': 'OpenCode', 'codex': 'Codex', 'agy': 'Agy', 'claude': 'Claude', 'openclaw': 'OpenClaw', 'hermes': 'Hermes'}
_BACKEND_DISPLAY = _HARNESS_DISPLAY


def _build_subtitle(root_dir: Path) -> Text:
    harness_val = get_config_value(root_dir, 'harness', default=get_config_value(root_dir, 'backend'))
    harness = _HARNESS_DISPLAY.get(harness_val, 'No harness')
    frontend = _FRONTEND_DISPLAY.get(
        get_config_value(root_dir, 'frontend'), 'No frontend',
    )
    path_str = str(root_dir).replace(str(Path.home()), '~')
    return (Text()
        .append(harness, style='white')
        .append(' — ', style='cyan')
        .append(frontend, style='white')
        .append(' — ', style='cyan')
        .append(path_str, style='white')
    )


def format_cron_readable(schedule: str) -> str:
    '''Format cron 5-field expression into concise text for cell display.'''
    parts = schedule.strip().split()
    if len(parts) != 5:
        return schedule
    m, h, dom, mon, dow = parts

    # Daily at HH:MM
    if m.isdigit() and h.isdigit() and dom == '*' and mon == '*' and dow == '*':
        return f'{int(h):02d}:{int(m):02d} daily'

    # Weekly on Day at HH:MM
    days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    if m.isdigit() and h.isdigit() and dom == '*' and mon == '*' and dow.isdigit():
        d_idx = int(dow) % 7
        return f'{days[d_idx]} {int(h):02d}:{int(m):02d}'

    # Every N minutes
    if m.startswith('*/') and h == '*' and dom == '*' and mon == '*' and dow == '*':
        return f'every {m[2:]}m'

    # Hourly
    if m.isdigit() and h == '*' and dom == '*' and mon == '*' and dow == '*':
        return f':{int(m):02d} hourly'

    return schedule


def _render_right_cell(
    kind: str,
    title: str,
    extra: str | int | None,
    enabled: bool,
    width: int = 38,
    offset: int = 24,
    tok_w: int = 12,
) -> Text:
    '''Format right-hand MCP, skill, agent, or job item cell for side-by-side layout.'''
    cell = Text()
    if kind == 'header':
        if extra:
            max_title = width - 2 - tok_w
            if len(title) > max_title: title = title[:max(max_title - 3, 0)] + '...'
        cell.append(title, style=BORDER_STYLE)
        extra_str = str(extra) if extra else ''
        if extra_str:
            sp = max(2, offset - len(title))
            tok_formatted = extra_str.rjust(tok_w)
            cell.append(' ' * sp + tok_formatted, style='dim white')
            used = len(title) + sp + len(tok_formatted)
        else: used = len(title)
        cell.append(' ' * max(0, width - used))
    elif kind == 'item':
        dot_style = 'bold green' if enabled else 'bold red'
        if extra is not None:
            max_title = width - 2 - 2 - tok_w
            if len(title) > max_title: title = title[:max(max_title - 3, 0)] + '...'
        else:
            max_title = width - 2
            if len(title) > max_title: title = title[:max(max_title - 3, 0)] + '...'
        cell.append('● ' if enabled else '○ ', style=dot_style)
        cell.append(title, style='bold white')
        used = 2 + len(title)
        if extra is not None:
            tok_str = f'{extra:,} tk' if isinstance(extra, int) else str(extra)
            sp = max(2, offset - used)
            tok_formatted = tok_str.rjust(tok_w)
            cell.append(' ' * sp + tok_formatted, style='dim white')
            used += sp + len(tok_formatted)
        cell.append(' ' * max(0, width - used))
    else: cell.append(' ' * width)
    return cell


from rich.cells import cell_len


def _pad_cell(text: str, width: int) -> str:
    '''Pad text to exact terminal cell width taking Unicode/Braille display widths into account.'''
    w = cell_len(text)
    if w > width:
        while text and cell_len(text) > width:
            text = text[:-1]
        w = cell_len(text)
    return text + ' ' * max(0, width - w)


def display_project_banner(root_dir: Path, splash: str | None = None, right_w: int = 38) -> None:
    '''Render side-by-side logo and component status header box.'''
    logo_path = Path(__file__).resolve().parent/'logo.txt'
    logo_lines = ([
        l.replace('\u2800', ' ').rstrip()
        for l in logo_path.read_text('utf-8').splitlines()
    ] if logo_path.exists() else [])

    version, date_str = load_version_info(root_dir)
    mcp_servers, skills, agents = discover_components(root_dir)
    enabled_mcp = get_enabled_mcp_servers(root_dir)
    from jobs import discover_jobs
    jobs = discover_jobs(root_dir)

    left_w, col_gap = 26, 4
    total_inner_w = left_w + col_gap + right_w

    # Count active components and tokens for display
    mcp_active = sum(1 for m in mcp_servers if m in enabled_mcp)
    mcp_tokens = sum(v.get('tokens', 0) for m, v in mcp_servers.items() if m in enabled_mcp)
    skill_active = sum(1 for v in skills.values() if v.get('enabled'))
    skill_tokens = sum(v.get('tokens', 0) for v in skills.values() if v.get('enabled'))
    agent_active = sum(1 for v in agents.values() if v.get('enabled'))
    agent_tokens = sum(v.get('tokens', 0) for v in agents.values() if v.get('enabled'))
    job_active = sum(1 for v in jobs.values() if v.get('enabled'))

    mcp_hdr = f'MCP Servers ({mcp_active}/{len(mcp_servers)})'
    skill_hdr = f'Skills ({skill_active}/{len(skills)})'
    agent_hdr = f'Agents ({agent_active}/{len(agents)})'
    job_hdr = f'Jobs ({job_active}/{len(jobs)})'

    # Components for the right column
    right_items = [
        ('header', mcp_hdr, f'{mcp_tokens:,} tk', True),
        *[
            ('item', m, mcp_servers[m].get('tokens', 0), m in enabled_mcp)
            for m in sorted(mcp_servers)
        ],
        ('empty', '', '', True),
        ('header', skill_hdr, f'{skill_tokens:,} tk', True),
        *[
            ('item', s, skills[s].get('tokens', 0), skills[s]['enabled'])
            for s in sorted(skills)
        ],
        ('empty', '', '', True),
        ('header', agent_hdr, f'{agent_tokens:,} tk', True),
        *[
            ('item', a, agents[a].get('tokens', 0), agents[a]['enabled'])
            for a in sorted(agents)
        ],
        ('empty', '', '', True),
        ('header', job_hdr, None, True),
        *[
            ('item', j, format_cron_readable(jobs[j].get('schedule', '')), jobs[j]['enabled'])
            for j in sorted(jobs)
        ],
    ]

    title = f' Podarcis — The Research Agent v{version} ({date_str}) '
    dash_count = max(4, (total_inner_w + 2) - len(title))
    d_left, d_right = dash_count // 2, dash_count - (dash_count // 2)

    splash_text = splash or random.choice(load_one_liners(root_dir))
    formatted_splash = f'★ {splash_text} ★'
    if len(formatted_splash) > total_inner_w:
        formatted_splash = formatted_splash[: total_inner_w - 3] + '...'

    def print_row(content: Text | str, style: str = '') -> None:
        row = Text().append('│ ', style=BORDER_STYLE)
        if isinstance(content, Text):
            row.append(content)
        else:
            row.append(content, style=style)
        row.append(' │', style=BORDER_STYLE)
        console.print(row)

    # Top border & splash section
    console.print(Text()
        .append('╭', style=BORDER_STYLE)
        .append('─' * d_left, style='#29b8db')
        .append(title, style='bold white on #29b8db')
        .append('─' * d_right, style='#29b8db')
        .append('╮', style=BORDER_STYLE)
    )

    print_row(formatted_splash.center(total_inner_w), style='italic dim')

    subtitle = _build_subtitle(root_dir)
    if len(subtitle) > total_inner_w:
        subtitle = subtitle[: total_inner_w - 3]
        subtitle.append('...', style='dim white')

    print_row(_center_text(subtitle, total_inner_w))
    print_row(' ' * total_inner_w)

    # Side-by-side logo and component status rendering
    left_lines = [_pad_cell(line, left_w) for line in logo_lines]
    fill_item = ('empty', '', '', True)
    for left_line, item in zip_longest(left_lines, right_items, fillvalue=fill_item):
        left_str = left_line if isinstance(left_line, str) else ' ' * left_w
        item_tuple = item if isinstance(item, tuple) and len(item) == 4 else fill_item
        kind, item_title, extra, enabled = item_tuple

        console.print(Text()
            .append('│ ', style=BORDER_STYLE)
            .append(left_str, style='#29b8db')
            .append(' ' * col_gap)
            .append(_render_right_cell(kind, item_title, extra, enabled, right_w))
            .append(' │', style=BORDER_STYLE)
        )

    print_row(' ' * total_inner_w)

    # Repository links (2-column: cyan name column, dim white remote column)
    name_w = 12
    rem_w = total_inner_w - name_w - 2
    for r_name in get_repo_names(root_dir):
        url = get_repo_url(root_dir, r_name) or 'local'
        name_str = r_name[:name_w].ljust(name_w)
        disp_url = url if len(url) <= rem_w else url[: rem_w - 3] + '...'
        url_str = disp_url.ljust(rem_w)

        print_row(Text()
            .append(f'  {name_str}', style='bold #29b8db')
            .append(url_str, style='dim white')
        )

    # Bottom border
    console.print(Text('╰' + '─' * (total_inner_w + 2) + '╯', style=BORDER_STYLE))


def display_install_banner(root_dir: Path, splash: str | None = None) -> None:
    '''Render clean minimal installation header box.'''
    version, date_str = load_version_info(root_dir)
    total_inner_w = 68

    title = f' Podarcis — The Research Agent v{version} ({date_str}) '
    dash_count = max(4, (total_inner_w + 2) - len(title))
    d_left, d_right = dash_count // 2, dash_count - (dash_count // 2)

    splash_text = splash or random.choice(load_one_liners(root_dir))
    formatted_splash = f'★ {splash_text} ★'
    if len(formatted_splash) > total_inner_w:
        formatted_splash = formatted_splash[: total_inner_w - 3] + '...'

    def print_row(content: Text | str, style: str = '') -> None:
        row = Text('│ ', style=BORDER_STYLE)
        if isinstance(content, str): row.append(content, style=style)
        else: row.append(content)
        row.append(' │', style=BORDER_STYLE)
        console.print(row)

    # Top border & header
    top_border = Text('╭', style=BORDER_STYLE)
    top_border.append('─' * d_left, style='#29b8db')
    top_border.append(title, style='bold white on #29b8db')
    top_border.append('─' * d_right, style='#29b8db')
    top_border.append('╮', style=BORDER_STYLE)
    console.print(top_border)

    print_row(formatted_splash.center(total_inner_w), style='italic dim')

    subtitle = _build_subtitle(root_dir)
    if len(subtitle) > total_inner_w:
        subtitle = subtitle[: total_inner_w - 3]
        subtitle.append('...', style='dim white')
    print_row(_center_text(subtitle, total_inner_w))

    # Bottom border
    bot_border = Text('╰' + '─' * (total_inner_w + 2) + '╯', style=BORDER_STYLE)
    console.print(bot_border)
