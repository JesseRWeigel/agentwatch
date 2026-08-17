"""The live board. Curses, standard library, no dependencies.

Only drawing and key handling live here. Everything shown is a `board.Row`, which is built and
tested without a terminal, so nothing about what the board SAYS depends on curses being able to
start.

Two decisions worth stating.

An uncertain match is drawn differently from a certain one, always, with a trailing `?` on the
session id and a footer saying why. The correlation between a process and its log is a guess in
the common case of two agents in one repository, and a board that draws a guess and a fact
identically is how somebody kills the wrong session.

Killing asks first, and it names what it is about to kill. `k` next to `j` on a list that
refreshes under the cursor is a bad place to put an irreversible action with no confirmation.
"""

from __future__ import annotations

import curses
import os
import shlex
import subprocess
import time

from . import board


REFRESH_SECONDS = 2.0

HELP = "up/down move   l log   c cd here   K kill   r refresh   q quit"


def _safe_addstr(window, y: int, x: int, text: str, attr: int = 0) -> None:
    """Draw, clipped to the window. Curses raises rather than clipping, including on the last cell
    of the last line, which is a classic way for a TUI to die on an unusual terminal size."""
    height, width = window.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    try:
        window.addnstr(y, x, text, max(0, width - x - 1), attr)
    except curses.error:
        pass


def _colour(row: board.Row) -> int:
    if row.opaque:
        return curses.color_pair(4)
    if row.likely_blocked:
        return curses.color_pair(3)
    if row.tool_is_current:
        return curses.color_pair(1)
    if row.idle_seconds is not None and row.idle_seconds > 300:
        return curses.color_pair(4)
    return curses.color_pair(2)


def draw(window, rows: list[board.Row], selected: int, message: str) -> None:
    window.erase()
    height, width = window.getmaxyx()

    title = f" agentwatch   {len(rows)} session(s)   {time.strftime('%H:%M:%S')} "
    _safe_addstr(window, 0, 0, title.ljust(max(0, width - 1)), curses.A_REVERSE)

    header = (f"{'PID':>7}  {'WHERE':<20} {'BRANCH':<12} {'STATE':<9} {'TOOL':<14} "
              f"{'CTX':>6} {'OUT':>6} {'IDLE':>6} {'AGE':>6}  LOG")
    _safe_addstr(window, 2, 0, header, curses.A_BOLD)

    if not rows:
        _safe_addstr(window, 4, 2, "no agent sessions found.")
        _safe_addstr(window, 5, 2, "nothing named claude or codex is running under this user.")
        _safe_addstr(window, height - 1, 0, HELP.ljust(max(0, width - 1)), curses.A_REVERSE)
        window.refresh()
        return

    first_row = 3
    visible = max(1, height - first_row - 4)
    top = max(0, min(selected - visible // 2, len(rows) - visible))

    for offset, row in enumerate(rows[top:top + visible]):
        index = top + offset
        mark = "" if row.certain else "?"
        text = (f"{row.pid:>7}  {row.label[:20]:<20} {(row.branch or '-')[:12]:<12} "
                f"{row.state:<9} {(row.tool or '-')[:14]:<14} "
                f"{board.tokens(row.context_tokens):>6} {board.tokens(row.output_tokens):>6} "
                f"{board.duration(row.idle_seconds):>6} {board.duration(row.age_seconds):>6}  "
                f"{(row.session_id or '-')[:8]}{mark}")
        attr = _colour(row)
        if index == selected:
            attr |= curses.A_REVERSE
        _safe_addstr(window, first_row + offset, 0, text.ljust(max(0, width - 1)), attr)

    chosen = rows[selected]
    detail_y = height - 3
    _safe_addstr(window, detail_y, 0, f"  {chosen.cwd or '(working directory not readable)'}",
                 curses.A_DIM)
    note = message or (chosen.confidence_reason if not chosen.certain else chosen.evidence)
    _safe_addstr(window, detail_y + 1, 0, f"  {note}", curses.A_DIM)
    _safe_addstr(window, height - 1, 0, HELP.ljust(max(0, width - 1)), curses.A_REVERSE)
    window.refresh()


def confirm(window, question: str) -> bool:
    height, width = window.getmaxyx()
    _safe_addstr(window, height - 2, 0, (question + " [y/N] ").ljust(max(0, width - 1)),
                 curses.A_REVERSE)
    window.refresh()
    window.nodelay(False)
    try:
        answer = window.getch()
    finally:
        window.nodelay(True)
    return answer in (ord("y"), ord("Y"))


def _suspend(window, command: list[str], cwd: str | None = None) -> None:
    """Drop out of curses, run something interactive, come back.

    endwin/refresh around the call rather than a subprocess writing into the curses screen. A
    pager drawing over a curses window leaves the terminal in a state neither program owns.
    """
    curses.endwin()
    try:
        subprocess.call(command, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        pass
    window.clear()
    curses.doupdate()


def loop(window, home: str, proc_root: str) -> None:
    curses.curs_set(0)
    window.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)     # working
    curses.init_pair(2, curses.COLOR_WHITE, -1)     # live
    curses.init_pair(3, curses.COLOR_YELLOW, -1)    # possibly blocked
    curses.init_pair(4, curses.COLOR_BLUE, -1)      # idle or opaque

    rows = board.build(home=home, proc_root=proc_root)
    selected = 0
    message = ""
    last = time.time()

    while True:
        selected = max(0, min(selected, len(rows) - 1)) if rows else 0
        draw(window, rows, selected, message)

        key = window.getch()
        if key != -1:
            message = ""
        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_DOWN, ord("j")) and rows:
            selected = min(selected + 1, len(rows) - 1)
        elif key in (curses.KEY_UP, ord("k")) and rows:
            selected = max(selected - 1, 0)
        elif key == ord("r"):
            rows = board.build(home=home, proc_root=proc_root)
            last = time.time()
        elif key == ord("l") and rows:
            row = rows[selected]
            path = _log_path(row, home)
            if path:
                pager = os.environ.get("PAGER") or "less"
                _suspend(window, [pager, "+F", path] if "less" in pager else [pager, path])
            else:
                message = "no log is associated with this session"
        elif key == ord("c") and rows:
            row = rows[selected]
            if row.cwd and os.path.isdir(row.cwd):
                # A child process cannot change the parent shell's directory, so this opens a
                # shell there instead of pretending to cd. Leaving it returns to the board.
                _suspend(window, [os.environ.get("SHELL") or "/bin/sh"], cwd=row.cwd)
            else:
                message = "the working directory is not readable"
        elif key == ord("K") and rows:
            row = rows[selected]
            if confirm(window, f"kill pid {row.pid} in {row.label}?"):
                try:
                    os.kill(row.pid, 15)
                    message = f"sent SIGTERM to {row.pid}"
                except (OSError, ProcessLookupError) as error:
                    message = f"could not kill {row.pid}: {error}"
                rows = board.build(home=home, proc_root=proc_root)
                last = time.time()

        if time.time() - last >= REFRESH_SECONDS:
            rows = board.build(home=home, proc_root=proc_root)
            last = time.time()
        time.sleep(0.05)


def _log_path(row: board.Row, home: str) -> str | None:
    if not row.session_id:
        return None
    projects = os.path.join(home, "projects")
    try:
        directories = os.listdir(projects)
    except OSError:
        return None
    for name in directories:
        candidate = os.path.join(projects, name, f"{row.session_id}.jsonl")
        if os.path.isfile(candidate):
            return candidate
    return None


def run(home: str, proc_root: str) -> int:
    try:
        curses.wrapper(loop, home, proc_root)
    except curses.error as error:
        print(f"the terminal will not start curses: {error}")
        print("use --once for a plain text board instead.")
        return 1
    return 0
