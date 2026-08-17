"""Read what a session is doing right now out of the tail of its log.

Three facts matter on a board of eight sessions: what it is doing, how much it has spent, and
whether it is stuck. The first two are recorded. The third is inferred, and this module is careful
about the difference.

BLOCKED IS AN INFERENCE. Nothing in the log says "waiting for the user to approve a tool call".
What it says is that an assistant message asked to use a tool and no result for that tool has been
written yet. That is also what an in-flight tool call looks like for its first few seconds, so the
only thing separating "running a test suite" from "waiting on a permission prompt nobody is
looking at" is how long it has been true. The threshold is a guess, so the inference is returned
with the evidence beside it and the display marks it as an inference.

The tail is read by seeking from the end. These logs reach 35 MB on a long session and this runs
once per session per refresh; reading them whole would make the tool the heaviest thing on the
machine it is meant to be watching, which is a funny way to build a resource monitor.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time


# Enough to hold the last several exchanges on any observed log. Tool results carrying file
# contents are large, so this is generous rather than tight.
TAIL_BYTES = 512 * 1024

# How long a tool call with no result must sit before it is called stuck rather than running.
# A guess, and named so that it reads as one at the call site.
LIKELY_BLOCKED_AFTER_SECONDS = 90.0


@dataclasses.dataclass(frozen=True)
class Activity:
    """What the log says, plus one thing it does not say and this inferred."""

    last_event: float | None = None          # epoch seconds of the most recent entry
    model: str | None = None
    current_tool: str | None = None          # a tool asked for with no result yet
    last_tool: str | None = None             # the most recent tool used at all
    context_tokens: int | None = None        # size of the last request's context
    output_tokens: int = 0                   # summed over the tail, so a floor not a total
    turns_in_tail: int = 0
    awaiting_tool_result: bool = False
    evidence: str = ""

    def idle_seconds(self, now: float | None = None) -> float | None:
        if self.last_event is None:
            return None
        return max(0.0, (now if now is not None else time.time()) - self.last_event)

    def likely_blocked(self, now: float | None = None) -> bool:
        """An inference, not a reading. See the module docstring."""
        if not self.awaiting_tool_result:
            return False
        idle = self.idle_seconds(now)
        return idle is not None and idle >= LIKELY_BLOCKED_AFTER_SECONDS


def read_tail(path: str, limit: int = TAIL_BYTES) -> list[str]:
    """The last complete lines of a file, without reading the front of it."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > limit:
                handle.seek(size - limit)
                handle.readline()      # drop the partial line the seek landed inside
            raw = handle.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def _timestamp(obj: dict) -> float | None:
    stamp = obj.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        # Recorded as RFC 3339 with a trailing Z, which fromisoformat rejects before 3.11.
        from datetime import datetime
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, ImportError):
        return None


def parse(lines: list[str]) -> Activity:
    """Fold a log tail into the current state of the session.

    Walks forwards, because whether a tool call is outstanding depends on what came after it, and
    that question cannot be answered by looking at the last line alone.
    """
    last_event = None
    model = None
    last_tool = None
    context_tokens = None
    output_tokens = 0
    turns = 0
    outstanding: dict[str, str] = {}     # tool_use id -> tool name, emptied by its result

    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue

        stamp = _timestamp(obj)
        if stamp is not None:
            last_event = stamp if last_event is None else max(last_event, stamp)

        kind = obj.get("type")
        message = obj.get("message")
        if not isinstance(message, dict):
            continue

        if kind == "assistant":
            turns += 1
            if isinstance(message.get("model"), str):
                model = message["model"]
            usage = message.get("usage")
            if isinstance(usage, dict):
                # The context is what the model was shown: fresh input plus both cache halves.
                total = 0
                for key in ("input_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int):
                        total += value
                if total:
                    context_tokens = total
                produced = usage.get("output_tokens")
                if isinstance(produced, int):
                    output_tokens += produced
            for item in message.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    name = item.get("name") or "?"
                    last_tool = name
                    if isinstance(item.get("id"), str):
                        outstanding[item["id"]] = name

        elif kind == "user":
            for item in message.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    outstanding.pop(item.get("tool_use_id"), None)

    current = next(iter(outstanding.values()), None) if outstanding else None
    if current is not None:
        evidence = (f"the log's last word is a request to use {current} with no result written "
                    f"for it")
    elif last_tool is not None:
        evidence = f"the last tool call, {last_tool}, has its result"
    else:
        evidence = "no tool call appears in the tail"

    return Activity(last_event=last_event, model=model, current_tool=current, last_tool=last_tool,
                    context_tokens=context_tokens, output_tokens=output_tokens, turns_in_tail=turns,
                    awaiting_tool_result=bool(outstanding), evidence=evidence)


def read(path: str, limit: int = TAIL_BYTES) -> Activity:
    return parse(read_tail(path, limit))
