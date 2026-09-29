from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from wiggum.git_ops import SafetyError, assert_safe_project, stage_changes


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


class GitSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        _git(self.root, "init", "--initial-branch=main")
        _git(self.root, "config", "user.name", "Wiggum Test")
        _git(self.root, "config", "user.email", "wiggum@example.test")
        (self.root / "README.md").write_text("baseline\n")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-m", "docs: create fixture")
        _git(self.root, "switch", "-c", "feature/wiggum")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_requires_clean_integration_branch_and_records_base_sha(self) -> None:
        snapshot = assert_safe_project(self.root, "feature/wiggum", "main")
        self.assertEqual(snapshot.branch, "feature/wiggum")
        (self.root / "unfinished.txt").write_text("not clean")
        with self.assertRaisesRegex(SafetyError, "uncommitted or untracked"):
            assert_safe_project(self.root, "feature/wiggum", "main")

    def test_refuses_protected_base_branch(self) -> None:
        _git(self.root, "switch", "main")
        with self.assertRaisesRegex(SafetyError, "protected base branch"):
            assert_safe_project(self.root, "main", "main")

    def test_never_stages_secret_files(self) -> None:
        (self.root / ".env").write_text("API_KEY=placeholder")
        with self.assertRaisesRegex(SafetyError, "secret-like files"):
            stage_changes(self.root)


if __name__ == "__main__":
    unittest.main()
