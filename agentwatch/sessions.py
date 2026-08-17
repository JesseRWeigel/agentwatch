"""Find the session log belonging to a process, and say how sure that is.

A running `claude` gives you a pid and a working directory. The interesting state lives in a
JSONL log under `~/.claude/projects/<mangled cwd>/<session id>.jsonl`, and joining the two is the
whole problem.

The obvious join is to mangle the process's cwd into a directory name. It is wrong, and this is
measured rather than assumed: of the 61 project directories on the development machine that
record a cwd, 2 disagree with the rule `"/" -> "-"`, because newer versions also map `"." -> "-"`
(`/home/jesse/.claude-mem/...` becomes `-home-jesse--claude-mem-...`, with the doubled dash) while
older directories on the same disk keep their dots. Both conventions exist side by side, because a
directory is created by whichever version was running at the time and never migrated.

Worse, the mangling is LOSSY in every version: `/a/b` and `/a-b` both become `-a-b`, so even a
perfect implementation of the current rule cannot be inverted.

So the directory name is used only as a HINT. The join is confirmed by reading the `cwd` the log
itself records, which is the only statement of provenance that comes from the session rather than
from a guess about the session.

When the evidence does not single out one log, this module says so. A dashboard that silently
picks the most recent candidate is worse than one that admits two sessions share a directory,
because the reader cannot tell a confident answer from a coin flip. That case is real and common:
two agents working in the same repository is the normal way anybody uses these tools.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import os


CLAUDE_HOME = os.path.join(os.path.expanduser("~"), ".claude")

# How far into a log to look for the metadata line carrying `cwd`. It is near the top in every
# observed file; a bounded scan keeps this cheap on the 8 MB logs a long session produces.
METADATA_SCAN_LINES = 400


class Confidence(enum.Enum):
    """How the log was tied to the process, in descending order of strength."""

    EXACT = "exact"           # the session id was on the command line
    UNIQUE = "unique"         # exactly one log records this cwd and was live for this process
    AMBIGUOUS = "ambiguous"   # several fit, and nothing here can separate them
    NONE = "none"             # no log fits


@dataclasses.dataclass(frozen=True)
class SessionLog:
    path: str
    session_id: str
    recorded_cwd: str | None
    modified: float
    size: int


@dataclasses.dataclass(frozen=True)
class Correlation:
    """The link from a process to its log, carrying the reason it was drawn."""

    confidence: Confidence
    log: SessionLog | None
    candidates: tuple[SessionLog, ...] = ()
    reason: str = ""

    @property
    def is_certain(self) -> bool:
        return self.confidence in (Confidence.EXACT, Confidence.UNIQUE)


def mangle(path: str) -> set[str]:
    """Every directory name a given cwd might have been stored under.

    A set, not a string, because more than one convention is in use and this code does not get to
    decide which version wrote the directory it is looking for.
    """
    slash_only = path.replace("/", "-")
    return {slash_only, slash_only.replace(".", "-")}


def _metadata(path: str) -> tuple[str | None, str | None]:
    """(cwd, gitBranch) as the log records them, or (None, None) if it never says.

    Read line by line with a cap rather than whole. These files reach tens of megabytes and this
    runs once per log per refresh.
    """
    cwd = branch = None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= METADATA_SCAN_LINES:
                    break
                if '"cwd"' not in line and '"gitBranch"' not in line:
                    continue          # cheap reject before the expensive parse
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if cwd is None and isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                if branch is None and isinstance(obj.get("gitBranch"), str):
                    branch = obj["gitBranch"]
                if cwd is not None and branch is not None:
                    break
    except OSError:
        return None, None
    return cwd, branch


def load_log(path: str) -> SessionLog | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    session_id = os.path.splitext(os.path.basename(path))[0]
    recorded_cwd, _ = _metadata(path)
    return SessionLog(path=path, session_id=session_id, recorded_cwd=recorded_cwd,
                      modified=stat.st_mtime, size=stat.st_size)


def logs_for_directory(directory: str) -> list[SessionLog]:
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        log = load_log(os.path.join(directory, name))
        if log is not None:
            out.append(log)
    return out


def candidate_logs(cwd: str, home: str = CLAUDE_HOME) -> list[SessionLog]:
    """Logs plausibly belonging to a session running in `cwd`.

    Directory names supply the candidates and the recorded cwd decides. A log that names a
    different cwd is dropped however well its directory name matched, and a log that names no cwd
    at all is kept, because the oldest logs predate that field and dropping them would make this
    silently blind to long-running sessions.
    """
    projects = os.path.join(home, "projects")
    found: dict[str, SessionLog] = {}
    for name in mangle(cwd):
        for log in logs_for_directory(os.path.join(projects, name)):
            if log.recorded_cwd is not None and log.recorded_cwd != cwd:
                continue
            found[log.path] = log
    return sorted(found.values(), key=lambda log: log.modified, reverse=True)


def correlate(process, home: str = CLAUDE_HOME, started_at: float | None = None) -> Correlation:
    """Tie one process to one log, or explain why that could not be done.

    `started_at` is the process's start time in seconds since the epoch. When it is known, logs
    last written BEFORE the process started are excluded: a session cannot have written its log
    before it existed. That single test resolves most of the two-agents-in-one-repo case, because
    the older session's log stops moving while the newer one keeps growing.
    """
    if process.session_id:
        # The command line named the session. Nothing beats that, but it still has to exist.
        for log in candidate_logs(process.cwd or "", home) if process.cwd else []:
            if log.session_id.lower() == process.session_id:
                return Correlation(Confidence.EXACT, log, (log,),
                                   "the session id was on the command line")
        direct = _log_by_id(process.session_id, home)
        if direct is not None:
            return Correlation(Confidence.EXACT, direct, (direct,),
                               "the session id was on the command line")

    if not process.cwd:
        return Correlation(Confidence.NONE, None, (),
                           "the working directory is not readable, so no log can be found")

    candidates = candidate_logs(process.cwd, home)
    if started_at is not None:
        live = [log for log in candidates if log.modified >= started_at]
        if live:
            candidates = live

    if not candidates:
        return Correlation(Confidence.NONE, None, (),
                           f"no log records {process.cwd} as its working directory")
    if len(candidates) == 1:
        return Correlation(Confidence.UNIQUE, candidates[0], tuple(candidates),
                           "exactly one log records this working directory")
    return Correlation(Confidence.AMBIGUOUS, None, tuple(candidates),
                       f"{len(candidates)} logs record this working directory and nothing here "
                       f"distinguishes them")


def correlate_all(processes, home: str = CLAUDE_HOME, started: dict | None = None) -> dict:
    """Correlate every process at once, which beats correlating each one alone.

    A log belongs to exactly one session. Per-process correlation throws that away and it is worth
    real accuracy: on the development machine two agents shared a directory, one of them named its
    session on the command line, and the other was reported as a coin flip between two logs, one
    of which was already spoken for. Claiming the certain matches first and removing them from
    everyone else's pool turns that second session from AMBIGUOUS into UNIQUE without guessing.

    Only CERTAIN matches claim. An ambiguous process cannot exclude anything, because it has no
    business removing a candidate from a rival on the strength of a guess.
    """
    started = started or {}
    results = {p.pid: correlate(p, home, started.get(p.pid)) for p in processes}

    claimed = {r.log.path for r in results.values() if r.is_certain and r.log is not None}
    if not claimed:
        return results

    for pid, result in list(results.items()):
        if result.is_certain or result.confidence is Confidence.NONE:
            continue
        remaining = [log for log in result.candidates if log.path not in claimed]
        if len(remaining) == 1:
            results[pid] = Correlation(
                Confidence.UNIQUE, remaining[0], tuple(remaining),
                "the only candidate left once other sessions claimed theirs")
        elif len(remaining) != len(result.candidates):
            results[pid] = Correlation(
                Confidence.AMBIGUOUS, None, tuple(remaining),
                f"{len(remaining)} logs remain after other sessions claimed theirs")
    return results


def most_recent(correlation: Correlation) -> SessionLog | None:
    """The best guess when there is no certain answer, kept separate from the certain ones.

    Offered as a separate call rather than folded into `log` on purpose. A caller that wants to
    show a likely answer has to ask for it, which means the display can mark it as a guess. Making
    `log` fall back to this quietly is how an ambiguous match starts being drawn like a sure one.
    """
    if correlation.log is not None:
        return correlation.log
    return correlation.candidates[0] if correlation.candidates else None


def _log_by_id(session_id: str, home: str) -> SessionLog | None:
    """Search every project directory for one session id. Used only when the cwd hint failed."""
    projects = os.path.join(home, "projects")
    try:
        directories = os.listdir(projects)
    except OSError:
        return None
    for name in directories:
        path = os.path.join(projects, name, f"{session_id}.jsonl")
        if os.path.isfile(path):
            return load_log(path)
    return None
