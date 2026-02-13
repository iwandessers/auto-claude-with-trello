"""Module-level agent runner.

The function must live at module level so that
``concurrent.futures.ProcessPoolExecutor`` can pickle it.
"""

import subprocess
from typing import Any, Dict


def run_agent_in_worktree(worktree_path: str, prompt: str,
                          timeout_seconds: int = 900) -> Dict[str, Any]:
    """Run a Claude Code agent inside *worktree_path*.

    Returns a dict with ``success``, ``output``, and ``error`` keys.
    """
    try:
        cmd = [
            'claude', '--dangerously-skip-permissions',
            '-p', prompt,
            '--allowedTools', 'Bash', 'Read', 'Write', 'Edit', 'MultiEdit',
        ]
        result = subprocess.run(
            cmd,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        # commit any changes the agent made
        subprocess.run(['git', 'add', '-A'], cwd=worktree_path,
                       capture_output=True)
        subprocess.run(
            ['git', 'commit', '-m', 'Agent work completed'],
            cwd=worktree_path, capture_output=True, text=True,
        )
        output = result.stdout
        if result.stderr.strip():
            output += f"\n---STDERR---\n{result.stderr}"
        return {'success': result.returncode == 0, 'output': output,
                'error': None}
    except subprocess.TimeoutExpired:
        return {'success': False, 'output': '',
                'error': f'Agent timed out after {timeout_seconds}s'}
    except Exception as exc:
        return {'success': False, 'output': '', 'error': str(exc)}
