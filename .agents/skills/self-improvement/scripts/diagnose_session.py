#!/usr/bin/env python3
'''Diagnose session pain points and manage platform issue logs in .podarcis/diagnostics/.'''

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any

# Ensure project root is in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DIAGNOSTICS_DIR = PROJECT_ROOT / '.podarcis' / 'diagnostics'
PAIN_POINTS_FILE = DIAGNOSTICS_DIR / 'pain_points.jsonl'
SESSIONS_DIR = DIAGNOSTICS_DIR / 'sessions'


# Import sanitizer
sys.path.insert(0, str(PROJECT_ROOT / '.agents' / 'mcp' / 'diagnostics'))
from sanitizer import sanitize_text

def ensure_diagnostics_dirs(base_dir: Optional[Path] = None) -> Path:
    '''Ensure .podarcis/diagnostics directory structure exists.'''
    diag_dir = (base_dir / '.podarcis' / 'diagnostics') if base_dir else DIAGNOSTICS_DIR
    sess_dir = diag_dir / 'sessions'
    diag_dir.mkdir(parents=True, exist_ok=True)
    sess_dir.mkdir(parents=True, exist_ok=True)
    return diag_dir


def parse_transcript(transcript_path: Path, base_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    '''Parse a transcript JSONL file and extract sanitized pain points and errors.'''
    pain_points: List[Dict[str, Any]] = []
    if not transcript_path.exists():
        return pain_points

    root = base_dir or PROJECT_ROOT
    step_index = 0
    try:
        with open(transcript_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                step_index += 1
                step_type = data.get('type', '')
                status = data.get('status', '')
                content = str(data.get('content', ''))
                source = data.get('source', '')

                # 1. Detect explicit error status or command/tool execution failures
                if status == 'ERROR' or 'The command failed with exit code' in content or 'Traceback (most recent call last)' in content:
                    first_line = content.split('\n')[0] if content else 'Unknown error'
                    sanitized_summary = sanitize_text(first_line[:120], root_dir=root)
                    sanitized_details = sanitize_text(content[:500], root_dir=root)
                    pain_points.append({
                        'id': f'err-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}-{step_index}',
                        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        'category': 'command_failure' if 'command failed' in content else 'execution_error',
                        'summary': sanitized_summary,
                        'details': sanitized_details,
                        'severity': 'high' if status == 'ERROR' else 'medium',
                        'resolved': False
                    })

                # 2. Detect user corrections / negative feedback keywords
                elif source == 'USER_EXPLICIT' or step_type == 'USER_INPUT':
                    lowered = content.lower()
                    correction_keywords = ['no,', 'wrong', 'fail', 'broken', 'error', 'don\'t', 'instead of', 'incorrect']
                    if any(kw in lowered for kw in correction_keywords):
                        sanitized_summary = sanitize_text(f'User correction: {content[:100]}', root_dir=root)
                        sanitized_details = sanitize_text(content[:400], root_dir=root)
                        pain_points.append({
                            'id': f'corr-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}-{step_index}',
                            'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            'category': 'user_correction',
                            'summary': sanitized_summary,
                            'details': sanitized_details,
                            'severity': 'medium',
                            'resolved': False
                        })
    except Exception as e:
        pain_points.append({
            'id': f'parser-err-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}',
            'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'category': 'parser_error',
            'summary': f'Failed to parse transcript: {str(e)}',
            'details': sanitize_text(str(e), root_dir=root),
            'severity': 'low',
            'resolved': False
        })

    return pain_points


def log_pain_points(points: List[Dict[str, Any]], base_dir: Optional[Path] = None) -> Path:
    '''Append pain points to .podarcis/diagnostics/pain_points.jsonl.'''
    diag_dir = ensure_diagnostics_dirs(base_dir)
    log_file = diag_dir / 'pain_points.jsonl'
    
    with open(log_file, 'a', encoding='utf-8') as f:
        for point in points:
            f.write(json.dumps(point) + '\n')
            
    return log_file


def get_active_issues(base_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    '''Read all unresolved pain points from .podarcis/diagnostics/pain_points.jsonl.'''
    diag_dir = (base_dir / '.podarcis' / 'diagnostics') if base_dir else DIAGNOSTICS_DIR
    log_file = diag_dir / 'pain_points.jsonl'
    
    if not log_file.exists():
        return []

    issues: List[Dict[str, Any]] = []
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if not item.get('resolved', False):
                    issues.append(item)
            except json.JSONDecodeError:
                continue
    return issues


def clear_issues(base_dir: Optional[Path] = None) -> int:
    '''Mark all issues as resolved in pain_points.jsonl.'''
    diag_dir = (base_dir / '.podarcis' / 'diagnostics') if base_dir else DIAGNOSTICS_DIR
    log_file = diag_dir / 'pain_points.jsonl'
    
    if not log_file.exists():
        return 0

    count = 0
    updated_lines = []
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if not item.get('resolved', False):
                    item['resolved'] = True
                    count += 1
                updated_lines.append(json.dumps(item))
            except json.JSONDecodeError:
                continue

    with open(log_file, 'w', encoding='utf-8') as f:
        for l in updated_lines:
            f.write(l + '\n')

    return count


def resolve_issue_by_id(issue_id: str, base_dir: Optional[Path] = None) -> bool:
    '''Mark a specific issue as resolved by its ID.'''
    diag_dir = (base_dir / '.podarcis' / 'diagnostics') if base_dir else DIAGNOSTICS_DIR
    log_file = diag_dir / 'pain_points.jsonl'

    if not log_file.exists():
        return False

    found = False
    updated_lines = []
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if item.get('id') == issue_id and not item.get('resolved', False):
                    item['resolved'] = True
                    found = True
                updated_lines.append(json.dumps(item))
            except json.JSONDecodeError:
                continue

    if found:
        with open(log_file, 'w', encoding='utf-8') as f:
            for l in updated_lines:
                f.write(l + '\n')

    return found



def main() -> int:
    parser = argparse.ArgumentParser(description='Diagnose session pain points and log to .podarcis/diagnostics.')
    parser.add_argument('--transcript', type=str, help='Path to transcript JSONL file to analyze')
    parser.add_argument('--list', action='store_true', help='List active platform issues')
    parser.add_argument('--clear', action='store_true', help='Clear or resolve current issues')
    parser.add_argument('--json', action='store_true', help='Output issues in JSON format')
    args = parser.parse_args()

    ensure_diagnostics_dirs()

    if args.clear:
        cleared = clear_issues()
        print(f'Cleared {cleared} issue(s).')
        return 0

    if args.transcript:
        t_path = Path(args.transcript)
        points = parse_transcript(t_path)
        log_pain_points(points)
        print(f'Parsed {t_path.name} and logged {len(points)} pain point(s).')

    issues = get_active_issues()
    if args.json:
        print(json.dumps(issues, indent=2))
    else:
        if not issues:
            print('No active platform pain points logged in .podarcis/diagnostics/')
        else:
            print(f'Current Platform Issues ({len(issues)} active):')
            for idx, issue in enumerate(issues, 1):
                cat = issue.get('category', 'issue')
                sev = issue.get('severity', 'medium')
                summ = issue.get('summary', '')
                ts = issue.get('timestamp', '')
                print(f'{idx}. [{sev.upper()}] [{cat}] {summ} ({ts})')

    return 0


if __name__ == '__main__':
    sys.exit(main())
