#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///

# How to run:
#   python3 patches/build-patches/apply-build-patches.py [--check-only] [--repo-root PATH]

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping

PATCHES_DIR: Final = Path(__file__).resolve().parent
MANIFEST_PATH: Final = PATCHES_DIR / "manifest.yml"
REQUIRED_PATCHES: Final = frozenset(
    {
        "allow-oplus-fwk-boot-jars",
        "restore-userdebug-variant",
        "device-not-debuggable-empty",
    }
)
MANIFEST_KEYS: Final = frozenset(
    {"name", "target_repo", "target_path", "apply_order", "sha256", "rationale"}
)


@dataclass(frozen=True)
class BuildPatch:
    name: str
    target_repo: str
    target_path: str
    apply_order: int
    sha256: str
    rationale: str


@dataclass(frozen=True)
class PatchResult:
    name: str
    repo: str
    check: str
    applied: str
    sha_prefix: str


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    stderr: str


@dataclass(frozen=True)
class ManifestError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def strip_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_key_value(text: str, line_number: int) -> tuple[str, str]:
    if ":" not in text:
        raise ManifestError(f"line {line_number}: expected key: value")
    key, value = text.split(":", 1)
    clean_key = key.strip()
    if not clean_key:
        raise ManifestError(f"line {line_number}: empty key")
    return clean_key, strip_value(value)


def build_entry(raw_entry: Mapping[str, str], index: int) -> BuildPatch:
    missing = MANIFEST_KEYS - raw_entry.keys()
    extra = raw_entry.keys() - MANIFEST_KEYS
    if missing:
        raise ManifestError(f"entry {index}: missing keys {sorted(missing)}")
    if extra:
        raise ManifestError(f"entry {index}: unknown keys {sorted(extra)}")
    try:
        apply_order = int(raw_entry["apply_order"])
    except ValueError as exc:
        raise ManifestError(f"entry {index}: apply_order must be an integer") from exc
    return BuildPatch(
        name=raw_entry["name"],
        target_repo=raw_entry["target_repo"],
        target_path=raw_entry["target_path"],
        apply_order=apply_order,
        sha256=raw_entry["sha256"],
        rationale=raw_entry["rationale"],
    )


def parse_manifest(path: Path) -> list[BuildPatch]:
    entries: list[BuildPatch] = []
    current: dict[str, str] | None = None
    in_patches = False
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "patches:":
            in_patches = True
            continue
        if not in_patches:
            raise ManifestError(f"line {line_number}: expected patches: header")
        if raw_line.startswith("  - "):
            if current is not None:
                entries.append(build_entry(current, len(entries) + 1))
            current = {}
            remainder = raw_line[4:].strip()
            if remainder:
                key, value = parse_key_value(remainder, line_number)
                current[key] = value
            continue
        if raw_line.startswith("    ") and current is not None:
            key, value = parse_key_value(stripped, line_number)
            current[key] = value
            continue
        raise ManifestError(f"line {line_number}: invalid manifest structure")
    if current is not None:
        entries.append(build_entry(current, len(entries) + 1))
    if not entries:
        raise ManifestError("manifest contains no patches")
    return entries


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_manifest(entries: list[BuildPatch], repo_root: Path) -> list[str]:
    errors: list[str] = []
    names = [entry.name for entry in entries]
    missing_required = REQUIRED_PATCHES - set(names)
    if missing_required:
        errors.append(f"required patches missing from manifest: {sorted(missing_required)}")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        errors.append(f"duplicate manifest entries: {duplicates}")
    on_disk = {path.stem for path in PATCHES_DIR.glob("*.patch")}
    orphans = sorted(on_disk - set(names))
    if orphans:
        errors.append(f"orphan .patch files not in manifest: {orphans}")
    listed_missing = sorted(set(names) - on_disk)
    if listed_missing:
        errors.append(f"manifest entries with no .patch file on disk: {listed_missing}")
    orders = sorted(entry.apply_order for entry in entries)
    if orders != list(range(1, len(entries) + 1)):
        errors.append(f"apply_order must be unique and sequential 1..{len(entries)} (got {orders})")
    for entry in entries:
        patch_file = PATCHES_DIR / f"{entry.name}.patch"
        target_repo = repo_root / entry.target_repo
        target_file = target_repo / entry.target_path
        if not entry.rationale.strip():
            errors.append(f"{entry.name}: empty rationale")
        if entry.sha256 == "TBD" or len(entry.sha256) != 64:
            errors.append(f"{entry.name}: sha256 must be a 64-character digest")
        if patch_file.is_file() and entry.sha256 != "TBD":
            actual_sha = sha256_of(patch_file)
            if actual_sha != entry.sha256:
                errors.append(f"{entry.name}: sha256 mismatch expected {entry.sha256} got {actual_sha}")
        if not target_repo.is_dir():
            errors.append(f"{entry.name}: target_repo does not exist: {target_repo}")
        elif not target_file.is_file():
            errors.append(f"{entry.name}: target_path does not exist: {target_file}")
    return errors


def git_apply(repo_path: Path, patch_file: Path, *, check: bool, reverse: bool = False) -> CommandResult:
    command = ["git", "apply"]
    if check:
        command.append("--check")
    if reverse:
        command.append("--reverse")
    command.append(str(patch_file))
    result = subprocess.run(command, cwd=repo_path, capture_output=True, text=True, check=False)
    return CommandResult(ok=result.returncode == 0, stderr=result.stderr.strip())


def required_added_lines(patch_file: Path) -> tuple[str, ...]:
    lines: list[str] = []
    for raw_line in patch_file.read_text(encoding="utf-8").splitlines():
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        line = raw_line[1:]
        if line.strip() and not line.lstrip().startswith("#"):
            lines.append(line)
    return tuple(lines)


def content_satisfied(entry: BuildPatch, repo_root: Path, patch_file: Path) -> bool:
    target_text = (repo_root / entry.target_repo / entry.target_path).read_text(encoding="utf-8")
    required_lines = required_added_lines(patch_file)
    return bool(required_lines) and all(line in target_text for line in required_lines)


def check_one(entry: BuildPatch, repo_root: Path, check_only: bool) -> PatchResult:
    patch_file = PATCHES_DIR / f"{entry.name}.patch"
    target_repo = repo_root / entry.target_repo
    actual_sha = sha256_of(patch_file)
    check_result = git_apply(target_repo, patch_file, check=True)
    if check_result.ok:
        if check_only:
            return PatchResult(entry.name, entry.target_repo, "PASS", "NOT_APPLIED", actual_sha[:16])
        apply_result = git_apply(target_repo, patch_file, check=False)
        if apply_result.ok:
            return PatchResult(entry.name, entry.target_repo, "PASS", "YES", actual_sha[:16])
        print(f"[APPLY FAIL] {entry.name}: {apply_result.stderr}", file=sys.stderr)
        return PatchResult(entry.name, entry.target_repo, "APPLY_FAIL", "NO", actual_sha[:16])
    reverse_result = git_apply(target_repo, patch_file, check=True, reverse=True)
    if reverse_result.ok:
        applied = "NOT_APPLIED" if check_only else "YES"
        return PatchResult(entry.name, entry.target_repo, "ALREADY_APPLIED", applied, actual_sha[:16])
    if check_only and content_satisfied(entry, repo_root, patch_file):
        return PatchResult(entry.name, entry.target_repo, "SATISFIED", "NOT_APPLIED", actual_sha[:16])
    print(f"[CHECK FAIL] {entry.name}: {check_result.stderr}", file=sys.stderr)
    return PatchResult(entry.name, entry.target_repo, "CHECK_FAIL", "N/A", actual_sha[:16])


def print_report(results: list[PatchResult], check_only: bool) -> int:
    print("=== MARKER-3 PREBUILD REPORT (patches/build-patches/) ===")
    header = f"{'patch':<35} {'repo':<30} {'check':<16} {'applied':<12} sha256[:16]"
    print(header)
    print("-" * len(header))
    for result in results:
        print(f"{result.name:<35} {result.repo:<30} {result.check:<16} {result.applied:<12} {result.sha_prefix}")
    failed = any(result.check in {"CHECK_FAIL", "APPLY_FAIL"} for result in results)
    all_applied = bool(results) and all(result.applied == "YES" for result in results)
    if check_only:
        gate = (
            "CHECK-ONLY FAIL - STOP; fix or re-sync before building"
            if failed
            else "CHECK-ONLY PASS - patches verified or already satisfied; NOT a build gate"
        )
        print(f"\nGATE: {gate}")
        return 1 if failed else 0
    gate = "PASS - all patches applied=YES; proceed to build" if not failed and all_applied else "FAIL - STOP; fix or re-sync before building"
    print(f"\nGATE: {gate}")
    return 0 if not failed and all_applied else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true", help="Validate manifest and check patch state without applying.")
    parser.add_argument("--repo-root", default=".", help="Android source checkout root.")
    args = parser.parse_args()
    repo_root = Path(args.repo_root).resolve()
    try:
        entries = parse_manifest(MANIFEST_PATH)
    except ManifestError as exc:
        print(f"[MANIFEST FAIL] {exc}", file=sys.stderr)
        print("\nGATE: FAIL - manifest invalid; fix patches/build-patches/manifest.yml")
        return 1
    errors = validate_manifest(entries, repo_root)
    if errors:
        for error in errors:
            print(f"[MANIFEST FAIL] {error}", file=sys.stderr)
        print("\nGATE: FAIL - manifest invalid; fix patches/build-patches/manifest.yml")
        return 1
    ordered_entries = sorted(entries, key=lambda entry: entry.apply_order)
    results = [check_one(entry, repo_root, args.check_only) for entry in ordered_entries]
    return print_report(results, args.check_only)


if __name__ == "__main__":
    raise SystemExit(main())
