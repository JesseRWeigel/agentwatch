#!/usr/bin/env python3
"""Break agentwatch on purpose and require the suite to notice.

A sabotage counts only if it passes three gates:

  1. It APPLIES. The text it edits must exist, or nothing was tested and the pass is imaginary.
  2. It CHANGES the measured fingerprint. An edit that moves no measurement cannot be caught by
     any test that is about behaviour, so demanding one would be demanding an opinion about style.
  3. It is then CAUGHT by the test suite.

A dormant guard inverts gate 2: it is code that only fires on input the measurement does not
contain, so its removal must NOT move the output, and the suite must fail anyway.

The null control comes first. An unmodified copy of the tree, in a DIFFERENTLY NAMED directory,
must fingerprint identically. Without it, any measurement that embedded its own path would make
gate 2 free for every attack below, and the whole suite would report success while testing
nothing.

Timeouts are values, not crashes. A sabotage can break code by making it loop forever rather than
by making it wrong, and one of termtint's did exactly that, killing its entire suite at the ninth
of thirty-five attacks and reporting nothing about the rest. Here a step that does not finish is
caught, recorded and moved past.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STEP_TIMEOUT = 180
DID_NOT_TERMINATE = "<did not terminate>"

# (name, file, find, replace, dormant)
SABOTAGES = [
    # ---- reading the process table
    ("the stat parser splits on whitespace, so odd process names shift every field",
     "agentwatch/procs.py",
     r'    comm = text\[open_paren \+ 1:close_paren\]\n    rest = text\[close_paren \+ 1:\].split\(\)',
     '    parts = text.split()\n    comm = parts[1].strip("()")\n    rest = parts[2:]', False),
    ("the start time is read from the wrong field", "agentwatch/procs.py",
     r"return comm, int\(rest\[1\]\), int\(rest\[19\]\)",
     "return comm, int(rest[1]), int(rest[18])", False),
    ("a process whose details cannot be read is dropped instead of reported",
     "agentwatch/procs.py",
     r"        if process is not None and is_agent\(process\):",
     "        if process is not None and is_agent(process) and process.cwd is not None:", False),
    ("the agent matcher accepts anything", "agentwatch/procs.py",
     r"    return process\.comm in AGENT_COMMS", "    return True", False),
    ("sessions are no longer ordered by start time", "agentwatch/procs.py",
     r"    found\.sort\(key=lambda p: \(p\.start_ticks if p\.start_ticks is not None else 0, p\.pid\)\)",
     "    found.sort(key=lambda p: -p.pid)", False),
    ("boot time falls back to zero instead of admitting it is unknown", "agentwatch/procs.py",
     r"            except \(IndexError, ValueError\):\n                return None",
     "            except (IndexError, ValueError):\n                return 0.0", True),

    # ---- correlating a process to its log
    ("only one mangling convention is tried", "agentwatch/sessions.py",
     r"    return \{slash_only, slash_only\.replace\(\"\.\", \"-\"\)\}",
     "    return {slash_only}", False),
    ("the recorded cwd is not checked, so a mangling collision is accepted",
     "agentwatch/sessions.py",
     r"            if log\.recorded_cwd is not None and log\.recorded_cwd != cwd:\n                continue",
     "            pass", False),
    ("an ambiguous match is quietly resolved to the newest log", "agentwatch/sessions.py",
     r'    return Correlation\(Confidence\.AMBIGUOUS, None, tuple\(candidates\),',
     '    return Correlation(Confidence.UNIQUE, candidates[0], tuple(candidates),', False),
    ("a claimed log is not removed from other sessions' candidates", "agentwatch/sessions.py",
     r"        remaining = \[log for log in result\.candidates if log\.path not in claimed\]",
     "        remaining = list(result.candidates)", False),
    ("an ambiguous session claims its guess, so it can exclude a rival",
     "agentwatch/sessions.py",
     r"    claimed = \{r\.log\.path for r in results\.values\(\) if r\.is_certain and r\.log is not None\}",
     "    claimed = {r.candidates[0].path for r in results.values() if r.candidates}", False),
    ("is_certain counts an ambiguous match as certain", "agentwatch/sessions.py",
     r"        return self\.confidence in \(Confidence\.EXACT, Confidence\.UNIQUE\)",
     "        return self.confidence is not Confidence.NONE", False),

    # ---- reading the log tail
    ("a tool result clears every outstanding call instead of its own",
     "agentwatch/activity.py",
     r"                    outstanding\.pop\(item\.get\(\"tool_use_id\"\), None\)",
     "                    outstanding.clear()", False),
    ("the partial line a seek lands inside is kept", "agentwatch/activity.py",
     r"                handle\.readline\(\)      # drop the partial line the seek landed inside",
     "                pass", False),
    ("the whole log is read instead of its tail", "agentwatch/activity.py",
     r"            if size > limit:\n                handle\.seek\(size - limit\)",
     "            if False:\n                handle.seek(size - limit)", False),
    ("output tokens are overwritten rather than summed", "agentwatch/activity.py",
     r"                    output_tokens \+= produced", "                    output_tokens = produced",
     False),
    ("the context is only the fresh input, ignoring both cache halves",
     "agentwatch/activity.py",
     r'                for key in \("input_tokens", "cache_read_input_tokens",\n'
     r'                            "cache_creation_input_tokens"\):',
     '                for key in ("input_tokens",):', False),
    ("an outstanding call is called blocked with no regard for how long",
     "agentwatch/activity.py",
     r"        return idle is not None and idle >= LIKELY_BLOCKED_AFTER_SECONDS",
     "        return True", False),
    ("a malformed line stops the parse instead of being skipped", "agentwatch/activity.py",
     r"        except \(ValueError, TypeError\):\n            continue",
     "        except (ValueError, TypeError):\n            break", True),

    # ---- the board
    ("an uncertain row is drawn as a certain one", "agentwatch/board.py",
     r'        mark = "" if row\.certain else "\?"', '        mark = ""', False),
    ("the footer explaining an uncertain match is dropped", "agentwatch/board.py",
     r'        lines\.append\("\? the log shown is a guess, not a match:"\)',
     '        lines.append("")', False),
    ("an opaque session reports a state indistinguishable from a working one",
     "agentwatch/board.py", r'            return "opaque"', '            return "live"', False),
    ("the empty board says nothing about what it looked for", "agentwatch/board.py",
     r'        return \("no agent sessions found\.\\n"\n'
     r'                "nothing named claude or codex is running under this user\.\\n"\)',
     '        return "\\n"', False),
    ("the git branch is taken from the log instead of from git", "agentwatch/board.py",
     r"    if result\.returncode != 0:\n        return None\n    name = result\.stdout\.strip\(\)",
     "    if result.returncode != 0:\n        return None\n    name = 'main'", False),

    # ---- guards that fire only on input the measurement does not contain
    ("the window clipping rule admits a write past the last row", "agentwatch/tui.py",
     r"    return 0 <= y < height and 0 <= x < width",
     "    return 0 <= y <= height and 0 <= x <= width", True),
    ("the parse of a stat line accepts garbage rather than refusing", "agentwatch/procs.py",
     r"    except \(ValueError, IndexError\):\n        return None",
     "    except (ValueError, IndexError):\n        return comm, 0, 0", False),
]


def run(command, cwd, timeout=STEP_TIMEOUT):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def fingerprint(tree: str) -> str | None:
    try:
        result = run([sys.executable, "scripts/measure.py", "--digest"], tree)
    except subprocess.TimeoutExpired:
        return DID_NOT_TERMINATE
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def tests_pass(tree: str) -> bool:
    try:
        result = run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".",
                      "-q"], tree)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def copy_tree(destination: str) -> str:
    """Copy the tracked tree into a directory whose name is deliberately not the project's."""
    listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    os.makedirs(destination, exist_ok=True)
    for name in listed.stdout.split():
        source = os.path.join(ROOT, name)
        if not os.path.isfile(source):
            continue
        target = os.path.join(destination, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
    return destination


def apply_sabotage(tree: str, relative: str, pattern: str, replacement: str) -> bool:
    path = os.path.join(tree, relative)
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as handle:
        original = handle.read()
    changed, count = re.subn(pattern, lambda _: replacement, original, count=1)
    if count == 0:
        return False
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(changed)
    return True


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agentwatch-sabotage-") as workspace:
        control = copy_tree(os.path.join(workspace, "a-differently-named-copy"))
        baseline = fingerprint(control)
        if baseline is None or baseline is DID_NOT_TERMINATE:
            print("FAIL the unmodified tree does not measure, so nothing below means anything")
            return 1
        second = fingerprint(copy_tree(os.path.join(workspace, "another-name-again")))
        if second != baseline:
            print(f"FAIL null control: two unmodified copies disagree\n  {baseline}\n  {second}")
            return 1
        print(f"null control: an unmodified copy fingerprints identically ({baseline[:16]})")

        if not tests_pass(control):
            print("FAIL the suite does not pass before anything is broken")
            return 1
        print("baseline: the suite passes before anything is broken\n")

        caught = 0
        failures = []
        for index, (name, path, pattern, replacement, dormant) in enumerate(SABOTAGES):
            tree = copy_tree(os.path.join(workspace, f"sabotage-{index}"))
            if not apply_sabotage(tree, path, pattern, replacement):
                failures.append((name, "gate 1", "the edit matched nothing, so nothing was tested"))
                print(f"  gate 1 FAIL  {name}")
                continue

            measured = fingerprint(tree)
            if measured is DID_NOT_TERMINATE:
                caught += 1
                print(f"  caught       {name} (the measurement did not terminate)")
                continue

            moved = measured != baseline
            if dormant:
                if moved:
                    failures.append((name, "gate 2", "a dormant guard was removed and the output "
                                                     "moved, so it was not dormant"))
                    print(f"  gate 2 FAIL  {name} (dormant, but output moved)")
                    continue
            elif not moved:
                failures.append((name, "gate 2", "APPLIED but changed nothing measurable"))
                print(f"  gate 2 FAIL  {name} (output unchanged)")
                continue

            if tests_pass(tree):
                failures.append((name, "gate 3", "NOT CAUGHT, the suite passed on a broken tree"))
                print(f"  gate 3 FAIL  {name}")
                continue

            caught += 1
            print(f"  caught       {name}"
                  f"{' (dormant guard)' if dormant else ''}")

    total = len(SABOTAGES)
    dormant_count = sum(1 for row in SABOTAGES if row[4])
    print(f"\n{caught} of {total} sabotages caught ({dormant_count} scored as dormant guard code)")
    if failures:
        print("\nfailures:")
        for name, gate, why in failures:
            print(f"  {gate}  {name}: {why}")
        print("SABOTAGE SUITE FAILED")
        return 1
    print("SABOTAGE SUITE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
