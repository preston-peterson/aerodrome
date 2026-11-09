"""The updater's list of files earlier releases left behind (updater.retired).

When an update commits, the new version moves every top-level name that
updater.retired() matches into .backups/<ts>/. Two ways that list can go wrong,
checked here against the git checkout:

  - a name the release ships matches it: every update would then move a live
    file out of the install folder;
  - a release stops shipping a top-level name and nobody adds it: installs keep
    the stale copy forever (the swap only touches the names a release contains).
    Checked against the last release tag reachable from HEAD.

Skipped outside a git checkout, and the second check without a release tag.
"""
import io
import shutil
import subprocess
import tarfile
import unittest
from pathlib import Path

import updater

REPO = Path(__file__).resolve().parent


def _git(*args, text=True):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                          text=text, check=True, timeout=60).stdout


def _shipped_now():
    """Top-level names of the tracked files the release zip would carry
    (tracked and not export-ignored — what package-release.sh ships)."""
    paths = [p for p in _git("ls-files", "-z").split("\0") if p]
    r = subprocess.run(["git", "-C", str(REPO), "check-attr", "-z", "--stdin", "export-ignore"],
                       input="\0".join(paths), capture_output=True, text=True, check=True,
                       timeout=60)
    f = r.stdout.split("\0")        # path NUL attribute NUL value NUL, per path
    return {f[i].split("/", 1)[0] for i in range(0, len(f) - 2, 3) if f[i + 2] != "set"}


def _shipped_at(tag):
    raw = subprocess.run(["git", "-C", str(REPO), "archive", tag], capture_output=True,
                         check=True, timeout=120).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        return {m.name.split("/", 1)[0] for m in t.getmembers()
                if m.name and m.name != "pax_global_header"}


@unittest.skipUnless(shutil.which("git") and (REPO / ".git").exists(), "needs the git checkout")
class TestRetiredList(unittest.TestCase):

    def test_nothing_the_release_ships_is_retired(self):
        shipped = _shipped_now()
        self.assertIn("main.py", shipped)
        self.assertIn("updater.py", shipped)
        clash = sorted(n for n in shipped if updater.retired(n))
        self.assertEqual(clash, [], "the release ships these, yet every update would move "
                                    "them aside: take them out of updater.RETIRED_NAMES")

    def test_every_name_the_last_release_shipped_is_shipped_or_retired(self):
        try:
            tag = _git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", "HEAD").strip()
        except subprocess.CalledProcessError:
            self.skipTest("no release tag reachable from HEAD")
        dropped = _shipped_at(tag) - _shipped_now()
        missing = sorted(n for n in dropped if not updater.retired(n))
        self.assertEqual(missing, [], f"{tag} shipped these and this release doesn't: add "
                                      "them to updater.RETIRED_NAMES so updates move them aside")

    def test_retired_names_are_bare_top_level_names(self):
        for n in updater.RETIRED_NAMES:
            self.assertTrue(updater._swappable(n), n)


if __name__ == "__main__":
    unittest.main()
