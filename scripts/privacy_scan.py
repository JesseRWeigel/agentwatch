#!/usr/bin/env python3
"""Prove that nothing about this machine leaves it.

This project has the worst privacy surface of anything in the fleet, because reading things that
identify you is its ENTIRE JOB. It collects home directory paths, the user name inside them,
repository names, branch names, session ids and command lines. On the terminal that is correct and
wanted. In anything published it is a disclosure, and the published artifacts are the docs page,
the committed fixtures and the measurements.

So the scan does three things.

It plants values and requires them to be absent. The planted home path, user name, API key and
hostname are INVENTED here rather than read from the environment. A scan that looks for the real
`$HOME` passes trivially on any machine whose checkout is not under a home directory, and would
have passed on CI while leaking on every laptop.

It proves it can fail before trusting that it passed. A positive control is pushed through the
same code, and if the scanner does not catch its own plants then every clean result below it is
worth nothing.

It assembles its patterns from fragments at runtime, so this file does not contain the literals it
searches for. Written out whole, a scan that walks the repository matches itself, and the usual
repair is to exclude the scanner from its own scan, which is how a scanner stops looking at the
one file most likely to contain a hardcoded secret.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Invented. Not read from the environment, on purpose.
PLANTED_USER = "plantedper" + "sonname"
PLANTED_HOME = "/" + "home/" + PLANTED_USER
PLANTED_KEY = "sk" + "-" + "live" + "-" + "PLANTEDSECRET" + "0123456789"
PLANTED_HOST = "planted" + "-" + "host" + ".internal.example"

PATTERNS = {
    "a home directory path": re.compile(r"/" + r"home/" + r"[A-Za-z0-9._-]+"),
    "a Windows user path": re.compile(r"[Cc]:\\\\?" + r"Users\\\\?" + r"[A-Za-z0-9._-]+"),
    "an API key": re.compile(r"\b" + r"sk-" + r"[A-Za-z0-9-]{12,}"),
    "an absolute build path": re.compile(re.escape(ROOT)),
}

# The fixture is allowed to contain its own invented home, and only that one.
FIXTURE_HOME = "/home/testsubject"


def scan(text: str, where: str, allow_fixture_home: bool = False) -> list[str]:
    problems = []
    for planted, label in ((PLANTED_HOME, "the planted home path"),
                           (PLANTED_USER, "the planted user name"),
                           (PLANTED_KEY, "the planted API key"),
                           (PLANTED_HOST, "the planted hostname")):
        if planted in text:
            problems.append(f"{where} contains {label}")
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            found = match.group(0)
            if allow_fixture_home and found.startswith(FIXTURE_HOME):
                continue
            problems.append(f"{where} contains {label}: {found[:60]}")
            break
    return problems


def main() -> int:
    problems: list[str] = []

    # 1. The scanner must be able to fail.
    control = (f"exported from {PLANTED_HOME}/pictures by {PLANTED_USER} with key {PLANTED_KEY} "
               f"on {PLANTED_HOST}")
    caught = scan(control, "positive control")
    if len(caught) < 4:
        print(f"FAIL the scanner missed its own positive control, catching {len(caught)} of 4, "
              f"so a clean result below would prove nothing")
        return 1
    print(f"  positive control: the scanner catches all 4 planted values")

    # 2. A negative control. The scanner must also be capable of staying quiet, or it would fail
    #    everything and look thorough while telling nobody anything.
    if scan("a perfectly ordinary sentence about colours", "negative control"):
        print("FAIL the scanner fires on text containing nothing, so it cannot distinguish")
        return 1
    print("  negative control: the scanner stays quiet on clean text")

    # 3. Every committed file. The fixture's invented home is permitted; a real one is not.
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    files = [f for f in tracked.stdout.split() if f]
    read = 0
    for name in files:
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path) or name.endswith((".png", ".jpg", ".ico")):
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            body = handle.read()
        read += 1
        problems += scan(body, name, allow_fixture_home=True)
    print(f"  read {read} tracked files; the fixture's invented home is allowed, a real one "
          f"is not")

    # 4. The published page and measurements, where the fixture home is NOT allowed either.
    #    Nothing published should carry any home path at all, invented or otherwise.
    for relative in ("docs/index.html", "docs/measured.json"):
        path = os.path.join(ROOT, relative)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            problems += scan(handle.read(), relative, allow_fixture_home=False)
        print(f"  {relative}: checked with no home path allowed at all")

    # 5. The tool's own JSON output, which is the thing a user is most likely to paste into an
    #    issue. It runs against the fixture, so the only paths in it are invented ones, but it
    #    must not carry anything from the machine running the scan.
    result = subprocess.run([sys.executable, "-m", "agentwatch", "--json", "--no-branch",
                             "--proc", os.path.join(ROOT, "tests", "fixtures", "proc"),
                             "--home", os.path.join(ROOT, "tests", "fixtures", "claude")],
                            cwd=ROOT, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"FAIL the tool exited {result.returncode} and its output could not be scanned")
        return 1
    json.loads(result.stdout)
    problems += scan(result.stdout, "the tool's --json output", allow_fixture_home=True)
    print("  scanned the tool's own --json output")

    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1
    print("  no real home path, user name, key or absolute build path in anything committed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
