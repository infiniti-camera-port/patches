from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from support import (
    OVERLAY_DIR,
    SOONG_FILES,
    copy_overlay,
    create_repo_root,
    initialize_repo,
    output_of,
    replace_manifest,
    run_overlay,
    set_git_executable,
    write_git_wrapper,
)

from build_patch_manifest import REQUIRED_PATCHES, parse_manifest


class ManifestSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="build-patch-manifest-")
        self.scratch = Path(self.temporary_directory.name)
        self.overlay = copy_overlay(self.scratch / "overlay")
        self.repo_root = create_repo_root(self.scratch / "repo")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def assert_rejected(self, expected: str) -> None:
        result = run_overlay(self.overlay, self.repo_root)
        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assertIn(expected, output_of(result))

    def test_libjxl_vendor_static_dependency_patches_are_required(self) -> None:
        expected = {"libhwy-vendor-available", "libskia-skcms-vendor-available"}
        entries = parse_manifest(OVERLAY_DIR / "manifest.yml")
        self.assertTrue(expected <= REQUIRED_PATCHES)
        by_name = {entry.name: entry for entry in entries}
        self.assertTrue(expected <= by_name.keys())
        self.assertEqual(
            by_name["libhwy-vendor-available"].expected_head,
            "12062ba78328dac793dcec57c63865ab480f1f18",
        )
        self.assertEqual(
            by_name["libskia-skcms-vendor-available"].expected_head,
            "22f5edb6c1bf350d1d9d67bb429db1a5539f3c05",
        )

    def test_dng_sdk_core_only_patch_is_required(self) -> None:
        # Given the strict graph requires the Android 16 r4 core-only DNG layout.
        expected = "libdng-sdk-core-only"
        entries = parse_manifest(OVERLAY_DIR / "manifest.yml")

        # When the guarded build-patch inventory is inspected.
        by_name = {entry.name: entry for entry in entries}

        # Then the exact DNG source and content guards must be mandatory.
        self.assertIn(expected, REQUIRED_PATCHES)
        self.assertIn(expected, by_name)
        entry = by_name[expected]
        self.assertEqual(entry.target_repo, "external/dng_sdk")
        self.assertEqual(entry.target_path, "Android.bp")
        self.assertEqual(entry.apply_order, 6)
        self.assertEqual(entry.sha256, "69685f9d25be3de68277118ee5e8bc3da045034f6b1fe5ed214db772287db4f5")
        self.assertEqual(entry.expected_head, "60de57ba9f18dd6366914ad74580063fe102c87c")
        self.assertEqual(
            entry.expected_base_sha256,
            "e317f6e4150f83b3ffbc534924d232e90c77b805284f8fdcb554639ab6419b66",
        )
        self.assertEqual(
            entry.expected_applied_sha256,
            "435ffd6377e69a6a919f656a70f428117c81c566a9a12b89ed827023dbc4c664",
        )
        self.assertNotIn("external/XMP-Toolkit-SDK", {item.target_repo for item in entries})

    def test_duplicate_mapping_key_is_rejected(self) -> None:
        replace_manifest(
            self.overlay,
            "    target_repo: build/soong\n",
            "    target_repo: build/soong\n    target_repo: build/soong\n",
        )
        self.assert_rejected("duplicate key")

    def test_duplicate_patches_section_is_rejected(self) -> None:
        manifest = self.overlay / "manifest.yml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + "\npatches:\n", encoding="utf-8")
        self.assert_rejected("patches: section")

    def test_invalid_utf8_manifest_has_named_error(self) -> None:
        manifest = self.overlay / "manifest.yml"
        manifest.write_bytes(b"patches:\n\xff\n")
        result = run_overlay(self.overlay, self.repo_root)
        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assertIn("[MANIFEST FAIL]", output_of(result))
        self.assertNotIn("Traceback", output_of(result))

    def test_absolute_and_parent_target_components_are_rejected(self) -> None:
        outside_repo = self.scratch / "outside"
        initialize_repo(outside_repo, SOONG_FILES)
        cases = (
            ("target_repo: build/soong", f"target_repo: {outside_repo}"),
            ("target_repo: build/soong", "target_repo: ../outside"),
            (
                "target_path: scripts/check_boot_jars/package_allowed_list.txt",
                f"target_path: {outside_repo / 'scripts/check_boot_jars/package_allowed_list.txt'}",
            ),
            ("target_path: scripts/check_boot_jars/package_allowed_list.txt", "target_path: ../outside.txt"),
        )
        for index, (old, new) in enumerate(cases):
            with self.subTest(new=new):
                case_overlay = copy_overlay(self.scratch / f"containment-{index}")
                original_overlay = self.overlay
                self.overlay = case_overlay
                replace_manifest(case_overlay, old, new)
                self.assert_rejected("path component")
                self.overlay = original_overlay

    def test_symlink_patch_file_is_rejected(self) -> None:
        patch_file = self.overlay / "allow-oplus-fwk-boot-jars.patch"
        payload = self.scratch / "payload.patch"
        payload.write_bytes(patch_file.read_bytes())
        patch_file.unlink()
        patch_file.symlink_to(payload)
        self.assert_rejected("symlink")

    def test_symlink_repo_component_is_rejected(self) -> None:
        external = self.scratch / "external-soong"
        initialize_repo(external, SOONG_FILES)
        soong = self.repo_root / "build/soong"
        for child in sorted(soong.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        soong.rmdir()
        soong.symlink_to(external, target_is_directory=True)
        self.assert_rejected("symlink")

    def test_symlink_target_path_is_rejected(self) -> None:
        target = self.repo_root / "build/soong/scripts/check_boot_jars/package_allowed_list.txt"
        external = self.scratch / "external-target.txt"
        external.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(external)
        self.assert_rejected("symlink")

    def test_nested_repo_path_must_equal_git_toplevel(self) -> None:
        soong = self.repo_root / "build/soong"
        nested_target = soong / "nested/scripts/check_boot_jars/package_allowed_list.txt"
        nested_target.parent.mkdir(parents=True)
        nested_target.write_text(SOONG_FILES["scripts/check_boot_jars/package_allowed_list.txt"], encoding="utf-8")
        replace_manifest(self.overlay, "target_repo: build/soong", "target_repo: build/soong/nested")
        self.assert_rejected("Git top-level")

    def test_inherited_git_environment_is_not_visible_to_git(self) -> None:
        wrapper_dir = self.scratch / "bin"
        wrapper_dir.mkdir()
        wrapper = write_git_wrapper(wrapper_dir, self.repo_root, "inspect")
        set_git_executable(self.overlay, wrapper)
        config = self.scratch / "gitconfig"
        config.write_text("[core]\n\tfsmonitor = /does/not/run\n", encoding="utf-8")
        result = run_overlay(
            self.overlay,
            self.repo_root,
            environment={
                "PATH": f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}",
                "GIT_CONFIG_GLOBAL": str(config),
                "XDG_CONFIG_HOME": str(self.scratch / "xdg"),
            },
        )
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertFalse((wrapper_dir / "unsafe-environment").exists())


if __name__ == "__main__":
    unittest.main()
