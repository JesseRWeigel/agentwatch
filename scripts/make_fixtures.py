#!/usr/bin/env python3
"""Build a synthetic /proc tree and a synthetic ~/.claude tree for the tests to read.

Everything committed here is invented. No path, user name or session id in the fixtures comes off
the machine that generated them, which the privacy scan proves by planting values and requiring
them to be absent from anything published.

The fixture is the reason the tests mean anything. A test suite for a process finder that reads
the real /proc passes or fails according to what happened to be running, so it cannot assert that
two sessions in one directory are reported as ambiguous, or that an unreadable process still gets
a row, or that a 15-character truncated `comm` is matched. Those are the cases worth testing and
none of them can be arranged on a live machine on demand.

The cases built here, each because something in the code exists for it:

  quiet-repo      one session, one log, one obvious answer
  shared-repo     TWO sessions in one directory, one naming its session on the command line.
                  The other must come out UNIQUE only after the first claims its log.
  crowded-repo    one session, many logs, genuinely ambiguous, must be reported as such
  legacy-dots     a directory mangled under the OLD rule, which keeps dots
  modern-dots     the same path mangled under the NEW rule, which turns dots into dashes
  opaque          a process whose cwd cannot be read, which must still get a row
  not-an-agent    a process that must NOT be picked up
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

# Invented. Deliberately not this machine's user, home, or session ids.
HOME = "/home/testsubject"
BOOT = 1700000000
TICKS = 100

PROJECTS = {
    "quiet-repo": f"{HOME}/work/quiet-repo",
    "shared-repo": f"{HOME}/work/shared-repo",
    "crowded-repo": f"{HOME}/work/crowded-repo",
    "dotted": f"{HOME}/.config/dotted",
}


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def stat_line(pid: int, comm: str, ppid: int, start_ticks: int) -> str:
    """A /proc/<pid>/stat line with the fields this project reads in the right places.

    The comm is written in parentheses as the kernel does. One fixture puts a space and a
    parenthesis inside that name, because the parser is written to survive it and a parser nobody
    has fed a hostile name to is not known to survive anything.
    """
    # Positions matter. After the closing parenthesis, field 3 is index 0, so ppid (field 4) is
    # index 1 and starttime (field 22) is index 19. Verified against a real /proc/self/stat:
    # its index 19 divided by the clock tick equals /proc/uptime.
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start_ticks)]
    assert fields[19] == str(start_ticks), "starttime must land on index 19"
    return f"{pid} ({comm}) " + " ".join(fields) + " 0 0 0\n"


def make_proc(root: str) -> None:
    write(os.path.join(root, "stat"), f"cpu 1 2 3\nbtime {BOOT}\nprocesses 100\n")

    def process(pid: int, comm: str, cmdline: list[str], cwd: str | None, exe: str | None,
                start: int, ppid: int = 1) -> None:
        base = os.path.join(root, str(pid))
        os.makedirs(base, exist_ok=True)
        write(os.path.join(base, "stat"), stat_line(pid, comm, ppid, start))
        write(os.path.join(base, "cmdline"), "\0".join(cmdline) + "\0")
        # The fixture stores links as plain files named cwd.link, because a git checkout cannot
        # carry a symlink to a path that does not exist on the machine doing the checkout. The
        # test harness turns them into real symlinks in a temporary directory.
        if cwd is not None:
            write(os.path.join(base, "cwd.link"), cwd)
        if exe is not None:
            write(os.path.join(base, "exe.link"), exe)

    version = f"{HOME}/.local/share/claude/versions/9.9.9"

    process(101, "claude", ["claude"], PROJECTS["quiet-repo"], version, 1000)
    # Two in one directory. 201 names its session, 202 does not, and 202 is only resolvable
    # because 201 claims one of the two logs first.
    process(201, "claude", ["claude", "--resume", "aaaaaaaa-1111-2222-3333-444444444444"],
            PROJECTS["shared-repo"], version, 2000)
    process(202, "claude", ["claude"], PROJECTS["shared-repo"], version, 2100)
    process(301, "claude", ["claude"], PROJECTS["crowded-repo"], version, 3000)
    process(401, "codex", ["codex"], PROJECTS["dotted"], None, 4000)
    # cwd deliberately absent: the process is visible and its details are not.
    process(501, "claude", ["claude"], None, None, 5000)
    # A truncated comm, which is what the kernel gives for a long executable name. It must not
    # match, and it is here so that the matcher is known to reject something.
    process(601, "claudia-helper", ["claudia-helper"], PROJECTS["quiet-repo"], None, 6000)
    # A name containing a space and a parenthesis, to exercise the stat parser.
    process(701, "we (ird) name", ["something"], PROJECTS["quiet-repo"], None, 7000)


def log_lines(session_id: str, cwd: str, branch: str, when: float, tools: list[tuple[str, bool]],
              model: str = "test-model-1") -> str:
    """A session log: a metadata line, then turns, some with an unanswered tool call."""
    out = [json.dumps({"type": "last-prompt", "sessionId": session_id, "cwd": cwd,
                       "gitBranch": branch, "version": "9.9.9"})]
    stamp = when
    for index, (tool, answered) in enumerate(tools):
        stamp += 1
        use_id = f"toolu_{index:04d}"
        out.append(json.dumps({
            "type": "assistant", "timestamp": _iso(stamp),
            "message": {"role": "assistant", "model": model,
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000 * (index + 1),
                                  "cache_creation_input_tokens": 100, "output_tokens": 50},
                        "content": [{"type": "tool_use", "id": use_id, "name": tool}]}}))
        if answered:
            stamp += 1
            out.append(json.dumps({
                "type": "user", "timestamp": _iso(stamp),
                "message": {"role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": use_id,
                                         "content": "ok"}]}}))
    return "\n".join(out) + "\n"


def _iso(stamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stamp)) + ".000Z"


def make_claude_home(root: str) -> None:
    projects = os.path.join(root, "projects")

    def add(directory: str, session_id: str, cwd: str, branch: str, when: float,
            tools: list[tuple[str, bool]], mtime: float) -> None:
        path = os.path.join(projects, directory, f"{session_id}.jsonl")
        write(path, log_lines(session_id, cwd, branch, when, tools))
        os.utime(path, (mtime, mtime))

    base = BOOT + 100

    add("-home-testsubject-work-quiet-repo", "11111111-0000-0000-0000-000000000001",
        PROJECTS["quiet-repo"], "main", base, [("Read", True), ("Edit", True)], base + 500)

    # Two logs, one directory. The first is named on 201's command line.
    add("-home-testsubject-work-shared-repo", "aaaaaaaa-1111-2222-3333-444444444444",
        PROJECTS["shared-repo"], "feature", base, [("Bash", True)], base + 600)
    add("-home-testsubject-work-shared-repo", "bbbbbbbb-1111-2222-3333-444444444444",
        PROJECTS["shared-repo"], "feature", base, [("Write", False)], base + 700)

    # Many logs, no way to tell them apart.
    for index in range(4):
        add("-home-testsubject-work-crowded-repo", f"cccccccc-0000-0000-0000-00000000000{index}",
            PROJECTS["crowded-repo"], "main", base, [("Grep", True)], base + 800 + index)

    # The same path under both mangling conventions, which is the situation on a real machine
    # where directories created by different versions sit side by side.
    add("-home-testsubject-.config-dotted", "dddddddd-0000-0000-0000-000000000001",
        PROJECTS["dotted"], "main", base, [("Read", True)], base + 900)
    add("-home-testsubject--config-dotted", "eeeeeeee-0000-0000-0000-000000000001",
        PROJECTS["dotted"], "main", base, [("Read", True)], base + 950)

    # A log whose recorded cwd is a DIFFERENT directory that mangles to the same name. This is
    # the lossy-mangling collision, and it must be rejected on the recorded cwd.
    add("-home-testsubject-work-quiet-repo", "ffffffff-0000-0000-0000-000000000001",
        f"{HOME}/work/quiet/repo", "main", base, [("Read", True)], base + 999)


def main() -> int:
    if os.path.isdir(FIXTURES):
        shutil.rmtree(FIXTURES)
    make_proc(os.path.join(FIXTURES, "proc"))
    make_claude_home(os.path.join(FIXTURES, "claude"))
    counted = sum(len(files) for _, _, files in os.walk(FIXTURES))
    print(f"wrote {counted} fixture files under tests/fixtures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
