from __future__ import annotations

import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

from support import OVERLAY_DIR, copy_overlay, create_repo_root, output_of, run_overlay

sys.path.insert(0, str(OVERLAY_DIR))

from build_patch_manifest import BuildPatch, ResolvedPatch, parse_manifest, resolve_manifest
from build_patch_runtime import CommandResult, GitClient, PreparedPatch, prepare_patches
from build_patch_transaction import execute_transaction


class ControlledGit:
    def __init__(self, item: PreparedPatch, behavior: str) -> None:
        self.item = item
        self.behavior = behavior
        self.signal_was_protected = False
        self.original_signal_handler = signal.getsignal(signal.SIGINT)

    def apply(self, repo: Path, patch_bytes: bytes, *, reverse: bool = False) -> CommandResult:
        target = self.item.resolved.target_file
        if reverse:
            if self.behavior == "interrupt":
                os.kill(os.getpid(), signal.SIGINT)
            target.write_bytes(self.item.before)
            return CommandResult(True, "", "", 0)
        target.write_bytes(self.item.after)
        if self.behavior == "nonzero":
            return CommandResult(False, "", "injected failure", 86)
        if self.behavior == "oserror":
            raise OSError("injected subprocess failure")
        if self.behavior == "interrupt":
            self.signal_was_protected = signal.getsignal(signal.SIGINT) != self.original_signal_handler
            os.kill(os.getpid(), signal.SIGINT)
            raise AssertionError("SIGINT handler returned unexpectedly")
        return CommandResult(True, "", "", 0)

    def apply_check(self, repo: Path, patch_bytes: bytes, *, reverse: bool = False) -> CommandResult:
        content = self.item.resolved.target_file.read_bytes()
        ok = content == (self.item.after if reverse else self.item.before)
        return CommandResult(ok, "", "", 0 if ok else 1)


class TransactionHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="build-patch-hardening-")
        self.scratch = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def make_item(self, *, pending: bool = True) -> PreparedPatch:
        target = self.scratch / "target"
        target.write_bytes(b"before")
        mode = target.stat().st_mode & 0o777
        entry = BuildPatch("test", "repo", "target", 1, "0" * 64, "test")
        resolved = ResolvedPatch(entry, b"test-patch", self.scratch.resolve(), target.resolve())
        after = b"after" if pending else b"before"
        return PreparedPatch(resolved, pending, b"before", mode, after, mode)

    def test_stale_already_applied_snapshot_cannot_report_success(self) -> None:
        item = self.make_item(pending=False)
        item.resolved.target_file.write_bytes(b"drift")
        outcome = execute_transaction([item], ControlledGit(item, "success"))
        self.assertFalse(outcome.ok)
        self.assertIn("concurrent", outcome.message)
        self.assertEqual(item.resolved.target_file.read_bytes(), b"drift")

    def test_nonzero_after_write_rolls_back_active_patch(self) -> None:
        item = self.make_item()
        outcome = execute_transaction([item], ControlledGit(item, "nonzero"))
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.rollback_failed)
        self.assertEqual(item.resolved.target_file.read_bytes(), b"before")

    def test_oserror_after_write_rolls_back_active_patch(self) -> None:
        item = self.make_item()
        outcome = execute_transaction([item], ControlledGit(item, "oserror"))
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.rollback_failed)
        self.assertIn("OSError", outcome.message)
        self.assertEqual(item.resolved.target_file.read_bytes(), b"before")

    def test_first_sigint_protects_recovery_from_second_sigint(self) -> None:
        item = self.make_item()
        git = ControlledGit(item, "interrupt")
        outcome = execute_transaction([item], git)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.interrupted)
        self.assertTrue(git.signal_was_protected)
        self.assertEqual(item.resolved.target_file.read_bytes(), b"before")

    def test_verified_patch_bytes_ignore_later_path_replacement(self) -> None:
        overlay = copy_overlay(self.scratch / "overlay")
        repo_root = create_repo_root(self.scratch / "repo")
        entries = parse_manifest(overlay / "manifest.yml")
        resolved = resolve_manifest(entries, overlay, repo_root)
        patch_file = overlay / "allow-oplus-fwk-boot-jars.patch"
        patch_file.write_bytes(
            patch_file.read_bytes()
            + b"\ndiff --git a/extra.txt b/extra.txt\nnew file mode 100644\n--- /dev/null\n+++ b/extra.txt\n@@ -0,0 +1 @@\n+pwned\n"
        )
        hooks = self.scratch / "hooks"
        hooks.mkdir()
        git = GitClient(hooks)
        prepared, errors = prepare_patches([resolved[0]], git)
        self.assertEqual(errors, [])
        outcome = execute_transaction(prepared, git)
        self.assertTrue(outcome.ok, outcome.message)
        self.assertFalse((repo_root / "build/soong/extra.txt").exists())

    def test_happy_apply_and_idempotent_reapply(self) -> None:
        overlay = copy_overlay(self.scratch / "happy-overlay")
        repo_root = create_repo_root(self.scratch / "happy-repo")
        first = run_overlay(overlay, repo_root, apply=True)
        self.assertEqual(first.returncode, 0, output_of(first))
        before_second = tuple(path.read_bytes() for path in repo_root.rglob("*") if path.is_file() and ".git" not in path.parts)
        second = run_overlay(overlay, repo_root, apply=True)
        after_second = tuple(path.read_bytes() for path in repo_root.rglob("*") if path.is_file() and ".git" not in path.parts)
        self.assertEqual(second.returncode, 0, output_of(second))
        self.assertEqual(output_of(second).count("ALREADY_APPLIED"), 6)
        self.assertEqual(after_second, before_second)


if __name__ == "__main__":
    unittest.main()
