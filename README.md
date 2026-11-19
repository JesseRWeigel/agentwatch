# agentwatch

Every Claude Code and `codex` session on this machine, found in the process table rather than
reported by an orchestrator.

Catalog task: `CLI-036`. One of a public catalog of build ideas:
https://github.com/JesseRWeigel/722-things-to-build

**[The board, the correlation cases, and why the join is hard →](https://jesserweigel.github.io/agentwatch/)**

Python 3.10 or later. Standard library only, no install, no configuration, no network.

## What this is

```
$ agentwatch --once
    PID  WHERE                  BRANCH         STATE     TOOL                CTX    OUT   IDLE    AGE  LOG
----------------------------------------------------------------------------------------------------------
   1421  mineflayer-chatgpt     main           idle      Edit               460k    49k   5m48  23h00  44c013fe?
   2385  Projects               -              live      Bash               381k   107k     5s  22h59  61325ca3
 490360  observer-sessions      -              live      -                    98k    41k     5s  11m15  a55bc0d1

? the log shown is a guess, not a match:
    pid 1421: 16 logs record this working directory and nothing here distinguishes them
```

`agentwatch` with no arguments opens the live TUI. Keys: `l` tails the selected session's log,
`c` opens a shell in its working directory, `K` kills it after confirming, `r` refreshes, `q`
quits. `--once` prints the board as text, `--json` emits it for scripting.

## Why this is not the dashboard you already have

An orchestrator knows the agents it launched. A dashboard fed by an orchestrator's gateway knows
the ones that registered themselves. Neither can see a session somebody started by typing
`claude` in a terminal, which is most of them.

This starts from `/proc` instead. The process table is the one authority that does not depend on a
session having told anybody it exists.

## The two hard parts

**Joining a process to its log.** A running session gives you a pid and a working directory. Its
state lives in a JSONL log under a directory named after that working directory with the slashes
replaced. That naming rule is not one rule: on the machine this was written on, of 61 project
directories that record a working directory, 59 follow `"/" → "-"` and 2 also map `"." → "-"`.
Both conventions sit on the same disk, because a directory is created by whichever version was
running at the time and never migrated. The rule is also lossy in every version, since `/a/b` and
`/a-b` produce the same name. So the directory name is only a hint, and the working directory
recorded inside the log is what decides.

**Two agents in one repository.** This is the normal way anybody uses these tools, and it means
several logs can fit one process. When that happens this says so rather than picking the most
recently modified one, because a reader cannot tell a confident answer from a coin flip and the
key next to the answer sends SIGTERM.

Correlating every process at once beats correlating each alone. A log belongs to one session, so a
process that names its session id on its command line claims that log, and a rival sharing the
directory is then left with exactly one candidate. That turns a guess into an answer without
guessing. It came from a real pair of sessions in one directory where the second was being
reported as a choice between two logs, one of which was already spoken for.

## What it refuses to do

**It never presents a guess as a fact.** `Correlation.log` is `None` unless the match is exact or
unique. A caller that wants the likely answer has to ask for it by name, so the display can mark
it, and the board prints a `?` on the row plus a footer saying why.

**It reports a session it cannot read.** `/proc/<pid>/cwd` is readable only by the owner, so
another user's session is visible in the table and opaque in its details. It gets a row saying so.
Dropping it would make "nothing is running" and "something is running that you may not inspect"
print the same board, and the second is exactly when somebody is hunting for missing CPU.

**It marks its one inference as an inference.** Nothing in a log says "waiting for you to approve
a tool call". What it says is that a tool was requested and no result has been written. That is
also what a tool call looks like for its first few seconds, so the only thing separating a running
test suite from a permission prompt nobody is looking at is how long it has been true. The
threshold is a guess, so the reading is shown with its evidence beside it.

## Measured

Reading a session log by seeking to its end takes **8 ms on a 35 MB file**. Reading it whole would
make the monitor the heaviest process on the machine it is watching.

> Timings above were taken on one development machine: an RTX 5090 with 32 GB of VRAM, 12 cores,
> 48 GB of RAM, running Linux under WSL2. Numbers from your own hardware will differ.

## Verifying it

```
bash scripts/verify.sh
```

No network, no install, no running agents required. Every check runs against a committed synthetic
fixture, so the suite passes or fails for reasons about the code rather than about what happened
to be running.

The fixture is the reason the tests mean anything. It contains a process whose `comm` is
`we (ird) name`, a process with no readable working directory, two sessions sharing a directory
where only one names its session id, a log whose recorded working directory rejects the directory
it sits in, and the same path spelled under both mangling conventions. None of those can be
arranged on a live machine on demand.

Worth naming:

- **A sabotage suite** of 26 attacks under three gates: each must apply, must move the measured
  fingerprint, and must then be caught. Three are dormant guards, which invert gate 2. A null
  control copies the unmodified tree into a differently named directory and requires an identical
  fingerprint, without which gate 2 would be free for every attack.
- **An independent recomputation** that ties every process to its log again without importing the
  package, walking every log rather than mangling a directory name, and reading the `stat` fields
  from the right rather than from the left. It walks its own import graph with `ast` and refuses
  to run if anything under it reaches the package, and that detector is exercised against four
  probes it must reject, including a computed `import_module`, and one it must accept.
- **A privacy scan** that plants an invented home path, user name, API key and hostname and fails
  if any survives. This project reads things that identify you as its entire job, so the published
  page is checked with no home path permitted at all.

## Status

The log reader skips non-object JSON records and ignores invalid message content and tool IDs.
One unexpected entry no longer prevents the board from showing the surrounding valid records.

Equal-timestamp candidates also keep the same display order without claiming a match.

Current unit suite:

```text
Ran 67 tests
```

Historical verification below:

`bash scripts/verify.sh`, exit code 0. Pasted whole, from the run that produced it:

```text
== 1. python, standard library only
   python 3.12.3, standard library only
   PASS

== 2. unit tests
----------------------------------------------------------------------
Ran 61 tests in 0.219s

OK
   PASS

== 3. the test count claimed in the README is the count that exists
   README says 61 unit tests and the runner ran 61
   PASS

== 4. the measurement is deterministic and does not track the working directory
   FINGERPRINT d65c8650564f9aea9d5f57039c73fee12f2f7e2ca50c6220d27f643c4a4c974b
   identical across two runs and from a copy under a different name
   PASS

== 5. sabotage suite, three gates and a null control
26 of 26 sabotages caught (3 scored as dormant guard code)
SABOTAGE SUITE PASSED
   PASS

== 6. independent recomputation, importing nothing from the package
  independent: this file imports nothing from agentwatch
  the import detector fires on 4 probes and stays quiet on 1
  recomputed the correlation for 6 processes from the fixture alone, walking every log rather than mangling a directory name
  agreed on confidence and on the chosen log for all 6 sessions
  the stat fields agree when read from the right rather than from the left
INDEPENDENT CHECK PASSED
   PASS

== 7. privacy scan with planted controls
  positive control: the scanner catches all 4 planted values
  negative control: the scanner stays quiet on clean text
  read 71 tracked files; the fixture's invented home is allowed, a real one is not
  docs/index.html: checked with no home path allowed at all
  docs/measured.json: checked with no home path allowed at all
  scanned the tool's own --json output
  no real home path, user name, key or absolute build path in anything committed
   PASS

== 8. the published page is not stale
  the published page matches the package (7 KB)
   PASS

== 9. an uncertain match is never presented as a certain one
   3 of 6 certain, and every verdict carries its reason
   PASS

== 10. the tool runs against the live machine without crashing
   PASS

== 11. --json emits parseable JSON
   6 rows
   PASS

== 12. the README is finished and carries this script's own success line
   10054 chars, 0 problem(s)
   PASS

== 13. verify did not modify the tree it was verifying
   71 tracked files unchanged
   PASS

VERIFY PASSED: agentwatch, 13 of 13 steps
```

## Unfinished

- **Only Linux.** Everything rests on `/proc`. macOS would need `libproc` or `ps` and a different
  way to read a process's working directory, and neither is written.
- **`codex` sessions are found but not read.** They are matched on `comm` and get a row with a pid,
  a working directory and an age, and the log format is not parsed, so the tool, token and idle
  columns stay empty for them.
- **The blocked threshold is a guess, not a measurement.** Ninety seconds separates "running" from
  "stuck" and nothing here justifies that number. Measuring the distribution of real tool-call
  durations would replace it with something defensible.
- **Nothing correlates a session to its subagents.** A session that spawns eight of them shows one
  row and one token figure, and the children are invisible.
- **Token figures are a floor, not a total.** They are summed over the log tail that was read, so
  a long session's true output is higher than the number shown.

## License

MIT.
