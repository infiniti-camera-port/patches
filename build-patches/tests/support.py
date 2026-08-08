from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from support_utilities import (
    added_payload_lines,  # noqa: F401
    output_of,  # noqa: F401
    replace_manifest,  # noqa: F401
    set_git_executable,  # noqa: F401
    write_git_wrapper,  # noqa: F401
)

OVERLAY_DIR = Path(__file__).resolve().parents[1]

SOONG_FILES = {
    "scripts/check_boot_jars/package_allowed_list.txt": """vendor\\.oplus\\..*
com\\.oneplus\\..*
vendor\\.oneplus\\..*

# QC adds
com.qualcomm.qti
com.quicinc.tcmiface
""",
    "scripts/gen_build_prop.py": """TEST_KEY_DIR = \"build/make/target/product/security\"

def get_build_variant(product_config):
  if product_config[\"Eng\"]:
    return \"eng\"
  else:
    return \"user\"

""",
}

def device_files(device: str) -> dict[str, str]:
    return {
        f"lineage_{device}.mk": f"""$(call inherit-product, device/oneplus/{device}/device.mk)
# Inherit some common Lineage stuff.
$(call inherit-product, vendor/lineage/config/common_full_phone.mk)

PRODUCT_NAME := lineage_{device}
PRODUCT_DEVICE := {device}
PRODUCT_MANUFACTURER := OnePlus
"""
    }


DEVICE_FILES = device_files("infiniti")

HIGHWAY_FILES = {
    "Android.bp": """package {
    default_applicable_licenses: ["external_highway_license"],
}

cc_library {
    name: "libhwy",
    host_supported: true,
    sdk_version: "current",
    stl: "c++_shared",
    export_include_dirs: [
        ".",
    ],
}
"""
}

SKIA_FILES = {
    "Android.bp": """rust_bindgen {
    name: "libfontations_ffi_bridge_headers",
}

cc_library_static {
    name: "libskia_skcms",
    host_supported: true,
    sdk_version: "current",
    srcs: [
        "modules/skcms/skcms.cc",
    ],
}
"""
}

DNG_FILES = {
    "Android.bp": """cc_library {
    name: "libdng_sdk",
    host_supported: true,
    sdk_version: "current",
    defaults: ["libdng_sdk-defaults"],
    vendor_available: true,

    sanitize: {
        // For now we want to disable the ubsan_minimal runtime so that we can
        never: true,
    },
}
"""
}

SETTINGS_FILES = {
    "src/com/android/settings/dashboard/CategoryManager.java": """package com.android.settings.dashboard;

public class CategoryManager {

    private synchronized void tryInitCategories(Context context, boolean forceClearCache) {
        if (mCategories == null) {
            mCategoryByKeyMap.clear();
            mCategories = TileUtils.getCategories(context, mTileByComponentCache);
            for (DashboardCategory category : mCategories) {
                mCategoryByKeyMap.put(category.key, category);
            }
            backwardCompatCleanupForCategory(mTileByComponentCache, mCategoryByKeyMap);
            mergeSecurityPrivacyKeys(context, mTileByComponentCache, mCategoryByKeyMap);
            sortCategories(context, mCategoryByKeyMap);
            filterDuplicateTiles(mCategoryByKeyMap);
        }
    }

    @VisibleForTesting
    synchronized void backwardCompatCleanupForCategory(
            Map<Pair<String, String>, Tile> tileByComponentCache,
            Map<String, DashboardCategory> categoryByKeyMap) {
        for (Entry<String, List<Tile>> entry : packageToTileMap.entrySet()) {
            if (useOldKey && !useNewKey) {
                for (Tile tile : tiles) {
                    newCategory.addTile(tile);
                }
            }
        }
    }

    /**
     * Merges {@link CategoryKey#CATEGORY_SECURITY_ADVANCED_SETTINGS} and {@link
     * CategoryKey#CATEGORY_PRIVACY} into {@link
     * CategoryKey#CATEGORY_MORE_SECURITY_PRIVACY_SETTINGS}
     */
    @VisibleForTesting
    synchronized void mergeSecurityPrivacyKeys(
            Context context,
            Map<Pair<String, String>, Tile> tileByComponentCache,
            Map<String, DashboardCategory> categoryByKeyMap) {
    }
}
"""
}


def run_command(arguments: list[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=None if env is None else dict(env),
        capture_output=True,
        text=True,
        check=False,
    )


def copy_overlay(destination: Path) -> Path:
    copied = Path(
        shutil.copytree(
            OVERLAY_DIR,
            destination,
            symlinks=True,
            ignore=shutil.ignore_patterns("tests", "__pycache__"),
        )
    )
    manifest = copied / "manifest.yml"
    manifest.write_text(
        "\n".join(
            line
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if not line.startswith(
                ("    expected_head:", "    expected_base_sha256:", "    expected_applied_sha256:")
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return copied


def initialize_repo(repo: Path, files: Mapping[str, str]) -> None:
    for relative_path, content in files.items():
        target = repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    result = run_command(["git", "init", "-q"], cwd=repo)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


def create_repo_root(root: Path) -> Path:
    fixtures: dict[str, Mapping[str, str]] = {
        "build/soong": SOONG_FILES,
        "external/google-highway": HIGHWAY_FILES,
        "external/skia": SKIA_FILES,
        "external/dng_sdk": DNG_FILES,
        "packages/apps/Settings": SETTINGS_FILES,
    }
    for device in ("infiniti", "macan", "macanc", "fairlady"):
        fixtures[f"device/oneplus/{device}"] = device_files(device)
    for relative, files in fixtures.items():
        checkout = root / relative
        initialize_repo(checkout, files)
        run_command(["git", "config", "user.name", "Fixture"], cwd=checkout)
        run_command(["git", "config", "user.email", "fixture@example.invalid"], cwd=checkout)
        run_command(["git", "add", "."], cwd=checkout)
        committed = run_command(["git", "commit", "-q", "-m", "fixture"], cwd=checkout)
        if committed.returncode != 0:
            raise RuntimeError(committed.stderr)
    project_root = root / ".repo" / "projects"
    project_root.mkdir(parents=True)
    revisions: dict[str, str] = {}
    for relative in sorted(fixtures):
        checkout = root / relative
        revisions[relative] = run_command(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
        project_git = project_root / f"{relative}.git"
        project_git.parent.mkdir(parents=True, exist_ok=True)
        project_git.symlink_to(checkout / ".git", target_is_directory=True)
    ordered = sorted(fixtures)
    (root / ".repo" / "project.list").write_text("\n".join(ordered) + "\n", encoding="utf-8")
    rows = "\n".join(
        f'  <project name="{path}" path="{path}" revision="{revisions[path]}" />'
        for path in ordered
    )
    (root / ".repo" / "resolved.xml").write_text(
        f"<manifest>\n{rows}\n</manifest>\n", encoding="utf-8"
    )
    repo_dir = root / ".repo" / "repo"
    repo_dir.mkdir()
    repo = repo_dir / "repo"
    repo.write_text(
        "#!/bin/sh\ncase \"$*\" in\n"
        "  'list -p') cat .repo/project.list ;;\n"
        "  'manifest -r') cat .repo/resolved.xml ;;\n"
        "  *) exit 64 ;;\nesac\n",
        encoding="utf-8",
    )
    repo.chmod(0o755)
    _assert_covers_manifest(fixtures)
    return root


def manifest_patch_count() -> int:
    sys.path.insert(0, str(OVERLAY_DIR))
    try:
        from build_patch_manifest import parse_manifest
    finally:
        sys.path.pop(0)
    return len(parse_manifest(OVERLAY_DIR / "manifest.yml"))


def _assert_covers_manifest(fixtures: Mapping[str, Mapping[str, str]]) -> None:
    # The fixture set drifted behind manifest.yml once and every test in the
    # suite failed at manifest validation before reaching its actual assertion,
    # which reads as 18 unrelated failures. Fail loudly on the real cause.
    sys.path.insert(0, str(OVERLAY_DIR))
    try:
        from build_patch_manifest import parse_manifest
    finally:
        sys.path.pop(0)
    required = {entry.target_repo for entry in parse_manifest(OVERLAY_DIR / "manifest.yml")}
    missing = sorted(required - set(fixtures))
    if missing:
        raise AssertionError(f"test fixtures do not cover manifest target repos: {missing}")


def run_overlay(
    overlay: Path,
    repo_root: Path,
    *,
    apply: bool = False,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [sys.executable, str(overlay / "apply-build-patches.py"), "--repo-root", str(repo_root)]
    if not apply:
        arguments.append("--check-only")
    process_environment = os.environ.copy()
    if environment is not None:
        process_environment.update(environment)
    return run_command(arguments, cwd=overlay, env=process_environment)
