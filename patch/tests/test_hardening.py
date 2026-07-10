from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_apply_patches import PROFILE, ProfileFixture


class RunnerHardeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ProfileFixture(Path(self.temporary.name))
        self.first = self.fixture.add_series("alpha", 1)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_git_wrapper(self, body: str) -> None:
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git executable is required")
        wrapper = self.fixture.root / "git-wrapper"
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

    def _assert_dirty_prerequisite_rejected(self) -> None:
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty prerequisite repository", result.stderr)
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)

    def test_rejects_tracked_dirty_prerequisite(self) -> None:
        self.fixture.write_metadata()
        prerequisite = self.fixture.repo_root / self.fixture.prerequisites[0][0]
        (prerequisite / "content.txt").write_text("modified\n", encoding="utf-8")
        self._assert_dirty_prerequisite_rejected()

    def test_rejects_staged_dirty_prerequisite(self) -> None:
        self.fixture.write_metadata()
        prerequisite = self.fixture.repo_root / self.fixture.prerequisites[0][0]
        (prerequisite / "content.txt").write_text("staged\n", encoding="utf-8")
        self.fixture._git(prerequisite, "add", "content.txt")
        self._assert_dirty_prerequisite_rejected()

    def test_rejects_untracked_dirty_prerequisite(self) -> None:
        self.fixture.write_metadata()
        prerequisite = self.fixture.repo_root / self.fixture.prerequisites[0][0]
        (prerequisite / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        self._assert_dirty_prerequisite_rejected()

    def test_sigint_during_replay_still_removes_worktree(self) -> None:
        self.fixture.write_metadata()
        target = self.fixture.repo_root / self.first.target_repo
        before = self.fixture._git(target, "worktree", "list", "--porcelain")
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */infiniti-alpha-*/worktree)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'am' ]; then kill -INT \"$PPID\"; exit 130; fi\n"
            "    done;;\n"
            "esac"
        )
        result = self.fixture.run()
        after = self.fixture._git(target, "worktree", "list", "--porcelain")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(after, before)

    def test_sigint_preserves_primary_when_worktree_cleanup_fails(self) -> None:
        self.fixture.write_metadata()
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */infiniti-alpha-*/worktree)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'am' ]; then kill -INT \"$PPID\"; exit 130; fi\n"
            "    done;;\n"
            "  */platform/alpha)\n"
            "    saw_worktree=0\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'worktree' ]; then saw_worktree=1; fi\n"
            "      if [ \"$saw_worktree\" = '1' ] && [ \"$argument\" = 'remove' ]; then exit 74; fi\n"
            "    done;;\n"
            "esac"
        )
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot remove replay worktree", result.stderr)
        self.assertIn("KeyboardInterrupt", result.stderr)

    def test_promotion_cas_preserves_concurrent_symbolic_ref_commit(self) -> None:
        self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        marker = self.fixture.root / "alpha-concurrent-created"
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */platform/alpha)\n"
            "    saw_git_path=0\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = '--git-path' ]; then saw_git_path=1; fi\n"
            "      if [ \"$saw_git_path\" = '1' ] && [ \"$argument\" = 'index' ] && [ ! -e '"
            + str(marker)
            + "' ]; then\n"
            "        printf concurrent > \"$2/concurrent.txt\"\n"
            "        \"$REAL_GIT\" -C \"$2\" add concurrent.txt\n"
            "        \"$REAL_GIT\" -C \"$2\" commit -qm concurrent\n"
            "        : > '" + str(marker) + "'\n"
            "        break\n"
            "      fi\n"
            "    done;;\n"
            "esac"
        )
        result = self.fixture.run("--apply")
        target = self.fixture.repo_root / self.first.target_repo
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HEAD changed before promotion for alpha", result.stderr)
        self.assertEqual(self.fixture._git(target, "log", "-1", "--format=%s"), "concurrent")
        self.assertNotEqual(self.fixture.head(self.first), self.first.base_sha)

    def test_index_lease_blocks_checkout_during_promotion(self) -> None:
        self.fixture.write_metadata()
        marker = self.fixture.root / "checkout-rc"
        target = self.fixture.repo_root / self.first.target_repo
        original_branch = self.fixture._git(target, "symbolic-ref", "--short", "HEAD")
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */platform/alpha)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'update-ref' ]; then\n"
            "        \"$REAL_GIT\" -C \"$2\" checkout -qb concurrent-branch >/dev/null 2>&1\n"
            "        printf '%s' \"$?\" > '" + str(marker) + "'\n"
            "        break\n"
            "      fi\n"
            "    done;;\n"
            "esac"
        )
        result = self.fixture.run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(marker.read_text(encoding="utf-8"), "0")
        self.assertEqual(self.fixture.tree(self.first), self.first.head_tree_sha)
        self.assertEqual(self.fixture._git(target, "symbolic-ref", "--short", "HEAD"), original_branch)

    def test_apply_rejects_detached_head_before_any_promotion(self) -> None:
        second = self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        first_target = self.fixture.repo_root / self.first.target_repo
        self.fixture._git(first_target, "checkout", "--detach", self.first.base_sha)
        worktrees_before = self.fixture._git(first_target, "worktree", "list", "--porcelain")
        result = self.fixture.run("--apply")
        symbolic = subprocess.run(
            ["git", "-C", str(first_target), "symbolic-ref", "--quiet", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("detached target HEADs", result.stderr)
        self.assertNotEqual(symbolic.returncode, 0)
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)
        self.assertEqual(self.fixture.head(second), second.base_sha)
        self.assertEqual(self.fixture._git(first_target, "worktree", "list", "--porcelain"), worktrees_before)

    def test_rollback_rejects_failed_status_inspection(self) -> None:
        self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        marker = self.fixture.root / "cas-ran"
        rollback_marker = self.fixture.root / "rollback-cas-ran"
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */platform/beta)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'update-ref' ]; then exit 73; fi\n"
            "    done;;\n"
            "  */platform/alpha)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'update-ref' ]; then\n"
            "        if [ -e '" + str(marker) + "' ]; then : > '" + str(rollback_marker) + "';\n"
            "        else : > '" + str(marker) + "'; fi\n"
            "      fi\n"
            "      if [ \"$argument\" = 'status' ] && [ -e '" + str(rollback_marker) + "' ]; then exit 75; fi\n"
            "    done;;\n"
            "esac"
        )
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback failed", result.stderr)
        self.assertIn("cannot inspect rollback repository", result.stderr)


if __name__ == "__main__":
    unittest.main()
