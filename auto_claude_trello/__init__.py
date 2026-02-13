"""Auto-Claude-with-Trello: Trello + BitBucket workflow automation with
multi-agent orchestration powered by Claude Code."""

from auto_claude_trello.config import (
    TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID, TRELLO_LIST_ID,
    TRELLO_ORCHESTRATOR_LIST_ID, ORCH_AGENT_LIMIT,
    BITBUCKET_ACCESS_TOKEN, BITBUCKET_WORKSPACE, BITBUCKET_REPO_SLUG,
    GIT_REPO_PATH, WORKFLOW_STATE_DIR, WORKTREE_BASE_DIR,
    CARDS_STATE_DIR, ATTACHMENTS_BASE_DIR, ORCHESTRATOR_STATE_DIR,
)
from auto_claude_trello.models import (
    TaskStatus, OrchestratorPhase, SubTask, OrchestratorState,
)
from auto_claude_trello.trello_api import TrelloAPI
from auto_claude_trello.git_helper import GitHelper
from auto_claude_trello.agent import run_agent_in_worktree
from auto_claude_trello.workflow import ExtendedWorkflowAutomation
from auto_claude_trello.orchestrator import Orchestrator, watch_for_orchestration_cards
from auto_claude_trello.utils import cleanup_worktrees, cleanup_old_attachments
from auto_claude_trello.cli import main, orchestrator_main
