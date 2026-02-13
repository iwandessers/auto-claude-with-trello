"""Multi-agent orchestrator.

Decomposes a Trello card into subtasks, runs them in parallel Claude Code
agents (each in its own git worktree), merges the results, and creates a
pull request.
"""

import concurrent.futures
import json
import os
import re
import signal
import subprocess
import time
import traceback
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Optional

import requests

from auto_claude_trello.config import (
    TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID, TRELLO_LIST_ID,
    TRELLO_ORCHESTRATOR_LIST_ID, ORCH_AGENT_LIMIT,
    BITBUCKET_ACCESS_TOKEN, BITBUCKET_WORKSPACE, BITBUCKET_REPO_SLUG,
    GIT_REPO_PATH, WORKTREE_BASE_DIR, ORCHESTRATOR_STATE_DIR,
)
from auto_claude_trello.models import (
    TaskStatus, OrchestratorPhase, SubTask, OrchestratorState,
)
from auto_claude_trello.trello_api import TrelloAPI
from auto_claude_trello.git_helper import GitHelper
from auto_claude_trello.agent import run_agent_in_worktree


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
        self._paused = False          # True when agent limit reached
        self._pause_comment_id: Optional[str] = None
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

    # -- agent limit / human approval gate ------------------------------------

    def _check_agent_limit(self, state: OrchestratorState) -> bool:
        """Return True if the orchestrator is paused waiting for approval."""
        if self._paused:
            return not self._has_human_continue(state)

        if state.total_agents_spawned >= ORCH_AGENT_LIMIT:
            self._paused = True
            msg = (
                f"## {self.BOT_TAG} Agent Limit Reached\n\n"
                f"The orchestrator has spawned **{state.total_agents_spawned}** "
                f"agents (limit: **{ORCH_AGENT_LIMIT}**).\n\n"
                f"No new agents will be started until a human replies to this "
                f"card with a comment containing the word **continue**.\n\n"
                f"Already-running agents will keep executing."
            )
            try:
                self.trello.add_comment(state.parent_card_id, msg)
                comments = self.trello.get_card_comments(state.parent_card_id)
                for c in comments:
                    text = c.get('data', {}).get('text', '')
                    if 'Agent Limit Reached' in text:
                        self._pause_comment_id = c['id']
                        break
            except Exception as exc:
                print(f"[ORCH] Warning: could not post pause comment: {exc}")
            print(f"[ORCH] Paused — waiting for human 'continue' comment "
                  f"(limit {ORCH_AGENT_LIMIT} reached).")
            return True

        return False

    def _has_human_continue(self, state: OrchestratorState) -> bool:
        """Check if a human has posted a comment containing 'continue'
        after the pause notice."""
        try:
            comments = self.trello.get_card_comments(state.parent_card_id)
        except Exception:
            return False

        for c in comments:
            cid = c['id']
            if cid == self._pause_comment_id:
                break
            text = c.get('data', {}).get('text', '')
            if self.BOT_TAG in text:
                continue
            if re.search(r'\bcontinue\b', text, re.IGNORECASE):
                print("[ORCH] Human approved continuation — resuming.")
                self._paused = False
                self._pause_comment_id = None
                return True
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
        """Delegate task decomposition to an agent."""
        prompt = (
            "You are a software architect. Decompose the following task "
            "into 3-8 independently executable subtasks for parallel "
            "coding agents.\n\n"
            f"TASK TITLE: {card_name}\n\n"
            f"TASK DESCRIPTION:\n{card_desc}\n\n"
            f"{('ATTACHMENTS INFO:' + chr(10) + attachments_info + chr(10)) if attachments_info else ''}"
            "Return ONLY a JSON array of subtask objects. Each object "
            "must have these fields:\n"
            '- "id": a short unique slug (e.g. "setup-auth")\n'
            '- "title": concise subtask title\n'
            '- "description": a complete, standalone prompt for a coding '
            "agent — include ALL context needed so the agent can work "
            "without seeing other subtasks\n"
            '- "dependencies": list of other subtask titles this depends on '
            "(empty list if none)\n"
            '- "estimated_files": list of file paths this subtask will '
            "likely touch\n"
            '- "priority": integer (1 = highest). Same priority means '
            "tasks can run in parallel.\n\n"
            "Rules:\n"
            "- Make each subtask independently implementable in its own "
            "git branch\n"
            "- Minimise file overlap between subtasks to avoid merge "
            "conflicts\n"
            "- Include concrete file paths and clear acceptance criteria "
            "in each description\n"
            "- Specify dependencies between subtasks by title\n"
            "- Always include a final integration/testing subtask that "
            "depends on all others\n"
            "- Return ONLY the JSON array, no markdown fences, no "
            "explanation"
        )

        print("[ORCH] Delegating task decomposition to agent…")
        result = run_agent_in_worktree(
            self.git.repo_path, prompt, timeout_seconds=300)

        if not result['success']:
            raise RuntimeError(
                f"Decomposition agent failed: {result.get('error', '?')}")

        return self._parse_subtasks_json(result['output'])

    def _parse_subtasks_json(self, raw: str) -> List[SubTask]:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)

        array_match = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if array_match:
            cleaned = array_match.group()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            fix_prompt = (
                "The following text was supposed to be a JSON array of "
                "subtask objects but it has syntax errors. Fix it and "
                "return ONLY the corrected JSON array, nothing else:"
                f"\n\n{raw}"
            )
            fix_result = run_agent_in_worktree(
                self.git.repo_path, fix_prompt, timeout_seconds=120)
            fixed = fix_result['output'].strip()
            if fixed.startswith("```"):
                flines = fixed.split("\n")
                flines = [l for l in flines
                          if not l.strip().startswith("```")]
                fixed = "\n".join(flines)
            array_match = re.search(r'\[.*\]', fixed, re.DOTALL)
            if array_match:
                fixed = array_match.group()
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
            if isinstance(st_dict, dict):
                st_dict['card_id'] = card['id']
            else:
                st_dict.card_id = card['id']

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
        """Delegate re-planning to an agent when a subtask fails."""
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

        prompt = (
            f"A subtask in an automated code orchestration failed.\n\n"
            f"{summary}\n"
            f"Original parent task: {state.parent_card_name}\n\n"
            f"Decide ONE of:\n"
            f"1. RETRY — provide modified instructions for the failed task\n"
            f"2. BRIDGE — provide 1-2 new bridging subtasks that work "
            f"around the failure\n"
            f"3. CANCEL — cancel all downstream dependents of the failed "
            f"task\n\n"
            f"Return ONLY a JSON object (no markdown fences) with:\n"
            f'- "action": "retry" | "bridge" | "cancel"\n'
            f'- "modified_instructions": string (only for retry)\n'
            f'- "new_tasks": array of subtask objects (only for bridge). '
            f'Each object needs: "id", "title", "description", '
            f'"dependencies", "estimated_files", "priority"\n'
            f'- "reason": brief explanation'
        )

        agent_cwd = failed_task.get('worktree_path') or self.git.repo_path
        print(f"[ORCH] Delegating re-plan for '{failed_task.get('title')}' "
              f"to agent…")
        replan_result = run_agent_in_worktree(
            agent_cwd, prompt, timeout_seconds=300)

        if not replan_result['success']:
            print(f"[ORCH] Re-plan agent failed — cancelling dependents.")
            self._mark_blocked(state, failed_task.get('title', ''))
            return []

        raw = replan_result['output'].strip()
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            print(f"[ORCH] Re-plan agent returned no JSON — "
                  f"cancelling dependents.")
            self._mark_blocked(state, failed_task.get('title', ''))
            return []

        try:
            decision = json.loads(json_match.group())
        except json.JSONDecodeError:
            print(f"[ORCH] Could not parse re-plan response, "
                  f"cancelling dependents.")
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

        self._update_subtask(state, st_id,
                             status=TaskStatus.RUNNING.value,
                             agent_branch=branch_name,
                             worktree_path=wt_path,
                             started_at=datetime.now().isoformat())
        state.total_agents_spawned += 1

        prompt = self._build_agent_prompt(state, subtask_dict)
        future = self._executor.submit(
            run_agent_in_worktree, wt_path, prompt, self.agent_timeout)
        self._futures[future] = st_id
        print(f"[ORCH] Started agent for '{subtask_dict['title']}' "
              f"on {branch_name}")

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
                # Push agent branch to remote
                agent_branch = (subtask_dict.get('agent_branch')
                                if isinstance(subtask_dict, dict)
                                else subtask_dict.agent_branch)
                agent_wt = (subtask_dict.get('worktree_path')
                            if isinstance(subtask_dict, dict)
                            else subtask_dict.worktree_path)
                if agent_branch and agent_wt:
                    try:
                        self.git.push(agent_branch, cwd=agent_wt)
                        print(f"[ORCH] Pushed branch {agent_branch}")
                    except Exception as push_exc:
                        print(f"[ORCH] Warning: failed to push "
                              f"{agent_branch}: {push_exc}")
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

        parent_branch = state.parent_branch
        merge_wt = self.git.create_worktree(parent_branch,
                                            f"merge-{state.orchestrator_id[:8]}")

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
                resolve_result = run_agent_in_worktree(
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

            wt = s.get('worktree_path')
            if wt:
                self.git.remove_worktree(wt)

        self.git.push(parent_branch, cwd=merge_wt)

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

    # -- reassessment ---------------------------------------------------------

    def _reassess_work(self, state: OrchestratorState) -> bool:
        """Delegate a review of all completed work to an agent.

        Spawns a review agent in a temporary worktree that merges every
        completed branch, inspects the combined result, and reports
        whether there are VERY CRITICAL problems.

        Returns True if critical issues were found (new subtasks added
        to *state*), False if the work is acceptable.
        """
        completed = [
            s for s in state.subtasks
            if (s.get('status') if isinstance(s, dict) else s.status)
            == TaskStatus.COMPLETE.value
        ]
        if not completed:
            return False

        review_branch = (f"orch/review-{state.orchestrator_id[:8]}-"
                         f"{str(uuid.uuid4())[:4]}")
        self.git.fetch()
        self.git.create_branch(review_branch, state.parent_branch)
        review_wt = self.git.create_worktree(review_branch,
                                             f"review-{state.orchestrator_id[:8]}")

        for sd in completed:
            branch = (sd.get('agent_branch') if isinstance(sd, dict)
                      else sd.agent_branch)
            if branch:
                subprocess.run(
                    ['git', 'merge', '--no-ff', '-m',
                     f'Review merge {branch}', branch],
                    cwd=review_wt, capture_output=True)
                if self.git.has_conflicts(cwd=review_wt):
                    subprocess.run(['git', 'checkout', '--theirs', '.'],
                                   cwd=review_wt, capture_output=True)
                    self.git.commit_all(
                        f"Auto-resolved conflicts for review of {branch}",
                        cwd=review_wt)

        summary_lines = []
        for sd in completed:
            s = sd if isinstance(sd, dict) else asdict(sd)
            summary_lines.append(
                f"- {s['title']}: branch={s.get('agent_branch','?')}, "
                f"files={', '.join(s.get('estimated_files', []))}"
            )
        summaries = "\n".join(summary_lines)

        review_prompt = (
            f"You are a senior code reviewer. You are inside a git worktree "
            f"that contains the merged output of several coding agents.\n\n"
            f"## Parent Task\n{state.parent_card_name}\n\n"
            f"## Completed Subtasks\n{summaries}\n\n"
            f"## Your Job\n"
            f"1. Use `git log --oneline` and `git diff HEAD~{len(completed)}` "
            f"to inspect what the agents changed.\n"
            f"2. Look for VERY CRITICAL problems ONLY:\n"
            f"   - Broken imports or syntax errors that prevent the project "
            f"from running\n"
            f"   - Security vulnerabilities (credentials leaked, SQL injection, "
            f"etc.)\n"
            f"   - Completely missing implementations (function stubs left empty "
            f"when they should have been filled)\n"
            f"   - Logic that is the exact opposite of what was requested\n"
            f"3. Do NOT flag style issues, minor bugs, missing tests, or "
            f"improvements. Those are not critical.\n\n"
            f"## Output\n"
            f"Return ONLY a JSON object (no markdown fences):\n"
            f'{{"critical": false}} if no very critical problems were found.\n'
            f"OR\n"
            f'{{"critical": true, "issues": ['
            f'{{"title": "short-slug", '
            f'"description": "Complete standalone prompt for a coding agent '
            f'to fix this issue. Include file paths and exact problem.", '
            f'"estimated_files": ["path/to/file"], '
            f'"priority": 1}}]}}\n'
            f"Remember: only VERY CRITICAL issues. When in doubt, it is fine."
        )

        print("[ORCH] Delegating post-execution review to agent…")
        review_result = run_agent_in_worktree(
            review_wt, review_prompt, timeout_seconds=300)

        self.git.remove_worktree(review_wt)
        subprocess.run(['git', 'branch', '-D', review_branch],
                       cwd=self.git.repo_path, capture_output=True)

        if not review_result['success']:
            print("[ORCH] Review agent failed — proceeding to merge anyway.")
            return False

        raw = review_result['output'].strip()
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            print("[ORCH] Review agent returned no JSON — "
                  "proceeding to merge.")
            return False

        try:
            verdict = json.loads(json_match.group())
        except json.JSONDecodeError:
            print("[ORCH] Review agent returned invalid JSON — "
                  "proceeding to merge.")
            return False

        if not verdict.get('critical', False):
            print("[ORCH] Review passed — no critical issues found.")
            return False

        issues = verdict.get('issues', [])
        if not issues:
            print("[ORCH] Review flagged critical but gave no issues — "
                  "proceeding to merge.")
            return False

        print(f"[ORCH] Review found {len(issues)} critical issue(s). "
              f"Creating fix subtasks…")
        for item in issues:
            fix_id = f"fix-{item.get('title', str(uuid.uuid4())[:6])}"
            fix_st = SubTask(
                id=fix_id,
                title=item.get('title', fix_id),
                description=item['description'],
                dependencies=[],
                estimated_files=item.get('estimated_files', []),
                priority=item.get('priority', 1),
            )
            state.subtasks.append(asdict(fix_st))

            if state.subtask_list_id:
                try:
                    card = self.trello.create_card(
                        state.subtask_list_id, fix_st.title,
                        fix_st.description)
                    self._update_subtask(state, fix_id, card_id=card['id'])
                except Exception:
                    pass

        self._post_status(
            state,
            extra=(f"**Post-execution review found {len(issues)} critical "
                   f"issue(s).** Spawning fix agents…")
        )
        self._save_state(state)
        return True

    # -- main orchestration loop ----------------------------------------------

    def orchestrate(self, card_id: str):
        """Full orchestration lifecycle for a single Trello card."""
        self._register_signals()

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

            self.git.fetch()
            self.git.create_branch(parent_branch)
            self.git.push(parent_branch)

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
                    # Reassess: delegate a review to an agent.
                    # If VERY CRITICAL issues are found, new fix
                    # subtasks are added and the loop continues.
                    if self._reassess_work(state):
                        print("[ORCH] Critical fixes queued — "
                              "continuing execution loop.")
                        continue
                    break

                # 5. Start ready agents (fill available slots)
                if not self._check_agent_limit(state):
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
                self._executor.shutdown(wait=True)

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


# -- watch loop ---------------------------------------------------------------

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

                orchestrator.orchestrate(cid)
        except KeyboardInterrupt:
            print("\n[ORCH-WATCH] Interrupted. Exiting.")
            break
        except Exception as exc:
            print(f"[ORCH-WATCH] Error: {exc}")
            traceback.print_exc()

        time.sleep(60)
