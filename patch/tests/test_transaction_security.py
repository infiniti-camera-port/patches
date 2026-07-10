from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from test_apply_patches import ProfileFixture

sys.path.insert(0, str(Path(__file__).parents[1]))

from git_ops import RunnerError, _validate_local_config  # noqa: E402


class TransactionSecurityTest(unittest.TestCase):
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
        wrapper = self.fixture.root / "transaction-git-wrapper"
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

    def test_rejects_command_capable_worktree_config_before_status(self) -> None:
        self.fixture.write_metadata()
        target = self.fixture.repo_root / self.first.target_repo
        marker = self.fixture.root / "worktree-filter-executed"
        self.fixture._git(target, "config", "extensions.worktreeConfig", "true")
        self.fixture._git(
            target,
            "config",
            "--worktree",
            "filter.evil.clean",
            f"/usr/bin/touch {marker}; /bin/cat",
        )
        info = target / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("content.txt filter=evil\n", encoding="utf-8")
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe executable git config", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)

    def test_rejects_benign_worktree_config_fail_closed(self) -> None:
        self.fixture.write_metadata()
        target = self.fixture.repo_root / self.first.target_repo
        self.fixture._git(target, "config", "extensions.worktreeConfig", "true")
        self.fixture._git(target, "config", "--worktree", "test.benign", "value")
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)

    def test_rejects_include_without_loading_included_commands(self) -> None:
        self.fixture.write_metadata()
        target = self.fixture.repo_root / self.first.target_repo
        included = self.fixture.root / "included-config"
        included.write_text("[filter \"evil\"]\n\tclean = /usr/bin/false\n", encoding="utf-8")
        self.fixture._git(target, "config", "include.path", str(included))
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe executable git config", result.stderr)

    def test_rejects_ambiguous_nul_config_framing(self) -> None:
        path = Path("/untrusted")
        bad_records = (
            "\0core.foo\nbar\0",
            "core.foo\nbar\0\0core.baz\nqux\0",
            "core.foo\nbar",
            "\nvalue\0",
        )
        for raw in bad_records:
            with self.subTest(raw=repr(raw)):
                with self.assertRaises(RunnerError):
                    _validate_local_config(raw, "target", path)

    def test_actual_targets_never_execute_git_am(self) -> None:
        second = self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */platform/*)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'am' ]; then exit 91; fi\n"
            "    done;;\n"
            "esac"
        )
        result = self.fixture.run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.tree(self.first), self.first.head_tree_sha)
        self.assertEqual(self.fixture.tree(second), second.head_tree_sha)

    def test_current_series_concurrent_commit_is_preserved(self) -> None:
        second = self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        marker = self.fixture.root / "concurrent-commit-created"
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */platform/beta)\n"
            "    saw_git_path=0\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = '--git-path' ]; then saw_git_path=1; fi\n"
            "      if [ \"$saw_git_path\" = '1' ] && [ \"$argument\" = 'index' ] && [ ! -e '"
            + str(marker)
            + "' ]; then\n"
            "        printf concurrent > \"$2/concurrent-current.txt\"\n"
            "        \"$REAL_GIT\" -C \"$2\" add concurrent-current.txt\n"
            "        \"$REAL_GIT\" -C \"$2\" commit -qm concurrent-current-series\n"
            "        : > '" + str(marker) + "'\n"
            "        break\n"
            "      fi\n"
            "    done;;\n"
            "esac"
        )
        result = self.fixture.run("--apply")
        beta = self.fixture.repo_root / second.target_repo
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HEAD changed before promotion for beta", result.stderr)
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)
        self.assertEqual(self.fixture._git(beta, "log", "-1", "--format=%s"), "concurrent-current-series")

    def test_rollback_preserves_concurrent_tracked_worktree_edit(self) -> None:
        self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        marker = self.fixture.root / "alpha-promoted"
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */platform/beta)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'update-ref' ]; then exit 73; fi\n"
            "    done;;\n"
            "  */platform/alpha)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'read-tree' ]; then\n"
            "        if [ -e '" + str(marker) + "' ]; then printf concurrent-uncommitted > \"$2/content.txt\";\n"
            "        else : > '" + str(marker) + "'; fi\n"
            "      fi\n"
            "    done;;\n"
            "esac"
        )
        result = self.fixture.run("--apply")
        target = self.fixture.repo_root / self.first.target_repo
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback failed", result.stderr)
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)
        self.assertEqual((target / "content.txt").read_text(encoding="utf-8"), "concurrent-uncommitted")
        self.assertTrue(self.fixture._git(target, "status", "--porcelain=v1"))

    def test_sigint_during_promotion_rolls_back_prior_series(self) -> None:
        second = self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        targets = [self.fixture.repo_root / item.target_repo for item in (self.first, second)]
        before = [self.fixture._git(target, "worktree", "list", "--porcelain") for target in targets]
        self._write_git_wrapper(
            "case \"$2\" in\n"
            "  */platform/beta)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'update-ref' ]; then kill -INT \"$PPID\"; exit 130; fi\n"
            "    done;;\n"
            "esac"
        )
        result = self.fixture.run("--apply")
        after = [self.fixture._git(target, "worktree", "list", "--porcelain") for target in targets]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("KeyboardInterrupt", result.stderr)
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)
        self.assertEqual(self.fixture.head(second), second.base_sha)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
