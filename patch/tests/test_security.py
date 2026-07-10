from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_apply_patches import PROFILE, ProfileFixture


class RunnerSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ProfileFixture(Path(self.temporary.name))
        self.first = self.fixture.add_series("alpha", 1)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_with_environment(self, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                str(self.fixture.patch_root / "apply-patches.py"),
                "--profile",
                PROFILE,
                "--repo-root",
                str(self.fixture.repo_root),
                "--apply",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def write_git_wrapper(self, body: str) -> Path:
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git executable is required")
        wrapper_dir = self.fixture.root / "bin"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "git"
        wrapper.write_text(f"#!/bin/sh\n{body}\nexec '{real_git}' \"$@\"\n", encoding="utf-8")
        wrapper.chmod(0o755)
        git_ops = self.fixture.patch_root / "git_ops.py"
        git_ops.write_text(
            git_ops.read_text(encoding="utf-8").replace(
                'GIT_EXECUTABLE: Final = "/usr/bin/git"',
                f'GIT_EXECUTABLE: Final = "{wrapper}"',
            ),
            encoding="utf-8",
        )
        return wrapper_dir

    def test_apply_rejects_target_symlink_outside_repo_root(self) -> None:
        self.fixture.write_metadata()
        target = self.fixture.repo_root / self.first.target_repo
        external = self.fixture.root / "external-target"
        target.rename(external)
        target.symlink_to(external, target_is_directory=True)
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked target repository", result.stderr)
        self.assertEqual(self.fixture._git(external, "rev-parse", "HEAD"), self.first.base_sha)

    def test_apply_disables_repository_hooks(self) -> None:
        self.fixture.write_metadata()
        target = self.fixture.repo_root / self.first.target_repo
        marker = self.fixture.root / "hook-executed"
        hook = target / ".git" / "hooks" / "post-applypatch"
        hook.write_text(f"#!/bin/sh\nprintf executed > '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        result = self.fixture.run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_apply_strips_inherited_git_hook_configuration(self) -> None:
        self.fixture.write_metadata()
        marker = self.fixture.root / "inherited-hook-executed"
        hooks = self.fixture.root / "external-hooks"
        hooks.mkdir()
        hook = hooks / "post-applypatch"
        hook.write_text(f"#!/bin/sh\nprintf executed > '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": str(hooks),
            }
        )
        result = self.run_with_environment(environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_apply_rejects_command_capable_local_git_config(self) -> None:
        self.fixture.write_metadata()
        target = self.fixture.repo_root / self.first.target_repo
        self.fixture._git(target, "config", "filter.evil.smudge", "touch /tmp/runner-filter-executed")
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe executable git config", result.stderr)

    def test_sync_prerequisite_accepts_canonical_local_lfs_config(self) -> None:
        self.fixture.write_metadata()
        prerequisite = self.fixture.repo_root / self.fixture.prerequisites[0][0]
        self.fixture._git(prerequisite, "config", "filter.lfs.clean", "git-lfs clean -- %f")
        self.fixture._git(prerequisite, "config", "filter.lfs.smudge", "git-lfs smudge -- %f")
        self.fixture._git(prerequisite, "config", "filter.lfs.process", "git-lfs filter-process")
        self.fixture._git(prerequisite, "config", "filter.lfs.required", "true")
        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sync_prerequisite_rejects_noncanonical_lfs_command(self) -> None:
        self.fixture.write_metadata()
        prerequisite = self.fixture.repo_root / self.fixture.prerequisites[0][0]
        self.fixture._git(prerequisite, "config", "filter.lfs.clean", "touch /tmp/not-lfs")
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe executable git config", result.stderr)

    def test_sync_prerequisite_rejects_multiline_lfs_command(self) -> None:
        self.fixture.write_metadata()
        prerequisite = self.fixture.repo_root / self.fixture.prerequisites[0][0]
        malicious = "git-lfs clean -- %f\nprintf escaped"
        self.fixture._git(prerequisite, "config", "filter.lfs.clean", malicious)
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe executable git config", result.stderr)

    def test_sync_prerequisite_rejects_duplicate_lfs_key(self) -> None:
        self.fixture.write_metadata()
        prerequisite = self.fixture.repo_root / self.fixture.prerequisites[0][0]
        self.fixture._git(prerequisite, "config", "--add", "filter.lfs.clean", "git-lfs clean -- %f")
        self.fixture._git(prerequisite, "config", "--add", "filter.lfs.clean", "git-lfs clean -- %f")
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate local Git config key", result.stderr)

    def test_sync_prerequisite_rejects_control_lfs_value(self) -> None:
        self.fixture.write_metadata()
        prerequisite = self.fixture.repo_root / self.fixture.prerequisites[0][0]
        malicious = "git-lfs clean -- %f\rprintf escaped"
        self.fixture._git(prerequisite, "config", "filter.lfs.clean", malicious)
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe executable git config", result.stderr)

    def test_metadata_rejects_falsified_head_sha(self) -> None:
        self.fixture.series[0] = dataclasses.replace(self.first, head_sha="0" * 40)
        self.fixture.write_metadata()
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("head_sha does not match final patch", result.stderr)

    def test_metadata_rejects_duplicate_json_keys(self) -> None:
        self.fixture.write_metadata()
        metadata = self.fixture.patch_root / "series.json"
        content = metadata.read_text(encoding="utf-8")
        duplicate = content.replace(
            f'"profile": "{PROFILE}",',
            f'"profile": "wrong",\n  "profile": "{PROFILE}",',
            1,
        )
        metadata.write_text(duplicate, encoding="utf-8")
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate metadata key", result.stderr)

    def test_metadata_rejects_unsafe_series_id(self) -> None:
        self.fixture.series[0] = dataclasses.replace(self.first, series_id="../escape")
        self.fixture.write_metadata()
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe series id", result.stderr)

    def test_metadata_rejects_symlinked_patch(self) -> None:
        self.fixture.write_metadata()
        patch = next((self.fixture.patch_root / self.first.directory).glob("*.patch"))
        external = self.fixture.root / "external.patch"
        patch.rename(external)
        patch.symlink_to(external)
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked patch", result.stderr)

    def test_check_only_removes_worktree_when_tree_read_fails(self) -> None:
        self.fixture.write_metadata()
        target = self.fixture.repo_root / self.first.target_repo
        before = self.fixture._git(target, "worktree", "list", "--porcelain")
        wrapper_dir = self.write_git_wrapper(
            "case \"$2\" in\n"
            "  */infiniti-alpha-*/worktree)\n"
            "    saw_rev_parse=0\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'rev-parse' ]; then saw_rev_parse=1; fi\n"
            "      if [ \"$saw_rev_parse\" = '1' ] && [ \"$argument\" = 'HEAD^{tree}' ]; then exit 72; fi\n"
            "    done;;\n"
            "esac"
        )
        environment = os.environ.copy()
        environment["PATH"] = f"{wrapper_dir}{os.pathsep}{environment['PATH']}"
        result = self.run_with_environment(environment)
        after = self.fixture._git(target, "worktree", "list", "--porcelain")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot read HEAD tree", result.stderr)
        self.assertEqual(after, before)

    def test_apply_rolls_back_series_one_when_series_two_promotion_fails(self) -> None:
        second = self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        wrapper_dir = self.write_git_wrapper(
            "case \"$2\" in\n"
            "  */platform/beta)\n"
            "    for argument in \"$@\"; do\n"
            "      if [ \"$argument\" = 'update-ref' ]; then exit 73; fi\n"
            "    done;;\n"
            "esac"
        )
        environment = os.environ.copy()
        environment["PATH"] = f"{wrapper_dir}{os.pathsep}{environment['PATH']}"
        result = self.run_with_environment(environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compare-and-swap rejected promotion for beta", result.stderr)
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)
        self.assertEqual(self.fixture.head(second), second.base_sha)


if __name__ == "__main__":
    unittest.main()
