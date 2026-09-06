"""Claude Code (``claude``) harness runner."""

from __future__ import annotations

import json

from . import HarnessRunner, RunResult, RunSpec


class ClaudeRunner(HarnessRunner):
    """Drive Claude Code through ``claude --print --output-format json``."""

    name = 'claude'
    binary = 'claude'

    def build_argv(self, spec: RunSpec) -> list[str]:
        # --permission-prompts none is what makes an unattended run safe: a
        # call that would prompt is denied outright instead of blocking the
        # systemd unit until RuntimeMaxSec kills it.
        argv = [
            self.binary, '--print', '--output-format', 'json',
            '--permission-prompts', 'none',
        ]
        if spec.persona:
            argv += ['--agent', spec.persona]
        if spec.model:
            argv += ['--model', spec.model]
        if spec.effort:
            argv += ['--effort', spec.effort]
        if spec.permission_mode:
            argv += ['--permission-mode', spec.permission_mode]
        if spec.allowed_tools:
            argv += ['--allowed-tools', *spec.allowed_tools]
        if spec.denied_tools:
            argv += ['--disallowed-tools', *spec.denied_tools]
        if spec.max_cost_usd is not None:
            argv += ['--max-budget-usd', str(spec.max_cost_usd)]
        return argv + [spec.prompt]

    def parse(self, stdout: str) -> RunResult:
        payload = json.loads(stdout)
        return RunResult(
            status='error' if payload.get('is_error') else 'success',
            text=payload.get('result', ''),
            cost_usd=payload.get('total_cost_usd'),
            session_id=payload.get('session_id'),
            exit_code=0,
            raw=payload,
        )
