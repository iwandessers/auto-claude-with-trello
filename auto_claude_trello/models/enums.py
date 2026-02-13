"""Status and phase enumerations."""

from enum import Enum


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
