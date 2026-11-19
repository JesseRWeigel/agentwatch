"""Correlation: which log belongs to which process, and how sure that is.

Every case here exists because it was observed on a real machine, not because it was imagined.
"""

from __future__ import annotations

import os
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentwatch import procs, sessions                          # noqa: E402
from agentwatch.sessions import Confidence                      # noqa: E402
from tests.common import claude_home, proc_root                 # noqa: E402


class Metadata(unittest.TestCase):
    def test_valid_non_object_record_does_not_hide_later_metadata(self):
        with tempfile.TemporaryDirectory() as dirname:
            path = os.path.join(dirname, "sample.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('[{"cwd":"/wrong","gitBranch":"wrong"}]\n')
                handle.write('{"cwd":"/expected","gitBranch":"main"}\n')
            log = sessions.load_log(path)
        self.assertIsNotNone(log)
        self.assertEqual(log.recorded_cwd, "/expected")


class CandidateOrder(unittest.TestCase):
    def test_equal_mtimes_keep_the_fallback_order_stable(self):
        cwd = "/expected"
        with tempfile.TemporaryDirectory() as home:
            project = os.path.join(home, "projects", "-expected")
            os.makedirs(project)
            names = ["first.jsonl", "last.jsonl"]
            for name in names:
                path = os.path.join(project, name)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write('{"cwd":"/expected"}\n')
                os.utime(path, (1700000000, 1700000000))

            with patch.object(sessions.os, "listdir", return_value=names):
                first = sessions.candidate_logs(cwd, home)
            with patch.object(sessions.os, "listdir", return_value=names[::-1]):
                second = sessions.candidate_logs(cwd, home)

        self.assertEqual([log.session_id for log in first], ["last", "first"])
        self.assertEqual(first, second)


class Mangling(unittest.TestCase):
    def test_both_conventions_are_offered(self):
        """Two rules exist on one disk and this code does not get to pick.

        Measured on the development machine: of 61 project directories recording a cwd, 59 match
        `"/" -> "-"` and 2 match `"/" -> "-"` plus `"." -> "-"`. A directory is created by
        whichever version was running and never migrated, so both are live.
        """
        names = sessions.mangle("/home/testsubject/.config/dotted")
        self.assertIn("-home-testsubject-.config-dotted", names)
        self.assertIn("-home-testsubject--config-dotted", names)

    def test_a_path_with_no_dots_yields_one_name(self):
        self.assertEqual(sessions.mangle("/a/b/c"), {"-a-b-c"})


class Correlation(unittest.TestCase):
    def setUp(self):
        self.home = claude_home()
        self.by_pid = {p.pid: p for p in procs.discover(proc_root())}

    def correlate(self, pid):
        return sessions.correlate(self.by_pid[pid], home=self.home)

    def test_one_session_in_one_directory_is_unique(self):
        result = self.correlate(101)
        self.assertIs(result.confidence, Confidence.UNIQUE)
        self.assertTrue(result.log.session_id.startswith("11111111"))

    def test_a_session_id_on_the_command_line_wins(self):
        result = self.correlate(201)
        self.assertIs(result.confidence, Confidence.EXACT)
        self.assertEqual(result.log.session_id, "aaaaaaaa-1111-2222-3333-444444444444")

    def test_two_sessions_in_one_directory_are_ambiguous_on_their_own(self):
        """202 shares a directory with 201 and names nothing, so alone it cannot be resolved."""
        result = self.correlate(202)
        self.assertIs(result.confidence, Confidence.AMBIGUOUS)
        self.assertIsNone(result.log)
        self.assertEqual(len(result.candidates), 2)

    def test_many_logs_in_one_directory_stay_ambiguous(self):
        result = self.correlate(301)
        self.assertIs(result.confidence, Confidence.AMBIGUOUS)
        self.assertGreaterEqual(len(result.candidates), 4)

    def test_a_log_whose_recorded_cwd_differs_is_rejected(self):
        """The mangling is lossy, so the recorded cwd is the authority.

        `/home/testsubject/work/quiet/repo` mangles to the same directory name as
        `/home/testsubject/work/quiet-repo`. Its log sits in that directory and must not be
        offered to a process running in the other one.
        """
        result = self.correlate(101)
        for candidate in result.candidates:
            self.assertNotEqual(candidate.session_id[:8], "ffffffff")

    def test_an_unreadable_working_directory_yields_none_with_a_reason(self):
        result = self.correlate(501)
        self.assertIs(result.confidence, Confidence.NONE)
        self.assertIn("not readable", result.reason)

    def test_both_mangling_conventions_are_searched(self):
        """The codex session's directory exists under both spellings, so both are found."""
        result = self.correlate(401)
        ids = {c.session_id[:8] for c in result.candidates}
        self.assertIn("dddddddd", ids)
        self.assertIn("eeeeeeee", ids)


class GlobalCorrelation(unittest.TestCase):
    """Correlating everything at once beats correlating each alone, and this is the case."""

    def setUp(self):
        self.home = claude_home()
        self.processes = procs.discover(proc_root())

    def test_a_claimed_log_is_removed_from_everyone_elses_candidates(self):
        """201 names its session, so 202 is left with exactly one possibility.

        Observed live: two agents in `~/.claude-mem/observer-sessions`, one with `--resume` on its
        command line, the other reported as a coin flip between two logs, one of which was already
        spoken for. This is that, made reproducible.
        """
        alone = sessions.correlate(
            {p.pid: p for p in self.processes}[202], home=self.home)
        self.assertIs(alone.confidence, Confidence.AMBIGUOUS)

        together = sessions.correlate_all(self.processes, home=self.home)
        self.assertIs(together[202].confidence, Confidence.UNIQUE)
        self.assertTrue(together[202].log.session_id.startswith("bbbbbbbb"))
        self.assertIn("claimed", together[202].reason)

    def test_an_ambiguous_process_does_not_claim_anything(self):
        """301 has four candidates and no right to remove any of them from anyone."""
        together = sessions.correlate_all(self.processes, home=self.home)
        self.assertIs(together[301].confidence, Confidence.AMBIGUOUS)
        self.assertGreaterEqual(len(together[301].candidates), 4)

    def test_certainty_is_not_invented_where_there_is_none(self):
        """The whole point. A guess must never be returned as `log`."""
        together = sessions.correlate_all(self.processes, home=self.home)
        for pid, result in together.items():
            if not result.is_certain:
                self.assertIsNone(result.log, f"pid {pid} returned a log it was not sure about")

    def test_most_recent_is_a_separate_request(self):
        """A caller can ask for a guess, but has to ask, so the display can mark it as one."""
        together = sessions.correlate_all(self.processes, home=self.home)
        ambiguous = together[301]
        self.assertIsNone(ambiguous.log)
        self.assertIsNotNone(sessions.most_recent(ambiguous))


if __name__ == "__main__":
    unittest.main()
