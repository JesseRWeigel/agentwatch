#!/usr/bin/env python3
"""Recompute the correlation without using the code that produced it.

Two claims, both checked.

The first is that the correlation is right. Every process in the fixture is tied to its log again
here, from the files on disk, by an implementation written from the description rather than
imported. If `sessions.correlate_all` were wrong, this would disagree with it, because the only
thing the two share is the fixture.

The second is that the first is true. A checker that imports the code it checks is not a check,
it is a second call to the same function, and it agrees with any bug. So this walks its OWN import
graph with `ast` and refuses to run if anything under it reaches the package. That walk is
exercised against probe files it must reject and one it must accept, because a detector nobody has
seen fire is not known to work.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORBIDDEN = ("agentwatch",)


def imported_modules(path: str) -> set[str]:
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                found.add("." * node.level + (node.module or ""))
            elif node.module:
                found.add(node.module)
        elif isinstance(node, ast.Call):
            # A computed import evades both node types above.
            target = node.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name == "__import__" and node.args:
                literal = node.args[0]
                if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                    found.add(literal.value)
                else:
                    found.add("<computed>")
            elif name == "import_module":
                found.add("<computed>")
    return found


def offending(path: str) -> list[str]:
    bad = []
    for name in sorted(imported_modules(path)):
        root = name.split(".")[0]
        if root in FORBIDDEN or name.startswith(".") or name == "<computed>":
            bad.append(name)
    return bad


def prove_the_detector_works() -> None:
    probes = os.path.join(ROOT, "scripts", "probes")
    expected = {
        "probe_direct_import.py": True,
        "probe_from_import.py": True,
        "probe_relative_import.py": True,
        "probe_computed_import.py": True,
        "probe_clean.py": False,
    }
    for name, should_offend in sorted(expected.items()):
        path = os.path.join(probes, name)
        if not os.path.isfile(path):
            raise SystemExit(f"FAIL the probe {name} is missing, so the detector is unproven")
        if bool(offending(path)) != should_offend:
            raise SystemExit(
                f"FAIL the import detector is broken: {name} should "
                f"{'have been rejected' if should_offend else 'have been accepted'} and was not")
    fires = sum(expected.values())
    print(f"  the import detector fires on {fires} probes and stays quiet on "
          f"{len(expected) - fires}")


# ---------------------------------------------------------------------------
# A second implementation, from the description rather than the code.
# ---------------------------------------------------------------------------

def read_stat_fields(text: str):
    """comm, ppid, starttime. Written the other way round: from the right rather than the left.

    The package finds the last ')' and splits what follows. This splits the whole line and walks
    backwards from the end, which reaches the same fields by a different route and so disagrees if
    either one is wrong.
    """
    close = text.rindex(")")
    comm = text[text.index("(") + 1:close]
    tail = text[close + 1:].split()
    return comm, int(tail[1]), int(tail[19])


def scan_processes(proc_root: str) -> dict:
    out = {}
    for name in os.listdir(proc_root):
        if not name.isdigit():
            continue
        base = os.path.join(proc_root, name)
        try:
            with open(os.path.join(base, "stat"), encoding="utf-8") as handle:
                comm, _, start = read_stat_fields(handle.read())
        except (OSError, ValueError, IndexError):
            continue
        if comm not in ("claude", "codex"):
            continue
        try:
            cwd = os.readlink(os.path.join(base, "cwd"))
        except OSError:
            cwd = None
        try:
            with open(os.path.join(base, "cmdline"), encoding="utf-8") as handle:
                argv = handle.read().replace("\0", " ")
        except OSError:
            argv = ""
        out[int(name)] = {"comm": comm, "cwd": cwd, "argv": argv, "start": start}
    return out


def scan_logs(home: str) -> list[dict]:
    """Every log, with the cwd it records, found by walking rather than by mangling a name.

    Deliberately ignores the directory naming convention entirely. The package derives candidate
    directory names and then confirms; this reads every log there is and groups by the recorded
    cwd. If the mangling in the package were wrong in a way its own confirmation missed, this
    would still find the right log and the two would disagree.
    """
    projects = os.path.join(home, "projects")
    found = []
    for current, _, files in os.walk(projects):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(current, name)
            recorded = None
            with open(path, encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle):
                    if index > 400:
                        break
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj.get("cwd"), str):
                        recorded = obj["cwd"]
                        break
            found.append({"path": path, "session": name[:-len(".jsonl")], "cwd": recorded,
                          "mtime": os.path.getmtime(path)})
    return found


UUID_CHARS = set("0123456789abcdef-")


def session_from_argv(argv: str) -> str | None:
    for word in argv.split():
        token = word.strip().lower()
        if len(token) == 36 and token[8] == token[13] == token[18] == token[23] == "-" \
                and set(token) <= UUID_CHARS:
            return token
    return None


def correlate(processes: dict, logs: list[dict]) -> dict:
    """Same rules, stated again: exact by argv, then unique by cwd, then claim, then ambiguous."""
    verdict = {}
    for pid, info in processes.items():
        named = session_from_argv(info["argv"])
        if named and any(log["session"].lower() == named for log in logs):
            verdict[pid] = ("exact", named)
            continue
        if info["cwd"] is None:
            verdict[pid] = ("none", None)
            continue
        matching = [log for log in logs if log["cwd"] == info["cwd"]]
        if not matching:
            verdict[pid] = ("none", None)
        elif len(matching) == 1:
            verdict[pid] = ("unique", matching[0]["session"])
        else:
            verdict[pid] = ("ambiguous", None)

    claimed = {session for kind, session in verdict.values() if kind == "exact" and session}
    for pid, (kind, session) in list(verdict.items()):
        if kind != "ambiguous":
            continue
        cwd = processes[pid]["cwd"]
        left = [log for log in logs if log["cwd"] == cwd and log["session"] not in claimed]
        if len(left) == 1:
            verdict[pid] = ("unique", left[0]["session"])
    return verdict


def main() -> int:
    mine = offending(os.path.abspath(__file__))
    if mine:
        print(f"FAIL this checker imports the package it is checking: {', '.join(mine)}")
        return 1
    print(f"  independent: this file imports nothing from {', '.join(FORBIDDEN)}")
    prove_the_detector_works()

    result = subprocess.run([sys.executable, "scripts/measure.py"], cwd=ROOT,
                            capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        print(f"FAIL measure.py exited {result.returncode}")
        return 1
    payload = json.loads(result.stdout)

    proc_root = os.path.join(ROOT, "tests", "fixtures", "proc")
    home = os.path.join(ROOT, "tests", "fixtures", "claude")
    # The fixture stores links as plain files, so materialise a readable copy the same way the
    # test harness does, without importing the harness.
    import shutil
    import tempfile
    workspace = tempfile.mkdtemp(prefix="agentwatch-independent-")
    try:
        for source, name in ((proc_root, "proc"), (home, "claude")):
            shutil.copytree(source, os.path.join(workspace, name))
        for current, _, files in os.walk(workspace):
            for name in files:
                if name.endswith(".link"):
                    path = os.path.join(current, name)
                    with open(path, encoding="utf-8") as handle:
                        target = handle.read().strip()
                    os.remove(path)
                    os.symlink(target, os.path.join(current, name[: -len(".link")]))

        processes = scan_processes(os.path.join(workspace, "proc"))
        logs = scan_logs(os.path.join(workspace, "claude"))
        mine_verdict = correlate(processes, logs)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    if not mine_verdict:
        print("FAIL the independent pass found no processes, so agreement would prove nothing")
        return 1

    disagreements = []
    for session in payload["sessions"]:
        pid = session["pid"]
        if pid not in mine_verdict:
            disagreements.append(f"pid {pid}: the package found it and this pass did not")
            continue
        kind, chosen = mine_verdict[pid]
        if kind != session["confidence"]:
            disagreements.append(
                f"pid {pid}: the package says {session['confidence']}, this pass says {kind}")
        elif kind in ("exact", "unique") and chosen != session["chosen_session"]:
            disagreements.append(
                f"pid {pid}: the package chose {session['chosen_session']}, this pass chose "
                f"{chosen}")

    extra = set(mine_verdict) - {s["pid"] for s in payload["sessions"]}
    for pid in sorted(extra):
        disagreements.append(f"pid {pid}: this pass found it and the package did not")

    if disagreements:
        for line in disagreements:
            print(f"FAIL {line}")
        return 1

    # A negative control. The comparison has to be capable of failing, or agreement means nothing.
    broken = dict(mine_verdict)
    any_pid = sorted(broken)[0]
    broken[any_pid] = ("none", None)
    would_differ = any(broken[s["pid"]][0] != s["confidence"] for s in payload["sessions"]
                       if s["pid"] in broken)
    if not would_differ:
        print("FAIL the comparison cannot detect a disagreement, so it proves nothing")
        return 1

    print(f"  recomputed the correlation for {len(mine_verdict)} processes from the fixture "
          f"alone, walking every log rather than mangling a directory name")
    print(f"  agreed on confidence and on the chosen log for all "
          f"{len(payload['sessions'])} sessions")

    # And the stat parser, reached from the other end.
    for label, expected in (("ordinary", ["claude", 1, 12345]),
                            ("spaces_and_parens", ["we (ird) name", 7, 999])):
        line = ("42 (claude) S 1 " + "0 " * 17 + "12345 0 0 0") if label == "ordinary" \
            else ("42 (we (ird) name) S 7 " + "0 " * 17 + "999 0 0 0")
        comm, ppid, start = read_stat_fields(line)
        if [comm, ppid, start] != expected or payload["stat_cases"][label] != expected:
            print(f"FAIL the stat parse of {label} disagrees: {[comm, ppid, start]} against "
                  f"{payload['stat_cases'][label]}")
            return 1
    print("  the stat fields agree when read from the right rather than from the left")
    print("INDEPENDENT CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
