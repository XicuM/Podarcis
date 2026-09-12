"""Execution of ``type: agent`` jobs: personas run headlessly on a schedule.

Autonomy is the safety boundary, and it is enforced here rather than by the
model. Harness tool allow-lists are only a first line: the agent never gets
Bash, so it cannot touch git, and every commit or push below is performed by
Podarcis after inspecting the working tree it left behind.
"""

from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path

from podarcis.console import console
from podarcis.repos import get_repo_names, push_repos

from .runners import (
    RunResult, RunSpec, available_harnesses, get_runner,
)

# Read-only surface: native inspection plus the gateway's search/audit tools.
READ_TOOLS = (
    'Read', 'Grep', 'Glob', 'TodoWrite',
    'mcp__podarcis__wiki_search', 'mcp__podarcis__wiki_fetch',
    'mcp__podarcis__wiki_lint', 'mcp__podarcis__literature_search',
    'mcp__podarcis__literature_status', 'mcp__podarcis__diagnostics_list',
)

WRITE_TOOLS = (
    'Edit', 'Write', 'mcp__podarcis__wiki_publish',
    'mcp__podarcis__wiki_reindex', 'mcp__podarcis__literature_download',
    'mcp__podarcis__diagnostics_log',
)

# Bash would let the agent run git and bypass the autonomy ladder entirely.
# WebSearch/WebFetch are barred for research by the citation-hierarchy rules
# in AGENTS.md; denying them here makes that mechanical for unattended runs.
DENIED_TOOLS = ('Bash', 'WebSearch', 'WebFetch')

AUTONOMY_LEVELS = ('report', 'branch', 'commit', 'push')


def _tools_for(autonomy: str) -> tuple[tuple[str, ...], str]:
    """Allowed tools and permission mode for an autonomy level."""
    if autonomy == 'report':
        return READ_TOOLS, 'dontAsk'
    return READ_TOOLS + WRITE_TOOLS, 'acceptEdits'


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git', *args], cwd=repo, capture_output=True, text=True, check=False,
    )


def _target_repos(root_dir: Path) -> list[Path]:
    """Configured repositories that exist locally as git checkouts."""
    return [
        d for name in get_repo_names(root_dir)
        if (d := root_dir / name).is_dir() and (d / '.git').exists()
    ]


def _worktree_state(root_dir: Path) -> dict[str, str]:
    """Porcelain status per repo, for before/after comparison."""
    return {
        r.name: _git(r, 'status', '--porcelain').stdout.strip()
        for r in _target_repos(root_dir)
    }


def _dirty_repos(root_dir: Path) -> list[Path]:
    return [
        r for r in _target_repos(root_dir)
        if _git(r, 'status', '--porcelain').stdout.strip()
    ]


def _audit_gate(root_dir: Path) -> tuple[bool, str]:
    """Run the same link/frontmatter audit as `podarcis lint`."""
    check_links = root_dir / '.agents' / 'mcp' / 'wiki' / 'check_links.py'
    py_bin = root_dir / '.venv' / 'bin' / 'python'
    proc = subprocess.run(
        [str(py_bin), str(check_links), str(root_dir)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return False, (proc.stdout or proc.stderr).strip()[-2000:]
    return True, 'Audit passed.'


def _commit_all(repos: list[Path], message: str) -> list[str]:
    done = []
    for repo in repos:
        _git(repo, 'add', '-A')
        if _git(repo, 'commit', '-m', message).returncode == 0:
            done.append(repo.name)
    return done


def _write_report(root_dir: Path, job_name: str, result: RunResult) -> Path:
    stamp = datetime.datetime.now(datetime.timezone.utc)
    out_dir = root_dir / 'tmp' / 'job_reports' / job_name
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f'{stamp:%Y%m%dT%H%M%SZ}.md'
    cost = f'{result.cost_usd:.4f}' if result.cost_usd is not None else 'n/a'
    path.write_text(
        f'# {job_name} — {stamp:%Y-%m-%d %H:%M:%S} UTC\n\n'
        f'- session: `{result.session_id}`\n- cost: ${cost}\n\n'
        f'{result.text}\n',
        encoding='utf-8',
    )
    return path


def run(root_dir: Path, job: dict, dry_run: bool = False) -> dict:
    """Run one agent job under its declared autonomy level."""
    opts = job.get('options') or {}
    prompt = opts.get('prompt', '').strip()
    if not prompt:
        return {'status': 'error', 'message': 'Agent job defines no prompt.'}

    autonomy = opts.get('autonomy', 'report')
    if autonomy not in AUTONOMY_LEVELS:
        return {
            'status': 'error',
            'message': f'Unknown autonomy {autonomy!r}; expected one of '
                       f'{", ".join(AUTONOMY_LEVELS)}.',
        }

    harness = opts.get('harness')
    if not harness:
        installed = ', '.join(available_harnesses()) or 'none installed'
        return {
            'status': 'error',
            'message': f'Agent job declares no harness. Set options.harness '
                       f'in the job YAML (installed: {installed}).',
        }

    allowed, permission_mode = _tools_for(autonomy)
    spec = RunSpec(
        prompt=prompt,
        cwd=root_dir,
        persona=opts.get('persona'),
        model=opts.get('model'),
        effort=opts.get('effort'),
        permission_mode=permission_mode,
        allowed_tools=allowed,
        denied_tools=DENIED_TOOLS,
        timeout_s=int(opts.get('timeout_s', 1800)),
        max_cost_usd=opts.get('max_cost_usd'),
        env={'PODARCIS_JOB_RUN': '1'},
    )
    try:
        runner = get_runner(harness)
    except RuntimeError as exc:
        return {'status': 'error', 'message': str(exc)}

    if dry_run:
        console.print(f'[dim]{" ".join(runner.build_argv(spec))}[/dim]')
        return {'status': 'dry_run', 'argv': runner.build_argv(spec)}

    before = _worktree_state(root_dir)
    pre_dirty = sorted(name for name, st in before.items() if st)
    if pre_dirty and autonomy != 'report':
        return {
            'status': 'error',
            'message': f'Refusing to run: uncommitted changes in '
                       f'{", ".join(pre_dirty)}. Commit or stash first.',
        }

    console.print(
        f'[dim]→ {runner.name} as {spec.persona or "default"} '
        f'(autonomy: {autonomy})[/dim]'
    )
    result = runner.run(spec)
    if not result.ok:
        return {'status': result.status, 'message': result.text}

    denials = result.raw.get('permission_denials') or []
    if denials:
        console.print(
            f'[yellow]{len(denials)} tool call(s) denied by autonomy '
            f"'{autonomy}'.[/yellow]"
        )

    out = {
        'status': 'success',
        'cost_usd': result.cost_usd,
        'session_id': result.session_id,
        'denials': len(denials),
    }
    return out | _apply_autonomy(root_dir, job['name'], autonomy, result, before)


def _apply_autonomy(
    root_dir: Path, job_name: str, autonomy: str, result: RunResult,
    before: dict[str, str],
) -> dict:
    """Persist whatever the agent produced, per the autonomy ladder."""
    if autonomy == 'report':
        path = _write_report(root_dir, job_name, result)
        # Only changes the run itself introduced count; the tree may well have
        # been dirty beforehand, and that is not this job's doing.
        after = _worktree_state(root_dir)
        touched = sorted(n for n, st in after.items() if st != before.get(n, ''))
        if touched:
            return {
                'status': 'error',
                'report': str(path),
                'message': f'report-only job mutated {", ".join(touched)}.',
            }
        return {'report': str(path)}

    dirty = _dirty_repos(root_dir)
    if not dirty:
        return {'message': 'Agent made no repository changes.'}

    message = f'chore({job_name}): scheduled agent run\n\n{result.text[:500]}'

    if autonomy == 'branch':
        branch = f'jobs/{job_name}'
        for repo in dirty:
            original = _git(repo, 'branch', '--show-current').stdout.strip()
            exists = _git(repo, 'rev-parse', '--verify', branch).returncode == 0
            _git(repo, 'switch', *([] if exists else ['-c']), branch)
            _commit_all([repo], message)
            _git(repo, 'switch', original)
        return {'branch': branch, 'repos': [r.name for r in dirty]}

    ok, detail = _audit_gate(root_dir)
    if not ok:
        return {
            'status': 'error',
            'message': f'Audit gate failed; changes left uncommitted.\n{detail}',
        }

    committed = _commit_all(dirty, message)
    if autonomy == 'push':
        return {'committed': committed, 'push': push_repos(root_dir)}
    return {'committed': committed}
