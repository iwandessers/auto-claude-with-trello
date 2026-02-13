"""Cleanup utilities for worktrees and old attachment files."""

import os
import shutil
import subprocess
import time

from auto_claude_trello.config import (
    GIT_REPO_PATH, ATTACHMENTS_BASE_DIR,
)


def cleanup_worktrees():
    """Clean up any orphaned worktrees."""
    print("Cleaning up worktrees...")

    result = subprocess.run(
        ['git', 'worktree', 'list', '--porcelain'],
        cwd=GIT_REPO_PATH,
        capture_output=True,
        text=True
    )

    worktree_paths = []
    for line in result.stdout.split('\n'):
        if line.startswith('worktree '):
            worktree_paths.append(line.split(' ', 1)[1])

    for path in worktree_paths:
        if not os.path.exists(path) and path != GIT_REPO_PATH:
            print(f"Removing orphaned worktree: {path}")
            subprocess.run(
                ['git', 'worktree', 'remove', path],
                cwd=GIT_REPO_PATH,
                capture_output=True
            )


def cleanup_old_attachments(days_old=7):
    """Clean up attachment files older than specified days."""
    if not os.path.exists(ATTACHMENTS_BASE_DIR):
        return

    cutoff_time = time.time() - (days_old * 24 * 60 * 60)

    for card_dir in os.listdir(ATTACHMENTS_BASE_DIR):
        card_path = os.path.join(ATTACHMENTS_BASE_DIR, card_dir)
        if os.path.isdir(card_path):
            if os.path.getmtime(card_path) < cutoff_time:
                try:
                    shutil.rmtree(card_path)
                    print(f"Cleaned up old attachments for card: {card_dir}")
                except Exception as e:
                    print(f"Error cleaning up old attachments for {card_dir}: {e}")
