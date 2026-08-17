"""Process discovery, against a fixture rather than against whatever is running."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentwatch import procs                                    # noqa: E402
from tests.common import proc_root                              # noqa: E402


class StatParsing(unittest.TestCase):
    def test_an_ordinary_line_parses(self):
        line = "42 (claude) S 1 " + "0 " * 17 + "12345 0 0 0"
        self.assertEqual(procs.parse_stat(line), ("claude", 1, 12345))

    def test_a_name_containing_spaces_and_parentheses_parses(self):
        """The one case that breaks the obvious implementation.

        A process may name itself anything, including `we (ird) name`. Splitting the line on
        whitespace puts the fields after it in the wrong places, so ppid and starttime come back
        as garbage and every duration on the board is wrong for that row. The parser takes the
        name between the first '(' and the LAST ')' for exactly this.
        """
        line = "42 (we (ird) name) S 7 " + "0 " * 17 + "999 0 0 0"
        self.assertEqual(procs.parse_stat(line), ("we (ird) name", 7, 999))

    def test_a_truncated_line_is_refused_rather_than_guessed(self):
        self.assertIsNone(procs.parse_stat("42 (claude) S 1"))
        self.assertIsNone(procs.parse_stat("nonsense"))


class Discovery(unittest.TestCase):
    def setUp(self):
        self.root = proc_root()
        self.found = procs.discover(self.root)
        self.by_pid = {p.pid: p for p in self.found}

    def test_agent_sessions_are_found(self):
        self.assertEqual(sorted(self.by_pid), [101, 201, 202, 301, 401, 501])

    def test_a_process_that_is_not_an_agent_is_not_collected(self):
        """The matcher has to be capable of saying no.

        `claudia-helper` is in the fixture for this. A matcher that accepted everything would
        pass every other test in this class.
        """
        self.assertNotIn(601, self.by_pid)
        self.assertNotIn(701, self.by_pid)

    def test_a_process_whose_details_are_hidden_is_still_reported(self):
        """Not being allowed to read a session is different from there being no session.

        Dropping pid 501 would make an unreadable agent and no agent print the same board, and
        the unreadable one is precisely the case where somebody is hunting for missing CPU.
        """
        hidden = self.by_pid[501]
        self.assertTrue(hidden.is_opaque)
        self.assertIsNone(hidden.cwd)
        self.assertIn("cwd", hidden.unreadable)

    def test_the_working_directory_is_read(self):
        self.assertTrue(self.by_pid[101].cwd.endswith("/work/quiet-repo"))

    def test_a_session_id_on_the_command_line_is_picked_up(self):
        self.assertEqual(self.by_pid[201].session_id, "aaaaaaaa-1111-2222-3333-444444444444")
        self.assertIsNone(self.by_pid[202].session_id)

    def test_the_binary_version_is_read_from_the_exe_link(self):
        self.assertEqual(self.by_pid[101].version, "9.9.9")

    def test_codex_counts_as_an_agent(self):
        self.assertEqual(self.by_pid[401].comm, "codex")

    def test_the_order_is_stable_and_by_start_time(self):
        """A board that reorders under the cursor is a board you cannot press a key on."""
        starts = [p.start_ticks for p in self.found]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual([p.pid for p in procs.discover(self.root)], [p.pid for p in self.found])


class BootTime(unittest.TestCase):
    def test_boot_time_comes_from_btime(self):
        self.assertEqual(procs.boot_time(proc_root()), 1700000000.0)

    def test_a_proc_without_btime_returns_none_rather_than_zero(self):
        """Zero would silently date every session to 1970 instead of admitting it is unknown."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "stat"), "w", encoding="utf-8") as handle:
                handle.write("cpu 1 2 3\n")
            self.assertIsNone(procs.boot_time(tmp))


if __name__ == "__main__":
    unittest.main()
