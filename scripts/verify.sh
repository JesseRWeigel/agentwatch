#!/usr/bin/env bash
# The verify command. Its exit code is the result.
#
# Standard library only. No network, no install, nothing to configure. Every check runs against a
# committed synthetic fixture rather than against whatever agents happen to be running, so this
# passes or fails for reasons about the code. A suite that needed a live agent to test the agent
# finder would be a suite that reported on the machine's mood.
#
# A missing dependency is a FAILURE and never a skip. A skipped check and a passing check look
# identical in a log a week later.
#
# The tree is digested before and after. A verify run that edits the repository can pass on a
# later run for reasons an earlier run created, which is indistinguishable from working.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

STEP=0
FAILED=0

step() {
  STEP=$((STEP + 1))
  printf '\n== %d. %s\n' "$STEP" "$1"
}

check() {
  if [ "$1" -eq 0 ]; then
    printf '   PASS\n'
  else
    printf '   FAIL (exit %d)\n' "$1"
    FAILED=$((FAILED + 1))
  fi
}

DIGEST=$(cat <<'EOF'
import hashlib, os, subprocess
out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
digest = hashlib.sha256()
for name in sorted(out.stdout.split()):
    if os.path.exists(name):
        with open(name, "rb") as handle:
            digest.update(name.encode())
            digest.update(handle.read())
print(digest.hexdigest())
EOF
)

# ---------------------------------------------------------------- interpreter

step "python, standard library only"
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  printf '   FAIL no python3 on PATH. Install python 3.10 or later; every check below needs it.\n'
  exit 1
fi
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 10):
    print(f"   FAIL python {sys.version.split()[0]}; this needs 3.10 or later")
    raise SystemExit(1)
import agentwatch.board, agentwatch.procs, agentwatch.sessions, agentwatch.activity  # noqa: F401
print(f"   python {sys.version.split()[0]}, standard library only")
EOF
check $?

BEFORE="$("$PY" -c "$DIGEST")"

# ---------------------------------------------------------------- the suite

step "unit tests"
"$PY" -m unittest discover -s tests -t . 2>&1 | tail -4
check "${PIPESTATUS[0]}"

step "the test count claimed in the README is the count that exists"
"$PY" - <<'EOF'
import re, subprocess, sys
out = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                     capture_output=True, text=True)
ran = int(re.search(r"^Ran (\d+) tests", out.stderr, re.M).group(1))
readme = open("README.md", encoding="utf-8").read()
claimed = [int(n) for n in re.findall(r"Ran (\d+) tests", readme)]
if not claimed:
    print("   FAIL the README pastes no test count, so it cannot be checked")
    raise SystemExit(1)
if ran not in claimed:
    print(f"   FAIL the README claims {claimed} tests and the runner ran {ran}")
    raise SystemExit(1)
print(f"   README says {ran} unit tests and the runner ran {ran}")
EOF
check $?

# ---------------------------------------------------------------- determinism

step "the measurement is deterministic and does not track the working directory"
"$PY" - <<'EOF'
import hashlib, os, shutil, subprocess, sys, tempfile

root = os.getcwd()
def run(cwd):
    out = subprocess.run([sys.executable, os.path.join("scripts", "measure.py"), "--digest"],
                         cwd=cwd, capture_output=True, text=True, timeout=900)
    if out.returncode != 0:
        print(f"   FAIL measure.py exited {out.returncode}\n{out.stderr[-800:]}")
        raise SystemExit(1)
    return out.stdout.strip()

first = run(root)
if run(root) != first:
    print("   FAIL two runs in the same directory disagree")
    raise SystemExit(1)

# A DIFFERENTLY NAMED directory. A copy whose last path component matched would hide exactly the
# bug this looks for, and the sabotage suite's null control depends on this holding.
with tempfile.TemporaryDirectory(prefix="agentwatch-elsewhere-") as tmp:
    elsewhere = os.path.join(tmp, "a-different-name")
    shutil.copytree(root, elsewhere, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    if run(elsewhere) != first:
        print("   FAIL the measurement changes when the tree is moved, so it reads something "
              "outside itself")
        raise SystemExit(1)

print(f"   FINGERPRINT {first}")
print("   identical across two runs and from a copy under a different name")
EOF
check $?

# ---------------------------------------------------------------- sabotage

step "sabotage suite, three gates and a null control"
SABOTAGE_LOG="$(mktemp)"
timeout 3600 "$PY" scripts/sabotage.py > "$SABOTAGE_LOG" 2>&1
SABOTAGE_EXIT=$?
# Every FAIL line, not a fixed-size tail. A tail shorter than the failure list reports fewer
# problems than the suite found, which is the one thing a verify step must never do.
grep -E "^ *(gate [0-9] FAIL|FAIL|[0-9]+ of [0-9]+|SABOTAGE SUITE)" "$SABOTAGE_LOG" \
  || tail -4 "$SABOTAGE_LOG"
rm -f "$SABOTAGE_LOG"
check "$SABOTAGE_EXIT"

# ---------------------------------------------------------------- independence

step "independent recomputation, importing nothing from the package"
"$PY" scripts/check_independent.py 2>&1 | tail -8
check "${PIPESTATUS[0]}"

# ---------------------------------------------------------------- privacy

step "privacy scan with planted controls"
"$PY" scripts/privacy_scan.py 2>&1 | tail -8
check "${PIPESTATUS[0]}"

# ---------------------------------------------------------------- the page

step "the published page is not stale"
"$PY" scripts/build_docs.py --check 2>&1 | tail -3
check "${PIPESTATUS[0]}"

# ---------------------------------------------------------------- the claims

step "an uncertain match is never presented as a certain one"
"$PY" - <<'EOF'
import sys
sys.path.insert(0, ".")
from agentwatch import procs, sessions
from tests.common import claude_home, proc_root

processes = procs.discover(proc_root())
results = sessions.correlate_all(processes, home=claude_home())
problems = []
for pid, result in sorted(results.items()):
    if not result.is_certain and result.log is not None:
        problems.append(f"pid {pid} is {result.confidence.value} and still returned a log")
    if result.is_certain and result.log is None:
        problems.append(f"pid {pid} is {result.confidence.value} and returned no log")
    if not result.reason:
        problems.append(f"pid {pid} gives no reason for its verdict")
if problems:
    for problem in problems:
        print(f"   FAIL {problem}")
    raise SystemExit(1)
certain = sum(1 for r in results.values() if r.is_certain)
print(f"   {certain} of {len(results)} certain, and every verdict carries its reason")
EOF
check $?

step "the tool runs against the live machine without crashing"
# It may find nothing, and that is a valid result. What must not happen is a traceback on
# somebody's real process table, which is the one input this project cannot rehearse.
"$PY" -m agentwatch --once --no-branch > /dev/null 2>&1
check $?

step "--json emits parseable JSON"
"$PY" -m agentwatch --json --no-branch --proc tests/fixtures/proc --home tests/fixtures/claude \
  2>/dev/null | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(f'   {len(d)} rows')"
check "${PIPESTATUS[1]}"

# ---------------------------------------------------------------- the README

step "the README is finished and carries this script's own success line"
"$PY" - <<'EOF'
import re, sys
with open("README.md", encoding="utf-8") as handle:
    text = handle.read()
prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
problems = []
for marker in ("TODO", "NOT YET VERIFIED", "Everything."):
    if marker in prose:
        problems.append(f"the README still contains the scaffold marker {marker!r}")
for heading in ("## Status", "## Unfinished"):
    if heading not in text:
        problems.append(f"no {heading} section")
if "VERIFY PASSED: agentwatch" not in text:
    problems.append("the Status section does not carry this script's success line")
print(f"   {len(text)} chars, {len(problems)} problem(s)")
for message in problems:
    print(f"   {message}")
sys.exit(1 if problems else 0)
EOF
check $?

# ---------------------------------------------------------------- the tree

step "verify did not modify the tree it was verifying"
AFTER="$("$PY" -c "$DIGEST")"
if [ "$BEFORE" = "$AFTER" ]; then
  printf '   %s tracked files unchanged\n' "$(git ls-files | wc -l)"
  check 0
else
  printf '   the tree changed during verification\n     before %s\n     after  %s\n' \
    "$BEFORE" "$AFTER"
  check 1
fi

# ---------------------------------------------------------------- result

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'VERIFY PASSED: agentwatch, %d of %d steps\n' "$STEP" "$STEP"
  exit 0
fi
printf 'VERIFY FAILED: agentwatch, %d of %d steps failed\n' "$FAILED" "$STEP"
exit 1
