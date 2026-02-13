"""Git operations helper for the orchestrator (worktree / branch management)."""

import os
import subprocess
from typing import List, Optional


class GitHelper:
    """Git operations helper for the orchestrator."""

    def __init__(self, repo_path: str, worktree_base: str,
                 debug: bool = False):
        self.repo_path = repo_path
        self.worktree_base = worktree_base
        self.debug = debug
        os.makedirs(worktree_base, exist_ok=True)

    def _run(self, cmd: List[str], cwd: Optional[str] = None,
             check: bool = True,
             timeout: int = 120) -> subprocess.CompletedProcess:
        cwd = cwd or self.repo_path
        if self.debug:
            print(f"[ORCH-GIT] {' '.join(cmd)}  (cwd={cwd})")
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"git command failed: {' '.join(cmd)}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def fetch(self):
        self._run(['git', 'fetch', 'origin'])

    def get_current_branch(self) -> str:
        r = self._run(['git', 'branch', '--show-current'])
        return r.stdout.strip()

    def create_branch(self, branch_name: str, start_point: str = "HEAD"):
        self._run(['git', 'branch', branch_name, start_point], check=False)

    def create_worktree(self, branch_name: str, label: str) -> str:
        wt_path = os.path.join(
            self.worktree_base,
            f"orch_{label}_{branch_name.replace('/', '_')}")
        if os.path.exists(wt_path):
            return wt_path
        self._run(['git', 'worktree', 'add', wt_path, branch_name])
        return wt_path

    def remove_worktree(self, wt_path: str):
        if os.path.exists(wt_path):
            self._run(['git', 'worktree', 'remove', '--force', wt_path],
                       check=False)

    def merge_branch(self, branch_name: str,
                     cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        return self._run(
            ['git', 'merge', '--no-ff', branch_name,
             '-m', f'Merge subtask branch {branch_name}'],
            cwd=cwd, check=False)

    def push(self, branch_name: str, cwd: Optional[str] = None):
        self._run(['git', 'push', '-u', 'origin', branch_name],
                  cwd=cwd, check=False)

    def has_conflicts(self, cwd: Optional[str] = None) -> bool:
        r = self._run(['git', 'diff', '--name-only', '--diff-filter=U'],
                      cwd=cwd, check=False)
        return bool(r.stdout.strip())

    def abort_merge(self, cwd: Optional[str] = None):
        self._run(['git', 'merge', '--abort'], cwd=cwd, check=False)

    def commit_all(self, message: str, cwd: Optional[str] = None):
        self._run(['git', 'add', '-A'], cwd=cwd)
        self._run(['git', 'commit', '-m', message], cwd=cwd, check=False)
