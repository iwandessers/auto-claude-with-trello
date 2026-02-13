"""Original extended workflow automation.

Monitors a Trello list for cards, processes card comments and BitBucket PR
comments as Claude Code instructions, and pushes results back.
"""

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from auto_claude_trello.config import (
    TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_LIST_ID,
    BITBUCKET_ACCESS_TOKEN, BITBUCKET_WORKSPACE, BITBUCKET_REPO_SLUG,
    GIT_REPO_PATH, WORKFLOW_STATE_DIR, WORKTREE_BASE_DIR,
    CARDS_STATE_DIR, ATTACHMENTS_BASE_DIR,
)


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
            'fields': 'id,name,url,mimeType,bytes'
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
        attachments_dir = os.path.join(ATTACHMENTS_BASE_DIR, card_id)
        os.makedirs(attachments_dir, exist_ok=True)

        filename = attachment['name']
        local_path = os.path.join(attachments_dir, filename)

        if os.path.exists(local_path):
            if self.debug:
                print(f"[DEBUG] Attachment already exists: {local_path}")
            return local_path

        try:
            if self.debug:
                print(f"[DEBUG] Attachment object: {attachment}")
                print(f"[DEBUG] Attempting to download from URL: {attachment.get('url', 'NO URL FIELD')}")

            download_url = attachment.get('url')
            if not download_url:
                print(f"Error: No 'url' field in attachment object for '{filename}'")
                return None

            headers = {
                'Authorization': f'OAuth oauth_consumer_key="{TRELLO_API_KEY}", oauth_token="{TRELLO_TOKEN}"'
            }

            if self.debug:
                print(f"[DEBUG] Download URL: {download_url}")
                print(f"[DEBUG] Using OAuth Authorization header")

            response = requests.get(download_url, headers=headers)
            response.raise_for_status()

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
        """Create a valid git branch name from card title and session ID."""
        branch = re.sub(r'[^a-zA-Z0-9\s-]', '', card_name)
        branch = branch.replace(' ', '-').lower()
        branch = re.sub(r'-+', '-', branch)
        session_short = session_id[:8]
        return f"feature/{branch}-{session_short}"[:50]

    def create_worktree(self, branch_name: str, card_id: str) -> Tuple[str, str]:
        """Create a new git worktree for the branch."""
        worktree_path = os.path.join(WORKTREE_BASE_DIR, f"{card_id}_{branch_name.replace('/', '_')}")

        print("Fetching latest from origin before any operations...")
        subprocess.run(
            ['git', 'fetch', 'origin'],
            cwd=GIT_REPO_PATH,
            capture_output=True
        )

        result = subprocess.run(
            ['git', 'branch', branch_name],
            cwd=GIT_REPO_PATH,
            capture_output=True
        )

        subprocess.run(
            ['git', 'worktree', 'add', worktree_path, branch_name],
            cwd=GIT_REPO_PATH,
            check=True
        )

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

        print("Fetching latest from origin before any operations...")
        subprocess.run(
            ['git', 'fetch', 'origin'],
            cwd=GIT_REPO_PATH,
            capture_output=True
        )

        if not os.path.exists(worktree_path):
            subprocess.run(
                ['git', 'worktree', 'add', worktree_path, branch_name],
                cwd=GIT_REPO_PATH,
                check=True
            )

        subprocess.run(
            ['git', 'pull'],
            cwd=worktree_path,
            capture_output=True
        )

        return worktree_path

    def execute_claude_code(self, instructions: str, worktree_path: str,
                            session_id: Optional[str] = None,
                            is_first_interaction: bool = True) -> str:
        """Execute instructions using Claude Code in the specified directory."""
        if self.debug:
            print(f"\n[DEBUG] Executing Claude Code with instruction length: {len(instructions)} characters")
            print(f"[DEBUG] First 200 chars of instruction: {instructions[:200]}...")
            print(f"[DEBUG] Session ID: {session_id if session_id else 'None (new session)'}")
            print(f"[DEBUG] Is first interaction: {is_first_interaction}")
            if len(instructions) > 10000:
                print(f"[WARNING] Very long instruction detected: {len(instructions)} characters!")

        escaped_instructions = instructions.replace('"', '\\"')

        cmd = ['claude', '--dangerously-skip-permissions']
        if session_id:
            if is_first_interaction:
                cmd.extend(['--session-id', session_id])
            else:
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
            if "Prompt is too long" in result.stderr and self.debug:
                print(f"[ERROR] Claude reported 'Prompt is too long' for instruction of {len(instructions)} characters")
        return output

    def commit_and_push(self, worktree_path: str, message: str,
                        card_id: str) -> Tuple[str, Optional[str]]:
        """Commit all changes and push to remote, extracting PR URL if present."""
        output = []
        pr_url = None

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

        card_state = self.load_card_state(card_id)

        if not card_state.get('session_id'):
            card_state['session_id'] = str(uuid.uuid4())
            if self.debug:
                print(f"[DEBUG] Generated new session ID: {card_state['session_id']}")

        branch_name = self.create_branch_name(card_name, card_state['session_id'])
        worktree_path, push_output = self.create_worktree(branch_name, card_id)

        card_state['branch'] = branch_name
        card_state['worktree_path'] = worktree_path
        card_state['card_name'] = card_name

        attachment_context = self.process_attachments(card_id)

        claude_instruction = f"{description}{attachment_context}"
        claude_output = self.execute_claude_code(
            claude_instruction,
            worktree_path,
            card_state.get('session_id'),
            is_first_interaction=True
        )

        commit_output, pr_url = self.commit_and_push(
            worktree_path,
            f"Initial implementation for: {card_name}",
            card_id
        )

        if not pr_url and push_output:
            pr_match = re.search(r'https://bitbucket\.org/[^\s]+/pull-requests/new[^\s]*', push_output)
            if pr_match:
                pr_url = pr_match.group(0)
            else:
                pr_match = re.search(r'https://github\.com/[^\s]+/pull/new/[^\s]*', push_output)
                if pr_match:
                    pr_url = pr_match.group(0)

        if pr_url:
            card_state['pr_url'] = pr_url

        comment = f"""\U0001f916 Automated Workflow Update:

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
            comment += f"\n\U0001f4c4 Create Pull Request: {pr_url}"
        else:
            comment += "\n\u26a0\ufe0f No PR URL found in push output. You may need to create it manually."

        self.add_comment_to_card(card_id, comment)

        self.save_card_state(card_id, card_state)

    def process_card_comments(self, card: Dict, comments: List[Dict],
                              card_state: Dict):
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

            has_mentions = bool(re.search(r'@\w+', comment_text))
            if has_mentions:
                print(f"Skipping Trello comment (contains user mentions)")
                card_state['processed_comments'].append(comment['id'])
                continue

            is_bot_comment = self.bot_signature in comment_text

            if is_bot_comment:
                print(f"Skipping Trello comment (bot comment detected)")
                card_state['processed_comments'].append(comment['id'])
                continue

            attachment_context = self.process_attachments(card_id)

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

            response_comment = f"""\U0001f916 Processed Trello comment update:

{claude_output}

Git Operations:
```
{commit_output}
```

Pull Request: {card_state.get('pr_url', 'Create PR manually from BitBucket')}

{self.bot_signature}"""

            self.add_comment_to_card(card_id, response_comment)
            card_state['processed_comments'].append(comment['id'])

        self.save_card_state(card_id, card_state)


    def process_pr_comments(self, card_id: str, card_state: Dict):
        """Process new comments from BitBucket PR as Claude Code instructions."""
        branch_name = card_state['branch']

        if self.debug:
            print(f"\n[DEBUG] process_pr_comments - Starting for card: {card_id}")
            print(f"[DEBUG] Branch name: {branch_name}")
            print(f"[DEBUG] Current PR ID in state: {card_state.get('pr_id', 'Not set')}")

        pr_data = self.get_pr_by_branch(branch_name)
        if not pr_data:
            if self.debug:
                print(f"[DEBUG] No PR found for branch {branch_name}, skipping PR comment processing")
            return

        pr_id = pr_data['id']

        if self.debug:
            print(f"[DEBUG] Found PR ID: {pr_id}")

        if not card_state.get('pr_id'):
            card_state['pr_id'] = pr_id
            self.save_card_state(card_id, card_state)
            if self.debug:
                print(f"[DEBUG] Updated card state with PR ID: {pr_id}")

        pr_comments = self.get_pr_comments(pr_id)

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
            comment_id = str(comment['id'])

            try:
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

                if not comment_text.strip():
                    if 'processed_pr_comments' not in card_state:
                        card_state['processed_pr_comments'] = []
                    card_state['processed_pr_comments'].append(str(comment_id))
                    continue

                is_deleted = comment.get('deleted', False)
                if is_deleted:
                    print(f"Skipping comment {comment_id} by {author_display_name} (deleted comment)")
                    if 'processed_pr_comments' not in card_state:
                        card_state['processed_pr_comments'] = []
                    card_state['processed_pr_comments'].append(str(comment_id))
                    continue

                is_bot_comment = self.bot_signature in comment_text

                if is_bot_comment:
                    print(f"Skipping comment {comment_id} by {author_display_name} (bot comment detected)")
                    if 'processed_pr_comments' not in card_state:
                        card_state['processed_pr_comments'] = []
                    card_state['processed_pr_comments'].append(str(comment_id))
                    continue

                print(f"Processing PR comment ID: {comment_id} from {author_display_name}: {comment_text[:50]}...")

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

                attachment_context = self.process_attachments(card_id)

                claude_instruction = f"Analyse the changes made in this git branch. Use this knowledge to process the following feedback.\n{comment_context}{attachment_context}"
                claude_output = self.execute_claude_code(
                    claude_instruction,
                    worktree_path,
                    card_state.get('session_id'),
                    is_first_interaction=False
                )

                commit_output, _ = self.commit_and_push(
                    worktree_path,
                    f"Update from PR comment by {author_display_name}: {comment_text[:50]}...",
                    card_id
                )

                response_text = f"""\U0001f916 Processed BitBucket PR comment:

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

                self.add_pr_comment(pr_id, response_text)

                self.add_comment_to_card(card_id, response_text)

            except Exception as e:
                print(f"Error processing comment {comment_id}: {e}")
                import traceback
                traceback.print_exc()
            finally:
                if 'processed_pr_comments' not in card_state:
                    card_state['processed_pr_comments'] = []
                if str(comment_id) not in card_state['processed_pr_comments']:
                    card_state['processed_pr_comments'].append(str(comment_id))

        if self.debug:
            print(f"[DEBUG] Saving card state with {len(card_state.get('processed_pr_comments', []))} processed PR comments")
            print(f"[DEBUG] Processed PR comment IDs being saved: {sorted([str(id) for id in card_state.get('processed_pr_comments', [])])}")
        self.save_card_state(card_id, card_state)

    def run(self):
        """Main workflow loop - check for new cards and comments from both Trello and BitBucket."""
        print(f"Starting workflow check at {datetime.now()}")
        print(f"Git repo: {GIT_REPO_PATH}")
        print(f"State directory: {WORKFLOW_STATE_DIR}")

        print("Updating main repository with latest changes...")
        try:
            fetch_result = subprocess.run(
                ['git', 'fetch', 'origin'],
                cwd=GIT_REPO_PATH,
                capture_output=True,
                text=True
            )
            if fetch_result.returncode != 0:
                print(f"Warning: Git fetch failed: {fetch_result.stderr}")

            current_branch_result = subprocess.run(
                ['git', 'branch', '--show-current'],
                cwd=GIT_REPO_PATH,
                capture_output=True,
                text=True
            )
            current_branch = current_branch_result.stdout.strip()

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

        try:
            all_card_states = self.get_all_card_states()

            cards = self.get_trello_cards()
            print(f"Found {len(cards)} cards in Trello list")

            for card in cards:
                card_id = card['id']

                if card_id not in all_card_states:
                    description = card.get('desc', '').strip()
                    if not description:
                        print(f"Skipping card '{card['name']}' ({card_id}) - description is empty")
                        continue
                    self.process_new_card(card)
                else:
                    card_state = self.load_card_state(card_id)

                    if not card_state.get('branch'):
                        continue

                    comments = self.get_card_comments(card_id)
                    self.process_card_comments(card, comments, card_state)

                    if BITBUCKET_ACCESS_TOKEN:
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
