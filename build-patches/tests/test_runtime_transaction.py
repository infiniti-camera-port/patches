from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from support import (
    OVERLAY_DIR,
    added_payload_lines,
    copy_overlay,
    create_repo_root,
    output_of,
    run_overlay,
    set_git_executable,
    write_git_wrapper,
)

sys.path.insert(0, str(OVERLAY_DIR))

from build_patch_manifest import BuildPatch, ResolvedPatch
from build_patch_runtime import CommandResult, PreparedPatch
from build_patch_transaction import execute_transaction


class ExitGit:
    def __init__(self, patches: list[PreparedPatch]) -> None:
        self._patches = {item.resolved.patch_bytes: item for item in patches}
        self._forward_count = 0

    def apply(self, repo: Path, patch_bytes: bytes, *, reverse: bool = False) -> CommandResult:
        item = self._patches[patch_bytes]
        if reverse:
            item.resolved.target_file.write_bytes(item.before)
        else:
            self._forward_count += 1
            if self._forward_count == 2:
                item.resolved.target_file.write_bytes(item.after)
                raise SystemExit(23)
            item.resolved.target_file.write_bytes(item.after)
        return CommandResult(True, "", "", 0)

    def apply_check(self, repo: Path, patch_bytes: bytes, *, reverse: bool = False) -> CommandResult:
        item = self._patches[patch_bytes]
        content = item.resolved.target_file.read_bytes()
        ok = content == (item.after if reverse else item.before)
        return CommandResult(ok, "", "", 0 if ok else 1)


class RuntimeTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="build-patch-runtime-")
        self.scratch = Path(self.temporary_directory.name)
        self.overlay = copy_overlay(self.scratch / "overlay")
        self.repo_root = create_repo_root(self.scratch / "repo")
        self.target_paths = (
            self.repo_root / "build/soong/scripts/check_boot_jars/package_allowed_list.txt",
            self.repo_root / "build/soong/scripts/gen_build_prop.py",
            self.repo_root / "device/oneplus/infiniti/lineage_infiniti.mk",
            self.repo_root / "external/google-highway/Android.bp",
            self.repo_root / "external/skia/Android.bp",
            self.repo_root / "external/dng_sdk/Android.bp",
        )
        self.original_bytes = tuple(path.read_bytes() for path in self.target_paths)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def assert_targets_unchanged(self) -> None:
        self.assertEqual(tuple(path.read_bytes() for path in self.target_paths), self.original_bytes)

    def wrapper_environment(self, fault: str) -> dict[str, str]:
        wrapper_dir = self.scratch / "bin"
        wrapper_dir.mkdir(exist_ok=True)
        wrapper = write_git_wrapper(wrapper_dir, self.repo_root, fault)
        set_git_executable(self.overlay, wrapper)
        return {"PATH": os.environ["PATH"]}

    def test_scattered_added_lines_are_not_accepted_as_satisfied(self) -> None:
        lines = added_payload_lines(self.overlay / "allow-oplus-fwk-boot-jars.patch")
        self.target_paths[0].write_text("\nnoise\n".join(reversed(lines)) + "\n", encoding="utf-8")
        result = run_overlay(self.overlay, self.repo_root)
        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assertIn("CHECK_FAIL", output_of(result))
        self.assertNotIn("SATISFIED", output_of(result))

    def test_all_entries_are_preflighted_before_first_mutation(self) -> None:
        self.target_paths[1].write_text("neither forward nor reverse\n", encoding="utf-8")
        self.original_bytes = tuple(path.read_bytes() for path in self.target_paths)
        result = run_overlay(self.overlay, self.repo_root, apply=True)
        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assert_targets_unchanged()

    def test_later_apply_failure_rolls_back_earlier_entry(self) -> None:
        result = run_overlay(
            self.overlay,
            self.repo_root,
            apply=True,
            environment=self.wrapper_environment("apply"),
        )
        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assertIn("ROLLBACK: PASS", output_of(result))
        self.assert_targets_unchanged()

    def test_rollback_failure_has_distinct_diagnostic(self) -> None:
        result = run_overlay(
            self.overlay,
            self.repo_root,
            apply=True,
            environment=self.wrapper_environment("rollback"),
        )
        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assertIn("ROLLBACK_FAIL", output_of(result))

    def test_sigint_rolls_back_invocation_owned_changes(self) -> None:
        result = run_overlay(
            self.overlay,
            self.repo_root,
            apply=True,
            environment=self.wrapper_environment("sigint"),
        )
        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assertIn("INTERRUPTED", output_of(result))
        self.assert_targets_unchanged()

    def test_system_exit_rolls_back_invocation_owned_changes(self) -> None:
        first_target = self.scratch / "first"
        second_target = self.scratch / "second"
        first_target.write_bytes(b"first-before")
        second_target.write_bytes(b"second-before")
        mode = first_target.stat().st_mode & 0o777
        first_entry = BuildPatch("first", "repo-first", "first", 1, "0" * 64, "first")
        second_entry = BuildPatch("second", "repo-second", "second", 2, "1" * 64, "second")
        first = PreparedPatch(
            ResolvedPatch(first_entry, b"first-patch", self.scratch.resolve(), first_target.resolve()),
            True,
            b"first-before",
            mode,
            b"first-after",
            mode,
        )
        second = PreparedPatch(
            ResolvedPatch(second_entry, b"second-patch", self.scratch.resolve(), second_target.resolve()),
            True,
            b"second-before",
            mode,
            b"second-after",
            mode,
        )
        outcome = execute_transaction([first, second], ExitGit([first, second]))
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.interrupted)
        self.assertFalse(outcome.rollback_failed)
        self.assertEqual(first_target.read_bytes(), b"first-before")
        self.assertEqual(second_target.read_bytes(), b"second-before")


if __name__ == "__main__":
    unittest.main()
