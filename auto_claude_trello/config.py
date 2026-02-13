"""Environment variables and path constants.

All configuration is loaded once at import time from environment
variables (with optional ``.env`` file support via *python-dotenv*).
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

# -- Trello -------------------------------------------------------------------
TRELLO_API_KEY = os.getenv('TRELLO_API_KEY')
TRELLO_TOKEN = os.getenv('TRELLO_TOKEN')
TRELLO_BOARD_ID = os.getenv('TRELLO_BOARD_ID')
TRELLO_LIST_ID = os.getenv('TRELLO_LIST_ID')
TRELLO_ORCHESTRATOR_LIST_ID = os.getenv('TRELLO_ORCHESTRATOR_LIST_ID')
ORCH_AGENT_LIMIT = int(os.getenv('ORCH_AGENT_LIMIT', '10'))

# -- BitBucket ----------------------------------------------------------------
BITBUCKET_ACCESS_TOKEN = os.getenv('BITBUCKET_ACCESS_TOKEN')
BITBUCKET_WORKSPACE = os.getenv('BITBUCKET_WORKSPACE')
BITBUCKET_REPO_SLUG = os.getenv('BITBUCKET_REPO_SLUG')

# -- Filesystem paths ---------------------------------------------------------
GIT_REPO_PATH = os.getenv('GIT_REPO_PATH')
if not GIT_REPO_PATH:
    print("ERROR: GIT_REPO_PATH environment variable must be set")
    sys.exit(1)

WORKFLOW_STATE_DIR = os.getenv('WORKFLOW_STATE_DIR',
                               os.path.expanduser('~/.trello-workflow'))
WORKTREE_BASE_DIR = os.path.join(WORKFLOW_STATE_DIR, 'worktrees')
CARDS_STATE_DIR = os.path.join(WORKFLOW_STATE_DIR, 'cards')
ATTACHMENTS_BASE_DIR = os.path.join(WORKFLOW_STATE_DIR, 'attachments')
ORCHESTRATOR_STATE_DIR = os.path.join(WORKFLOW_STATE_DIR, 'orchestrator')
