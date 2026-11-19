"""Reading the log tail, and the one inference this project makes."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentwatch import activity                                 # noqa: E402


def assistant(tool=None, use_id="t1", output=50, context=1000, stamp="2024-01-01T00:00:00.000Z"):
    content = [{"type": "tool_use", "id": use_id, "name": tool}] if tool else [
        {"type": "text", "text": "hello"}]
    return json.dumps({"type": "assistant", "timestamp": stamp,
                       "message": {"role": "assistant", "model": "m1",
                                   "usage": {"input_tokens": 10,
                                             "cache_read_input_tokens": context,
                                             "cache_creation_input_tokens": 0,
                                             "output_tokens": output},
                                   "content": content}})


def result(use_id="t1", stamp="2024-01-01T00:00:01.000Z"):
    return json.dumps({"type": "user", "timestamp": stamp,
                       "message": {"role": "user",
                                   "content": [{"type": "tool_result", "tool_use_id": use_id,
                                                "content": "ok"}]}})


class Parsing(unittest.TestCase):
    def test_an_answered_tool_call_is_not_outstanding(self):
        state = activity.parse([assistant("Read"), result()])
        self.assertFalse(state.awaiting_tool_result)
        self.assertIsNone(state.current_tool)
        self.assertEqual(state.last_tool, "Read")

    def test_an_unanswered_tool_call_is_outstanding(self):
        state = activity.parse([assistant("Bash")])
        self.assertTrue(state.awaiting_tool_result)
        self.assertEqual(state.current_tool, "Bash")

    def test_a_result_only_clears_its_own_call(self):
        """Two calls in flight, one answered. Matching on id rather than on order.

        Clearing "the outstanding call" on any result would report a session as idle while it is
        still waiting on the second of two parallel tool calls.
        """
        state = activity.parse([assistant("Read", "a"), assistant("Bash", "b"), result("a")])
        self.assertTrue(state.awaiting_tool_result)
        self.assertEqual(state.current_tool, "Bash")

    def test_tokens_are_summed_and_context_is_the_last_one(self):
        state = activity.parse([assistant("Read", "a", output=10, context=100), result("a"),
                                assistant("Edit", "b", output=20, context=900), result("b")])
        self.assertEqual(state.output_tokens, 30)
        self.assertEqual(state.context_tokens, 910)   # 10 input + 900 cache read
        self.assertEqual(state.turns_in_tail, 2)

    def test_a_malformed_line_is_skipped_rather_than_fatal(self):
        """Log tails are read from a live writer, so a half-written last line is normal."""
        state = activity.parse(["{not json", "", assistant("Read"), '{"partial":'])
        self.assertEqual(state.last_tool, "Read")

    def test_non_object_json_records_are_ignored(self):
        state = activity.parse(["null", "42", "[]", assistant("Read"), result()])
        self.assertEqual(state.last_tool, "Read")
        self.assertFalse(state.awaiting_tool_result)

    def test_non_array_message_content_is_ignored(self):
        malformed = json.dumps({"type": "assistant", "message": {"content": 42}})
        state = activity.parse([assistant("Edit"), malformed, result()])
        self.assertEqual(state.last_tool, "Edit")
        self.assertFalse(state.awaiting_tool_result)

    def test_non_string_tool_ids_cannot_break_or_clear_outstanding_calls(self):
        bad_result = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": []}]}})
        state = activity.parse([assistant("Read"), bad_result])
        self.assertTrue(state.awaiting_tool_result)
        self.assertEqual(state.current_tool, "Read")
        self.assertFalse(activity.parse([assistant("Read"), bad_result, result()])
                         .awaiting_tool_result)

    def test_non_string_tool_names_are_not_displayed_as_values(self):
        malformed = '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":42}]}}'
        state = activity.parse([malformed])
        self.assertEqual(state.current_tool, "?")

    def test_an_empty_tail_yields_an_empty_state_rather_than_an_error(self):
        state = activity.parse([])
        self.assertIsNone(state.last_event)
        self.assertEqual(state.output_tokens, 0)


class BlockedInference(unittest.TestCase):
    """`blocked` is inferred and the tests say so, because the code says so."""

    def test_a_fresh_unanswered_call_is_not_called_blocked(self):
        """A tool call that just started looks identical to one waiting on a prompt."""
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 5)) + ".000Z"
        state = activity.parse([assistant("Bash", stamp=stamp)])
        self.assertTrue(state.awaiting_tool_result)
        self.assertFalse(state.likely_blocked(now))

    def test_a_stale_unanswered_call_is_called_blocked(self):
        now = time.time()
        old = now - activity.LIKELY_BLOCKED_AFTER_SECONDS - 60
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(old)) + ".000Z"
        state = activity.parse([assistant("Bash", stamp=stamp)])
        self.assertTrue(state.likely_blocked(now))

    def test_an_answered_call_is_never_blocked_however_old(self):
        stamp = "2001-01-01T00:00:00.000Z"
        state = activity.parse([assistant("Bash", stamp=stamp), result(stamp=stamp)])
        self.assertFalse(state.likely_blocked(time.time()))

    def test_the_evidence_is_carried_with_the_inference(self):
        """A claim the reader cannot check is a claim they have to take on faith."""
        state = activity.parse([assistant("Bash")])
        self.assertIn("Bash", state.evidence)
        self.assertIn("no result", state.evidence)


class TailReading(unittest.TestCase):
    def test_only_the_end_of_a_large_file_is_read(self):
        """These logs reach tens of megabytes and this runs once per session per refresh."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for index in range(20000):
                    handle.write(json.dumps({"type": "filler", "n": index, "pad": "x" * 200}) + "\n")
                handle.write(assistant("Edit", "z") + "\n")
            self.assertGreater(os.path.getsize(path), 2_000_000)

            lines = activity.read_tail(path, limit=64 * 1024)
            self.assertLess(len(lines), 2000)
            self.assertEqual(activity.parse(lines).last_tool, "Edit")

    def test_a_partial_first_line_from_the_seek_is_dropped(self):
        """Seeking lands mid-line; keeping that fragment would feed the parser broken JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "a", "pad": "y" * 5000}) + "\n")
                handle.write(assistant("Read", "q") + "\n")
            lines = activity.read_tail(path, limit=1000)
            for line in lines:
                json.loads(line)          # every returned line must be whole

    def test_a_missing_file_yields_no_lines_rather_than_raising(self):
        self.assertEqual(activity.read_tail("/nonexistent/nope.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
