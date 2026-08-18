"""Sanitization and redaction utilities for diagnostic logs and PRs.

Ensures no private user data, absolute filesystem paths, tokens, or credentials
leak into .podarcis/diagnostics/ or remote pull requests.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, List, Set

# Regex patterns for credential and secret detection
RE_PATTERNS = [
    # OpenAI / Anthropic / Generic API keys
    (re.compile(r"sk-[a-zA-Z0-9_\-]{20,}", re.IGNORECASE), "[REDACTED_API_KEY]"),
    # GitHub Tokens (classic and fine-grained)
    (re.compile(r"(ghp_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{20,})", re.IGNORECASE), "[REDACTED_GH_TOKEN]"),
    # Bearer tokens
    (re.compile(r"(Bearer\s+)[a-zA-Z0-9_\-\.]{20,}", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    # Key-value secret assignments (e.g., password=foo, api_key=bar, token=baz)
    (
        re.compile(
            r"((?:password|passwd|secret|token|api[_-]?key|auth[_-]?key|access[_-]?key)\s*[:=]\s*['\"]?)([^'\"\s\n&]{6,})(['\"]?)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]\3",
    ),
    # Email addresses
    (re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", re.IGNORECASE), "[REDACTED_EMAIL]"),
    # User home paths: /home/<user>/... or /Users/<user>/...
    (re.compile(r"/(?:home|Users)/[a-zA-Z0-9_\-\.]+(/)?", re.IGNORECASE), r"<HOME>\1"),
]

# Sensitive directories that MUST NEVER be included in platform PRs
FORBIDDEN_PR_PREFIXES = (
    "workspace/",
    "sources/",
    "wiki/",
    ".env",
    "tmp/",
    ".podarcis/diagnostics/",
)

# Allowed platform prefixes for pull requests
ALLOWED_PR_PREFIXES = (
    ".agents/",
    ".podarcis/",
    "AGENTS.md",
    "pyproject.toml",
    "pytest.ini",
    "README.md",
)


def sanitize_text(text: str, root_dir: Optional[Path] = None) -> str:
    """Redact secrets, tokens, emails, and sensitive paths from text."""
    if not text:
        return ""

    sanitized = text

    # If root_dir is provided, replace absolute project paths with <PROJECT_ROOT>
    if root_dir:
        root_str = str(root_dir.resolve())
        sanitized = sanitized.replace(root_str, "<PROJECT_ROOT>")

    for pattern, replacement in RE_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)

    return sanitized


def validate_pr_scope(changed_files: List[str]) -> tuple[bool, List[str]]:
    """Verify that only platform files are touched in a pull request.
    
    Returns (is_valid, list_of_violations).
    """
    violations: List[str] = []
    
    for f in changed_files:
        norm_path = f.replace("\\", "/").lstrip("./")
        
        # Check explicit forbidden prefixes
        for forbidden in FORBIDDEN_PR_PREFIXES:
            if norm_path.startswith(forbidden):
                violations.append(f"Forbidden domain/private path modified: {norm_path}")
                break
        else:
            # Check if within allowed platform prefixes
            if not any(norm_path.startswith(allowed) for allowed in ALLOWED_PR_PREFIXES):
                violations.append(f"Out-of-scope platform path modified: {norm_path}")

    return len(violations) == 0, violations
