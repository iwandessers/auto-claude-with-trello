"""CLI entry points for both the workflow and the orchestrator."""

import argparse
import sys
import time

from auto_claude_trello.config import (
    TRELLO_API_KEY, TRELLO_TOKEN,
    TRELLO_ORCHESTRATOR_LIST_ID,
    GIT_REPO_PATH, WORKTREE_BASE_DIR,
)
from auto_claude_trello.trello_api import TrelloAPI
from auto_claude_trello.git_helper import GitHelper
from auto_claude_trello.orchestrator import Orchestrator, watch_for_orchestration_cards
from auto_claude_trello.workflow import ExtendedWorkflowAutomation
from auto_claude_trello.utils import cleanup_worktrees, cleanup_old_attachments


def main():
    """Run the workflow once or in a loop."""
    parser = argparse.ArgumentParser(
        description='Trello and BitBucket automation workflow')
    parser.add_argument('--loop', action='store_true',
                        help='Run in loop mode')
    parser.add_argument('--cleanup', action='store_true',
                        help='Clean up worktrees only')
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug output')
    args = parser.parse_args()

    cleanup_worktrees()
    cleanup_old_attachments()

    automation = ExtendedWorkflowAutomation(debug=args.debug)

    if args.loop:
        print("Running in loop mode. Press Ctrl+C to stop.")
        while True:
            automation.run()
            print("\nWaiting 60 seconds before next check...")
            time.sleep(60)
    elif args.cleanup:
        print("Cleaning up worktrees only...")
        cleanup_worktrees()
    else:
        automation.run()


def orchestrator_main():
    """Entry point for the orchestrator CLI."""
    parser = argparse.ArgumentParser(
        description='Orchestrator: decompose Trello cards into parallel '
                    'Claude Code agents')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--card-id',
                       help='Orchestrate a specific Trello card')
    group.add_argument('--watch', action='store_true',
                       help='Watch the orchestrator list for cards')
    parser.add_argument('--max-agents', type=int, default=3,
                        help='Max parallel agents (default: 3)')
    parser.add_argument('--poll-interval', type=int, default=30,
                        help='Seconds between poll cycles (default: 30)')
    parser.add_argument('--debug', action='store_true',
                        help='Verbose output')
    args = parser.parse_args()

    if not GIT_REPO_PATH:
        print("ERROR: GIT_REPO_PATH must be set")
        sys.exit(1)
    if not TRELLO_API_KEY or not TRELLO_TOKEN:
        print("ERROR: TRELLO_API_KEY and TRELLO_TOKEN must be set")
        sys.exit(1)

    if args.watch:
        if not TRELLO_ORCHESTRATOR_LIST_ID:
            print("ERROR: TRELLO_ORCHESTRATOR_LIST_ID must be set for "
                  "--watch mode")
            sys.exit(1)
        watch_for_orchestration_cards(
            max_agents=args.max_agents,
            poll_interval=args.poll_interval,
            debug=args.debug,
        )
    else:
        trello = TrelloAPI(TRELLO_API_KEY, TRELLO_TOKEN, debug=args.debug)
        git = GitHelper(GIT_REPO_PATH, WORKTREE_BASE_DIR, debug=args.debug)
        orch = Orchestrator(trello, git, max_agents=args.max_agents,
                            poll_interval=args.poll_interval, debug=args.debug)
        orch.orchestrate(args.card_id)
