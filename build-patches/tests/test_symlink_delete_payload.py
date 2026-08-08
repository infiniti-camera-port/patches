from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, assert_never

import pytest
from support import copy_overlay, create_repo_root, output_of, run_overlay

PayloadFault = Literal[
    "regular",
    "new-file",
    "rename",
    "copy",
    "wrong-target",
    "wrong-mode",
    "missing",
    "extra",
    "duplicate",
]


@pytest.mark.parametrize(
    "fault",
    [
        "regular",
        "new-file",
        "rename",
        "copy",
        "wrong-target",
        "wrong-mode",
        "missing",
        "extra",
        "duplicate",
    ],
)
def test_symlink_delete_payload_rejects_malformed_member_sets(
    tmp_path: Path, fault: PayloadFault
) -> None:
    # Given a canonical overlay whose vector payload is malformed in one named way.
    overlay = copy_overlay(tmp_path / "overlay")
    payload = overlay / "remove-broken-build-tools.patch"
    raw = payload.read_text(encoding="utf-8")
    match fault:
        case "regular":
            raw = raw.replace("deleted file mode 120000", "deleted file mode 100644", 1)
        case "new-file":
            raw = raw.replace("deleted file mode 120000", "new file mode 120000", 1)
        case "rename":
            raw = raw.replace("deleted file mode 120000", "rename from path/darwin-x86/date", 1)
        case "copy":
            raw = raw.replace("deleted file mode 120000", "copy from path/darwin-x86/date", 1)
        case "wrong-target":
            raw = raw.replace("../../darwin-x86/bin/toybox", "../../darwin-x86/bin/date", 1)
        case "wrong-mode":
            raw = raw.replace("deleted file mode 120000", "deleted file mode 120777", 1)
        case "missing":
            raw = raw[raw.index("diff --git", raw.index("diff --git") + 1) :]
        case "extra":
            raw = raw + raw[raw.rindex("diff --git") :]
        case "duplicate":
            first = raw[: raw.index("diff --git", raw.index("diff --git") + 1)]
            raw = first + raw
        case _:
            assert_never(fault)
    payload.write_text(raw, encoding="utf-8")
    manifest = overlay / "manifest.yml"
    manifest_raw = manifest.read_text()
    entry_start = manifest_raw.index("  - name: remove-broken-build-tools")
    hash_start = manifest_raw.index("    sha256: ", entry_start) + len("    sha256: ")
    hash_end = manifest_raw.index("\n", hash_start)
    manifest.write_text(
        manifest_raw[:hash_start]
        + hashlib.sha256(raw.encode()).hexdigest()
        + manifest_raw[hash_end:]
    )
    repo_root = create_repo_root(tmp_path / "repo")

    # When full registry validation reads the payload.
    result = run_overlay(overlay, repo_root)

    # Then the malformed state vector is rejected before mutation.
    assert result.returncode != 0
    assert "MANIFEST FAIL" in output_of(result)
