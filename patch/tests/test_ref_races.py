from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from test_apply_patches import ProfileFixture


class RefRaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ProfileFixture(Path(self.temporary.name))
        self.first = self.fixture.add_series("alpha", 1)
        self.target = self.fixture.repo_root / self.first.target_repo
        self.original_branch = self.fixture._git(self.target, "symbolic-ref", "--short", "HEAD")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_git_wrapper(self, body: str) -> None:
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git executable is required")
        wrapper = self.fixture.root / "ref-race-git-wrapper"
        wrapper.write_text(
            f"#!/bin/sh\nREAL_GIT='{real_git}'\n{body}\nexec \"$REAL_GIT\" \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        git_ops = self.fixture.patch_root / "git_ops.py"
        git_ops.write_text(
            git_ops.read_text(encoding="utf-8").replace(
                'GIT_EXECUTABLE: Final = "/usr/bin/git"',
                f'GIT_EXECUTABLE: Final = "{wrapper}"',
            ),
            encoding="utf-8",
        )

    def _inject_before_index_lease(self, checkout: str) -> Path:
        marker = self.fixture.root / "checkout-rc"
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */platform/alpha)\n"
            "    saw_git_path=0\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = '--git-path' ]; then saw_git_path=1; fi\n"
            "      if [ \"$saw_git_path\" = '1' ] && [ \"$argument\" = 'index' ] && [ ! -e '"
            + str(marker)
            + "' ]; then\n"
            "        \"$REAL_GIT\" -C \"$2\" "
            + checkout
            + " >/dev/null 2>&1\n"
            "        printf '%s' \"$?\" > '"
            + str(marker)
            + "'\n"
            "        break\n"
            "      fi\n"
            "    done;;\n"
            "esac"
        )
        return marker

    def test_branch_switch_before_lease_is_preserved(self) -> None:
        self.fixture.write_metadata()
        marker = self._inject_before_index_lease("checkout -qb concurrent-branch")
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HEAD changed before promotion", result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "0")
        self.assertEqual(self.fixture._git(self.target, "symbolic-ref", "--short", "HEAD"), "concurrent-branch")
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)
        self.assertEqual(self.fixture._git(self.target, "rev-parse", self.original_branch), self.first.base_sha)
        self.assertFalse(self.fixture._git(self.target, "status", "--porcelain=v1"))

    def test_branch_to_detached_switch_before_lease_is_preserved(self) -> None:
        self.fixture.write_metadata()
        marker = self._inject_before_index_lease(f"checkout --detach {self.first.base_sha}")
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "0")
        self.assertFalse(self.fixture._git(self.target, "branch", "--show-current"))
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)
        self.assertEqual(self.fixture._git(self.target, "rev-parse", self.original_branch), self.first.base_sha)

    def test_apply_lists_multiple_detached_targets_before_staging(self) -> None:
        second = self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        self.fixture._git(self.target, "checkout", "--detach", self.first.base_sha)
        second_target = self.fixture.repo_root / second.target_repo
        self.fixture._git(second_target, "checkout", "--detach", second.base_sha)
        before = self.fixture._git(self.target, "worktree", "list", "--porcelain")
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("platform/alpha", result.stderr)
        self.assertIn("platform/beta", result.stderr)
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)
        self.assertEqual(self.fixture.head(second), second.base_sha)
        self.assertEqual(self.fixture._git(self.target, "worktree", "list", "--porcelain"), before)

    def test_check_only_supports_detached_target(self) -> None:
        self.fixture.write_metadata()
        self.fixture._git(self.target, "checkout", "--detach", self.first.base_sha)
        before = self.fixture._git(self.target, "worktree", "list", "--porcelain")
        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.fixture._git(self.target, "branch", "--show-current"))
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)
        self.assertEqual(self.fixture._git(self.target, "worktree", "list", "--porcelain"), before)

    def test_symbolic_ref_switch_during_cas_rolls_back_ref_and_worktree(self) -> None:
        self.fixture.write_metadata()
        self.fixture._git(self.target, "branch", "concurrent-branch", self.first.base_sha)
        marker = self.fixture.root / "symbolic-switch-rc"
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */platform/alpha)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'update-ref' ]; then\n"
            "        \"$REAL_GIT\" -C \"$2\" symbolic-ref HEAD refs/heads/concurrent-branch\n"
            "        printf '%s' \"$?\" > '"
            + str(marker)
            + "'\n"
            "        break\n"
            "      fi\n"
            "    done;;\n"
            "esac"
        )
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "0")
        self.assertEqual(self.fixture._git(self.target, "symbolic-ref", "--short", "HEAD"), "concurrent-branch")
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)
        self.assertEqual(self.fixture._git(self.target, "rev-parse", self.original_branch), self.first.base_sha)
        self.assertEqual((self.target / "content.txt").read_text(encoding="utf-8"), "base-alpha\n")
        self.assertFalse(self.fixture._git(self.target, "status", "--porcelain=v1"))


if __name__ == "__main__":
    unittest.main()
