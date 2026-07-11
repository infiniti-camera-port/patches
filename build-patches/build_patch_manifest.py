from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Final, Mapping, NamedTuple

from build_patch_payload import PatchPayloadError, read_verified_patch

REQUIRED_PATCHES: Final = frozenset(
    {
        "allow-oplus-fwk-boot-jars",
        "restore-userdebug-variant",
        "device-not-debuggable-empty",
        "libdng-sdk-core-only",
        "libhwy-vendor-available",
        "libskia-skcms-vendor-available",
    }
)
REQUIRED_MANIFEST_KEYS: Final = frozenset(
    {"name", "target_repo", "target_path", "apply_order", "sha256", "rationale"}
)
SOURCE_GUARD_KEYS: Final = frozenset(
    {"expected_head", "expected_base_sha256", "expected_applied_sha256"}
)
MANIFEST_KEYS: Final = REQUIRED_MANIFEST_KEYS | SOURCE_GUARD_KEYS
NAME_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_PATTERN: Final = re.compile(r"[0-9a-f]{40}\Z")


class ManifestError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class BuildPatch(NamedTuple):
    name: str
    target_repo: str
    target_path: str
    apply_order: int
    sha256: str
    rationale: str
    expected_head: str | None = None
    expected_base_sha256: str | None = None
    expected_applied_sha256: str | None = None


class ResolvedPatch(NamedTuple):
    entry: BuildPatch
    patch_bytes: bytes
    repo_path: Path
    target_file: Path


def _strip_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_key_value(text: str, line_number: int) -> tuple[str, str]:
    if ":" not in text:
        raise ManifestError(f"line {line_number}: expected key: value")
    key, value = text.split(":", 1)
    clean_key = key.strip()
    if clean_key not in MANIFEST_KEYS:
        raise ManifestError(f"line {line_number}: unsupported key {clean_key!r}")
    return clean_key, _strip_value(value)


def _parse_relative(value: str, field: str, index: int) -> str:
    candidate = PurePosixPath(value)
    if (
        not value
        or candidate.is_absolute()
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ManifestError(f"entry {index}: {field} has unsafe path component: {value!r}")
    return value


def _build_entry(raw_entry: Mapping[str, str], index: int) -> BuildPatch:
    missing = REQUIRED_MANIFEST_KEYS - raw_entry.keys()
    if missing:
        raise ManifestError(f"entry {index}: missing keys {sorted(missing)}")
    try:
        apply_order = int(raw_entry["apply_order"])
    except ValueError as exc:
        raise ManifestError(f"entry {index}: apply_order must be an integer") from exc
    name = raw_entry["name"]
    sha256 = raw_entry["sha256"]
    rationale = raw_entry["rationale"]
    if NAME_PATTERN.fullmatch(name) is None:
        raise ManifestError(f"entry {index}: unsafe patch name {name!r}")
    if SHA256_PATTERN.fullmatch(sha256) is None:
        raise ManifestError(f"entry {index}: sha256 must be a lowercase 64-character digest")
    if not rationale:
        raise ManifestError(f"entry {index}: empty rationale")
    present_guards = SOURCE_GUARD_KEYS & raw_entry.keys()
    if present_guards and present_guards != SOURCE_GUARD_KEYS:
        raise ManifestError(f"entry {index}: source guards must be specified together")
    expected_head = raw_entry.get("expected_head")
    expected_base_sha256 = raw_entry.get("expected_base_sha256")
    expected_applied_sha256 = raw_entry.get("expected_applied_sha256")
    if expected_head is not None and COMMIT_PATTERN.fullmatch(expected_head) is None:
        raise ManifestError(f"entry {index}: expected_head must be a lowercase 40-character commit")
    source_hashes = (expected_base_sha256, expected_applied_sha256)
    if any(value is not None and SHA256_PATTERN.fullmatch(value) is None for value in source_hashes):
        raise ManifestError(f"entry {index}: source hashes must be lowercase 64-character digests")
    return BuildPatch(
        name=name,
        target_repo=_parse_relative(raw_entry["target_repo"], "target_repo", index),
        target_path=_parse_relative(raw_entry["target_path"], "target_path", index),
        apply_order=apply_order,
        sha256=sha256,
        rationale=rationale,
        expected_head=expected_head,
        expected_base_sha256=expected_base_sha256,
        expected_applied_sha256=expected_applied_sha256,
    )


def parse_manifest(path: Path) -> list[BuildPatch]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    except UnicodeError as exc:
        raise ManifestError(f"cannot decode manifest as UTF-8: {exc}") from exc
    entries: list[BuildPatch] = []
    current: dict[str, str] | None = None
    section_count = 0
    for line_number, raw_line in enumerate(lines, 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line == "patches:":
            section_count += 1
            if section_count != 1:
                raise ManifestError(f"line {line_number}: exactly one patches: section is required")
            continue
        if section_count == 0:
            raise ManifestError(f"line {line_number}: expected patches: section")
        if raw_line.startswith("  - "):
            if current is not None:
                entries.append(_build_entry(current, len(entries) + 1))
            current = {}
            key, value = _parse_key_value(raw_line[4:].strip(), line_number)
        elif raw_line.startswith("    ") and current is not None:
            key, value = _parse_key_value(stripped, line_number)
        else:
            raise ManifestError(f"line {line_number}: unsupported YAML structure")
        if key in current:
            raise ManifestError(f"line {line_number}: duplicate key {key!r}")
        current[key] = value
    if section_count != 1:
        raise ManifestError("exactly one patches: section is required")
    if current is not None:
        entries.append(_build_entry(current, len(entries) + 1))
    if not entries:
        raise ManifestError("manifest contains no patches")
    return entries


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _walk_existing(root: Path, relative: str, label: str) -> Path:
    current = root
    for component in PurePosixPath(relative).parts:
        current = current / component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ManifestError(f"{label} does not exist: {current}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ManifestError(f"{label} contains symlink component: {current}")
    return current


def _regular_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ManifestError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ManifestError(f"{label} is a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ManifestError(f"{label} is not a regular file: {path}")


def resolve_manifest(entries: list[BuildPatch], patches_dir: Path, repo_root: Path) -> list[ResolvedPatch]:
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
    orders = sorted(entry.apply_order for entry in entries)
    if orders != list(range(1, len(entries) + 1)):
        raise ManifestError(f"apply_order must be unique and sequential 1..{len(entries)} (got {orders})")
    try:
        disk_paths = list(patches_dir.iterdir())
    except OSError as exc:
        raise ManifestError(f"cannot inventory patch directory: {exc}") from exc
    patch_paths = [path for path in disk_paths if path.suffix == ".patch"]
    disk_names = {path.stem for path in patch_paths}
    orphans = sorted(disk_names - set(names))
    absent = sorted(set(names) - disk_names)
    if orphans:
        raise ManifestError(f"orphan .patch files not in manifest: {orphans}")
    if absent:
        raise ManifestError(f"manifest entries with no .patch file on disk: {absent}")
    root = repo_root.resolve(strict=True)
    if repo_root.is_symlink():
        raise ManifestError(f"repo root is a symlink: {repo_root}")
    resolved: list[ResolvedPatch] = []
    for entry in sorted(entries, key=lambda item: item.apply_order):
        patch_file = patches_dir / f"{entry.name}.patch"
        try:
            patch_bytes = read_verified_patch(patch_file, entry.target_path)
        except PatchPayloadError as exc:
            raise ManifestError(f"{entry.name}: {exc}") from exc
        actual_sha = hashlib.sha256(patch_bytes).hexdigest()
        if actual_sha != entry.sha256:
            raise ManifestError(f"{entry.name}: sha256 mismatch expected {entry.sha256} got {actual_sha}")
        repo_path = _walk_existing(root, entry.target_repo, f"{entry.name}: target_repo")
        if not repo_path.is_dir():
            raise ManifestError(f"{entry.name}: target_repo is not a directory: {repo_path}")
        target_file = _walk_existing(repo_path, entry.target_path, f"{entry.name}: target_path")
        _regular_file(target_file, f"{entry.name}: target_path")
        resolved_repo = repo_path.resolve(strict=True)
        resolved_target = target_file.resolve(strict=True)
        if not _is_within(resolved_repo, root) or not _is_within(resolved_target, resolved_repo):
            raise ManifestError(f"{entry.name}: resolved path escapes repo root")
        resolved.append(ResolvedPatch(entry, patch_bytes, resolved_repo, resolved_target))
    return resolved


def paths_are_current(resolved: ResolvedPatch) -> bool:
    try:
        current_repo = resolved.repo_path.resolve(strict=True)
        current_target = resolved.target_file.resolve(strict=True)
        target_metadata = resolved.target_file.lstat()
    except OSError:
        return False
    return (
        current_repo == resolved.repo_path
        and current_target == resolved.target_file
        and stat.S_ISREG(target_metadata.st_mode)
        and not stat.S_ISLNK(target_metadata.st_mode)
    )
