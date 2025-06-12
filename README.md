# Auto Claude with Trello

An automated workflow tool that monitors Trello cards and (optionally) BitBucket PR comments, using them as instructions for Claude Code to automatically implement features and respond to feedback.

## What It Does

This tool bridges Trello project management with automated code development:

1. **Monitors Trello cards** in a specified list for new tasks
2. **Creates git branches** and worktrees for each card
3. **Uses Claude Code** to implement features based on card descriptions
4. **Creates pull requests** automatically
5. **Processes comments** from both Trello cards and BitBucket PRs as additional Claude Code instructions
6. **Provides automated feedback** by commenting back to both platforms

## Who It's For

- **Development teams** using Trello for project management and BitBucket for code hosting
- **Solo developers** wanting to automate the creation of initial implementations from task descriptions
- **Project managers** who want to provide feedback directly in Trello or PR comments and have it automatically processed

## Prerequisites

- Python 3.7+
- Git repository with remote origin
- Claude Code CLI installed and configured
- Trello account with API access
- BitBucket account with repository access (optional but recommended)

## Installation

1. Clone this repository
2. Install dependencies:
   ```bash
   pip install requests python-dotenv
   ```
3. Set up your environment variables (see below)

## Environment Variables

Create a `.env` file in the project root with the following variables:

### Required Variables

```env
# Trello Configuration
TRELLO_API_KEY=your_trello_api_key
TRELLO_TOKEN=your_trello_api_token
TRELLO_BOARD_ID=your_board_id
TRELLO_LIST_ID=your_list_id_to_monitor

# Git Repository
GIT_REPO_PATH=/absolute/path/to/your/git/repository

# State Management
WORKFLOW_STATE_DIR=/path/to/state/directory  # Defaults to ~/.trello-workflow
```

### Optional Variables

> Please be aware that the BitBucket PR integration has not been tested and may not work as expected. If you use this feature, please provide feedback on any issues you encounter.

```env
# BitBucket Configuration (for PR integration)
BITBUCKET_ACCESS_TOKEN=your_bitbucket_access_token
BITBUCKET_WORKSPACE=your_workspace_name
BITBUCKET_REPO_SLUG=your_repository_name
```

### Getting Trello Credentials

1. **API Key**: Visit https://trello.com/app-key
2. **Token**: Click the "Token" link on the API key page and authorize the application
3. **Board ID**: From your board URL `https://trello.com/b/BOARD_ID/board-name`
4. **List ID**: Use the Trello API or browser developer tools to find the list ID. Tip: put `.json` as extension to the Trello URL, and find the list ID in there.

### Getting BitBucket Credentials

1. Go to BitBucket → Personal settings → App passwords
2. Or for repository-specific: Repository settings → Access tokens
3. Create token with these permissions:
   - `repository:read` (to fetch PRs and comments)
   - `pullrequest:read` (to read PR details)

## Usage

### Single Run
```bash
python auto-claude-with-trello.py
```

### Continuous Monitoring
```bash
python auto-claude-with-trello.py --loop
```

### Cleanup Orphaned Worktrees
```bash
python auto-claude-with-trello.py --cleanup
```

## How It Works

1. **New Card Detection**: When a new card is added to the monitored Trello list:
   - Creates a new git branch named `feature/card-name-cardid`
   - Creates a git worktree for isolated development
   - Executes the card description as Claude Code instructions
   - Commits and pushes changes
   - Comments back to Trello with results and PR link

2. **Comment Processing**: For existing cards:
   - Monitors Trello card comments for new instructions
   - Monitors BitBucket PR comments for code review feedback
   - Executes each comment as Claude Code instructions
   - Updates the code and pushes changes
   - Provides feedback on both platforms

3. **State Management**: 
   - Maintains state for each card in `~/.trello-workflow/cards/`
   - Tracks processed comments to avoid duplication
   - Manages git worktrees in `~/.trello-workflow/worktrees/`

## File Structure

```
~/.trello-workflow/
├── cards/           # Card state files (*.json)
└── worktrees/       # Git worktrees for each card
```

## Safety Features

- Skips bot-generated comments (containing 🤖)
- Maintains separate worktrees to avoid conflicts
- Preserves state between runs
- Handles missing worktrees gracefully

## Example Workflow

1. Create a Trello card: "Add user authentication system"
2. Tool detects new card and creates branch `feature/add-user-auth-abc123`
3. Claude Code implements initial authentication based on card description
4. Tool creates PR and comments back to Trello with results
5. Team reviews PR and adds comment: "Please add password validation"
6. Tool processes PR comment, updates code, and pushes changes
7. Process repeats for additional feedback

## Troubleshooting

- Ensure Claude Code CLI is properly installed and configured
- Check that git repository has proper remote origin setup
- Verify all environment variables are correctly set
- Use `--cleanup` flag if worktrees become orphaned
- Check state files in `~/.trello-workflow/` if issues persist