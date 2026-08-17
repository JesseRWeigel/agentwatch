"""The small rules that are easy to write and impossible to notice breaking.

Every test here exists because the sabotage suite broke the rule and nothing failed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentwatch import board, procs                             # noqa: E402


class BootTime(unittest.TestCase):
    def test_a_malformed_btime_yields_none_rather_than_zero(self):
        """Zero is a real answer that happens to be wrong, and it is wrong by 55 years.

        Falling back to 0.0 dates every session to 1970, so the board prints an age of two
        decades for a process that started a minute ago. None means unknown, and unknown prints
        as a dash.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "stat"), "w", encoding="utf-8") as handle:
                handle.write("cpu 1 2 3\nbtime notanumber\n")
            self.assertIsNone(procs.boot_time(tmp))

    def test_a_btime_with_no_value_yields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "stat"), "w", encoding="utf-8") as handle:
                handle.write("btime\n")
            self.assertIsNone(procs.boot_time(tmp))


class WindowClipping(unittest.TestCase):
    """`tui.fits` is imported lazily, because importing curses fails on a terminal-less CI box."""

    def setUp(self):
        try:
            from agentwatch.tui import fits
        except ImportError as error:                            # pragma: no cover
            self.skipTest(f"curses unavailable: {error}")
        self.fits = fits

    def test_inside_is_inside(self):
        self.assertTrue(self.fits(0, 0, 24, 80))
        self.assertTrue(self.fits(23, 79, 24, 80))

    def test_the_row_below_the_last_is_out(self):
        self.assertFalse(self.fits(24, 0, 24, 80))

    def test_the_column_past_the_last_is_out(self):
        self.assertFalse(self.fits(0, 80, 24, 80))

    def test_negatives_are_out(self):
        """A negative coordinate is what `height - 3` gives on a two-line terminal."""
        self.assertFalse(self.fits(-1, 0, 24, 80))
        self.assertFalse(self.fits(0, -1, 24, 80))

    def test_a_window_with_no_room_admits_nothing(self):
        self.assertFalse(self.fits(0, 0, 0, 0))


class GitBranch(unittest.TestCase):
    """The branch must come from git, not from whatever the log recorded when it started.

    A session running for a day has very likely changed branches, and a stale value is worse than
    none because nobody would think to doubt it.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentwatch-git-")
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.path.join(self.tmp, "gitconfig"),
               "GIT_CONFIG_SYSTEM": os.path.join(self.tmp, "gitconfig")}
        def git(*args):
            return subprocess.run(["git", "-C", self.tmp, *args], capture_output=True, text=True,
                                  env=env)
        if git("init", "-q").returncode != 0:                   # pragma: no cover
            self.skipTest("git unavailable")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        with open(os.path.join(self.tmp, "f"), "w", encoding="utf-8") as handle:
            handle.write("x")
        git("add", "f")
        git("commit", "-q", "-m", "one")
        git("checkout", "-q", "-b", "a-branch-that-is-not-main")
        self.git = git

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_current_branch_is_read_from_the_repository(self):
        self.assertEqual(board.git_branch(self.tmp), "a-branch-that-is-not-main")

    def test_a_directory_that_is_not_a_repository_yields_none(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertIsNone(board.git_branch(plain))

    def test_a_missing_directory_yields_none_rather_than_raising(self):
        self.assertIsNone(board.git_branch("/nonexistent/definitely/not/here"))


if __name__ == "__main__":
    unittest.main()
