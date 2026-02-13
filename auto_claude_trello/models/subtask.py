"""SubTask dataclass — represents a single unit of work for a coding agent."""

from dataclasses import dataclass, field
from typing import List, Optional

from auto_claude_trello.models.enums import TaskStatus


@dataclass
class SubTask:
    id: str
    title: str
    description: str
    dependencies: List[str] = field(default_factory=list)
    estimated_files: List[str] = field(default_factory=list)
    priority: int = 0
    status: str = TaskStatus.PENDING.value
    card_id: Optional[str] = None
    agent_branch: Optional[str] = None
    worktree_path: Optional[str] = None
    agent_session_id: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result_summary: Optional[str] = None
    error: Optional[str] = None
    merged: bool = False
