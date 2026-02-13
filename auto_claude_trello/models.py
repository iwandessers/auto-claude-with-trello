"""Data models: enums and dataclasses used by both the workflow and the
orchestrator."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class TaskStatus(Enum):
    """Status of an individual subtask."""
    PENDING = "pending"
    READY = "ready"            # dependencies met, awaiting agent slot
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    BLOCKED = "blocked"        # a dependency failed
    CANCELLED = "cancelled"


class OrchestratorPhase(Enum):
    """High-level phase of the orchestration run."""
    PLANNING = "planning"
    EXECUTING = "executing"
    MERGING = "merging"
    REVIEWING = "reviewing"
    COMPLETE = "complete"
    STOPPED = "stopped"
    FAILED = "failed"


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


@dataclass
class OrchestratorState:
    orchestrator_id: str
    parent_card_id: str
    parent_card_name: str
    parent_branch: str
    original_list_id: Optional[str] = None
    subtask_list_id: Optional[str] = None
    phase: str = OrchestratorPhase.PLANNING.value
    subtasks: List[Dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_status_post: Optional[str] = None
    status_post_count: int = 0
    total_agents_spawned: int = 0
