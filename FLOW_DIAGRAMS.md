# Flow Diagrams

Visual reference for every execution path in `auto-claude-with-trello.py`.

---

## 1. Normal Task Flow

```
python auto-claude-with-trello.py [--loop]
```

```
    Trello List (TRELLO_LIST_ID)             BitBucket PR
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~             ~~~~~~~~~~~~
            |                                      |
            v                                      |
    +---------------+                              |
    | Poll for new  |<-------- 60s loop -----------+
    |   cards       |                              |
    +-------+-------+                              |
            |                                      |
      new card found?                              |
       /          \                                |
     no           yes                              |
      |            v                               |
      |   +--------------------+                   |
      |   | Generate session   |                   |
      |   | UUID & branch name |                   |
      |   +---------+----------+                   |
      |             v                              |
      |   +--------------------+                   |
      |   | git fetch          |                   |
      |   | git branch         |                   |
      |   | git worktree add   |                   |
      |   | git push -u origin |                   |
      |   +---------+----------+                   |
      |             v                              |
      |   +--------------------+                   |
      |   | Download card      |                   |
      |   | attachments        |                   |
      |   +---------+----------+                   |
      |             v                              |
      |   +--------------------+                   |
      |   | claude -p          |                   |
      |   | --session-id UUID  |                   |
      |   | (card description  |                   |
      |   |  + attachments)    |                   |
      |   +---------+----------+                   |
      |             v                              |
      |   +--------------------+                   |
      |   | git add -A         |                   |
      |   | git commit         |                   |
      |   | git push           |                   |
      |   +---------+----------+                   |
      |             v                              |
      |   +--------------------+                   |
      |   | Post result        |                   |
      |   | comment to Trello  |                   |
      |   +---------+----------+                   |
      |             v                              |
      |   +--------------------+                   |
      |   | Save card state    |                   |
      |   | (.json)            |                   |
      |   +--------------------+                   |
      |                                            |
      +---- existing card? ----+                   |
            |                  |                   |
            v                  v                   |
    +---------------+  +------------------+        |
    | Check Trello  |  | Check BitBucket  |        |
    | comments      |  | PR comments      |--------+
    +-------+-------+  +--------+---------+
            |                    |
      new comment?         new comment?
            |                    |
            v                    v
    +---------------+  +------------------+
    | claude         |  | claude            |
    |  --resume UUID |  |  --resume UUID   |
    |  -p "comment"  |  |  -p "PR context" |
    +-------+-------+  +--------+---------+
            |                    |
            v                    v
    +---------------+  +------------------+
    | commit & push |  | commit & push    |
    | comment back  |  | comment to PR    |
    | to Trello     |  | + Trello         |
    +---------------+  +------------------+
```

---

## 2. Orchestrator Flow

```
python auto-claude-with-trello.py orchestrate --card-id ID
python auto-claude-with-trello.py orchestrate --watch
```

```
    Trello Orchestrator List               State Files
    (TRELLO_ORCHESTRATOR_LIST_ID)          (~/.trello-workflow/orchestrator/)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~      ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            |
            v
    +-------------------+
    | Card detected on  |
    | orchestrator list |
    +---------+---------+
              |
              v
    +-------------------+    PLANNING PHASE
    | Fetch card name,  |
    | desc, attachments |
    +---------+---------+
              |
              v
    +-------------------+
    | Delegate decomp   |
    | to agent          |
    | "Decompose into   |
    |  3-8 subtasks..." |
    +---------+---------+
              |
              v
    +-------------------+
    | Parse JSON ->     |
    | SubTask objects   |
    | (retry on bad JSON|
    +---------+---------+
              |
              v
    +-------------------+
    | Create parent     |
    | branch & push     |
    +---------+---------+
              |
              v
    +-------------------+
    | Create Trello     |
    | list: "Agents:    |
    | {card_name}"      |
    +---------+---------+
              |
              v
    +-------------------+
    | Create one Trello |
    | card per subtask  |
    +---------+---------+
              |
              v
    +-------------------+
    | Post plan comment |
    | on parent card    |
    +---------+---------+
              |
              v
    +---------+---------+    EXECUTING PHASE
    |                   |
    |   POLL CYCLE      |<----- every poll_interval (30s) ----+
    |   (repeats)       |                                     |
    |                   |                                     |
    +---------+---------+                                     |
              |                                               |
              v                                               |
    +-------------------+                                     |
    | 1. Stop check:    |     card moved off list?            |
    |    fetch card,    +----> YES -> finish running agents   |
    |    check idList   |             post "stopped" comment  |
    +---------+---------+             EXIT                    |
              | NO                                            |
              v                                               |
    +-------------------+                                     |
    | 2. Harvest done   |     for each finished agent:        |
    |    agents         |     - update SubTask status         |
    +---------+---------+     - push branch to remote         |
              |               - post result to Trello card    |
              v                                               |
    +-------------------+                                     |
    | 3. Re-plan on     |     delegate to agent: retry /      |
    |    failures       |     bridge / cancel downstream?     |
    +---------+---------+                                     |
              |                                               |
              v                                               |
    +-------------------+                                     |
    | 4. All terminal?  +---> NO  -> continue to step 5       |
    +---------+---------+                                     |
              | YES                                           |
              v                                               |
    +-------------------+                                     |
    | 4b. Reassess:     |     delegate review to agent        |
    |     spawn review  |     (merges all completed branches  |
    |     agent         |      in temp worktree, inspects)    |
    +---------+---------+                                     |
              |                                               |
        critical issues?                                      |
        /            \                                        |
      YES            NO                                       |
       |              |                                       |
       v              v                                       |
    create fix     exit loop -> MERGE                         |
    subtasks,                                                 |
    continue loop                                             |
              |                                               |
              v                                               |
    +-------------------+                                     |
    | 5. Agent limit?   |     total_spawned >= ORCH_AGENT_    |
    |    (ORCH_AGENT_   +---> YES -> post "paused" comment    |
    |     LIMIT)        |            wait for human "continue"|
    +---------+---------+            (skip starting agents)   |
              | NO / approved                                 |
              v                                               |
    +-------------------+                                     |
    | 5b. Start ready   |     for each ready subtask (up to   |
    |     agents        |     max_agents slots):              |
    |     (see flow 3)  |     - create branch + worktree     |
    +---------+---------+     - submit to ProcessPoolExecutor |
              |                                               |
              v                                               |
    +-------------------+                                     |
    | 6. Post status    |     every 5 cycles: dashboard       |
    |    (every 5       |     comment on parent card          |
    |     cycles)       +-------------------------------------+
    +-------------------+

              |
              v  (all tasks terminal)
    +---------+---------+    MERGING PHASE
    |                   |
    | For each completed|    in priority order:
    | subtask branch:   |
    |  git merge --no-ff|
    |                   |
    |  conflicts? ---+  |
    |    |           |  |
    |   yes         no  |
    |    v           |  |
    |  claude -p     |  |
    |  "resolve      |  |
    |   conflicts"   |  |
    |    |           |  |
    |  resolved? -+  |  |
    |   |         |  |  |
    |  yes       no  |  |
    |   v         v  v  |
    |  commit  abort |  |
    |          merge |  |
    |                |  |
    | clean worktree |  |
    |                |  |
    +---------+------+  |
              |         |
              v         |
    +---------+---------+
    | git push parent   |
    | branch            |
    +---------+---------+
              |
              v
    +-------------------+    REVIEWING PHASE
    | Create BitBucket  |
    | Pull Request      |
    +---------+---------+
              |
              v
    +-------------------+    COMPLETE PHASE
    | Post final status |
    | comment to Trello |
    +---------+---------+
              |
              v
    +-------------------+
    | Move card back to |
    | original_list_id  |
    +-------------------+
```

---

## 3. Orchestrator Sub-Agent Flow

Runs inside `ProcessPoolExecutor`. Each sub-agent is a separate OS process in
its own git worktree. Multiple sub-agents run in parallel (up to `--max-agents`).

```
    Orchestrator (parent process)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            |
            | submit(_run_agent_in_worktree, ...)
            v
    +-------------------+
    | ProcessPool       |     up to max_agents workers
    | Executor          |
    +---+---+---+-------+
        |   |   |
        v   v   v         each worker process:
    +-------------------+
    | Worktree:         |  orch_{subtask_id}_{branch}
    | /worktrees/orch_  |
    | {id}_{branch}     |
    +---------+---------+
              |
              v
    +-------------------+
    | claude             |
    |  --dangerously-    |
    |    skip-permissions|
    |  -p "{prompt}"     |
    |  --allowedTools    |
    |    Bash Read Write |
    |    Edit MultiEdit  |
    +---------+---------+
              |
              | The prompt includes:
              | - Parent task name & description
              | - Full subtask description
              | - Target file list
              | - "Only implement what's described"
              | - "Commit with prefix [subtask-title]"
              | - "Do NOT push"
              |
              v
    +-------------------+
    | Agent works in    |
    | worktree:         |
    | reads, writes,    |
    | edits files,      |
    | runs commands     |
    +---------+---------+
              |
              v
    +-------------------+
    | git add -A        |
    | git commit -m     |
    |  "Agent work      |
    |   completed"      |
    +---------+---------+
              |
              v
    +-------------------+
    | Return result:    |
    | {success, output, |
    |  error}           |
    +---------+---------+
              |
              v  (back in parent process)
    +-------------------+
    | Orchestrator      |
    | harvests result   |
    | in next poll      |
    | cycle             |
    +---------+---------+
              |
         success?
        /        \
      yes         no
       |           |
       v           v
    +--------+  +-----------+
    | Status:   | Status:     |
    | COMPLETE  | FAILED      |
    | Push      | Post error  |
    | branch to | to subtask  |
    | remote,   | Trello card |
    | post to   | -> delegate |
    | Trello    |    re-plan  |
    | card      |    to agent |
    +--------+  +-----------+

    Timeout: 900s default (--agent-timeout not yet exposed via CLI)
    If timed out: returns {success: False, error: "timed out"}
```
