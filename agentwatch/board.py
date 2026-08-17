"""Assemble one row per session. Pure data, so the board can be tested without a terminal.

The rendering lives in `tui.py` and knows nothing except how to draw these rows. Keeping the two
apart is what lets the tests assert on what the board SAYS rather than on where curses put it, and
it is why `--once` can print the same board as plain text without a second implementation of the
logic that would drift from the first one.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import time

from . import activity as activity_module
from . import procs, sessions


@dataclasses.dataclass(frozen=True)
class Row:
    pid: int
    cwd: str | None
    label: str                       # the directory's short name, or a note about why there is none
    branch: str | None
    version: str | None
    session_id: str | None
    confidence: str
    confidence_reason: str
    certain: bool
    model: str | None
    tool: str | None                 # what it is doing, or the last thing it did
    tool_is_current: bool
    context_tokens: int | None
    output_tokens: int
    idle_seconds: float | None
    age_seconds: float | None
    likely_blocked: bool
    evidence: str
    opaque: bool                     # the process exists and we may not read its details

    @property
    def state(self) -> str:
        if self.opaque:
            return "opaque"
        if self.likely_blocked:
            return "blocked?"
        if self.tool_is_current:
            return "working"
        if self.idle_seconds is not None and self.idle_seconds > 300:
            return "idle"
        return "live"


def git_branch(path: str) -> str | None:
    """The branch of the worktree a session is sitting in, read from git rather than the log.

    The log records the branch it started on. A session that has been running for a day has very
    likely changed branches since, and the stale value is worse than none because nobody would
    think to doubt it.
    """
    try:
        result = subprocess.run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def short_label(cwd: str | None) -> str:
    if not cwd:
        return "(not readable)"
    base = os.path.basename(cwd.rstrip("/"))
    return base or cwd


def build(home: str = sessions.CLAUDE_HOME, proc_root: str = procs.PROC_ROOT,
          now: float | None = None, want_branch: bool = True) -> list[Row]:
    now = now if now is not None else time.time()
    boot = procs.boot_time(proc_root)
    ticks = procs.clock_ticks()

    processes = procs.discover(proc_root)
    started = {}
    for process in processes:
        if boot is not None and process.start_ticks is not None:
            started[process.pid] = boot + process.start_ticks / ticks

    correlations = sessions.correlate_all(processes, home=home, started=started)

    rows = []
    for process in processes:
        correlation = correlations[process.pid]
        log = sessions.most_recent(correlation)
        act = activity_module.read(log.path) if log else activity_module.Activity()

        start = started.get(process.pid)
        rows.append(Row(
            pid=process.pid,
            cwd=process.cwd,
            label=short_label(process.cwd),
            branch=git_branch(process.cwd) if (want_branch and process.cwd) else None,
            version=process.version,
            session_id=log.session_id if log else None,
            confidence=correlation.confidence.value,
            confidence_reason=correlation.reason,
            certain=correlation.is_certain,
            model=act.model,
            tool=act.current_tool or act.last_tool,
            tool_is_current=act.current_tool is not None,
            context_tokens=act.context_tokens,
            output_tokens=act.output_tokens,
            idle_seconds=act.idle_seconds(now),
            age_seconds=(now - start) if start is not None else None,
            likely_blocked=act.likely_blocked(now),
            evidence=act.evidence,
            opaque=process.is_opaque,
        ))
    return rows


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}"


def tokens(count: int | None) -> str:
    if count is None:
        return "-"
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.0f}k"
    return f"{count / 1_000_000:.1f}M"


def render_text(rows: list[Row]) -> str:
    """The board as plain text, for `--once`, for piping, and for the tests.

    A session whose details are unreadable gets a row saying so rather than being dropped. See
    `procs`: the difference between "nothing is running" and "something is running that you are
    not allowed to look at" is the whole reason somebody opens this.
    """
    if not rows:
        return ("no agent sessions found.\n"
                "nothing named claude or codex is running under this user.\n")

    header = (f"{'PID':>7}  {'WHERE':<22} {'BRANCH':<14} {'STATE':<9} {'TOOL':<16} "
              f"{'CTX':>6} {'OUT':>6} {'IDLE':>6} {'AGE':>6}  LOG")
    lines = [header, "-" * len(header)]
    for row in rows:
        mark = "" if row.certain else "?"
        session = f"{(row.session_id or '-')[:8]}{mark}"
        lines.append(
            f"{row.pid:>7}  {row.label[:22]:<22} {(row.branch or '-')[:14]:<14} "
            f"{row.state:<9} {(row.tool or '-')[:16]:<16} "
            f"{tokens(row.context_tokens):>6} {tokens(row.output_tokens):>6} "
            f"{duration(row.idle_seconds):>6} {duration(row.age_seconds):>6}  {session}")

    uncertain = [r for r in rows if not r.certain]
    if uncertain:
        lines.append("")
        lines.append("? the log shown is a guess, not a match:")
        for row in uncertain:
            lines.append(f"    pid {row.pid}: {row.confidence_reason}")

    blocked = [r for r in rows if r.likely_blocked]
    if blocked:
        lines.append("")
        lines.append("possibly waiting on a permission prompt (inferred, not recorded):")
        for row in blocked:
            lines.append(f"    pid {row.pid}: {row.evidence}")
    return "\n".join(lines) + "\n"
