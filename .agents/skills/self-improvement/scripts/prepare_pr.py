#!/usr/bin/env python3
'''Prepare and open a fortified Pull Request for platform self-improvement.

Enforces strict privacy and boundary validation:
- Verifies that NO workspace, sources, wiki, .env, or private domain files are touched.
- Redacts secrets, tokens, and absolute paths from the PR title and description.
- Stages only allowed platform files (.agents/, .podarcis/, AGENTS.md).
'''

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple, Optional

# Project root setup
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import sanitization and validation utilities
sys.path.insert(0, str(PROJECT_ROOT / '.agents' / 'mcp' / 'diagnostics'))
from sanitizer import sanitize_text, validate_pr_scope, FORBIDDEN_PR_PREFIXES, ALLOWED_PR_PREFIXES
from diagnose_session import clear_issues, get_active_issues


def get_git_status_files(base_dir: Optional[Path] = None) -> List[str]:
    '''Get list of all modified, untracked, and staged files relative to repo root.'''
    cwd = str(base_dir or PROJECT_ROOT)
    res = subprocess.run(
        ['git', 'status', '--porcelain'],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False
    )
    if res.returncode != 0:
        return []

    files = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: XY <file> or XY <file1> -> <file2>
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            path_part = parts[1]
            if ' -> ' in path_part:
                path_part = path_part.split(' -> ')[1]
            files.append(path_part.strip('"\''))
    return files


def prepare_pr(
    title: str,
    description: str,
    branch_name: Optional[str] = None,
    dry_run: bool = False,
    auto_clear: bool = True,
    base_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    '''Validate boundaries, sanitize metadata, and create a Pull Request branch.'''
    root = base_dir or PROJECT_ROOT
    changed_files = get_git_status_files(root)

    if not changed_files:
        return False, 'No modified files detected in workspace.'

    # 1. Strict Boundary Verification
    is_valid, violations = validate_pr_scope(changed_files)
    if not is_valid:
        violation_msg = '\n'.join(f' - {v}' for v in violations)
        return False, (
            f'PR Preparation ABORTED due to privacy/boundary violations:\n{violation_msg}\n'
            'Platform self-improvement PRs must NEVER touch domain folders (workspace/, wiki/, sources/) or .env files.'
        )

    # 2. Sanitize Title and Description
    sanitized_title = sanitize_text(title, root_dir=root)
    sanitized_desc = sanitize_text(description, root_dir=root)

    # 3. Formulate branch name if not provided
    if not branch_name:
        slug = re.sub(r'[^a-zA-Z0-9_\-]+', '-', sanitized_title.lower())[:30].strip('-')
        branch_name = f'fix/platform-{slug or "improvement"}'

    if dry_run:
        return True, (
            f'[DRY RUN] Scope valid.\n'
            f'Target Branch: {branch_name}\n'
            f'Title: {sanitized_title}\n'
            f'Files to stage: {changed_files}\n'
            f'Body:\n{sanitized_desc}'
        )

    # 4. Create git branch and commit platform changes
    cwd = str(root)
    try:
        # Check current branch
        branch_res = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=cwd, capture_output=True, text=True, check=True)
        orig_branch = branch_res.stdout.strip()

        # Checkout new branch
        subprocess.run(['git', 'checkout', '-b', branch_name], cwd=cwd, capture_output=True, text=True, check=True)

        # Stage only platform files
        for f in changed_files:
            norm = f.replace('\\', '/').lstrip('./')
            if any(norm.startswith(allowed) for allowed in ALLOWED_PR_PREFIXES):
                subprocess.run(['git', 'add', f], cwd=cwd, check=True)

        # Commit changes
        commit_msg = f'{sanitized_title}\n\n{sanitized_desc}'
        subprocess.run(['git', 'commit', '-m', commit_msg], cwd=cwd, capture_output=True, text=True, check=True)

        # 5. Attempt GitHub PR creation via gh CLI if available
        gh_available = subprocess.run(['which', 'gh'], capture_output=True, check=False).returncode == 0
        pr_url = ''
        if gh_available:
            pr_res = subprocess.run(
                ['gh', 'pr', 'create', '--title', sanitized_title, '--body', sanitized_desc, '--head', branch_name],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False
            )
            if pr_res.returncode == 0:
                pr_url = pr_res.stdout.strip()

        # 6. Clear resolved pain points
        if auto_clear:
            clear_issues(base_dir=root)

        msg = f'Successfully created branch [{branch_name}] and committed platform fix.'
        if pr_url:
            msg += f'\nPull Request opened: {pr_url}'
        else:
            msg += f'\nPush branch and open PR with:\n  git push origin {branch_name}\n  gh pr create --title "{sanitized_title}" --body "{sanitized_desc}"'

        return True, msg

    except subprocess.CalledProcessError as e:
        return False, f'Git execution failed: {e.stderr or str(e)}'
    except Exception as e:
        return False, f'Unexpected error during PR preparation: {str(e)}'


def main() -> int:
    parser = argparse.ArgumentParser(description='Prepare and open a fortified Pull Request for platform improvements.')
    parser.add_argument('--title', type=str, required=True, help='PR title (will be sanitized)')
    parser.add_argument('--body', type=str, default='Platform self-improvement fix for logged pain points.', help='PR body description')
    parser.add_argument('--branch', type=str, help='Custom branch name')
    parser.add_argument('--dry-run', action='store_true', help='Validate boundaries without modifying git state')
    args = parser.parse_args()

    success, msg = prepare_pr(
        title=args.title,
        description=args.body,
        branch_name=args.branch,
        dry_run=args.dry_run
    )

    if success:
        print(msg)
        return 0
    else:
        print(f'ERROR: {msg}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
