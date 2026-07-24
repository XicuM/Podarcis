'''Header banner and lore rendering for the TUI application.'''

import random
from pathlib import Path
from tui.common import load_one_liners, load_version_info
from tui.components import discover_components, get_enabled_mcp_servers
from tui.console import HAS_RICH, console

if HAS_RICH:
    from rich.table import Table
    from rich.text import Text


def display_project_banner(root_dir: Path, splash: str | None = None) -> None:
    '''Render side-by-side logo and component status header box.'''
    logo_path = Path(__file__).resolve().parent/'logo.txt'
    logo_lines = [l.replace('\u2800', ' ').rstrip() for l in logo_path.read_text(encoding='utf-8').splitlines()] if logo_path.exists() else []

    version, date_str = load_version_info(root_dir)
    mcp_servers, skills = discover_components(root_dir)
    enabled_mcp = get_enabled_mcp_servers(root_dir)

    cwd = str(root_dir)
    left_w, right_w = 26, 42

    enabled_count_mcp = sum(1 for m in mcp_servers if m in enabled_mcp)
    enabled_mcp_tokens = sum(
        info.get('tokens', 0) for m, info in mcp_servers.items()
        if m in enabled_mcp
    )
    enabled_count_skills = sum(1 for s in skills if skills[s]['enabled'])
    # Token count in header title sums strictly enabled skills only
    enabled_skill_tokens = sum(skills[s].get('tokens', 0) for s in skills if skills[s]['enabled'])

    mcp_hdr = f'MCP Servers ({enabled_count_mcp}/{len(mcp_servers)})'
    mcp_extra = f'{enabled_mcp_tokens:,} tk'
    right_items = [('header', mcp_hdr, mcp_extra, True)]
    for m in sorted(mcp_servers):
        is_enabled = m in enabled_mcp
        tok_count = mcp_servers[m].get('tokens', 0)
        right_items.append(('item', m, tok_count, is_enabled))

    skills_hdr = f'Skills ({enabled_count_skills}/{len(skills)})'
    skills_extra = f'{enabled_skill_tokens:,} tk'
    right_items.extend([
        ('empty', '', '', True),
        ('header', skills_hdr, skills_extra, True)
    ])
    for s in sorted(skills):
        is_enabled = skills[s]['enabled']
        tok_count = skills[s].get('tokens', 0)
        right_items.append(('item', s, tok_count, is_enabled))

    left_lines = [line[:left_w].ljust(left_w) for line in logo_lines]
    max_rows = max(len(left_lines), len(right_items))
    while len(left_lines) < max_rows:
        left_lines.append(' ' * left_w)
    while len(right_items) < max_rows:
        right_items.append(('empty', '', '', True))

    col_gap = 4
    total_inner_w = left_w + col_gap + right_w
    title = f' Podarcis v{version} ({date_str}) '
    dash_count = max(4, (total_inner_w + 2) - len(title))
    d_left = dash_count // 2
    d_right = dash_count - d_left

    splash_text = splash or random.choice(load_one_liners(root_dir))
    formatted_splash = f'★ {splash_text} ★'
    if len(formatted_splash) > total_inner_w:
        formatted_splash = formatted_splash[:total_inner_w - 3] + '...'

    col2_offset = 20

    if HAS_RICH:
        top_border = Text()
        top_border.append('╭', style='bold #29b8db')
        top_border.append('─' * d_left, style='#29b8db')
        top_border.append(title, style='bold white on #29b8db')
        top_border.append('─' * d_right, style='#29b8db')
        top_border.append('╮', style='bold #29b8db')
        console.print(top_border)

        splash_row = Text()
        splash_row.append('│ ', style='bold #29b8db')
        splash_row.append(formatted_splash.center(total_inner_w), style='italic dim')
        splash_row.append(' │', style='bold #29b8db')
        console.print(splash_row)

        space_row = Text()
        space_row.append('│ ', style='bold #29b8db')
        space_row.append(' ' * total_inner_w)
        space_row.append(' │', style='bold #29b8db')
        console.print(space_row)

        for l_line, item in zip(left_lines, right_items):
            kind = item[0]
            row = Text()
            row.append('│ ', style='bold #29b8db')
            row.append(l_line[:left_w].ljust(left_w), style='#29b8db')
            row.append(' ' * col_gap)

            if kind == 'header':
                title_str, extra_str = item[1], item[2]
                row.append(title_str, style='bold #29b8db')
                if extra_str:
                    sp = max(1, col2_offset - len(title_str))
                    row.append(' ' * sp)
                    row.append(extra_str, style='dim white')
                    used = len(title_str) + sp + len(extra_str)
                else:
                    used = len(title_str)
                row.append(' ' * max(0, right_w - used))
            elif kind == 'item':
                name, tokens, enabled = item[1], item[2], item[3]
                dot = '● ' if enabled else '○ '
                dot_style = 'bold green' if enabled else 'bold red'
                row.append(dot, style=dot_style)
                row.append(name, style='bold white')
                if tokens is not None:
                    tok_str = f'{tokens:,} tk'
                    sp = max(1, col2_offset - (2 + len(name)))
                    row.append(' ' * sp)
                    row.append(tok_str, style='dim white')
                    used = 2 + len(name) + sp + len(tok_str)
                else:
                    used = 2 + len(name)
                row.append(' ' * max(0, right_w - used))
            else:
                row.append(' ' * right_w)

            row.append(' │', style='bold #29b8db')
            console.print(row)

        space_row_bot = Text()
        space_row_bot.append('│ ', style='bold #29b8db')
        space_row_bot.append(' ' * total_inner_w)
        space_row_bot.append(' │', style='bold #29b8db')
        console.print(space_row_bot)

        bot_row = Text()
        bot_row.append('│ ', style='bold #29b8db')
        bot_row.append(cwd.center(total_inner_w), style='dim white')
        bot_row.append(' │', style='bold #29b8db')
        console.print(bot_row)

        bot_border = Text()
        bot_border.append('╰', style='bold #29b8db')
        bot_border.append('─' * (total_inner_w + 2), style='#29b8db')
        bot_border.append('╯', style='bold #29b8db')
        console.print(bot_border)
    else:
        top_border = f'╭{"─" * d_left}{title}{"─" * d_right}╮'
        bot_border = f'╰{"─" * (total_inner_w + 2)}╯'

        print(top_border)
        print(f'│ {formatted_splash:^{total_inner_w}} │')
        print(f'│ {" " * total_inner_w} │')
        for l_line, item in zip(left_lines, right_items):
            kind = item[0]
            if kind == 'header':
                title_str, extra_str = item[1], item[2]
                if extra_str:
                    sp = max(1, col2_offset - len(title_str))
                    r_str = f"{title_str}{' ' * sp}{extra_str}".ljust(right_w)
                else:
                    r_str = title_str.ljust(right_w)
            elif kind == 'item':
                name, tokens, enabled = item[1], item[2], item[3]
                dot = '● ' if enabled else '○ '
                if tokens is not None:
                    tok_str = f'{tokens:,} tk'
                    sp = max(1, col2_offset - (2 + len(name)))
                    r_str = f"{dot}{name}{' ' * sp}{tok_str}".ljust(right_w)
                else:
                    r_str = f'{dot}{name}'.ljust(right_w)
            else:
                r_str = ' ' * right_w
            print(f'│ {l_line[:left_w].ljust(left_w)}{" " * col_gap}{r_str:<{right_w}} │')

        print(f'│ {" " * total_inner_w} │')
        print(f'│ {cwd:^{total_inner_w}} │')
        print(bot_border)
