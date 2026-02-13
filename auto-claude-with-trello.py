#!/usr/bin/env python3
"""
Extended workflow that processes both Trello comments AND BitBucket PR comments
as Claude Code instructions.

Environment Variables Required:
- TRELLO_API_KEY: Your Trello API key
- TRELLO_TOKEN: Your Trello API token
- TRELLO_BOARD_ID: The board to monitor
- TRELLO_LIST_ID: The list to monitor for tasks
- BITBUCKET_ACCESS_TOKEN: Repository access token from BitBucket
- BITBUCKET_WORKSPACE: Your BitBucket workspace
- BITBUCKET_REPO_SLUG: Your repository name
- GIT_REPO_PATH: Path to your git repository (e.g., /path/to/your/repo)
- WORKFLOW_STATE_DIR: Directory to store workflow state (optional, defaults to ~/.trello-workflow)
- TRELLO_ORCHESTRATOR_LIST_ID: Trello list ID that triggers orchestration.
  Drop a card onto this list to decompose it into subtasks and execute them
  in parallel via multiple Claude Code agents. Move the card off the list to stop.

To create a BitBucket repository access token:
1. Go to BitBucket → Personal settings → App passwords
2. Or for repository-specific: Repository settings → Access tokens
3. Create token with these permissions:
   - repository:read (Grants ability to read repository content, PRs, comments. Essential for fetching information.)
   - repository:write (Grants ability to push changes to the repository. Essential for committing Claude's output.)
   - pullrequest:read (Grants ability to read pull request details and comments specifically. Often covered by 'repository:read' but good to ensure if available.)
   - pullrequest:write (Grants ability to comment on pull requests, approve, merge, decline. Essential for adding feedback and managing PRs.)
"""

import os
import sys
import json
import subprocess
import time
import re
import argparse
import shutil
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
TRELLO_API_KEY = os.getenv('TRELLO_API_KEY')
TRELLO_TOKEN = os.getenv('TRELLO_TOKEN')
TRELLO_BOARD_ID = os.getenv('TRELLO_BOARD_ID')
TRELLO_LIST_ID = os.getenv('TRELLO_LIST_ID')
TRELLO_ORCHESTRATOR_LIST_ID = os.getenv('TRELLO_ORCHESTRATOR_LIST_ID')

BITBUCKET_ACCESS_TOKEN = os.getenv('BITBUCKET_ACCESS_TOKEN')
BITBUCKET_WORKSPACE = os.getenv('BITBUCKET_WORKSPACE')
BITBUCKET_REPO_SLUG = os.getenv('BITBUCKET_REPO_SLUG')

# Repository and state configuration
GIT_REPO_PATH = os.getenv('GIT_REPO_PATH')
if not GIT_REPO_PATH:
    print("ERROR: GIT_REPO_PATH environment variable must be set")
    sys.exit(1)

# State directory - outside of git repo
WORKFLOW_STATE_DIR = os.getenv('WORKFLOW_STATE_DIR', os.path.expanduser('~/.trello-workflow'))
WORKTREE_BASE_DIR = os.path.join(WORKFLOW_STATE_DIR, 'worktrees')
CARDS_STATE_DIR = os.path.join(WORKFLOW_STATE_DIR, 'cards')
ATTACHMENTS_BASE_DIR = os.path.join(WORKFLOW_STATE_DIR, 'attachments')


class ExtendedWorkflowAutomation:
    def __init__(self, debug=False):
        self.debug = debug
        self.ensure_directories()
        self.bb_base_url = f"https://api.bitbucket.org/2.0/repositories/{BITBUCKET_WORKSPACE}/{BITBUCKET_REPO_SLUG}"
        self.bb_headers = {
            'Authorization': f'Bearer {BITBUCKET_ACCESS_TOKEN}',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        } if BITBUCKET_ACCESS_TOKEN else None
        # Unique identifier for bot-generated comments
        self.bot_signature = "[auto-claude-bot:processed]"
        
        if self.debug:
            print("[DEBUG] BitBucket Configuration:")
            print(f"  - Workspace: {BITBUCKET_WORKSPACE}")
            print(f"  - Repository: {BITBUCKET_REPO_SLUG}")
            print(f"  - API Base URL: {self.bb_base_url}")
            print(f"  - Access Token: {'Configured' if BITBUCKET_ACCESS_TOKEN else 'Not configured'}")
        
    def ensure_directories(self):
        """Create necessary directories if they don't exist."""
        os.makedirs(WORKFLOW_STATE_DIR, exist_ok=True)
        os.makedirs(WORKTREE_BASE_DIR, exist_ok=True)
        os.makedirs(CARDS_STATE_DIR, exist_ok=True)
        os.makedirs(ATTACHMENTS_BASE_DIR, exist_ok=True)
    
    def get_card_state_file(self, card_id: str) -> str:
        """Get the state file path for a specific card."""
        return os.path.join(CARDS_STATE_DIR, f"{card_id}.json")
    
    def load_card_state(self, card_id: str) -> Dict:
        """Load state for a specific card."""
        state_file = self.get_card_state_file(card_id)
        if os.path.exists(state_file):
            with open(state_file, 'r') as f:
                state = json.load(f)
                # Ensure processed_pr_comments exists and contains only strings
                if 'processed_pr_comments' not in state:
                    state['processed_pr_comments'] = []
                else:
                    # Ensure all IDs are strings for consistency
                    state['processed_pr_comments'] = [str(id) for id in state['processed_pr_comments']]
                return state
        return {
            'card_id': card_id,
            'branch': None,
            'pr_url': None,
            'pr_id': None,
            'session_id': None,  # Claude Code session ID for conversation continuity
            'last_update': None,
            'processed_comments': [],
            'processed_pr_comments': [],  # List of string IDs of processed PR comments
            'created_at': datetime.now().isoformat()
        }
    
    def save_card_state(self, card_id: str, state: Dict):
        """Save state for a specific card."""
        state_file = self.get_card_state_file(card_id)
        state['last_update'] = datetime.now().isoformat()
        
        if self.debug:
            print(f"[DEBUG] Saving state for card {card_id}")
            print(f"[DEBUG] Processed Trello comments: {len(state.get('processed_comments', []))} IDs")
            print(f"[DEBUG] Processed PR comments: {len(state.get('processed_pr_comments', []))} IDs")
        
        with open(state_file, 'w') as f:
            json.dump(state, f, indent=2)
    
    def get_all_card_states(self) -> Dict[str, Dict]:
        """Load all card states."""
        states = {}
        for state_file in Path(CARDS_STATE_DIR).glob("*.json"):
            try:
                with open(state_file, 'r') as f:
                    state = json.load(f)
                    card_id = state.get('card_id', state_file.stem)
                    states[card_id] = state
            except Exception as e:
                print(f"Error loading state file {state_file}: {e}")
        return states


    # BitBucket API Rate Limiting:
    # BitBucket Cloud has API rate limits. While this script's 60-second loop delay
    # (if run with --loop) significantly reduces the risk of hitting these limits for
    # typical usage, very high activity repositories or more frequent checks might
    # approach these limits.
    # The API typically returns a 429 HTTP status code when rate limits are exceeded.
    # Currently, the script relies on the delay and general error handling.
    # For more aggressive polling or extremely active repositories, implementing
    # explicit handling for 429 responses with an exponential backoff and retry
    # strategy would be a robust future enhancement for the BitBucket API calls.
    
    # === BitBucket PR Methods ===
    
    def get_pr_by_branch(self, branch_name: str) -> Optional[Dict]:
        """Find a PR by its source branch name."""
        if not self.bb_headers:
            if self.debug:
                print(f"[DEBUG] Skipping PR lookup - no BitBucket headers configured")
            return None
            
        url = f"{self.bb_base_url}/pullrequests"
        params = {
            'q': f'source.branch.name="{branch_name}"',
            'state': 'OPEN'
        }
        
        if self.debug:
            print(f"\n[DEBUG] get_pr_by_branch - Looking for PR with branch: {branch_name}")
            print(f"[DEBUG] API URL: {url}")
            print(f"[DEBUG] Query params: {params}")
        
        try:
            response = requests.get(url, headers=self.bb_headers, params=params)
            if self.debug:
                print(f"[DEBUG] Response status: {response.status_code}")
                
            if response.status_code == 200:
                data = response.json()
                if self.debug:
                    print(f"[DEBUG] Found {len(data.get('values', []))} PRs")
                    
                if data['values']:
                    pr = data['values'][0]
                    if self.debug:
                        print(f"[DEBUG] PR found: ID={pr['id']}, Title='{pr.get('title', 'N/A')}'")
                        print(f"[DEBUG] PR State: {pr.get('state', 'N/A')}")
                        print(f"[DEBUG] PR Links: {pr.get('links', {}).get('html', {}).get('href', 'N/A')}")
                    return pr
                else:
                    if self.debug:
                        print(f"[DEBUG] No PR found for branch: {branch_name}")
            else:
                if self.debug:
                    print(f"[DEBUG] Failed to fetch PRs. Response: {response.text[:200]}...")
        except Exception as e:
            print(f"Error fetching PR: {e}")
            if self.debug:
                import traceback
                print(f"[DEBUG] Full error traceback:")
                traceback.print_exc()
        
        return None
    
    def get_pr_comments(self, pr_id: int) -> List[Dict]:
        """Fetch all comments from a BitBucket PR and filter for unresolved inline comments."""
        if not self.bb_headers:
            if self.debug:
                print(f"[DEBUG] Skipping PR comments fetch - no BitBucket headers configured")
            return []
            
        url = f"{self.bb_base_url}/pullrequests/{pr_id}/comments"
        all_comments = []
        
        if self.debug:
            print(f"\n[DEBUG] get_pr_comments - Fetching comments for PR ID: {pr_id}")
            print(f"[DEBUG] Initial URL: {url}")
        
        try:
            page_count = 0
            while url:
                page_count += 1
                if self.debug:
                    print(f"[DEBUG] Fetching page {page_count}...")
                    
                response = requests.get(url, headers=self.bb_headers)
                if self.debug:
                    print(f"[DEBUG] Response status: {response.status_code}")
                    
                if response.status_code == 200:
                    data = response.json()
                    page_comments = data.get('values', [])
                    all_comments.extend(page_comments)
                    
                    if self.debug:
                        print(f"[DEBUG] Page {page_count}: Found {len(page_comments)} comments")
                        for idx, comment in enumerate(page_comments):
                            author = comment.get('user', {}).get('display_name', 'Unknown')
                            content = comment.get('content', {}).get('raw', '')[:50]
                            print(f"[DEBUG]   Comment {idx+1}: ID={comment['id']}, Author={author}, Content='{content}...'")
                    
                    url = data.get('next')
                else:
                    if self.debug:
                        print(f"[DEBUG] Failed to fetch comments. Response: {response.text[:200]}...")
                    break
        except Exception as e:
            print(f"Error fetching PR comments: {e}")
            if self.debug:
                import traceback
                print(f"[DEBUG] Full error traceback:")
                traceback.print_exc()
        
        if self.debug:
            print(f"[DEBUG] Total comments fetched: {len(all_comments)}")
                    
        return all_comments
    
    def add_pr_comment(self, pr_id: int, comment: str):
        """Add a comment to a BitBucket PR."""
        if not self.bb_headers:
            if self.debug:
                print(f"[DEBUG] Skipping PR comment add - no BitBucket headers configured")
            return
            
        url = f"{self.bb_base_url}/pullrequests/{pr_id}/comments"
        data = {
            'content': {
                'raw': comment
            }
        }
        
        if self.debug:
            print(f"\n[DEBUG] add_pr_comment - Adding comment to PR ID: {pr_id}")
            print(f"[DEBUG] API URL: {url}")
            print(f"[DEBUG] Comment length: {len(comment)} characters")
            print(f"[DEBUG] Comment preview: {comment[:100]}...")
        
        try:
            response = requests.post(url, headers=self.bb_headers, json=data)
            if self.debug:
                print(f"[DEBUG] Response status: {response.status_code}")
                
            if response.status_code == 201:
                if self.debug:
                    resp_data = response.json()
                    print(f"[DEBUG] Comment added successfully! Comment ID: {resp_data.get('id', 'N/A')}")
            else:
                print(f"Failed to add PR comment: {response.status_code}")
                if self.debug:
                    print(f"[DEBUG] Response body: {response.text[:500]}...")
        except Exception as e:
            print(f"Error adding PR comment: {e}")
            if self.debug:
                import traceback
                print(f"[DEBUG] Full error traceback:")
                traceback.print_exc()
    
    # === Trello Methods ===
    
    def get_trello_cards(self) -> List[Dict]:
        """Fetch all cards from the specified Trello list."""
        url = f"https://api.trello.com/1/lists/{TRELLO_LIST_ID}/cards"
        params = {
            'key': TRELLO_API_KEY,
            'token': TRELLO_TOKEN,
            'fields': 'id,name,desc,dateLastActivity'
            # Removed 'actions' and 'actions_limit' - we fetch comments separately
        }
        
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    
    def get_card_comments(self, card_id: str) -> List[Dict]:
        """Get all comments for a specific card."""
        url = f"https://api.trello.com/1/cards/{card_id}/actions"
        params = {
            'key': TRELLO_API_KEY,
            'token': TRELLO_TOKEN,
            'filter': 'commentCard'
        }
        
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    
    def get_card_attachments(self, card_id: str) -> List[Dict]:
        """Get all attachments for a specific card."""
        url = f"https://api.trello.com/1/cards/{card_id}/attachments"
        params = {
            'key': TRELLO_API_KEY,
            'token': TRELLO_TOKEN,
            'fields': 'id,name,url,mimeType,bytes'  # Explicitly request the url field
        }
        
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()
    
    def add_comment_to_card(self, card_id: str, comment: str):
        """Add a comment to a Trello card."""
        url = f"https://api.trello.com/1/cards/{card_id}/actions/comments"
        params = {
            'key': TRELLO_API_KEY,
            'token': TRELLO_TOKEN,
            'text': comment
        }
        
        response = requests.post(url, params=params)
        response.raise_for_status()
    
    # === Attachment Methods ===
    
    def download_attachment(self, attachment: Dict, card_id: str) -> str:
        """Download attachment to workflow state directory and return local path."""
        # Create card-specific attachments directory
        attachments_dir = os.path.join(ATTACHMENTS_BASE_DIR, card_id)
        os.makedirs(attachments_dir, exist_ok=True)
        
        filename = attachment['name']
        local_path = os.path.join(attachments_dir, filename)
        
        # Skip download if file already exists
        if os.path.exists(local_path):
            if self.debug:
                print(f"[DEBUG] Attachment already exists: {local_path}")
            return local_path
        
        try:
            # Debug: print what we're getting
            if self.debug:
                print(f"[DEBUG] Attachment object: {attachment}")
                print(f"[DEBUG] Attempting to download from URL: {attachment.get('url', 'NO URL FIELD')}")
            
            # The attachment URL needs OAuth authentication via Authorization header
            download_url = attachment.get('url')
            if not download_url:
                print(f"Error: No 'url' field in attachment object for '{filename}'")
                return None
            
            # Trello attachment downloads require OAuth Authorization header, NOT query parameters
            # Format: Authorization: OAuth oauth_consumer_key="KEY", oauth_token="TOKEN"
            headers = {
                'Authorization': f'OAuth oauth_consumer_key="{TRELLO_API_KEY}", oauth_token="{TRELLO_TOKEN}"'
            }
            
            if self.debug:
                print(f"[DEBUG] Download URL: {download_url}")
                print(f"[DEBUG] Using OAuth Authorization header")
            
            response = requests.get(download_url, headers=headers)
            response.raise_for_status()
            
            # Save to local file
            with open(local_path, 'wb') as f:
                f.write(response.content)
            
            if self.debug:
                print(f"[DEBUG] Downloaded attachment: {filename} -> {local_path}")
            
            return local_path
            
        except requests.exceptions.RequestException as e:
            print(f"Error downloading attachment '{filename}': {e}")
            if self.debug:
                print(f"[DEBUG] Full attachment object: {attachment}")
                if hasattr(e, 'response') and e.response is not None:
                    print(f"[DEBUG] Response status: {e.response.status_code}")
                    print(f"[DEBUG] Response headers: {e.response.headers}")
                    print(f"[DEBUG] Response content: {e.response.text[:500]}...")
            return None
    
    def process_attachments(self, card_id: str) -> str:
        """Process all attachments for a card and return context string."""
        attachments = self.get_card_attachments(card_id)
        
        if not attachments:
            return ""
        
        attachment_context = "\n\nAttached files available for analysis:"
        attachment_paths = []
        
        for attachment in attachments:
            local_path = self.download_attachment(attachment, card_id)
            if local_path:
                attachment_paths.append(local_path)
                attachment_context += f"\n- {attachment['name']} ({attachment.get('bytes', 'unknown size')} bytes)"
                attachment_context += f"\n  File path: {local_path}"
                attachment_context += f"\n  Type: {attachment.get('mimeType', 'unknown')}"
                
                # For text files, include content directly
                if attachment.get('mimeType', '').startswith('text/') and attachment.get('bytes', 0) < 10000:
                    try:
                        with open(local_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        attachment_context += f"\n  Content:\n```\n{content}\n```"
                    except Exception as e:
                        attachment_context += f"\n  (Could not read content: {e})"
        
        return attachment_context
    
    def cleanup_attachments(self, card_id: str):
        """Clean up downloaded attachments for a card."""
        attachments_dir = os.path.join(ATTACHMENTS_BASE_DIR, card_id)
        if os.path.exists(attachments_dir):
            try:
                shutil.rmtree(attachments_dir)
                if self.debug:
                    print(f"[DEBUG] Cleaned up attachments for card: {card_id}")
            except Exception as e:
                print(f"Error cleaning up attachments for card {card_id}: {e}")
    
    # === Git and Claude Code Methods ===
    
    def create_branch_name(self, card_name: str, session_id: str) -> str:
        """Create a valid git branch name from card title and session ID.

        Args:
            card_name: The Trello card name
            session_id: The Claude Code session UUID for guaranteed uniqueness
        """
        # Clean the card name
        branch = re.sub(r'[^a-zA-Z0-9\s-]', '', card_name)
        branch = branch.replace(' ', '-').lower()
        branch = re.sub(r'-+', '-', branch)
        # Use first 8 chars of session ID for uniqueness (short UUID format)
        session_short = session_id[:8]
        return f"feature/{branch}-{session_short}"[:50]
    
    def create_worktree(self, branch_name: str, card_id: str) -> Tuple[str, str]:
        """Create a new git worktree for the branch."""
        worktree_path = os.path.join(WORKTREE_BASE_DIR, f"{card_id}_{branch_name.replace('/', '_')}")
        
        # Fetch latest from origin before creating branch
        print("Fetching latest from origin before any operations...")
        subprocess.run(
            ['git', 'fetch', 'origin'],
            cwd=GIT_REPO_PATH,
            capture_output=True
        )
        
        # Create branch in the main repo
        result = subprocess.run(
            ['git', 'branch', branch_name],
            cwd=GIT_REPO_PATH,
            capture_output=True
        )
        
        # Add worktree
        subprocess.run(
            ['git', 'worktree', 'add', worktree_path, branch_name],
            cwd=GIT_REPO_PATH,
            check=True
        )
        
        # Push branch to remote
        result = subprocess.run(
            ['git', 'push', '-u', 'origin', branch_name],
            cwd=worktree_path,
            capture_output=True,
            text=True
        )
        
        return worktree_path, result.stdout + result.stderr
    
    def checkout_worktree(self, branch_name: str, card_id: str) -> str:
        """Checkout existing worktree or create if missing."""
        worktree_path = os.path.join(WORKTREE_BASE_DIR, f"{card_id}_{branch_name.replace('/', '_')}")
        
        # Fetch latest from origin before any operations
        print("Fetching latest from origin before any operations...")
        subprocess.run(
            ['git', 'fetch', 'origin'],
            cwd=GIT_REPO_PATH,
            capture_output=True
        )
        
        if not os.path.exists(worktree_path):
            # Recreate worktree if it was deleted
            subprocess.run(
                ['git', 'worktree', 'add', worktree_path, branch_name],
                cwd=GIT_REPO_PATH,
                check=True
            )
        
        # Pull latest changes
        subprocess.run(
            ['git', 'pull'],
            cwd=worktree_path,
            capture_output=True
        )
        
        return worktree_path
    
    def execute_claude_code(self, instructions: str, worktree_path: str, session_id: Optional[str] = None, is_first_interaction: bool = True) -> str:
        """Execute instructions using Claude Code in the specified directory.

        Args:
            instructions: The instruction to execute
            worktree_path: Path to the git worktree
            session_id: Optional session ID for conversation continuity
            is_first_interaction: True for first interaction (creates session), False for continuing
        """
        # Debug logging
        if self.debug:
            print(f"\n[DEBUG] Executing Claude Code with instruction length: {len(instructions)} characters")
            print(f"[DEBUG] First 200 chars of instruction: {instructions[:200]}...")
            print(f"[DEBUG] Session ID: {session_id if session_id else 'None (new session)'}")
            print(f"[DEBUG] Is first interaction: {is_first_interaction}")
            if len(instructions) > 10000:
                print(f"[WARNING] Very long instruction detected: {len(instructions)} characters!")

        # Escape quotes in instructions for shell command
        escaped_instructions = instructions.replace('"', '\\"')

        # Build command with session handling
        cmd = ['claude', '--dangerously-skip-permissions']
        if session_id:
            if is_first_interaction:
                # First interaction: create new session with specific ID
                cmd.extend(['--session-id', session_id])
            else:
                # Subsequent interactions: resume existing session
                cmd.extend(['--resume', session_id])
        cmd.extend(['-p', escaped_instructions])

        result = subprocess.run(
            cmd,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=1800  # 30 minutes timeout
        )

        output = f"Claude Code Output:\n{result.stdout}"
        if result.stderr.strip():
            output += f"\n\nErrors (if any):\n{result.stderr}"
            # Check for specific error
            if "Prompt is too long" in result.stderr and self.debug:
                print(f"[ERROR] Claude reported 'Prompt is too long' for instruction of {len(instructions)} characters")
        return output
    
    def commit_and_push(self, worktree_path: str, message: str, card_id: str) -> Tuple[str, Optional[str]]:
        """Commit all changes and push to remote, extracting PR URL if present."""
        output = []
        pr_url = None
        
        # Check if there are changes to commit
        status_result = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=worktree_path,
            capture_output=True,
            text=True
        )
        
        if not status_result.stdout.strip():
            output.append("No changes to commit")
            return '\n'.join(output), None
        
        result = subprocess.run(
            ['git', 'add', '-A'],
            cwd=worktree_path,
            capture_output=True,
            text=True
        )
        output.append(f"Git add: {result.stdout}")
        
        commit_message = f"{message}\n\nTrello Card ID: {card_id}"
        result = subprocess.run(
            ['git', 'commit', '-m', commit_message],
            cwd=worktree_path,
            capture_output=True,
            text=True
        )
        output.append(f"Git commit: {result.stdout}")
        
        result = subprocess.run(
            ['git', 'push', '--set-upstream', 'origin'],
            cwd=worktree_path,
            capture_output=True,
            text=True
        )
        push_output = result.stdout + result.stderr
        output.append(f"Git push: {push_output}")
        
        # Extract PR URL from push output
        pr_match = re.search(r'https://bitbucket\.org/[^\s]+/pull-requests/new[^\s]*', push_output)
        if not pr_match:
            pr_match = re.search(r'remote:\s*(https://bitbucket\.org/[^\s]+/pull-requests/\d+)', push_output)
        
        if pr_match:
            pr_url = pr_match.group(0)
        
        return '\n'.join(output), pr_url
    
    # === Processing Methods ===
    
    def process_new_card(self, card: Dict):
        """Process a newly discovered card."""
        card_id = card['id']
        card_name = card['name']
        description = card['desc']
        
        print(f"Processing new card: {card_name} ({card_id})")
        
        # Load or create card state
        card_state = self.load_card_state(card_id)

        # Generate session ID for Claude Code conversation continuity
        if not card_state.get('session_id'):
            card_state['session_id'] = str(uuid.uuid4())
            if self.debug:
                print(f"[DEBUG] Generated new session ID: {card_state['session_id']}")

        # Create branch using session ID for guaranteed uniqueness
        branch_name = self.create_branch_name(card_name, card_state['session_id'])
        worktree_path, push_output = self.create_worktree(branch_name, card_id)

        # Update state with branch info
        card_state['branch'] = branch_name
        card_state['worktree_path'] = worktree_path
        card_state['card_name'] = card_name
        
        # Process attachments and include in instruction
        attachment_context = self.process_attachments(card_id)

        # Execute Claude Code with description and attachments (first interaction)
        claude_instruction = f"{description}{attachment_context}"
        claude_output = self.execute_claude_code(
            claude_instruction,
            worktree_path,
            card_state.get('session_id'),
            is_first_interaction=True
        )
        
        # Commit and push
        commit_output, pr_url = self.commit_and_push(
            worktree_path,
            f"Initial implementation for: {card_name}",
            card_id
        )
        
        # Extract PR URL from initial push if not found in commit push
        if not pr_url and push_output:
            # Check for Bitbucket PR URL
            pr_match = re.search(r'https://bitbucket\.org/[^\s]+/pull-requests/new[^\s]*', push_output)
            if pr_match:
                pr_url = pr_match.group(0)
            else:
                # Check for GitHub PR URL
                pr_match = re.search(r'https://github\.com/[^\s]+/pull/new/[^\s]*', push_output)
                if pr_match:
                    pr_url = pr_match.group(0)
        
        # Update state with PR info
        if pr_url:
            card_state['pr_url'] = pr_url
        
        # Add comment to Trello card
        comment = f"""🤖 Automated Workflow Update:

Branch created: `{branch_name}`
Worktree: `{worktree_path}`

{claude_output}

Git Operations:
```
{push_output}
{commit_output}
```

{self.bot_signature}"""
        
        if pr_url:
            comment += f"\n📄 Create Pull Request: {pr_url}"
        else:
            comment += "\n⚠️ No PR URL found in push output. You may need to create it manually."
        
        self.add_comment_to_card(card_id, comment)
        
        # Save card state
        self.save_card_state(card_id, card_state)
    
    def process_card_comments(self, card: Dict, comments: List[Dict], card_state: Dict):
        """Process new comments on an existing card from Trello."""
        card_id = card['id']
        card_name = card['name']
        branch_name = card_state['branch']
        
        processed_ids = set(card_state.get('processed_comments', []))
        new_comments = [c for c in comments if c['id'] not in processed_ids]
        
        if not new_comments:
            return
        
        print(f"Processing {len(new_comments)} new Trello comments for: {card_name} ({card_id})")
        
        worktree_path = self.checkout_worktree(branch_name, card_id)
        
        for comment in new_comments:
            comment_text = comment['data']['text']

            # Skip comments with user mentions/tags
            has_mentions = bool(re.search(r'@\w+', comment_text))
            if has_mentions:
                print(f"Skipping Trello comment (contains user mentions)")
                card_state['processed_comments'].append(comment['id'])
                continue

            # Skip bot comments - check for bot signature
            is_bot_comment = self.bot_signature in comment_text

            if is_bot_comment:
                print(f"Skipping Trello comment (bot comment detected)")
                card_state['processed_comments'].append(comment['id'])
                continue
            
            # Process attachments for additional context
            attachment_context = self.process_attachments(card_id)

            # Continue existing session for comment processing
            claude_instruction = f"{comment_text}{attachment_context}"
            claude_output = self.execute_claude_code(
                claude_instruction,
                worktree_path,
                card_state.get('session_id'),
                is_first_interaction=False
            )
            
            commit_output, _ = self.commit_and_push(
                worktree_path,
                f"Update from Trello comment: {comment_text[:50]}...",
                card_id
            )
            
            response_comment = f"""🤖 Processed Trello comment update:

{claude_output}

Git Operations:
```
{commit_output}
```

Pull Request: {card_state.get('pr_url', 'Create PR manually from BitBucket')}

{self.bot_signature}"""
            
            self.add_comment_to_card(card_id, response_comment)
            card_state['processed_comments'].append(comment['id'])
        
        # Save updated state
        self.save_card_state(card_id, card_state)
    
    
    def process_pr_comments(self, card_id: str, card_state: Dict):
        """Process new comments from BitBucket PR as Claude Code instructions."""
        branch_name = card_state['branch']
        
        if self.debug:
            print(f"\n[DEBUG] process_pr_comments - Starting for card: {card_id}")
            print(f"[DEBUG] Branch name: {branch_name}")
            print(f"[DEBUG] Current PR ID in state: {card_state.get('pr_id', 'Not set')}")
        
        # Find PR for this branch
        pr_data = self.get_pr_by_branch(branch_name)
        if not pr_data:
            if self.debug:
                print(f"[DEBUG] No PR found for branch {branch_name}, skipping PR comment processing")
            return
        
        pr_id = pr_data['id']
        
        if self.debug:
            print(f"[DEBUG] Found PR ID: {pr_id}")
        
        # Update PR ID in state if not set
        if not card_state.get('pr_id'):
            card_state['pr_id'] = pr_id
            self.save_card_state(card_id, card_state)
            if self.debug:
                print(f"[DEBUG] Updated card state with PR ID: {pr_id}")
        
        # Get all PR comments
        pr_comments = self.get_pr_comments(pr_id)
        
        # Filter for new comments
        # Ensure all processed IDs are strings for consistent comparison
        processed_pr_ids = set(str(id) for id in card_state.get('processed_pr_comments', []))
        new_pr_comments = [c for c in pr_comments if str(c['id']) not in processed_pr_ids]
        
        if self.debug:
            print(f"[DEBUG] Total PR comments: {len(pr_comments)}")
            print(f"[DEBUG] Already processed: {len(processed_pr_ids)}")
            print(f"[DEBUG] New comments to process: {len(new_pr_comments)}")
            if processed_pr_ids:
                print(f"[DEBUG] Processed comment IDs: {list(processed_pr_ids)[:5]}{'...' if len(processed_pr_ids) > 5 else ''}")
        
        if not new_pr_comments:
            if self.debug:
                print(f"[DEBUG] No new PR comments to process")
            return
        
        print(f"Found {len(new_pr_comments)} new BitBucket PR comments for card: {card_id}")
        
        worktree_path = self.checkout_worktree(branch_name, card_id)
        
        for comment in new_pr_comments:
            # Extract metadata first to always have comment_id
            comment_id = str(comment['id'])
            
            try:
                # Extract comment details
                comment_text = comment.get('content', {}).get('raw', '')
                author_display_name = comment.get('user', {}).get('display_name', 'Unknown')
                author_username = comment.get('user', {}).get('username', 'unknown')
                created_on = comment.get('created_on', '')
                updated_on = comment.get('updated_on', '')
                parent_id = comment.get('parent', {}).get('id') if comment.get('parent') else None
                inline_path = comment.get('inline', {}).get('path') if comment.get('inline') else None
                inline_from = comment.get('inline', {}).get('from') if comment.get('inline') else None
                inline_to = comment.get('inline', {}).get('to') if comment.get('inline') else None
                
                if self.debug:
                    print(f"\n[DEBUG] Processing comment ID: {comment_id}")
                    print(f"[DEBUG] Author: {author_display_name}")
                    print(f"[DEBUG] Comment text length: {len(comment_text)} characters")
                    print(f"[DEBUG] Comment preview: {comment_text[:100]}...")
                
                # Skip if comment is empty
                if not comment_text.strip():
                    if 'processed_pr_comments' not in card_state:
                        card_state['processed_pr_comments'] = []
                    card_state['processed_pr_comments'].append(str(comment_id))
                    continue

                # Skip if comment has been deleted
                is_deleted = comment.get('deleted', False)
                if is_deleted:
                    print(f"Skipping comment {comment_id} by {author_display_name} (deleted comment)")
                    if 'processed_pr_comments' not in card_state:
                        card_state['processed_pr_comments'] = []
                    card_state['processed_pr_comments'].append(str(comment_id))
                    continue

                # Skip if comment is from the bot itself - check for bot signature
                is_bot_comment = self.bot_signature in comment_text
                
                if is_bot_comment:
                    print(f"Skipping comment {comment_id} by {author_display_name} (bot comment detected)")
                    if 'processed_pr_comments' not in card_state:
                        card_state['processed_pr_comments'] = []
                    card_state['processed_pr_comments'].append(str(comment_id))
                    continue

                print(f"Processing PR comment ID: {comment_id} from {author_display_name}: {comment_text[:50]}...")
                
                # Prepare full comment context
                comment_context = f"""BitBucket PR Comment Details:
- Author: {author_display_name} (@{author_username})
- Created: {created_on}
- Updated: {updated_on}
- Comment ID: {comment_id}
{f'- Parent Comment ID: {parent_id}' if parent_id else ''}
{f'- Inline comment on file: {inline_path}' if inline_path else ''}
{f'- Line range: {inline_from} to {inline_to}' if inline_from else ''}

Comment Text:
{comment_text}
"""
                
                # Process attachments for additional context
                attachment_context = self.process_attachments(card_id)

                # Execute as Claude Code instruction with full context (continue existing session)
                claude_instruction = f"Analyse the changes made in this git branch. Use this knowledge to process the following feedback.\n{comment_context}{attachment_context}"
                claude_output = self.execute_claude_code(
                    claude_instruction,
                    worktree_path,
                    card_state.get('session_id'),
                    is_first_interaction=False
                )
                
                # Commit and push
                commit_output, _ = self.commit_and_push(
                    worktree_path,
                    f"Update from PR comment by {author_display_name}: {comment_text[:50]}...",
                    card_id
                )
                
                # Add response to both PR and Trello
                response_text = f"""🤖 Processed BitBucket PR comment:

**Author**: {author_display_name} (@{author_username})
**Created**: {created_on}
**Comment ID**: {comment_id}
{f'**Reply to**: Comment #{parent_id}' if parent_id else ''}
{f'**File**: {inline_path} (lines {inline_from}-{inline_to})' if inline_path else ''}

**Comment**: {comment_text[:200]}{'...' if len(comment_text) > 200 else ''}

**Claude Code Response**:
{claude_output}

**Git Operations**:
```
{commit_output}
```

{self.bot_signature}"""
                
                # Add to PR
                self.add_pr_comment(pr_id, response_text)
                
                # Add to Trello
                self.add_comment_to_card(card_id, response_text)
                
            except Exception as e:
                print(f"Error processing comment {comment_id}: {e}")
                import traceback
                traceback.print_exc()
            finally:
                # Always mark as processed (ensure it's a string)
                if 'processed_pr_comments' not in card_state:
                    card_state['processed_pr_comments'] = []
                if str(comment_id) not in card_state['processed_pr_comments']:
                    card_state['processed_pr_comments'].append(str(comment_id))
        
        # Save updated state
        if self.debug:
            print(f"[DEBUG] Saving card state with {len(card_state.get('processed_pr_comments', []))} processed PR comments")
            print(f"[DEBUG] Processed PR comment IDs being saved: {sorted([str(id) for id in card_state.get('processed_pr_comments', [])])}")
        self.save_card_state(card_id, card_state)
    
    def run(self):
        """Main workflow loop - check for new cards and comments from both Trello and BitBucket."""
        print(f"Starting workflow check at {datetime.now()}")
        print(f"Git repo: {GIT_REPO_PATH}")
        print(f"State directory: {WORKFLOW_STATE_DIR}")
        
        # Ensure the main repo has the latest changes before processing tickets
        print("Updating main repository with latest changes...")
        try:
            # First fetch all changes from origin
            fetch_result = subprocess.run(
                ['git', 'fetch', 'origin'],
                cwd=GIT_REPO_PATH,
                capture_output=True,
                text=True
            )
            if fetch_result.returncode != 0:
                print(f"Warning: Git fetch failed: {fetch_result.stderr}")
            
            # Get current branch
            current_branch_result = subprocess.run(
                ['git', 'branch', '--show-current'],
                cwd=GIT_REPO_PATH,
                capture_output=True,
                text=True
            )
            current_branch = current_branch_result.stdout.strip()
            
            # Pull latest changes for current branch
            if current_branch:
                pull_result = subprocess.run(
                    ['git', 'pull', 'origin', current_branch],
                    cwd=GIT_REPO_PATH,
                    capture_output=True,
                    text=True
                )
                if pull_result.returncode != 0:
                    print(f"Warning: Git pull failed: {pull_result.stderr}")
                else:
                    print(f"Successfully updated branch '{current_branch}'")
            
            # Also fetch all remote branches to ensure we have latest branch information
            fetch_all_result = subprocess.run(
                ['git', 'fetch', '--all'],
                cwd=GIT_REPO_PATH,
                capture_output=True,
                text=True
            )
            if fetch_all_result.returncode == 0:
                print("Successfully fetched all remote branches")
                
        except Exception as e:
            print(f"Warning: Could not update repository: {e}")
            # Continue processing even if update fails
        
        try:
            # Load all existing card states
            all_card_states = self.get_all_card_states()
            
            # Get current cards from Trello
            cards = self.get_trello_cards()
            print(f"Found {len(cards)} cards in Trello list")
            
            for card in cards:
                card_id = card['id']

                if card_id not in all_card_states:
                    # New card found - skip if description is empty
                    description = card.get('desc', '').strip()
                    if not description:
                        print(f"Skipping card '{card['name']}' ({card_id}) - description is empty")
                        continue
                    self.process_new_card(card)
                else:
                    # Existing card - check for new comments from both sources
                    card_state = self.load_card_state(card_id)  # Load fresh state
                    
                    # Skip if no branch created yet
                    if not card_state.get('branch'):
                        continue
                    
                    # Process Trello comments
                    comments = self.get_card_comments(card_id)
                    self.process_card_comments(card, comments, card_state)
                    
                    # Process BitBucket PR comments (if PR exists)
                    if BITBUCKET_ACCESS_TOKEN:  # Only if BitBucket is configured
                        if self.debug:
                            print(f"\n[DEBUG] Checking for BitBucket PR comments for card: {card_id}")
                        self.process_pr_comments(card_id, card_state)
                    elif self.debug:
                        print(f"\n[DEBUG] Skipping BitBucket PR comment check - no access token configured")
            
            print("Workflow check completed successfully")
            
        except Exception as e:
            print(f"Error in workflow: {e}")
            import traceback
            traceback.print_exc()


def cleanup_worktrees():
    """Clean up any orphaned worktrees."""
    print("Cleaning up worktrees...")
    
    # List all worktrees
    result = subprocess.run(
        ['git', 'worktree', 'list', '--porcelain'],
        cwd=GIT_REPO_PATH,
        capture_output=True,
        text=True
    )
    
    # Parse worktree paths
    worktree_paths = []
    for line in result.stdout.split('\n'):
        if line.startswith('worktree '):
            worktree_paths.append(line.split(' ', 1)[1])
    
    # Remove worktrees that don't exist
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
    
    import time
    cutoff_time = time.time() - (days_old * 24 * 60 * 60)
    
    for card_dir in os.listdir(ATTACHMENTS_BASE_DIR):
        card_path = os.path.join(ATTACHMENTS_BASE_DIR, card_dir)
        if os.path.isdir(card_path):
            # Check if directory is old
            if os.path.getmtime(card_path) < cutoff_time:
                try:
                    shutil.rmtree(card_path)
                    print(f"Cleaned up old attachments for card: {card_dir}")
                except Exception as e:
                    print(f"Error cleaning up old attachments for {card_dir}: {e}")


def main():
    """Run the workflow once or in a loop."""
    parser = argparse.ArgumentParser(description='Trello and BitBucket automation workflow')
    parser.add_argument('--loop', action='store_true', help='Run in loop mode')
    parser.add_argument('--cleanup', action='store_true', help='Clean up worktrees only')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    args = parser.parse_args()
    
    # Clean up any orphaned worktrees and old attachments on startup
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


###############################################################################
# ORCHESTRATOR SYSTEM
#
# Multi-agent task decomposition and parallel execution engine.
# Drop a Trello card onto the TRELLO_ORCHESTRATOR_LIST_ID list to trigger
# automatic decomposition into subtasks, parallel execution via Claude Code
# agents in separate git worktrees, merge, and PR creation.
#
# Move the card OFF the list at any time to halt execution.
###############################################################################

import signal
import traceback
import concurrent.futures
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


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


ORCHESTRATOR_STATE_DIR = os.path.join(WORKFLOW_STATE_DIR, 'orchestrator')


class TrelloAPI:
    """Standalone Trello REST API helper for the orchestrator."""

    BASE = "https://api.trello.com/1"

    def __init__(self, api_key: str, token: str, debug: bool = False):
        self.auth = {'key': api_key, 'token': token}
        self.debug = debug

    def _dbg(self, msg: str):
        if self.debug:
            print(f"[ORCH-TRELLO] {msg}")

    # -- cards ----------------------------------------------------------------

    def get_card(self, card_id: str) -> Dict:
        url = f"{self.BASE}/cards/{card_id}"
        params = {**self.auth, 'fields': 'id,name,desc,idList,idBoard'}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_cards_on_list(self, list_id: str) -> List[Dict]:
        url = f"{self.BASE}/lists/{list_id}/cards"
        params = {**self.auth, 'fields': 'id,name,desc,idList,dateLastActivity'}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_card_attachments(self, card_id: str) -> List[Dict]:
        url = f"{self.BASE}/cards/{card_id}/attachments"
        params = {**self.auth, 'fields': 'id,name,url,mimeType,bytes'}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def add_comment(self, card_id: str, text: str):
        url = f"{self.BASE}/cards/{card_id}/actions/comments"
        params = {**self.auth, 'text': text}
        resp = requests.post(url, params=params, timeout=30)
        resp.raise_for_status()

    def move_card(self, card_id: str, list_id: str):
        url = f"{self.BASE}/cards/{card_id}"
        params = {**self.auth, 'idList': list_id}
        resp = requests.put(url, params=params, timeout=30)
        resp.raise_for_status()

    # -- lists ----------------------------------------------------------------

    def create_list(self, board_id: str, name: str) -> Dict:
        url = f"{self.BASE}/boards/{board_id}/lists"
        params = {**self.auth, 'name': name}
        resp = requests.post(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def create_card(self, list_id: str, name: str, desc: str = "") -> Dict:
        url = f"{self.BASE}/cards"
        params = {**self.auth, 'idList': list_id, 'name': name, 'desc': desc}
        resp = requests.post(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def archive_list(self, list_id: str):
        url = f"{self.BASE}/lists/{list_id}/closed"
        params = {**self.auth, 'value': 'true'}
        resp = requests.put(url, params=params, timeout=30)
        resp.raise_for_status()


class GitHelper:
    """Git operations helper for the orchestrator."""

    def __init__(self, repo_path: str, worktree_base: str, debug: bool = False):
        self.repo_path = repo_path
        self.worktree_base = worktree_base
        self.debug = debug
        os.makedirs(worktree_base, exist_ok=True)

    def _run(self, cmd: List[str], cwd: Optional[str] = None,
             check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
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
        wt_path = os.path.join(self.worktree_base,
                               f"orch_{label}_{branch_name.replace('/', '_')}")
        if os.path.exists(wt_path):
            return wt_path
        self._run(['git', 'worktree', 'add', wt_path, branch_name])
        return wt_path

    def remove_worktree(self, wt_path: str):
        if os.path.exists(wt_path):
            self._run(['git', 'worktree', 'remove', '--force', wt_path],
                       check=False)

    def merge_branch(self, branch_name: str, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        return self._run(['git', 'merge', '--no-ff', branch_name,
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


# ---------------------------------------------------------------------------
# Module-level agent runner (must be at module level for ProcessPoolExecutor
# pickling).
# ---------------------------------------------------------------------------

def _run_agent_in_worktree(worktree_path: str, prompt: str,
                           timeout_seconds: int = 900) -> Dict[str, Any]:
    """Run a Claude Code agent inside *worktree_path*.

    Returns a dict with 'success', 'output', and 'error' keys.
    This function is intentionally at module level so
    ``concurrent.futures.ProcessPoolExecutor`` can pickle it.
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


class Orchestrator:
    """Multi-agent orchestrator that decomposes a Trello card into subtasks,
    runs them in parallel Claude Code agents, merges the results, and creates
    a pull request."""

    BOT_TAG = "[orchestrator-bot]"

    def __init__(self, trello: TrelloAPI, git: GitHelper,
                 max_agents: int = 3, poll_interval: int = 30,
                 agent_timeout: int = 900, debug: bool = False):
        self.trello = trello
        self.git = git
        self.max_agents = max_agents
        self.poll_interval = poll_interval
        self.agent_timeout = agent_timeout
        self.debug = debug
        self._stop_requested = False
        self._executor: Optional[concurrent.futures.ProcessPoolExecutor] = None
        # future -> subtask id
        self._futures: Dict[concurrent.futures.Future, str] = {}
        os.makedirs(ORCHESTRATOR_STATE_DIR, exist_ok=True)

    # -- signal handling ------------------------------------------------------

    def _register_signals(self):
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        print(f"\n[ORCH] Received signal {signum}, requesting graceful stop…")
        self._stop_requested = True

    # -- state persistence ----------------------------------------------------

    def _state_path(self, card_id: str) -> str:
        return os.path.join(ORCHESTRATOR_STATE_DIR, f"{card_id}.json")

    def _save_state(self, state: OrchestratorState):
        state.updated_at = datetime.now().isoformat()
        path = self._state_path(state.parent_card_id)
        with open(path, 'w') as f:
            json.dump(asdict(state), f, indent=2)

    def _load_state(self, card_id: str) -> Optional[OrchestratorState]:
        path = self._state_path(card_id)
        if not os.path.exists(path):
            return None
        with open(path, 'r') as f:
            data = json.load(f)
        return OrchestratorState(**{
            k: v for k, v in data.items()
            if k in OrchestratorState.__dataclass_fields__
        })

    # -- stop detection -------------------------------------------------------

    def _check_for_stop(self, state: OrchestratorState) -> bool:
        """Return True if orchestration should stop."""
        if self._stop_requested:
            return True
        try:
            card = self.trello.get_card(state.parent_card_id)
            if card['idList'] != TRELLO_ORCHESTRATOR_LIST_ID:
                print(f"[ORCH] Card moved off orchestrator list — stopping.")
                return True
        except Exception as exc:
            print(f"[ORCH] Warning: could not check card list: {exc}")
        return False

    # -- status comments ------------------------------------------------------

    def _status_comment(self, state: OrchestratorState,
                        extra: str = "") -> str:
        subtasks = [SubTask(**s) if isinstance(s, dict) else s
                    for s in state.subtasks]
        counts: Dict[str, int] = {}
        for st in subtasks:
            status_val = st.status if isinstance(st.status, str) else st.status
            counts[status_val] = counts.get(status_val, 0) + 1

        running_names = [s.title for s in subtasks
                         if s.status == TaskStatus.RUNNING.value]

        state.status_post_count += 1
        now = datetime.now().isoformat(timespec='seconds')
        state.last_status_post = now

        lines = [
            f"## {self.BOT_TAG} Orchestrator Status #{state.status_post_count}",
            f"**Time:** {now}",
            f"**Phase:** {state.phase}",
            f"**Agents:** {len(running_names)}/{self.max_agents} active, "
            f"{state.total_agents_spawned} total spawned",
            "",
            "### Task Counts",
        ]
        for status_name in [e.value for e in TaskStatus]:
            c = counts.get(status_name, 0)
            if c:
                lines.append(f"- **{status_name}**: {c}")

        if running_names:
            lines.append("")
            lines.append("### Currently Running")
            for n in running_names:
                lines.append(f"- {n}")

        if extra:
            lines.append("")
            lines.append(extra)

        return "\n".join(lines)

    def _post_status(self, state: OrchestratorState, extra: str = ""):
        comment = self._status_comment(state, extra)
        try:
            self.trello.add_comment(state.parent_card_id, comment)
        except Exception as exc:
            print(f"[ORCH] Warning: failed to post status comment: {exc}")

    # -- task decomposition ---------------------------------------------------

    def _decompose_task(self, card_name: str, card_desc: str,
                        attachments_info: str) -> List[SubTask]:
        prompt = f"""You are a software architect. Decompose the following task into 3-8 independently executable subtasks for parallel coding agents.

TASK TITLE: {card_name}

TASK DESCRIPTION:
{card_desc}

{('ATTACHMENTS INFO:' + chr(10) + attachments_info) if attachments_info else ''}

Return ONLY a JSON array of subtask objects. Each object must have these fields:
- "id": a short unique slug (e.g. "setup-auth")
- "title": concise subtask title
- "description": a complete, standalone prompt for a coding agent — include ALL context needed so the agent can work without seeing other subtasks
- "dependencies": list of other subtask titles this depends on (empty list if none)
- "estimated_files": list of file paths this subtask will likely touch
- "priority": integer (1 = highest). Same priority means tasks can run in parallel.

Rules:
- Make each subtask independently implementable in its own git branch
- Minimise file overlap between subtasks to avoid merge conflicts
- Include concrete file paths and clear acceptance criteria in each description
- Specify dependencies between subtasks by title
- Always include a final integration/testing subtask that depends on all others
- Return ONLY the JSON array, no markdown fences, no explanation"""

        raw = self._call_claude(prompt)
        subtasks = self._parse_subtasks_json(raw)
        return subtasks

    def _call_claude(self, prompt: str) -> str:
        cmd = ['claude', '-p', prompt]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=300)
        return result.stdout.strip()

    def _parse_subtasks_json(self, raw: str) -> List[SubTask]:
        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # Ask Claude to fix it
            fix_prompt = (
                "The following text was supposed to be a JSON array of subtask "
                "objects but it has syntax errors. Fix it and return ONLY the "
                f"corrected JSON array, nothing else:\n\n{raw}"
            )
            fixed_raw = self._call_claude(fix_prompt)
            fixed = fixed_raw.strip()
            if fixed.startswith("```"):
                flines = fixed.split("\n")
                flines = [l for l in flines if not l.strip().startswith("```")]
                fixed = "\n".join(flines)
            data = json.loads(fixed)

        subtasks = []
        for item in data:
            subtasks.append(SubTask(
                id=item.get('id', str(uuid.uuid4())[:8]),
                title=item['title'],
                description=item['description'],
                dependencies=item.get('dependencies', []),
                estimated_files=item.get('estimated_files', []),
                priority=item.get('priority', 99),
            ))
        return subtasks

    # -- Trello list and card creation ----------------------------------------

    def _create_subtask_list_and_cards(self, state: OrchestratorState):
        truncated = state.parent_card_name[:40]
        list_name = f"\U0001F916 Agents: {truncated}"
        board_id = TRELLO_BOARD_ID
        new_list = self.trello.create_list(board_id, list_name)
        state.subtask_list_id = new_list['id']

        for st_dict in state.subtasks:
            st = SubTask(**st_dict) if isinstance(st_dict, dict) else st_dict
            desc_body = (
                f"**Subtask:** {st.title}\n\n"
                f"**Priority:** {st.priority}\n"
                f"**Dependencies:** {', '.join(st.dependencies) if st.dependencies else 'None'}\n"
                f"**Target files:** {', '.join(st.estimated_files) if st.estimated_files else 'TBD'}\n\n"
                f"---\n\n{st.description}"
            )
            card = self.trello.create_card(state.subtask_list_id,
                                           st.title, desc_body)
            st.card_id = card['id']
            # write back
            if isinstance(st_dict, dict):
                st_dict['card_id'] = card['id']
            else:
                st_dict.card_id = card['id']

        # Post plan comment on parent card
        plan_lines = ["## \U0001F916 Orchestration Plan\n"]
        for i, sd in enumerate(state.subtasks, 1):
            s = sd if isinstance(sd, dict) else asdict(sd)
            deps = ", ".join(s.get('dependencies', [])) or "none"
            plan_lines.append(
                f"{i}. **{s['title']}** (priority {s['priority']}, "
                f"deps: {deps})"
            )
        plan_lines.append(
            "\n> Move this card off the orchestrator list to halt execution."
        )
        self.trello.add_comment(state.parent_card_id,
                                "\n".join(plan_lines))

    # -- dependency resolution ------------------------------------------------

    def _subtask_objs(self, state: OrchestratorState) -> List[SubTask]:
        return [SubTask(**s) if isinstance(s, dict) else s
                for s in state.subtasks]

    def _update_subtask(self, state: OrchestratorState, subtask_id: str,
                        **kwargs):
        for i, sd in enumerate(state.subtasks):
            s = sd if isinstance(sd, dict) else asdict(sd)
            if s.get('id') == subtask_id:
                if isinstance(sd, dict):
                    sd.update(kwargs)
                else:
                    for k, v in kwargs.items():
                        setattr(sd, k, v)
                    state.subtasks[i] = asdict(sd)
                return

    def _completed_titles(self, state: OrchestratorState) -> set:
        return {
            (s['title'] if isinstance(s, dict) else s.title)
            for s in state.subtasks
            if (s.get('status') if isinstance(s, dict) else s.status)
            == TaskStatus.COMPLETE.value
        }

    def _ready_subtasks(self, state: OrchestratorState) -> List[Dict]:
        completed = self._completed_titles(state)
        ready = []
        for sd in state.subtasks:
            s = sd if isinstance(sd, dict) else asdict(sd)
            if s['status'] != TaskStatus.PENDING.value:
                continue
            deps = s.get('dependencies', [])
            if all(d in completed for d in deps):
                ready.append(sd)
        # sort by priority
        ready.sort(key=lambda x: x.get('priority', 99)
                   if isinstance(x, dict) else x.priority)
        return ready

    def _mark_blocked(self, state: OrchestratorState, failed_title: str):
        """Mark all subtasks that transitively depend on a failed task as
        blocked."""
        for sd in state.subtasks:
            s = sd if isinstance(sd, dict) else asdict(sd)
            if s['status'] in (TaskStatus.PENDING.value, TaskStatus.READY.value):
                deps = s.get('dependencies', [])
                if failed_title in deps:
                    if isinstance(sd, dict):
                        sd['status'] = TaskStatus.BLOCKED.value
                    else:
                        sd.status = TaskStatus.BLOCKED.value

    # -- re-planning on failure -----------------------------------------------

    def _replan_on_failure(self, state: OrchestratorState,
                           failed_task: Dict) -> List[SubTask]:
        """Ask Claude whether to retry, add bridging tasks, or cancel
        downstream dependents. Returns new SubTask objects (if any)."""
        completed = self._completed_titles(state)
        summary = (
            f"Completed tasks: {', '.join(completed) if completed else 'none'}\n"
            f"Failed task: {failed_task.get('title', '?')}\n"
            f"Error: {failed_task.get('error', 'unknown')}\n"
        )
        pending = [
            (s.get('title') if isinstance(s, dict) else s.title)
            for s in state.subtasks
            if (s.get('status') if isinstance(s, dict) else s.status)
            in (TaskStatus.PENDING.value, TaskStatus.READY.value)
        ]
        summary += f"Pending tasks: {', '.join(pending) if pending else 'none'}\n"

        prompt = f"""A subtask in an automated code orchestration failed.

{summary}

Original parent task: {state.parent_card_name}

Decide ONE of:
1. RETRY — provide modified instructions for the failed task
2. BRIDGE — provide 1-2 new bridging subtasks that work around the failure
3. CANCEL — cancel all downstream dependents of the failed task

Return ONLY a JSON object with:
- "action": "retry" | "bridge" | "cancel"
- "modified_instructions": string (only for retry)
- "new_tasks": array of subtask objects (only for bridge, same schema as decomposition)
- "reason": brief explanation"""

        raw = self._call_claude(prompt)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)

        try:
            decision = json.loads(cleaned)
        except json.JSONDecodeError:
            print(f"[ORCH] Could not parse re-plan response, cancelling dependents.")
            self._mark_blocked(state, failed_task.get('title', ''))
            return []

        action = decision.get('action', 'cancel')

        if action == 'retry':
            new_desc = decision.get('modified_instructions',
                                    failed_task.get('description', ''))
            self._update_subtask(state, failed_task['id'],
                                 status=TaskStatus.PENDING.value,
                                 description=new_desc,
                                 error=None)
            print(f"[ORCH] Retrying task '{failed_task.get('title')}' "
                  f"with modified instructions.")
            return []

        elif action == 'bridge':
            new_tasks_raw = decision.get('new_tasks', [])
            new_subtasks = []
            for item in new_tasks_raw:
                st = SubTask(
                    id=item.get('id', str(uuid.uuid4())[:8]),
                    title=item['title'],
                    description=item['description'],
                    dependencies=item.get('dependencies', []),
                    estimated_files=item.get('estimated_files', []),
                    priority=item.get('priority', 50),
                )
                new_subtasks.append(st)
                state.subtasks.append(asdict(st))
                # Create Trello card for new task
                if state.subtask_list_id:
                    try:
                        card = self.trello.create_card(
                            state.subtask_list_id, st.title, st.description)
                        self._update_subtask(state, st.id,
                                             card_id=card['id'])
                    except Exception:
                        pass
            self._mark_blocked(state, failed_task.get('title', ''))
            print(f"[ORCH] Added {len(new_subtasks)} bridging tasks, "
                  f"blocked dependents of '{failed_task.get('title')}'.")
            return new_subtasks

        else:  # cancel
            self._mark_blocked(state, failed_task.get('title', ''))
            print(f"[ORCH] Cancelled dependents of "
                  f"'{failed_task.get('title')}'.")
            return []

    # -- agent lifecycle ------------------------------------------------------

    def _build_agent_prompt(self, state: OrchestratorState,
                            subtask: Dict) -> str:
        return (
            f"You are one of several coding agents working on a larger task.\n\n"
            f"## Parent Task\n"
            f"**{state.parent_card_name}**\n\n"
            f"## Your Subtask: {subtask['title']}\n\n"
            f"{subtask['description']}\n\n"
            f"## Target Files\n"
            f"{', '.join(subtask.get('estimated_files', [])) or 'Determine from the description.'}\n\n"
            f"## Instructions\n"
            f"- Only implement what is described above.\n"
            f"- Commit your changes with a message prefixed with "
            f"[{subtask['title']}].\n"
            f"- Do NOT push to remote.\n"
        )

    def _start_agent(self, state: OrchestratorState, subtask_dict: Dict):
        st_id = subtask_dict['id']
        branch_name = f"orch/{re.sub(r'[^a-z0-9-]', '-', st_id.lower())}-{state.orchestrator_id[:6]}"
        branch_name = branch_name[:50]

        self.git.fetch()
        self.git.create_branch(branch_name, state.parent_branch)
        wt_path = self.git.create_worktree(branch_name, st_id)

        prompt = self._build_agent_prompt(state, subtask_dict)
        future = self._executor.submit(
            _run_agent_in_worktree, wt_path, prompt, self.agent_timeout)
        self._futures[future] = st_id

        self._update_subtask(state, st_id,
                             status=TaskStatus.RUNNING.value,
                             agent_branch=branch_name,
                             worktree_path=wt_path,
                             started_at=datetime.now().isoformat())
        state.total_agents_spawned += 1
        print(f"[ORCH] Started agent for '{subtask_dict['title']}' "
              f"on branch {branch_name}")

    def _harvest_agents(self, state: OrchestratorState):
        done_futures = [f for f in self._futures if f.done()]
        for future in done_futures:
            st_id = self._futures.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                result = {'success': False, 'output': '',
                          'error': str(exc)}

            subtask_dict = None
            for sd in state.subtasks:
                s = sd if isinstance(sd, dict) else asdict(sd)
                if s['id'] == st_id:
                    subtask_dict = sd
                    break

            if subtask_dict is None:
                continue

            title = (subtask_dict['title'] if isinstance(subtask_dict, dict)
                     else subtask_dict.title)

            if result['success']:
                self._update_subtask(state, st_id,
                                     status=TaskStatus.COMPLETE.value,
                                     completed_at=datetime.now().isoformat(),
                                     result_summary=result['output'][:500])
                print(f"[ORCH] Agent completed: '{title}'")
                # Post result to subtask Trello card
                card_id = (subtask_dict.get('card_id')
                           if isinstance(subtask_dict, dict)
                           else subtask_dict.card_id)
                if card_id:
                    try:
                        self.trello.add_comment(
                            card_id,
                            f"**Agent completed successfully.**\n\n"
                            f"```\n{result['output'][:2000]}\n```"
                        )
                    except Exception:
                        pass
            else:
                error_msg = result.get('error', 'unknown error')
                self._update_subtask(state, st_id,
                                     status=TaskStatus.FAILED.value,
                                     completed_at=datetime.now().isoformat(),
                                     error=error_msg)
                print(f"[ORCH] Agent failed: '{title}': {error_msg}")
                card_id = (subtask_dict.get('card_id')
                           if isinstance(subtask_dict, dict)
                           else subtask_dict.card_id)
                if card_id:
                    try:
                        self.trello.add_comment(
                            card_id,
                            f"**Agent FAILED.**\n\nError: {error_msg}\n\n"
                            f"```\n{result['output'][:2000]}\n```"
                        )
                    except Exception:
                        pass

    # -- merge phase ----------------------------------------------------------

    def _merge_all(self, state: OrchestratorState):
        state.phase = OrchestratorPhase.MERGING.value
        self._save_state(state)

        # Checkout parent branch in a worktree for merging
        parent_branch = state.parent_branch
        merge_wt = self.git.create_worktree(parent_branch,
                                            f"merge-{state.orchestrator_id[:8]}")

        # Pull latest on parent branch
        subprocess.run(['git', 'pull', 'origin', parent_branch],
                       cwd=merge_wt, capture_output=True)

        sorted_tasks = sorted(
            [s for s in state.subtasks
             if (s.get('status') if isinstance(s, dict) else s.status)
             == TaskStatus.COMPLETE.value],
            key=lambda x: x.get('priority', 99)
            if isinstance(x, dict) else x.priority,
        )

        for sd in sorted_tasks:
            s = sd if isinstance(sd, dict) else asdict(sd)
            branch = s.get('agent_branch')
            if not branch or s.get('merged'):
                continue

            print(f"[ORCH] Merging branch {branch}…")
            result = self.git.merge_branch(branch, cwd=merge_wt)

            if self.git.has_conflicts(cwd=merge_wt):
                print(f"[ORCH] Merge conflict on {branch}, "
                      f"attempting auto-resolution…")
                resolve_prompt = (
                    "Resolve ALL git merge conflict markers in this repository. "
                    "Look at every file with conflict markers (<<<<<<< ======= >>>>>>>) "
                    "and produce a clean resolution that preserves the intent of both sides. "
                    "Stage the resolved files with git add."
                )
                resolve_result = _run_agent_in_worktree(
                    merge_wt, resolve_prompt, timeout_seconds=300)
                if resolve_result['success'] and not self.git.has_conflicts(
                        cwd=merge_wt):
                    self.git.commit_all(
                        f"Resolved merge conflicts for {branch}",
                        cwd=merge_wt)
                    if isinstance(sd, dict):
                        sd['merged'] = True
                    print(f"[ORCH] Conflicts resolved for {branch}")
                else:
                    self.git.abort_merge(cwd=merge_wt)
                    print(f"[ORCH] Could not resolve conflicts for {branch}, "
                          f"skipping.")
            else:
                if isinstance(sd, dict):
                    sd['merged'] = True
                print(f"[ORCH] Merged {branch} cleanly.")

            # Clean up the subtask worktree
            wt = s.get('worktree_path')
            if wt:
                self.git.remove_worktree(wt)

        # Push parent branch
        self.git.push(parent_branch, cwd=merge_wt)

        # Clean up merge worktree
        self.git.remove_worktree(merge_wt)

    def _create_pr(self, state: OrchestratorState):
        """Create a BitBucket PR for the parent branch."""
        state.phase = OrchestratorPhase.REVIEWING.value
        self._save_state(state)

        if not BITBUCKET_ACCESS_TOKEN:
            print("[ORCH] No BitBucket token configured — skipping PR creation.")
            return

        merged_tasks = [
            (s.get('title') if isinstance(s, dict) else s.title)
            for s in state.subtasks
            if (s.get('merged') if isinstance(s, dict) else s.merged)
        ]
        description = (
            f"## Orchestrated Implementation: {state.parent_card_name}\n\n"
            f"### Completed Subtasks\n"
        )
        for t in merged_tasks:
            description += f"- {t}\n"
        description += (
            f"\n*Auto-generated by the orchestrator. "
            f"Orchestrator ID: {state.orchestrator_id}*"
        )

        bb_url = (f"https://api.bitbucket.org/2.0/repositories/"
                  f"{BITBUCKET_WORKSPACE}/{BITBUCKET_REPO_SLUG}/pullrequests")
        headers = {
            'Authorization': f'Bearer {BITBUCKET_ACCESS_TOKEN}',
            'Content-Type': 'application/json',
        }
        payload = {
            'title': f"[Orchestrated] {state.parent_card_name[:60]}",
            'source': {'branch': {'name': state.parent_branch}},
            'description': description,
        }
        try:
            resp = requests.post(bb_url, headers=headers,
                                 json=payload, timeout=30)
            if resp.status_code in (200, 201):
                pr_data = resp.json()
                pr_url = pr_data.get('links', {}).get('html', {}).get('href', '')
                print(f"[ORCH] PR created: {pr_url}")
                return pr_url
            else:
                print(f"[ORCH] PR creation returned {resp.status_code}: "
                      f"{resp.text[:300]}")
        except Exception as exc:
            print(f"[ORCH] PR creation error: {exc}")
        return None

    # -- completion -----------------------------------------------------------

    def _complete(self, state: OrchestratorState):
        state.phase = OrchestratorPhase.COMPLETE.value
        self._save_state(state)

        completed = [
            s for s in state.subtasks
            if (s.get('status') if isinstance(s, dict) else s.status)
            == TaskStatus.COMPLETE.value
        ]
        failed = [
            s for s in state.subtasks
            if (s.get('status') if isinstance(s, dict) else s.status)
            == TaskStatus.FAILED.value
        ]
        final = (
            f"## {self.BOT_TAG} Orchestration Complete\n\n"
            f"- **Completed subtasks:** {len(completed)}\n"
            f"- **Failed subtasks:** {len(failed)}\n"
            f"- **Total agents spawned:** {state.total_agents_spawned}\n"
            f"- **Branch:** `{state.parent_branch}`\n"
        )
        self._post_status(state, extra=final)

        # Move card back to original list
        target_list = state.original_list_id or TRELLO_LIST_ID
        if target_list:
            try:
                self.trello.move_card(state.parent_card_id, target_list)
                print(f"[ORCH] Moved card back to list {target_list}")
            except Exception as exc:
                print(f"[ORCH] Warning: could not move card back: {exc}")

    def _handle_stop(self, state: OrchestratorState):
        state.phase = OrchestratorPhase.STOPPED.value
        self._save_state(state)

        # Wait for running agents to finish but don't start new ones
        if self._futures:
            print(f"[ORCH] Waiting for {len(self._futures)} active agents "
                  f"to finish…")
            concurrent.futures.wait(self._futures.keys(),
                                    timeout=self.agent_timeout + 60)
            self._harvest_agents(state)
            self._save_state(state)

        self._post_status(
            state,
            extra="**Orchestration stopped by user** (card moved off list). "
                  "Worktrees left intact for manual inspection."
        )

    # -- terminal check -------------------------------------------------------

    def _all_terminal(self, state: OrchestratorState) -> bool:
        terminal = {TaskStatus.COMPLETE.value, TaskStatus.FAILED.value,
                    TaskStatus.BLOCKED.value, TaskStatus.CANCELLED.value}
        return all(
            (s.get('status') if isinstance(s, dict) else s.status)
            in terminal
            for s in state.subtasks
        )

    # -- main orchestration loop ----------------------------------------------

    def orchestrate(self, card_id: str):
        """Full orchestration lifecycle for a single Trello card."""
        self._register_signals()

        # Load or create state
        state = self._load_state(card_id)
        resuming = state is not None

        if not resuming:
            card = self.trello.get_card(card_id)
            orch_id = str(uuid.uuid4())[:12]
            parent_branch = f"orch/{re.sub(r'[^a-z0-9-]', '-', card['name'][:30].lower())}-{orch_id}"

            state = OrchestratorState(
                orchestrator_id=orch_id,
                parent_card_id=card_id,
                parent_card_name=card['name'],
                parent_branch=parent_branch,
                original_list_id=card.get('idList')
                if card.get('idList') != TRELLO_ORCHESTRATOR_LIST_ID
                else TRELLO_LIST_ID,
            )

            # Planning phase
            state.phase = OrchestratorPhase.PLANNING.value
            self._save_state(state)

            print(f"[ORCH] Decomposing task: {card['name']}")
            attachments = ""
            try:
                atts = self.trello.get_card_attachments(card_id)
                if atts:
                    attachments = "\n".join(
                        f"- {a['name']} ({a.get('mimeType', '?')})"
                        for a in atts)
            except Exception:
                pass

            subtasks = self._decompose_task(card['name'], card.get('desc', ''),
                                            attachments)
            state.subtasks = [asdict(st) for st in subtasks]
            self._save_state(state)

            print(f"[ORCH] Created {len(subtasks)} subtasks")

            # Create parent branch
            self.git.fetch()
            self.git.create_branch(parent_branch)
            self.git.push(parent_branch)

            # Create Trello list and cards
            self._create_subtask_list_and_cards(state)
            self._save_state(state)

        else:
            print(f"[ORCH] Resuming orchestration for card {card_id}, "
                  f"phase={state.phase}")

        # Execution phase
        state.phase = OrchestratorPhase.EXECUTING.value
        self._save_state(state)

        self._executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=self.max_agents)
        cycle = 0

        try:
            while True:
                cycle += 1
                if self.debug:
                    print(f"[ORCH] Poll cycle {cycle}")

                # 1. Stop check
                if self._check_for_stop(state):
                    self._handle_stop(state)
                    return

                # 2. Harvest completed agents
                self._harvest_agents(state)
                self._save_state(state)

                # 3. Re-plan on failures
                for sd in list(state.subtasks):
                    s = sd if isinstance(sd, dict) else asdict(sd)
                    if s['status'] == TaskStatus.FAILED.value and not s.get('_replanned'):
                        if isinstance(sd, dict):
                            sd['_replanned'] = True
                        self._replan_on_failure(state, s)
                        self._save_state(state)

                # 4. Check if all tasks reached terminal state
                if self._all_terminal(state):
                    break

                # 5. Start ready agents (fill available slots)
                running_count = sum(
                    1 for s in state.subtasks
                    if (s.get('status') if isinstance(s, dict) else s.status)
                    == TaskStatus.RUNNING.value
                )
                available_slots = self.max_agents - running_count
                ready = self._ready_subtasks(state)

                for sd in ready[:available_slots]:
                    s = sd if isinstance(sd, dict) else asdict(sd)
                    self._start_agent(state, s)
                    self._save_state(state)

                # 6. Post status every 5 cycles
                if cycle % 5 == 0:
                    self._post_status(state)
                    self._save_state(state)

                # Sleep
                time.sleep(self.poll_interval)

        except Exception as exc:
            state.phase = OrchestratorPhase.FAILED.value
            self._save_state(state)
            print(f"[ORCH] Orchestration failed: {exc}")
            traceback.print_exc()
            self._post_status(state, extra=f"**Orchestration error:** {exc}")
            return
        finally:
            if self._executor:
                self._executor.shutdown(wait=False)

        # Merge phase
        print("[ORCH] All subtasks reached terminal state. Starting merge…")
        try:
            self._merge_all(state)
            self._save_state(state)
        except Exception as exc:
            print(f"[ORCH] Merge phase error: {exc}")
            traceback.print_exc()

        # PR creation
        pr_url = self._create_pr(state)

        # Completion
        self._complete(state)
        if pr_url:
            try:
                self.trello.add_comment(
                    state.parent_card_id,
                    f"**Pull Request created:** {pr_url}")
            except Exception:
                pass

        print(f"[ORCH] Orchestration complete for '{state.parent_card_name}'")


def watch_for_orchestration_cards(max_agents: int = 3,
                                  poll_interval: int = 30,
                                  debug: bool = False):
    """Continuously poll the TRELLO_ORCHESTRATOR_LIST_ID for cards and
    orchestrate them one at a time."""
    trello = TrelloAPI(TRELLO_API_KEY, TRELLO_TOKEN, debug=debug)
    git = GitHelper(GIT_REPO_PATH, WORKTREE_BASE_DIR, debug=debug)
    orchestrator = Orchestrator(trello, git, max_agents=max_agents,
                                poll_interval=poll_interval, debug=debug)
    seen: set = set()

    # Load already-orchestrated card IDs from state files
    if os.path.isdir(ORCHESTRATOR_STATE_DIR):
        for fname in os.listdir(ORCHESTRATOR_STATE_DIR):
            if fname.endswith('.json'):
                seen.add(fname.replace('.json', ''))

    print(f"[ORCH-WATCH] Watching list {TRELLO_ORCHESTRATOR_LIST_ID} "
          f"for orchestration cards…")

    while True:
        try:
            cards = trello.get_cards_on_list(TRELLO_ORCHESTRATOR_LIST_ID)
            for card in cards:
                cid = card['id']
                if cid in seen:
                    continue
                print(f"[ORCH-WATCH] New card detected: "
                      f"'{card['name']}' ({cid})")
                seen.add(cid)

                # Store original list before we start (card is already on
                # the orchestrator list so default to TRELLO_LIST_ID)
                orchestrator.orchestrate(cid)
        except KeyboardInterrupt:
            print("\n[ORCH-WATCH] Interrupted. Exiting.")
            break
        except Exception as exc:
            print(f"[ORCH-WATCH] Error: {exc}")
            traceback.print_exc()

        time.sleep(60)


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

    # Validate common requirements
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


if __name__ == '__main__':
    # Dispatch: if the first positional arg is "orchestrate", run the
    # orchestrator CLI; otherwise run the original workflow.
    if len(sys.argv) > 1 and sys.argv[1] == 'orchestrate':
        sys.argv = [sys.argv[0]] + sys.argv[2:]  # strip the subcommand
        orchestrator_main()
    else:
        main()
