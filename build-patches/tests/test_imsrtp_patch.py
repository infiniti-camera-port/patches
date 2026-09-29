from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from tests.imsrtp_fixture import IMSRTP_FILES
from tests.support import (
    OVERLAY_DIR,
    added_payload_lines,
    copy_overlay,
    create_repo_root,
    output_of,
    run_overlay,
)

sys.path.insert(0, str(OVERLAY_DIR))

from build_patch_manifest import REQUIRED_PATCHES, parse_manifest


class ImsRtpPatchTests(unittest.TestCase):
    def test_imsrtp_rust_backend_patch_is_required(self) -> None:
        # Given the strict graph has no Rust consumer for the imsrtp interface.
        expected = "oplus-subsys-imsrtp-disable-rust"
        entries = parse_manifest(OVERLAY_DIR / "manifest.yml")

        # When the guarded build-patch inventory is inspected.
        by_name = {entry.name: entry for entry in entries}

        # Then the exact source guard must require the isolated Rust disposition.
        self.assertIn(expected, REQUIRED_PATCHES)
        self.assertIn(expected, by_name)
        entry = by_name[expected]
        self.assertEqual(entry.target_repo, "hardware/oplus")
        self.assertEqual(entry.target_path, "interfaces/Android.bp")
        self.assertEqual(entry.apply_order, 7)
        self.assertEqual(entry.sha256, "30003dbcb9fe966f17d8bd4a8af88d9f2e0fa1f8926012860132d0b7078068eb")
        self.assertEqual(entry.expected_head, "76923bf4fb7e2cdb56ba986459ab1e92604d203a")
        self.assertEqual(
            entry.expected_base_sha256,
            "40ee5acd6d160e014561e7bd181514f74e69f5dff217a5bed19180571042f3a4",
        )
        self.assertEqual(
            entry.expected_applied_sha256,
            "f2968b6fc58c3cb42c80e880217d887437606e07a5a994e22b536682b7ec3100",
        )

    def test_imsrtp_payload_changes_only_the_rust_backend(self) -> None:
        # Given the exact native IMSRTP and imported subsys interface fixture.
        with tempfile.TemporaryDirectory(prefix="build-patch-imsrtp-") as scratch:
            root = create_repo_root(Path(scratch) / "repo")
            overlay = copy_overlay(Path(scratch) / "overlay")
            target = root / "hardware/oplus/interfaces/Android.bp"
            before = target.read_text(encoding="utf-8")

            # When all guarded overlays are applied through the real runner.
            result = run_overlay(overlay, root, apply=True)
            after = target.read_text(encoding="utf-8")

            # Then only the explicit three-line Rust disposition is added.
            rust_block = "        rust: {\n            enabled: false,\n        },\n"
            self.assertEqual(result.returncode, 0, output_of(result))
            self.assertEqual(after.replace(rust_block, "", 1), before)
            self.assertEqual(after.count(rust_block), 1)
            self.assertEqual(after.count('name: "vendor.oplus.hardware.subsys_imsrtp"'), 1)
            self.assertEqual(after.count('name: "vendor.oplus.hardware.subsys"'), 1)
            self.assertEqual(after.count('vendor_available: true'), 1)
            self.assertEqual(after.count('odm_available: true'), 1)
            self.assertEqual(after.count('version: "1"'), 2)
            self.assertEqual(after.count('version: "5"'), 1)
            self.assertNotIn("prefer_rlib", after)

    def test_imsrtp_payload_digest_and_target_are_exact(self) -> None:
        # Given the committed mail-format payload.
        payload = OVERLAY_DIR / "oplus-subsys-imsrtp-disable-rust.patch"
        patch_bytes = payload.read_bytes()

        # When its content-address and diff headers are inspected.
        digest = hashlib.sha256(patch_bytes).hexdigest()
        text = patch_bytes.decode("utf-8")
        added = added_payload_lines(payload)
        removed = [
            line[1:]
            for line in text.splitlines()
            if line.startswith("-") and line != "--" and not line.startswith("---") and line[1:].strip()
        ]

        # Then it is one Android.bp payload with no linkage or API alteration.
        self.assertEqual(digest, "30003dbcb9fe966f17d8bd4a8af88d9f2e0fa1f8926012860132d0b7078068eb")
        self.assertEqual(text.count("diff --git a/interfaces/Android.bp b/interfaces/Android.bp"), 1)
        self.assertEqual(added, ["        rust: {", "            enabled: false,", "        },"])
        self.assertEqual(removed, [])
        changed = "\n".join(added + removed)
        self.assertNotIn("prefer_rlib", changed)
        self.assertNotIn("vendor_available", changed)
        self.assertNotIn("odm_available", changed)
        self.assertNotIn("versions_with_info", changed)
        self.assertNotIn("backend.ndk", changed)

    def test_imsrtp_fixture_preserves_native_contract(self) -> None:
        # Given the runner fixture used by every transactional test.
        source = IMSRTP_FILES["interfaces/Android.bp"]

        # When its importer and imported-interface contracts are counted.
        # Then CPP, V1/current imports, vendor/ODM availability, and V1-V5 remain pinned.
        self.assertEqual(source.count("cpp: {"), 2)
        self.assertEqual(source.count("enabled: true"), 2)
        self.assertEqual(source.count('imports: ["vendor.oplus.hardware.subsys-V5"]'), 1)
        self.assertEqual(source.count('"vendor.oplus.hardware.subsys-V5"'), 2)
        self.assertEqual(source.count("vendor_available: true"), 1)
        self.assertEqual(source.count("odm_available: true"), 1)
        self.assertEqual(source.count('version: "1"'), 2)
        self.assertEqual(source.count('version: "5"'), 1)


if __name__ == "__main__":
    unittest.main()
