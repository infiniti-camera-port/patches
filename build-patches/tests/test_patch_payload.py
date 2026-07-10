from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from support import OVERLAY_DIR

sys.path.insert(0, str(OVERLAY_DIR))

from build_patch_payload import PatchPayloadError, read_verified_patch


class PatchPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="build-patch-payload-")
        self.scratch = Path(self.temporary_directory.name)
        self.source = OVERLAY_DIR / "allow-oplus-fwk-boot-jars.patch"
        self.target = "scripts/check_boot_jars/package_allowed_list.txt"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_exact_text_payload_is_accepted(self) -> None:
        payload = read_verified_patch(self.source, self.target)
        self.assertEqual(payload, self.source.read_bytes())

    def test_non_text_operations_are_rejected(self) -> None:
        markers = (
            b"GIT binary patch",
            b"old mode 100644",
            b"new file mode 100644",
            b"new file mode 120000",
            b"rename from old",
            b"copy from old",
            b"Subproject commit 0000000000000000000000000000000000000000",
        )
        for index, marker in enumerate(markers):
            with self.subTest(marker=marker):
                candidate = self.scratch / f"operation-{index}.patch"
                candidate.write_bytes(self.source.read_bytes() + b"\n" + marker + b"\n")
                with self.assertRaises(PatchPayloadError):
                    read_verified_patch(candidate, self.target)

    def test_second_touched_path_is_rejected(self) -> None:
        candidate = self.scratch / "extra-path.patch"
        candidate.write_bytes(
            self.source.read_bytes()
            + b"\ndiff --git a/extra.txt b/extra.txt\n--- a/extra.txt\n+++ b/extra.txt\n@@ -1 +1 @@\n-old\n+new\n"
        )
        with self.assertRaises(PatchPayloadError):
            read_verified_patch(candidate, self.target)


if __name__ == "__main__":
    unittest.main()
