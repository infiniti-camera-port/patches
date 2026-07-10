from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_patch_runtime import safe_git_environment
from support import copy_overlay, create_repo_root, output_of, run_command, run_overlay


class GitBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="build-patch-git-boundary-")
        self.scratch = Path(self.temporary_directory.name)
        self.overlay = copy_overlay(self.scratch / "overlay")
        self.repo_root = create_repo_root(self.scratch / "repo")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def configure(self, repo: Path, key: str, value: str) -> None:
        result = run_command(["git", "config", key, value], cwd=repo)
        self.assertEqual(result.returncode, 0, output_of(result))

    def write_attribute(self, repo: Path, value: str) -> None:
        info = repo / ".git/info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text(value, encoding="utf-8")

    def test_check_only_rejects_local_clean_filter_before_it_executes(self) -> None:
        marker = self.scratch / "local-filter-executed"
        soong = self.repo_root / "build/soong"
        self.configure(soong, "filter.evil.clean", f"/usr/bin/touch {marker}; /bin/cat")
        self.write_attribute(
            soong,
            "scripts/check_boot_jars/package_allowed_list.txt filter=evil\n",
        )

        result = run_overlay(self.overlay, self.repo_root)

        self.assertFalse(marker.exists(), output_of(result))
        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assertIn("unsafe executable git config", output_of(result))

    def test_check_only_rejects_command_capable_local_config(self) -> None:
        cases = (
            ("core.hooksPath", "/tmp/hooks"),
            ("core.fsmonitor", "/usr/bin/false"),
            ("core.sshCommand", "/usr/bin/false"),
            ("filter.evil.process", "/usr/bin/false"),
            ("filter.evil.smudge", "/usr/bin/false"),
            ("filter.evil.required", "true"),
            ("diff.evil.command", "/usr/bin/false"),
            ("merge.evil.driver", "/usr/bin/false"),
            ("interactive.diffFilter", "/usr/bin/false"),
            ("pager.status", "/usr/bin/false"),
        )
        for index, (key, value) in enumerate(cases):
            with self.subTest(key=key):
                overlay = copy_overlay(self.scratch / f"local-config-overlay-{index}")
                repo_root = create_repo_root(self.scratch / f"local-config-repo-{index}")
                self.configure(repo_root / "build/soong", key, value)
                result = run_overlay(overlay, repo_root)
                self.assertNotEqual(result.returncode, 0, output_of(result))
                self.assertIn("unsafe executable git config", output_of(result))

    def test_check_only_rejects_include_and_include_if_without_loading_them(self) -> None:
        included = self.scratch / "included-config"
        included.write_text("[filter \"evil\"]\n\tclean = /usr/bin/false\n", encoding="utf-8")
        keys = ("include.path", "includeIf.onbranch:main.path")
        for index, key in enumerate(keys):
            with self.subTest(key=key):
                overlay = copy_overlay(self.scratch / f"include-overlay-{index}")
                repo_root = create_repo_root(self.scratch / f"include-repo-{index}")
                self.configure(repo_root / "build/soong", key, str(included))
                result = run_overlay(overlay, repo_root)
                self.assertNotEqual(result.returncode, 0, output_of(result))
                self.assertIn("unsafe executable git config", output_of(result))

    def test_check_only_rejects_config_worktree_even_without_extension(self) -> None:
        soong = self.repo_root / "build/soong"
        (soong / ".git/config.worktree").write_text("[test]\n\tbenign = value\n", encoding="utf-8")

        result = run_overlay(self.overlay, self.repo_root)

        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assertIn("worktree Git config is disabled", output_of(result))

    def test_check_only_rejects_worktree_config_extension(self) -> None:
        self.configure(self.repo_root / "build/soong", "extensions.worktreeConfig", "true")

        result = run_overlay(self.overlay, self.repo_root)

        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assertIn("unsafe executable git config", output_of(result))

    def test_check_only_strips_inherited_executable_git_config(self) -> None:
        marker = self.scratch / "inherited-filter-executed"
        soong = self.repo_root / "build/soong"
        self.write_attribute(
            soong,
            "scripts/check_boot_jars/package_allowed_list.txt filter=evil\n",
        )

        result = run_overlay(
            self.overlay,
            self.repo_root,
            environment={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "filter.evil.clean",
                "GIT_CONFIG_VALUE_0": f"/usr/bin/touch {marker}; /bin/cat",
            },
        )

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertFalse(marker.exists(), output_of(result))

    def test_check_only_uses_trusted_git_instead_of_path_wrapper(self) -> None:
        marker = self.scratch / "path-git-executed"
        wrapper_directory = self.scratch / "path-bin"
        wrapper_directory.mkdir()
        wrapper = wrapper_directory / "git"
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrapper.write_text(
            "#!/bin/sh\n"
            f"/usr/bin/touch {shlex.quote(str(marker))}\n"
            f"exec {shlex.quote(str(real_git))} \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        result = run_overlay(
            self.overlay,
            self.repo_root,
            environment={"PATH": f"{wrapper_directory}{os.pathsep}{os.environ['PATH']}"},
        )

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertFalse(marker.exists(), output_of(result))

    def test_safe_environment_neutralizes_git_control_surfaces(self) -> None:
        environment = safe_git_environment(
            {
                "PATH": "/tmp/attacker",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": "/tmp/hooks",
                "GIT_ATTR_NOSYSTEM": "0",
                "LANG": "en_US.UTF-8",
            }
        )

        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_ATTR_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("GIT_CONFIG_COUNT", environment)
        self.assertNotIn("GIT_CONFIG_KEY_0", environment)
        self.assertNotIn("GIT_CONFIG_VALUE_0", environment)

    def test_harmless_dot_gitattributes_and_local_config_are_accepted(self) -> None:
        soong = self.repo_root / "build/soong"
        self.configure(soong, "user.name", "Fixture User")
        (soong / ".gitattributes").write_text(
            "scripts/check_boot_jars/package_allowed_list.txt filter=unconfigured\n",
            encoding="utf-8",
        )

        result = run_overlay(self.overlay, self.repo_root)

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertIn("GATE: CHECK-ONLY PASS", output_of(result))


if __name__ == "__main__":
    unittest.main()
