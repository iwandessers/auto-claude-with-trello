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
        
    def ensure_directories(self):
        """Create necessary directories if they don't exist."""
        os.makedirs(WORKFLOW_STATE_DIR, exist_ok=True)
        os.makedirs(WORKTREE_BASE_DIR, exist_ok=True)
        os.makedirs(CARDS_STATE_DIR, exist_ok=True)
    
    def get_card_state_file(self, card_id: str) -> str:
        """Get the state file path for a specific card."""
        return os.path.join(CARDS_STATE_DIR, f"{card_id}.json")
    
    def load_card_state(self, card_id: str) -> Dict:
        """Load state for a specific card."""
        state_file = self.get_card_state_file(card_id)
        if os.path.exists(state_file):
            with open(state_file, 'r') as f:
                return json.load(f)
        return {
            'card_id': card_id,
            'branch': None,
            'pr_url': None,
            'pr_id': None,
            'last_update': None,
            'processed_comments': [],
            'processed_pr_comments': [],
            'created_at': datetime.now().isoformat()
        }
    
    def save_card_state(self, card_id: str, state: Dict):
        """Save state for a specific card."""
        state_file = self.get_card_state_file(card_id)
        state['last_update'] = datetime.now().isoformat()
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
            return None
            
        url = f"{self.bb_base_url}/pullrequests"
        params = {
            'q': f'source.branch.name="{branch_name}"',
            'state': 'OPEN'
        }
        
        try:
            response = requests.get(url, headers=self.bb_headers, params=params)
            if response.status_code == 200:
                data = response.json()
                if data['values']:
                    return data['values'][0]
        except requests.exceptions.RequestException as e:
            error_message = f"Error fetching PR: {e}"
            if e.response is not None:
                try:
                    error_details = e.response.json()
                    error_message += f" - Details: {error_details.get('error', {}).get('message', e.response.text)}"
                except ValueError: # Not a JSON response
                    error_message += f" - Status: {e.response.status_code}, Content: {e.response.text[:200]}" # Log first 200 chars
            print(error_message)
        
        return None
    
    def get_pr_comments(self, pr_id: int) -> List[Dict]:
        """Fetch all comments from a BitBucket PR."""
        if not self.bb_headers:
            return []
            
        url = f"{self.bb_base_url}/pullrequests/{pr_id}/comments"
        comments = []
        initial_request = True # Flag for the first request
        
        try:
            while url:
                if initial_request:
                    params = {'pagelen': 50}
                    response = requests.get(url, headers=self.bb_headers, params=params)
                    initial_request = False # Unset flag after first request
                else:
                    # Subsequent requests use the 'next' URL which includes pagination parameters
                    response = requests.get(url, headers=self.bb_headers)

                if response.status_code == 200:
                    data = response.json()
                    comments.extend(data.get('values', []))
                    url = data.get('next')
                else:
                    # Log error for non-200 responses if needed, then break
                    error_message = f"Error fetching PR comments: Status {response.status_code}"
                    try:
                        error_details = response.json()
                        error_message += f" - Details: {error_details.get('error', {}).get('message', response.text)}"
                    except ValueError:
                        error_message += f" - Content: {response.text[:200]}"
                    print(error_message)
                    break
        except requests.exceptions.RequestException as e:
            error_message = f"Error fetching PR comments: {e}"
            if e.response is not None:
                try:
                    error_details = e.response.json()
                    error_message += f" - Details: {error_details.get('error', {}).get('message', e.response.text)}"
                except ValueError: # Not a JSON response
                    error_message += f" - Status: {e.response.status_code}, Content: {e.response.text[:200]}"
            print(error_message)
                    
        return comments
    
    def add_pr_comment(self, pr_id: int, comment: str):
        """Add a comment to a BitBucket PR."""
        if not self.bb_headers:
            return
            
        url = f"{self.bb_base_url}/pullrequests/{pr_id}/comments"
        data = {
            'content': {
                'raw': comment
            }
        }
        
        try:
            response = requests.post(url, headers=self.bb_headers, json=data)
            response.raise_for_status() # Will raise an HTTPError for bad responses (4xx or 5xx)
        except requests.exceptions.RequestException as e:
            error_message = f"Error adding PR comment: {e}"
            if e.response is not None:
                try:
                    error_details = e.response.json()
                    error_message += f" - Details: {error_details.get('error', {}).get('message', e.response.text)}"
                except ValueError: # Not a JSON response
                    error_message += f" - Status: {e.response.status_code}, Content: {e.response.text[:200]}"
            print(error_message)
    
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
        
        response = requests.get(url, params=params)
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
    
    # === Git and Claude Code Methods ===
    
    def create_branch_name(self, card_name: str, card_id: str) -> str:
        """Create a valid git branch name from card title and ID."""
        # Clean the card name
        branch = re.sub(r'[^a-zA-Z0-9\s-]', '', card_name)
        branch = branch.replace(' ', '-').lower()
        branch = re.sub(r'-+', '-', branch)
        # Include last 6 chars of card ID for uniqueness
        return f"feature/{branch}-{card_id[-6:]}"[:50]
    
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
    
    def execute_claude_code(self, instructions: str, worktree_path: str) -> str:
        """Execute instructions using Claude Code in the specified directory."""
        # Debug logging
        if self.debug:
            print(f"\n[DEBUG] Executing Claude Code with instruction length: {len(instructions)} characters")
            print(f"[DEBUG] First 200 chars of instruction: {instructions[:200]}...")
            if len(instructions) > 10000:
                print(f"[WARNING] Very long instruction detected: {len(instructions)} characters!")
        
        # Escape quotes in instructions for shell command
        escaped_instructions = instructions.replace('"', '\\"')
        
        result = subprocess.run(
            ['claude', '--dangerously-skip-permissions', '-p', escaped_instructions],
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
            ['git', 'push'],
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
        
        # Create branch
        branch_name = self.create_branch_name(card_name, card_id)
        worktree_path, push_output = self.create_worktree(branch_name, card_id)
        
        # Update state with branch info
        card_state['branch'] = branch_name
        card_state['worktree_path'] = worktree_path
        card_state['card_name'] = card_name
        
        # Execute Claude Code with description
        claude_instruction = f"Analyse the changes made in this git branch. Use this knowledge to process the following feedback.\n{description}"
        claude_output = self.execute_claude_code(claude_instruction, worktree_path)
        
        # Commit and push
        commit_output, pr_url = self.commit_and_push(
            worktree_path,
            f"Initial implementation for: {card_name}",
            card_id
        )
        
        # Extract PR URL from initial push if not found in commit push
        if not pr_url and push_output:
            pr_match = re.search(r'https://bitbucket\.org/[^\s]+/pull-requests/new[^\s]*', push_output)
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

"""
        
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
            
            # Skip bot comments - check multiple indicators
            is_bot_comment = (
                '🤖' in comment_text or
                'Automated Workflow Update:' in comment_text or
                'Processed BitBucket PR comment:' in comment_text or
                'Processed Trello comment update:' in comment_text or
                'Claude Code Response' in comment_text or
                'Claude Code Output:' in comment_text
            )
            
            if is_bot_comment:
                print(f"Skipping Trello comment (bot comment detected)")
                card_state['processed_comments'].append(comment['id'])
                continue
            
            claude_instruction = f"Analyse the changes made in this git branch. Use this knowledge to process the following feedback.\n{comment_text}"
            claude_output = self.execute_claude_code(claude_instruction, worktree_path)
            
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
"""
            
            self.add_comment_to_card(card_id, response_comment)
            card_state['processed_comments'].append(comment['id'])
        
        # Save updated state
        self.save_card_state(card_id, card_state)
    
    
    def process_pr_comments(self, card_id: str, card_state: Dict):
        """Process new comments from BitBucket PR as Claude Code instructions."""
        branch_name = card_state['branch']
        
        # Find PR for this branch
        pr_data = self.get_pr_by_branch(branch_name)
        if not pr_data:
            return
        
        pr_id = pr_data['id']
        
        # Update PR ID in state if not set
        if not card_state.get('pr_id'):
            card_state['pr_id'] = pr_id
            self.save_card_state(card_id, card_state)
        
        # Get all PR comments
        pr_comments = self.get_pr_comments(pr_id)
        
        # Filter for new comments
        processed_pr_ids = set(card_state.get('processed_pr_comments', []))
        new_pr_comments = [c for c in pr_comments if str(c['id']) not in processed_pr_ids]
        
        if not new_pr_comments:
            return
        
        print(f"Found {len(new_pr_comments)} new BitBucket PR comments for card: {card_id}")
        
        worktree_path = self.checkout_worktree(branch_name, card_id)
        
        for comment in new_pr_comments:
            # Extract all comment information
            comment_text = comment.get('content', {}).get('raw', '')
            comment_html = comment.get('content', {}).get('html', '')
            comment_markup = comment.get('content', {}).get('markup', '')
            
            # Extract author information
            author_info = comment.get('user', {})
            author_display_name = author_info.get('display_name', 'Unknown')
            author_username = author_info.get('username', '')
            author_uuid = author_info.get('uuid')
            author_account_id = author_info.get('account_id', '')
            
            # Extract metadata
            comment_id = str(comment['id'])
            created_on = comment.get('created_on', '')
            updated_on = comment.get('updated_on', '')
            
            # Extract parent comment info if this is a reply
            parent_id = comment.get('parent', {}).get('id', '') if comment.get('parent') else ''
            
            # Extract inline info if this is an inline comment
            inline_info = comment.get('inline', {})
            inline_path = inline_info.get('path', '') if inline_info else ''
            inline_from = inline_info.get('from', '') if inline_info else ''
            inline_to = inline_info.get('to', '') if inline_info else ''

            # Skip if comment is empty
            if not comment_text.strip():
                card_state['processed_pr_comments'].append(comment_id)
                continue

            # Skip if comment is from the bot itself
            # Check multiple indicators:
            # 1. Bot in display name
            # 2. Starts with bot emoji
            # 3. Contains specific auto-claude patterns
            is_bot_comment = (
                'bot' in author_display_name.lower() or 
                comment_text.strip().startswith('🤖') or
                'Automated Workflow Update:' in comment_text or
                'Processed BitBucket PR comment:' in comment_text or
                'Processed Trello comment update:' in comment_text or
                'Claude Code Response' in comment_text
            )
            
            if is_bot_comment:
                print(f"Skipping comment {comment_id} by {author_display_name} (bot comment detected)")
                card_state['processed_pr_comments'].append(comment_id)
                continue

            print(f"Executing PR comment from {author_display_name}: {comment_text[:50]}...")
            
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
            
            # Execute as Claude Code instruction with full context
            claude_instruction = f"Analyse the changes made in this git branch. Use this knowledge to process the following feedback.\n{comment_context}"
            claude_output = self.execute_claude_code(claude_instruction, worktree_path)
            
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
"""
            
            # Add to PR
            self.add_pr_comment(pr_id, response_text)
            
            # Add to Trello
            self.add_comment_to_card(card_id, response_text)
            
            # Mark as processed
            card_state['processed_pr_comments'].append(comment_id)
        
        # Save updated state
        self.save_card_state(card_id, card_state)
    
    def run(self):
        """Main workflow loop - check for new cards and comments from both Trello and BitBucket."""
        print(f"Starting workflow check at {datetime.now()}")
        print(f"Git repo: {GIT_REPO_PATH}")
        print(f"State directory: {WORKFLOW_STATE_DIR}")
        
        try:
            # Load all existing card states
            all_card_states = self.get_all_card_states()
            
            # Get current cards from Trello
            cards = self.get_trello_cards()
            
            for card in cards:
                card_id = card['id']
                
                if card_id not in all_card_states:
                    # New card found
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
                        self.process_pr_comments(card_id, card_state)
            
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


def main():
    """Run the workflow once or in a loop."""
    parser = argparse.ArgumentParser(description='Trello and BitBucket automation workflow')
    parser.add_argument('--loop', action='store_true', help='Run in loop mode')
    parser.add_argument('--cleanup', action='store_true', help='Clean up worktrees only')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    args = parser.parse_args()
    
    # Clean up any orphaned worktrees on startup
    cleanup_worktrees()
    
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


if __name__ == '__main__':
    main()