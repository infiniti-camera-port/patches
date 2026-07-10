#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///

# How to run:
#   python3 patches/build-patches/apply-build-patches.py [--check-only] [--repo-root PATH]

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Final, NamedTuple

from build_patch_manifest import ManifestError, parse_manifest, resolve_manifest
from build_patch_runtime import GitClient, PatchResult, prepare_patches, report_rows
from build_patch_transaction import execute_transaction, verify_prepared_snapshot

PATCHES_DIR: Final = Path(__file__).resolve().parent
MANIFEST_PATH: Final = PATCHES_DIR / "manifest.yml"


class Arguments(NamedTuple):
    check_only: bool
    repo_root: Path


def _print_report(results: list[PatchResult]) -> None:
    print("=== MARKER-3 PREBUILD REPORT (patches/build-patches/) ===")
    header = f"{'patch':<35} {'repo':<30} {'check':<16} {'applied':<12} sha256[:16]"
    print(header)
    print("-" * len(header))
    for result in results:
        print(
            f"{result.name:<35} {result.repo:<30} {result.check:<16} "
            f"{result.applied:<12} {result.sha_prefix}"
        )


def _arguments() -> Arguments:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate manifest and check patch state without applying.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path("."), help="Android source checkout root.")
    namespace = parser.parse_args()
    return Arguments(check_only=namespace.check_only is True, repo_root=namespace.repo_root)


def main() -> int:
    arguments = _arguments()
    repo_root = arguments.repo_root
    try:
        entries = parse_manifest(MANIFEST_PATH)
        resolved = resolve_manifest(entries, PATCHES_DIR, repo_root)
    except (ManifestError, OSError) as exc:
        print(f"[MANIFEST FAIL] {exc}", file=sys.stderr)
        print("\nGATE: FAIL - manifest invalid; fix patches/build-patches/manifest.yml")
        return 1
    with tempfile.TemporaryDirectory(prefix="build-patch-git-") as raw_directory:
        hooks_directory = Path(raw_directory) / "hooks"
        hooks_directory.mkdir(mode=0o700)
        git = GitClient(hooks_directory)
        prepared, errors = prepare_patches(resolved, git)
        if errors:
            for error in errors:
                print(f"[CHECK FAIL] {error}", file=sys.stderr)
            failed_names = {error.partition(":")[0] for error in errors}
            _print_report(
                [
                    PatchResult(
                        item.entry.name,
                        item.entry.target_repo,
                        "CHECK_FAIL",
                        "N/A",
                        item.entry.sha256[:16],
                    )
                    for item in resolved
                    if item.entry.name in failed_names
                ]
            )
            print("\nGATE: CHECK-ONLY FAIL - STOP; fix or re-sync before building")
            return 1
        if arguments.check_only:
            check_error = verify_prepared_snapshot(prepared, git)
            if check_error:
                print(f"[CHECK FAIL] {check_error}", file=sys.stderr)
                print("\nGATE: CHECK-ONLY FAIL - STOP; fix or re-sync before building")
                return 1
            _print_report(report_rows(prepared, applied=False))
            print("\nGATE: CHECK-ONLY PASS - patches are forward- or reverse-applicable; NOT a build gate")
            return 0
        outcome = execute_transaction(prepared, git)
        if outcome.ok:
            _print_report(report_rows(prepared, applied=True))
            print("\nGATE: PASS - all patches applied=YES; proceed to build")
            return 0
        if outcome.rollback_failed:
            print(f"[ROLLBACK_FAIL] {outcome.message}", file=sys.stderr)
            print("\nROLLBACK: FAIL - manual recovery required; concurrent edits were preserved")
        else:
            label = "INTERRUPTED" if outcome.interrupted else "APPLY FAIL"
            print(f"[{label}] {outcome.message}", file=sys.stderr)
            print("\nROLLBACK: PASS - invocation-owned changes reverted and verified")
        print("GATE: FAIL - STOP; fix or re-sync before building")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
