"""OrchestratorState dataclass — persistent state for an orchestration run."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from auto_claude_trello.models.enums import OrchestratorPhase


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
