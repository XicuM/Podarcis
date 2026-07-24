'''Discovery, inspection, and state management for MCP servers and Skills.'''

import subprocess
from pathlib import Path
from tui.common import get_venv_pip, load_json, save_json
from tui.console import HAS_RICH, console

SERVER_NAME_MAP = {
    'finance': 'finance-mcp',
    'wiki': 'wiki-mcp',
    'research': 'research-mcp',
    'gdrive': 'google-drive-mcp',
    'menumaker': 'menumaker'
}


def get_skill_desc(root_dir: Path, name: str) -> str:
    '''Parse description field from skill SKILL.md YAML frontmatter.'''
    skill_file = root_dir / '.agents' / 'skills' / name / 'SKILL.md'
    if skill_file.exists():
        try:
            content = skill_file.read_text(encoding='utf-8')
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if line.strip().lower().startswith('description:'):
                            return line.split(':', 1)[1].strip()
        except Exception:
            pass
    return 'Skill module'


def get_mcp_desc(root_dir: Path, key: str, dir_name: str) -> str:
    '''Extract summary docstring from MCP server entrypoint module.'''
    server_py = root_dir / '.agents' / 'mcp' / dir_name / 'server.py'
    if server_py.exists():
        try:
            content = server_py.read_text(encoding='utf-8')
            import re
            m = re.match(r'^\s*(?:"""|"\'")(.*?)(?:"""|"\'")', content, re.DOTALL)
            if m:
                first_line = m.group(1).strip().splitlines()[0].strip()
                if '—' in first_line:
                    return first_line.split('—', 1)[1].strip()
                if '-' in first_line:
                    return first_line.split('-', 1)[1].strip()
                return first_line
        except Exception:
            pass
    return 'MCP server'


def install_deps(root: Path, target: str, is_req: bool, message: str) -> None:
    '''Quietly install python dependencies via virtual environment pip.'''
    pip_cmd = get_venv_pip(root)
    cmd = pip_cmd + ['install', '-r', target] if is_req else pip_cmd + ['install', target]
    if HAS_RICH:
        with console.status(f'[#29b8db]{message}[/#29b8db]', spinner='dots'):
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        print(f'{message}...', end='', flush=True)
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        print(' Done!')


def is_skill_enabled(skill_path: Path) -> bool:
    '''Check if skill is active based on SKILL.md frontmatter flags.'''
    skill_file = skill_path / 'SKILL.md'
    if not skill_file.exists():
        return False
    content = skill_file.read_text(encoding='utf-8')
    if not content.startswith('---'):
        return True
    parts = content.split('---', 2)
    if len(parts) < 3:
        return True
    for line in parts[1].splitlines():
        line_clean = line.strip().lower()
        if 'disable-model-invocation:' in line_clean and 'true' in line_clean:
            return False
        if 'user-invocable:' in line_clean and 'false' in line_clean:
            return False
    return True


def count_tokens(text: str) -> int:
    '''Calculate token count using fast character heuristic (~4 chars/tok).'''
    if not text:
        return 0
    return max(1, round(len(text) / 4.0))


def count_mcp_tokens(mcp_dir: Path) -> int:
    '''Estimate tool schema token size for an MCP server entrypoint.'''
    server_py = mcp_dir / 'server.py'
    if not server_py.exists():
        return 0
    try:
        content = server_py.read_text(encoding='utf-8')
        import re
        pieces = []
        fast_tools = re.findall(r'@(?:mcp|app)\.tool\(.*?\)\s*(?:async\s+)?def\s+([a-zA-Z0-9_]+)\((.*?)\):', content, re.DOTALL)
        for tname, args in fast_tools:
            pieces.append(f'tool: {tname} ({args.strip()})')

        std_tools = re.findall(r'name=[\"\']([a-zA-Z0-9_]+)[\"\']', content)
        for tname in std_tools:
            pieces.append(f'tool: {tname}')

        docstrings = re.findall(r'\"\"\"(.*?)\"\"\"', content, re.DOTALL) + re.findall(r'\'\'\'(.*?)\'\'\'', content, re.DOTALL)
        for doc in docstrings:
            pieces.append(doc.strip())

        if pieces:
            return count_tokens('\n'.join(pieces))
        return max(100, round(count_tokens(content) * 0.35))
    except Exception:
        return 500


def load_token_cache(root: Path) -> dict:
    '''Load cached token counts from persistent cache file.'''
    cache_file = root / '.agents' / 'token_cache.json'
    return load_json(cache_file)


def save_token_cache(root: Path, cache: dict) -> None:
    '''Save cached token counts to persistent cache file.'''
    


def discover_components(root: Path) -> tuple[dict, dict]:
    '''Scan filesystem to discover registered MCP servers and skills, using persistent mtime token cache.'''
    mcp_dir = root / '.agents' / 'mcp'
    skills_dir = root / '.agents' / 'skills'

    token_cache = load_token_cache(root)
    cache_modified = False

    mcp_servers = {}
    if mcp_dir.exists():
        for d in mcp_dir.iterdir():
            if d.is_dir():
                key = SERVER_NAME_MAP.get(d.name, d.name)
                req_file = d / 'requirements.txt'
                server_py = d / 'server.py'
                mtime = server_py.stat().st_mtime if server_py.exists() else 0
                cache_key = f'mcp:{d.name}'

                cached = token_cache.get(cache_key)
                if cached and cached.get('mtime') == mtime:
                    tok_count = cached['tokens']
                else:
                    tok_count = count_mcp_tokens(d)
                    token_cache[cache_key] = {'mtime': mtime, 'tokens': tok_count}
                    cache_modified = True

                mcp_servers[key] = {
                    'dir_name': d.name,
                    'path': d,
                    'req': req_file if req_file.exists() else None,
                    'type': 'mcp',
                    'tokens': tok_count
                }

    skills = {}
    if skills_dir.exists():
        for d in skills_dir.iterdir():
            if d.is_dir():
                req_file = d / 'requirements.txt'
                skill_file = d / 'SKILL.md'
                content = skill_file.read_text(encoding='utf-8') if skill_file.exists() else ''
                desc = get_skill_desc(root, d.name)
                decl_text = f'- {d.name} ({skill_file}): {desc}'
                mtime = skill_file.stat().st_mtime if skill_file.exists() else 0
                cache_key = f'skill:{d.name}'

                cached = token_cache.get(cache_key)
                if cached and cached.get('mtime') == mtime:
                    tok_count = cached['tokens']
                    decl_tok = cached.get('decl_tokens', count_tokens(decl_text))
                else:
                    tok_count = count_tokens(content)
                    decl_tok = count_tokens(decl_text)
                    token_cache[cache_key] = {'mtime': mtime, 'tokens': tok_count, 'decl_tokens': decl_tok}
                    cache_modified = True

                skills[d.name] = {
                    'dir_name': d.name,
                    'path': d,
                    'req': req_file if req_file.exists() else None,
                    'type': 'skill',
                    'enabled': is_skill_enabled(d),
                    'tokens': tok_count,
                    'decl_tokens': decl_tok,
                    'chars': len(content),
                    'words': len(content.split())
                }

    if cache_modified: save_json(root/'.agents'/'token_cache.json', token_cache)

    return mcp_servers, skills


def get_enabled_mcp_servers(root: Path) -> set[str]:
    '''Retrieve set of active MCP server identifiers from configuration files.'''
    opencode_path = root / 'opencode.json'
    enabled = set()
    data = load_json(opencode_path)
    mcp_data = data.get('mcp', {})
    for key, cfg in mcp_data.items():
        if cfg.get('enabled', True):
            enabled.add(key)
            for dir_name, mapped in SERVER_NAME_MAP.items():
                if mapped == key:
                    enabled.add(dir_name)
                elif dir_name == key:
                    enabled.add(mapped)

    mcp_cfg_path = root / '.agents' / 'mcp_config.json'
    if mcp_cfg_path.exists():
        mcp_cfg_data = load_json(mcp_cfg_path)
        mcp_servers = mcp_cfg_data.get('mcpServers', {})
        for key, cfg in mcp_servers.items():
            if cfg.get('disabled', False):
                enabled.discard(key)
                for dir_name, mapped in SERVER_NAME_MAP.items():
                    if mapped == key:
                        enabled.discard(dir_name)
                    elif dir_name == key:
                        enabled.discard(mapped)
    return enabled


def set_mcp_server_status(root: Path, server_key: str, enable: bool, mcp_info: dict) -> None:
    '''Persist enabled state for specified MCP server across config files.'''
    opencode_path = root / 'opencode.json'
    opencode_data = load_json(opencode_path)
    if 'mcp' not in opencode_data:
        opencode_data['mcp'] = {}

    dir_name = mcp_info['dir_name']
    server_script = f'.agents/mcp/{dir_name}/server.py'

    for k in (server_key, dir_name):
        if k in opencode_data['mcp']:
            opencode_data['mcp'][k]['enabled'] = enable

    if server_key not in opencode_data['mcp'] and dir_name not in opencode_data['mcp']:
        opencode_data['mcp'][server_key] = {
            'type': 'local',
            'command': ['.venv/bin/python', server_script],
            'environment': {},
            'enabled': enable
        }
    save_json(opencode_path, opencode_data)

    mcp_cfg_path = root / '.agents' / 'mcp_config.json'
    if mcp_cfg_path.exists():
        mcp_cfg_data = load_json(mcp_cfg_path)
        if 'mcpServers' in mcp_cfg_data:
            for k in (server_key, dir_name):
                if k in mcp_cfg_data['mcpServers']:
                    if not enable:
                        mcp_cfg_data['mcpServers'][k]['disabled'] = True
                    else:
                        mcp_cfg_data['mcpServers'][k].pop('disabled', None)
                elif enable and k == server_key:
                    mcp_cfg_data['mcpServers'][server_key] = {
                        'command': '.venv/bin/python',
                        'args': [server_script],
                        'env': {'PROJECT_ROOT': '.'}
                    }
            save_json(mcp_cfg_path, mcp_cfg_data)

    if enable and mcp_info.get('req'):
        install_deps(root, str(mcp_info['req']), True, f'Installing deps for {server_key}...')
        console.print(f'[green]✓ Dependencies installed for {server_key}.[/green]')


def set_skill_status(root: Path, skill_name: str, enable: bool, skill_info: dict) -> None:
    '''Update SKILL.md frontmatter flags to enable or disable model invocation.'''
    skill_file = skill_info['path'] / 'SKILL.md'
    if not skill_file.exists():
        return

    content = skill_file.read_text(encoding='utf-8')
    if not content.startswith('---'):
        if not enable:
            content = f'---\ndisable-model-invocation: true\nuser-invocable: false\ndisabled: true\n---\n\n{content}'
    else:
        parts = content.split('---', 2)
        if len(parts) >= 3:
            fm_lines = parts[1].strip().splitlines()
            new_fm = [
                line for line in fm_lines
                if (line.split(':')[0].strip() if ':' in line else line.strip()) not in (
                    'disable-model-invocation', 'user-invocable', 'disabled'
                )
            ]
            if not enable:
                new_fm.extend(['disable-model-invocation: true', 'user-invocable: false', 'disabled: true'])
            body = parts[2].lstrip('\r\n')
            content = f'---\n{"\n".join(new_fm)}\n---\n\n{body}'

    skill_file.write_text(content, encoding='utf-8')
    if enable and skill_info.get('req'):
        install_deps(root, str(skill_info['req']), True, f'Installing dependencies for skill {skill_name}...')
