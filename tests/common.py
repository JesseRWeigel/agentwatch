"""Turn the committed fixture into a readable /proc, in a temporary directory.

The fixture stores `cwd` and `exe` as ordinary files named `cwd.link` and `exe.link` holding the
target path. They cannot be committed as symlinks: they point at `/home/testsubject/...`, which
does not exist, and a git checkout of a dangling symlink is fine but `os.readlink` on Windows
checkouts and some CI filesystems is not. Materialising them here keeps the committed tree plain
files and gives the code under test a real symlink to read, which is what it will meet in life.
"""

from __future__ import annotations

import functools
import os
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def materialise(destination: str) -> str:
    """Copy the fixture and replace every *.link file with a real symlink."""
    shutil.copytree(FIXTURES, destination)
    for current, _, files in os.walk(destination):
        for name in files:
            if not name.endswith(".link"):
                continue
            path = os.path.join(current, name)
            with open(path, encoding="utf-8") as handle:
                target = handle.read().strip()
            os.remove(path)
            os.symlink(target, os.path.join(current, name[: -len(".link")]))
    return destination


@functools.lru_cache(maxsize=1)
def fixture_root() -> str:
    """One materialised copy for the whole run.

    Cached because every test needs it and copying a few dozen files per test is the difference
    between a suite that runs in a second and one nobody waits for. Nothing under test writes to
    it, so sharing is safe; the one test that needs a mutated tree makes its own copy.
    """
    destination = tempfile.mkdtemp(prefix="agentwatch-fixture-")
    return materialise(os.path.join(destination, "tree"))


def proc_root() -> str:
    return os.path.join(fixture_root(), "proc")


def claude_home() -> str:
    return os.path.join(fixture_root(), "claude")
