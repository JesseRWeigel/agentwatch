"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from . import board, procs, sessions


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentwatch",
        description="Every Claude Code and codex session on this machine, found in the process "
                    "table rather than reported by an orchestrator.")
    parser.add_argument("--once", action="store_true",
                        help="print the board once as text and exit, instead of the live TUI")
    parser.add_argument("--json", action="store_true",
                        help="emit the board as JSON, implying --once")
    parser.add_argument("--home", default=sessions.CLAUDE_HOME,
                        help="the .claude directory to read session logs from")
    parser.add_argument("--proc", default=procs.PROC_ROOT,
                        help="the proc filesystem to read, for testing against a fixture")
    parser.add_argument("--no-branch", action="store_true",
                        help="skip the git call per session, for a faster board")
    args = parser.parse_args(argv)

    if args.json or args.once:
        rows = board.build(home=args.home, proc_root=args.proc, want_branch=not args.no_branch)
        if args.json:
            payload = [
                {k: v for k, v in row.__dict__.items()} | {"state": row.state} for row in rows]
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            sys.stdout.write(board.render_text(rows))
        return 0

    from . import tui
    return tui.run(args.home, args.proc)


if __name__ == "__main__":
    sys.exit(main())
