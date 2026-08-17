"""The assembled board, and what it refuses to say."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentwatch import board                                    # noqa: E402
from tests.common import claude_home, proc_root                 # noqa: E402


class Building(unittest.TestCase):
    def setUp(self):
        # want_branch=False so the suite never shells out to git. A test that runs `git` against
        # a path invented for a fixture measures the machine, not the code.
        self.rows = board.build(home=claude_home(), proc_root=proc_root(), now=1700010000.0,
                                want_branch=False)
        self.by_pid = {row.pid: row for row in self.rows}

    def test_every_discovered_session_gets_a_row(self):
        self.assertEqual(sorted(self.by_pid), [101, 201, 202, 301, 401, 501])

    def test_an_unreadable_session_is_shown_as_opaque_not_dropped(self):
        row = self.by_pid[501]
        self.assertTrue(row.opaque)
        self.assertEqual(row.state, "opaque")
        self.assertEqual(row.label, "(not readable)")

    def test_an_uncertain_row_is_marked_uncertain(self):
        self.assertFalse(self.by_pid[301].certain)
        self.assertTrue(self.by_pid[201].certain)

    def test_the_age_comes_from_boot_time_plus_start_ticks(self):
        # fixture boot is 1700000000, pid 101 starts at 1000 ticks = 10s, now is 1700010000.
        self.assertAlmostEqual(self.by_pid[101].age_seconds, 9990.0, places=1)

    def test_a_row_reports_the_model_from_its_log(self):
        self.assertEqual(self.by_pid[101].model, "test-model-1")


class Rendering(unittest.TestCase):
    def setUp(self):
        self.rows = board.build(home=claude_home(), proc_root=proc_root(), now=1700010000.0,
                                want_branch=False)
        self.text = board.render_text(self.rows)

    def test_an_uncertain_match_is_drawn_differently_and_explained(self):
        """A board that draws a guess like a fact is how somebody kills the wrong session.

        Asserted on the ROW, not on the whole page. Searching the page for a "?" passed even with
        the row marker removed, because the footer contains one, so the test agreed that a guess
        was marked while the line a reader actually looks at said nothing. The sabotage suite
        found that; this now checks the row and the footer separately.
        """
        row = [l for l in self.text.splitlines() if l.strip().startswith("301")][0]
        self.assertTrue(row.rstrip().endswith("?"),
                        f"the uncertain row carries no marker: {row!r}")
        self.assertIn("the log shown is a guess, not a match", self.text)
        self.assertIn("pid 301", self.text)

    def test_a_certain_match_carries_no_question_mark(self):
        line = [l for l in self.text.splitlines() if l.strip().startswith("201")][0]
        self.assertNotIn("?", line)

    def test_the_empty_board_says_what_it_looked_for(self):
        """"Nothing found" must not read like an error, and must say what "nothing" means."""
        text = board.render_text([])
        self.assertIn("no agent sessions found", text)
        self.assertIn("claude or codex", text)

    def test_every_row_appears_in_the_output(self):
        for row in self.rows:
            self.assertIn(str(row.pid), self.text)


class Formatting(unittest.TestCase):
    def test_durations_are_compact_and_ordered(self):
        self.assertEqual(board.duration(45), "45s")
        self.assertEqual(board.duration(90), "1m30")
        self.assertEqual(board.duration(3700), "1h01")
        self.assertEqual(board.duration(90000), "1d01")
        self.assertEqual(board.duration(None), "-")

    def test_token_counts_are_compact(self):
        self.assertEqual(board.tokens(999), "999")
        self.assertEqual(board.tokens(45000), "45k")
        self.assertEqual(board.tokens(1_500_000), "1.5M")
        self.assertEqual(board.tokens(None), "-")

    def test_a_short_label_falls_back_to_the_whole_path_at_the_root(self):
        self.assertEqual(board.short_label("/srv/a/b/repo"), "repo")
        self.assertEqual(board.short_label("/srv/a/b/repo/"), "repo")
        self.assertEqual(board.short_label(None), "(not readable)")


if __name__ == "__main__":
    unittest.main()
