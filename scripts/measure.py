#!/usr/bin/env python3
"""Deterministic measurement of what agentwatch reports, over the committed fixture.

    measure.py            the measurements as JSON
    measure.py --digest   a sha256 of them, which is what the sabotage suite compares

Measurements, never verdicts. The payload records what was found, not whether it was right: the
number of sessions, each one's confidence and the reason for it, the parsed activity. A payload of
booleans saying "correlation worked" would let a sabotage that broke correlation still print True
and clear gate 2 for free, because the only thing being compared would be an opinion.

The fixture is the input, so this is a measurement of the code and not of the machine. Nothing
here reads the real /proc or the real ~/.claude, which is also why it can be committed and
compared byte for byte on somebody else's computer.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agentwatch import activity, board, procs, sessions          # noqa: E402
from tests.common import claude_home, proc_root                  # noqa: E402

# A fixed instant, so durations are stable. Reading the clock here would change the fingerprint
# every second and make the sabotage suite's gate 2 meaningless.
NOW = 1700010000.0


def measure() -> dict:
    root = proc_root()
    home = claude_home()

    processes = procs.discover(root)
    boot = procs.boot_time(root)
    ticks = procs.clock_ticks()
    started = {p.pid: boot + p.start_ticks / ticks for p in processes
               if boot is not None and p.start_ticks is not None}
    correlations = sessions.correlate_all(processes, home=home, started=started)

    found = []
    for process in processes:
        correlation = correlations[process.pid]
        log = sessions.most_recent(correlation)
        state = activity.read(log.path) if log else activity.Activity()
        found.append({
            "pid": process.pid,
            "comm": process.comm,
            "cwd_basename": os.path.basename(process.cwd) if process.cwd else None,
            "opaque": process.is_opaque,
            "version": process.version,
            "session_id_from_argv": process.session_id,
            "confidence": correlation.confidence.value,
            "reason": correlation.reason,
            "certain": correlation.is_certain,
            "candidate_count": len(correlation.candidates),
            "chosen_session": log.session_id if log else None,
            "model": state.model,
            "current_tool": state.current_tool,
            "last_tool": state.last_tool,
            "context_tokens": state.context_tokens,
            "output_tokens": state.output_tokens,
            "turns_in_tail": state.turns_in_tail,
            "awaiting_tool_result": state.awaiting_tool_result,
            "evidence": state.evidence,
        })

    # The stat parser, exercised directly. The fixture's odd-named process is not an agent, so
    # nothing above ever parses a name containing a space and a parenthesis, and a parser that
    # split on whitespace measured identically. Refusals are recorded too: a parse that returns
    # zeros instead of None looks like a process that started at the epoch.
    stat_cases = {
        "ordinary": procs.parse_stat("42 (claude) S 1 " + "0 " * 17 + "12345 0 0 0"),
        "spaces_and_parens": procs.parse_stat("42 (we (ird) name) S 7 " + "0 " * 17 + "999 0 0 0"),
        "too_short": procs.parse_stat("42 (claude) S 1"),
        "no_parens": procs.parse_stat("nonsense"),
        "not_a_number": procs.parse_stat("42 (claude) S x " + "0 " * 17 + "1 0 0 0"),
    }

    # The branch, read from a real repository built here. `want_branch=False` above keeps the
    # board fast and deterministic, which also meant the git call was never measured at all.
    with tempfile.TemporaryDirectory(prefix="agentwatch-git-") as repo:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.path.join(repo, "cfg"),
               "GIT_CONFIG_SYSTEM": os.path.join(repo, "cfg")}
        def git(*args):
            return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                                  env=env)
        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        with open(os.path.join(repo, "f"), "w", encoding="utf-8") as handle:
            handle.write("x")
        git("add", "f")
        git("commit", "-q", "-m", "one")
        git("checkout", "-q", "-b", "measured-branch-name")
        branch_case = {"in_a_repo": board.git_branch(repo),
                       "outside_a_repo": board.git_branch(os.path.dirname(repo))}

    rows = board.build(home=home, proc_root=root, now=NOW, want_branch=False)

    # The RENDERED board, not only the data behind it. Leaving it out cost seven sabotages: an
    # uncertain row drawn as a certain one, the footer explaining it removed, and the empty board
    # made silent all APPLIED, broke something a reader depends on, and moved nothing, because the
    # fingerprint stopped at the row objects and never looked at the text a person actually reads.
    rendered = board.render_text(rows)
    rendered_empty = board.render_text([])

    # Parsing cases the fixture logs cannot produce, each pinned here so a sabotage against it is
    # visible. Two tool calls in flight with one answered is not a state a static fixture holds,
    # and neither is "this call has been outstanding for an hour", which is the whole basis of the
    # blocked inference.
    def turn(tool, use_id, stamp):
        return json.dumps({"type": "assistant", "timestamp": stamp,
                           "message": {"role": "assistant", "model": "m",
                                       "usage": {"input_tokens": 5, "cache_read_input_tokens": 50,
                                                 "cache_creation_input_tokens": 5,
                                                 "output_tokens": 7},
                                       "content": [{"type": "tool_use", "id": use_id,
                                                    "name": tool}]}})

    def answer(use_id, stamp):
        return json.dumps({"type": "user", "timestamp": stamp,
                           "message": {"role": "user",
                                       "content": [{"type": "tool_result", "tool_use_id": use_id,
                                                    "content": "ok"}]}})

    old = "2024-01-01T00:00:00.000Z"
    parallel = activity.parse([turn("Read", "a", old), turn("Bash", "b", old), answer("a", old)])
    stale = activity.parse([turn("Bash", "z", old)])
    fresh_stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW - 1)) + ".000Z"
    fresh = activity.parse([turn("Bash", "z", fresh_stamp)])

    parsing = {
        "two_calls_one_answered": {"current": parallel.current_tool,
                                   "awaiting": parallel.awaiting_tool_result},
        "stale_call_is_blocked": stale.likely_blocked(NOW),
        "fresh_call_is_not_blocked": fresh.likely_blocked(NOW),
        "evidence_for_stale": stale.evidence,
    }

    # A tail read that must actually seek, so a sabotage removing the partial-line discard is
    # visible: without it the first line comes back truncated and fails to parse.
    with tempfile.TemporaryDirectory(prefix="agentwatch-measure-") as tmp:
        big = os.path.join(tmp, "big.jsonl")
        with open(big, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "pad", "pad": "x" * 20000}) + "\n")
            handle.write(turn("Edit", "q", old) + "\n")
        lines = activity.read_tail(big, limit=2048)
        whole = 0
        for line in lines:
            try:
                json.loads(line)
                whole += 1
            except ValueError:
                pass
        tail_read = {"lines": len(lines), "parseable": whole, "all_whole": whole == len(lines)}

    return {
        "sessions": found,
        "rendered_board": rendered,
        "rendered_empty_board": rendered_empty,
        "parsing": parsing,
        "tail_read": tail_read,
        "stat_cases": {k: list(v) if v else None for k, v in stat_cases.items()},
        "branch": branch_case,
        "board": [{"pid": r.pid, "label": r.label, "state": r.state, "certain": r.certain,
                   "tool": r.tool, "context_tokens": r.context_tokens,
                   "output_tokens": r.output_tokens, "age_seconds": r.age_seconds}
                  for r in rows],
        "totals": {
            "sessions": len(found),
            "certain": sum(1 for f in found if f["certain"]),
            "ambiguous": sum(1 for f in found if f["confidence"] == "ambiguous"),
            "opaque": sum(1 for f in found if f["opaque"]),
        },
        "mangling": {
            "with_dots": sorted(sessions.mangle("/home/testsubject/.config/dotted")),
            "without_dots": sorted(sessions.mangle("/a/b/c")),
        },
    }


def main(argv) -> int:
    payload = measure()
    text = json.dumps(payload, indent=2, sort_keys=True)
    if "--digest" in argv:
        print(hashlib.sha256(text.encode()).hexdigest())
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
