"""Harness-agnostic runner contract for non-interactive coding-agent CLIs.

This subpackage is deliberately free of Podarcis imports: it depends only on
the standard library so it can be lifted out into its own distribution the
moment a second consumer appears. Anything Podarcis-specific (personas,
autonomy, git handling) belongs in ``jobs.agent``, not here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

__all__ = [
    'RunSpec', 'RunResult', 'HarnessRunner', 'RUNNERS', 'get_runner',
    'available_harnesses',
]


@dataclass(frozen=True)
class RunSpec:
    """One unattended agent invocation, expressed independently of harness."""

    prompt: str
    cwd: Path
    persona: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    timeout_s: int = 1800
    max_cost_usd: float | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    """Normalised outcome of a harness invocation."""

    status: str
    text: str = ''
    cost_usd: float | None = None
    session_id: str | None = None
    exit_code: int | None = None
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 'success'


class HarnessRunner(ABC):
    """Invoke one coding-agent CLI non-interactively and normalise its result.

    Subclasses supply only ``build_argv`` and ``parse``; process handling,
    timeouts and failure mapping are shared here so every harness behaves
    identically to the scheduler above it.
    """

    name: ClassVar[str]
    binary: ClassVar[str]

    @classmethod
    def available(cls) -> bool:
        """Whether this harness's CLI is installed on the current machine."""
        return shutil.which(cls.binary) is not None

    @abstractmethod
    def build_argv(self, spec: RunSpec) -> list[str]:
        """Translate a RunSpec into this harness's command line."""

    @abstractmethod
    def parse(self, stdout: str) -> RunResult:
        """Translate successful harness stdout into a RunResult."""

    def run(self, spec: RunSpec) -> RunResult:
        """Execute the harness, honouring the spec's timeout."""
        argv = self.build_argv(spec)
        try:
            proc = subprocess.run(
                argv, cwd=spec.cwd, capture_output=True, text=True,
                timeout=spec.timeout_s, env=os.environ | spec.env,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                status='timeout',
                text=f'{self.name} exceeded {spec.timeout_s}s',
            )

        if proc.returncode != 0:
            return RunResult(
                status='error',
                text=(proc.stderr or proc.stdout).strip(),
                exit_code=proc.returncode,
            )
        return self.parse(proc.stdout)


def _load_runners() -> dict[str, type[HarnessRunner]]:
    """Import the shipped runner modules and index them by harness name."""
    from . import claude

    return {cls.name: cls for cls in (claude.ClaudeRunner,)}


RUNNERS: dict[str, type[HarnessRunner]] = _load_runners()


def get_runner(name: str) -> HarnessRunner:
    """Instantiate a runner by harness name, or explain why it cannot run."""
    if name not in RUNNERS:
        known = ', '.join(sorted(RUNNERS))
        raise RuntimeError(f'Unknown harness {name!r}. Available: {known}')
    cls = RUNNERS[name]
    if not cls.available():
        raise RuntimeError(
            f'Harness {name!r} selected but {cls.binary!r} is not on PATH.'
        )
    return cls()


def available_harnesses() -> list[str]:
    """Names of registered harnesses whose CLI is installed."""
    return sorted(n for n, c in RUNNERS.items() if c.available())
