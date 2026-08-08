from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from build_patch_manifest import (
    REQUIRED_PATCHES,
    BuildPatch,
    ManifestError,
    ValidatedPatch,
)
from build_patch_payload import PatchPayloadError, read_verified_patch

CANONICAL_TARGETS: Final = {
    "allow-oplus-fwk-boot-jars": ("build/soong", "scripts/check_boot_jars/package_allowed_list.txt", 1),
    "restore-userdebug-variant": ("build/soong", "scripts/gen_build_prop.py", 2),
    "device-not-debuggable-empty": ("device/oneplus/infiniti", "lineage_infiniti.mk", 3),
    "libhwy-vendor-available": ("external/google-highway", "Android.bp", 4),
    "libskia-skcms-vendor-available": ("external/skia", "Android.bp", 5),
    "libdng-sdk-core-only": ("external/dng_sdk", "Android.bp", 6),
    "settings-rehome-oem-tiles": (
        "packages/apps/Settings",
        "src/com/android/settings/dashboard/CategoryManager.java",
        7,
    ),
    "device-not-debuggable-macan": ("device/oneplus/macan", "lineage_macan.mk", 8),
    "device-not-debuggable-macanc": ("device/oneplus/macanc", "lineage_macanc.mk", 9),
    "device-not-debuggable-fairlady": ("device/oneplus/fairlady", "lineage_fairlady.mk", 10),
}


def validate_registry(entries: list[BuildPatch], patches_dir: Path) -> list[ValidatedPatch]:
    names = [entry.name for entry in entries]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ManifestError(f"duplicate manifest entries: {duplicates}")
    missing = sorted(REQUIRED_PATCHES - set(names))
    unexpected = sorted(set(names) - REQUIRED_PATCHES)
    if missing:
        raise ManifestError(f"required patches missing from manifest: {missing}")
    if unexpected:
        raise ManifestError(f"unexpected patches in manifest: {unexpected}")
    for entry in entries:
        target = (entry.target_repo, entry.target_path, entry.apply_order)
        if target != CANONICAL_TARGETS[entry.name]:
            raise ManifestError(f"{entry.name}: Git top-level or canonical target/order mismatch")
    orders = sorted(entry.apply_order for entry in entries)
    if orders != list(range(1, len(entries) + 1)):
        raise ManifestError(f"apply_order must be unique and sequential 1..{len(entries)} (got {orders})")
    try:
        disk_names = {path.stem for path in patches_dir.iterdir() if path.suffix == ".patch"}
    except OSError as exc:
        raise ManifestError(f"cannot inventory patch directory: {exc}") from exc
    orphans = sorted(disk_names - set(names))
    absent = sorted(set(names) - disk_names)
    if orphans:
        raise ManifestError(f"orphan .patch files not in manifest: {orphans}")
    if absent:
        raise ManifestError(f"manifest entries with no .patch file on disk: {absent}")
    validated: list[ValidatedPatch] = []
    for entry in sorted(entries, key=lambda item: item.apply_order):
        try:
            patch_bytes = read_verified_patch(patches_dir / f"{entry.name}.patch", entry.target_path)
        except PatchPayloadError as exc:
            raise ManifestError(f"{entry.name}: {exc}") from exc
        actual_sha = hashlib.sha256(patch_bytes).hexdigest()
        if actual_sha != entry.sha256:
            raise ManifestError(f"{entry.name}: sha256 mismatch expected {entry.sha256} got {actual_sha}")
        validated.append(ValidatedPatch(entry, patch_bytes))
    return validated
