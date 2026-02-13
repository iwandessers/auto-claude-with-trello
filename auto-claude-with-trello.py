#!/usr/bin/env python3
"""Entry point — dispatches to the workflow or orchestrator CLI.

All logic lives in the ``auto_claude_trello`` package.  This file
exists solely so the original ``python auto-claude-with-trello.py``
invocation keeps working.

See FLOW_DIAGRAMS.md for visual execution-path diagrams.
"""

import sys

from auto_claude_trello.cli import main, orchestrator_main

if __name__ == '__main__':
    # Dispatch: if the first positional arg is "orchestrate", run the
    # orchestrator CLI; otherwise run the original workflow.
    if len(sys.argv) > 1 and sys.argv[1] == 'orchestrate':
        sys.argv = [sys.argv[0]] + sys.argv[2:]  # strip the subcommand
        orchestrator_main()
    else:
        main()
