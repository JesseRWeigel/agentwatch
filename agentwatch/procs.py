"""Find agent sessions in the process table.

This is the part no orchestrator can do. Toryo, Taisho and a dashboard fed by an orchestrator's
gateway all know about the agents they launched, because they launched them. None of them can see
a session somebody started by typing `claude` in a terminal, which is most of them.

So discovery starts from `/proc` and nothing else. The process table is the one authority that
does not depend on a session having registered itself somewhere.

Two rules this module keeps.

A process we cannot read is REPORTED, not skipped. `/proc/<pid>/cwd` is a symlink readable only
by the owner and root, so a session belonging to another user is visible in the table and opaque
in its details. Dropping those would make "no agents are running" and "agents are running that I
am not allowed to inspect" print the same thing, and the second is the case where somebody is
about to wonder where their CPU went.

Nothing here reads `/proc` at an import-time constant. The root is a parameter, defaulting to
`/proc`, so the tests run against a fixture tree instead of against whatever happens to be running
on the machine that day. A test suite that needs a live agent to test the agent finder is a test
suite that passes or fails for reasons unrelated to the code.
"""

from __future__ import annotations

import dataclasses
import os
import re


PROC_ROOT = "/proc"

# `comm` is the kernel's name for the executable, truncated to 15 characters. It is the cheapest
# reliable filter and it is what a bare `claude` invocation presents, whose cmdline is just the
# word "claude" with no arguments to match on.
AGENT_COMMS = {"claude", "codex"}

# A session id in the command line, which the SDK and `--resume` invocations carry. When it is
# there it is definitive and no correlation guessing is needed at all.
SESSION_ID = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.I)

# The installed-version directory, e.g. `~/.local/share/claude/versions/2.1.229`. This is the
# version of the BINARY, which is not always the version that wrote the session log: a session
# started before an upgrade keeps running the old code and records the old version in its log.
VERSION_FROM_EXE = re.compile(r"/versions/([0-9][^/]*)$")


@dataclasses.dataclass(frozen=True)
class Process:
    """One process, with the fields that survive not being allowed to read it."""

    pid: int
    comm: str
    ppid: int | None = None
    cmdline: tuple[str, ...] = ()
    cwd: str | None = None
    exe: str | None = None
    version: str | None = None
    start_ticks: int | None = None
    session_id: str | None = None
    # Which of the above could not be read, and why. Empty when everything was readable.
    unreadable: tuple[str, ...] = ()

    @property
    def is_opaque(self) -> bool:
        """True when the details are hidden, which is different from there being none."""
        return "cwd" in self.unreadable


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (OSError, PermissionError):
        return None


def _readlink(path: str) -> str | None:
    try:
        return os.readlink(path)
    except (OSError, PermissionError):
        return None


def clock_ticks() -> int:
    """Ticks per second, for turning a start time into a duration."""
    try:
        return os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):       # pragma: no cover - every Linux defines this
        return 100


def boot_time(root: str = PROC_ROOT) -> float | None:
    """Seconds since the epoch at which the machine booted, from /proc/stat's btime line."""
    text = _read(os.path.join(root, "stat"))
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def parse_stat(text: str) -> tuple[str, int, int] | None:
    """(comm, ppid, starttime) from the contents of /proc/<pid>/stat.

    The second field is the executable name IN PARENTHESES, and a process is free to have
    parentheses and spaces in that name. Splitting on whitespace is the obvious implementation and
    it misreads every such process, shifting every field after it by one. So the name is taken
    between the FIRST '(' and the LAST ')', and the numeric fields are read from what follows.
    """
    open_paren = text.find("(")
    close_paren = text.rfind(")")
    if open_paren < 0 or close_paren < open_paren:
        return None
    comm = text[open_paren + 1:close_paren]
    rest = text[close_paren + 1:].split()
    # No length check here on purpose. One was written, and the sabotage suite showed it could
    # not be observed: on a short line `int(rest[19])` raises IndexError, which the except below
    # already turns into the same None. A guard whose removal changes nothing is not a guard, it
    # is a second spelling of the line under it, and two spellings of one rule drift apart.
    try:
        # rest[0] is state, so field 4 (ppid) is rest[1] and field 22 (starttime) is rest[19].
        return comm, int(rest[1]), int(rest[19])
    except (ValueError, IndexError):
        return None


def read_process(pid: int, root: str = PROC_ROOT) -> Process | None:
    """Everything readable about one pid, or None if it is gone."""
    base = os.path.join(root, str(pid))
    stat_text = _read(os.path.join(base, "stat"))
    if stat_text is None:
        return None                       # exited between listing and reading, which is normal
    parsed = parse_stat(stat_text)
    if parsed is None:
        return None
    comm, ppid, start_ticks = parsed

    unreadable: list[str] = []

    raw_cmdline = _read(os.path.join(base, "cmdline"))
    if raw_cmdline is None:
        unreadable.append("cmdline")
        cmdline: tuple[str, ...] = ()
    else:
        cmdline = tuple(part for part in raw_cmdline.split("\0") if part)

    cwd = _readlink(os.path.join(base, "cwd"))
    if cwd is None:
        unreadable.append("cwd")

    exe = _readlink(os.path.join(base, "exe"))
    if exe is None:
        unreadable.append("exe")

    version = None
    if exe:
        match = VERSION_FROM_EXE.search(exe)
        if match:
            version = match.group(1)

    session_id = None
    joined = " ".join(cmdline)
    match = SESSION_ID.search(joined)
    if match:
        session_id = match.group(1).lower()

    return Process(pid=pid, comm=comm, ppid=ppid, cmdline=cmdline, cwd=cwd, exe=exe,
                   version=version, start_ticks=start_ticks, session_id=session_id,
                   unreadable=tuple(unreadable))


def pids(root: str = PROC_ROOT) -> list[int]:
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    return sorted(int(name) for name in entries if name.isdigit())


def is_agent(process: Process) -> bool:
    """Whether this process is an agent session rather than something it spawned.

    Matched on `comm` rather than on the command line. A bare `claude` has no arguments at all,
    so there is nothing else to match, and matching the word anywhere in a command line would
    collect every `grep claude`, every editor with the word in a filename, and this program.
    """
    return process.comm in AGENT_COMMS


def discover(root: str = PROC_ROOT) -> list[Process]:
    """Every agent session on the machine, including the ones we cannot see inside.

    Sorted by start time so the oldest session is first, which is a stable order across refreshes.
    A list sorted by CPU or by activity reorders itself under the reader's cursor.
    """
    found = []
    for pid in pids(root):
        process = read_process(pid, root)
        if process is not None and is_agent(process):
            found.append(process)
    found.sort(key=lambda p: (p.start_ticks if p.start_ticks is not None else 0, p.pid))
    return found
