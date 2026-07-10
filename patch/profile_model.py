from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Final, List, Mapping, Sequence, Union


JsonValue = Union[None, bool, int, float, str, List["JsonValue"], Dict[str, "JsonValue"]]

SHA_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
PATCH_PATTERN: Final = re.compile(r"^(?P<number>[0-9]{4})-.+\.patch$")
PATCH_FROM_PATTERN: Final = re.compile(r"^From (?P<sha>[0-9a-f]{40}) ")
SERIES_ID_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ROOT_KEYS: Final = frozenset({"schema_version", "profile", "sync_only_prerequisites", "series"})
PREREQUISITE_KEYS: Final = frozenset({"repo", "build_path", "ref", "sha"})
SERIES_KEYS: Final = frozenset(
    {
        "id",
        "directory",
        "target_repo",
        "source_repo",
        "base_sha",
        "head_sha",
        "head_tree_sha",
        "count",
        "apply_order",
    }
)


@dataclass(frozen=True)  # noqa: SLOTS_OK
class ProfileError(Exception):
    issue: str

    def __str__(self) -> str:
        return self.issue


@dataclass(frozen=True)  # noqa: SLOTS_OK
class Prerequisite:
    repo: str
    build_path: PurePosixPath
    ref: str
    sha: str


@dataclass(frozen=True)  # noqa: SLOTS_OK
class Series:
    series_id: str
    directory: str
    target_repo: PurePosixPath
    source_repo: str
    base_sha: str
    head_sha: str
    head_tree_sha: str
    apply_order: int
    patches: tuple[Path, ...]


@dataclass(frozen=True)  # noqa: SLOTS_OK
class Profile:
    name: str
    prerequisites: tuple[Prerequisite, ...]
    series: tuple[Series, ...]

    @property
    def patch_count(self) -> int:
        return sum(len(item.patches) for item in self.series)


def _mapping(value: JsonValue, context: str) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return value
    raise ProfileError(issue=f"{context} must be an object")


def _sequence(value: JsonValue, context: str) -> list[JsonValue]:
    if isinstance(value, list):
        return value
    raise ProfileError(issue=f"{context} must be an array")


def _string(row: Mapping[str, JsonValue], key: str, context: str) -> str:
    value = row.get(key)
    if isinstance(value, str) and value:
        return value
    raise ProfileError(issue=f"{context}.{key} must be a non-empty string")


def _integer(row: Mapping[str, JsonValue], key: str, context: str) -> int:
    value = row.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ProfileError(issue=f"{context}.{key} must be an integer")


def _exact_keys(row: Mapping[str, JsonValue], expected: frozenset[str], context: str) -> None:
    actual = frozenset(row)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProfileError(issue=f"{context} schema mismatch: missing={missing}, extra={extra}")


def _path(value: str, context: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or not path.parts or ".." in path.parts:
        raise ProfileError(issue=f"{context} must be a normalized relative path")
    return path


def _sha(value: str, context: str) -> str:
    if SHA_PATTERN.fullmatch(value) is None:
        raise ProfileError(issue=f"{context} must be a lowercase 40-character Git SHA")
    return value


def _json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    parsed: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in parsed:
            raise ProfileError(issue=f"duplicate metadata key: {key}")
        parsed[key] = value
    return parsed


def _safe_series_directory(patch_root: Path, directory: str) -> Path:
    root = patch_root.resolve()
    path = root / directory
    if path.is_symlink() or path.resolve() != path:
        raise ProfileError(issue=f"symlinked patch directory: {directory}")
    return path


def _safe_patch(patch_root: Path, patch: Path) -> Path:
    if patch.is_symlink():
        raise ProfileError(issue=f"symlinked patch: {patch.name}")
    resolved = patch.resolve()
    try:
        resolved.relative_to(patch_root.resolve())
    except ValueError as error:
        raise ProfileError(issue=f"patch escapes patch root: {patch}") from error
    if not resolved.is_file():
        raise ProfileError(issue=f"patch is not a regular file: {patch}")
    return resolved


def _parse_prerequisite(value: JsonValue, index: int) -> Prerequisite:
    context = f"sync_only_prerequisites[{index}]"
    row = _mapping(value, context)
    _exact_keys(row, PREREQUISITE_KEYS, context)
    return Prerequisite(
        repo=_string(row, "repo", context),
        build_path=_path(_string(row, "build_path", context), f"{context}.build_path"),
        ref=_string(row, "ref", context),
        sha=_sha(_string(row, "sha", context), f"{context}.sha"),
    )


def _parse_series(value: JsonValue, index: int, patch_root: Path) -> Series:
    context = f"series[{index}]"
    row = _mapping(value, context)
    _exact_keys(row, SERIES_KEYS, context)
    directory = _string(row, "directory", context)
    target_repo = _path(_string(row, "target_repo", context), f"{context}.target_repo")
    if directory != target_repo.as_posix().replace("/", ","):
        raise ProfileError(issue=f"{context}.directory does not encode target_repo")
    series_id = _string(row, "id", context)
    if SERIES_ID_PATTERN.fullmatch(series_id) is None:
        raise ProfileError(issue=f"unsafe series id: {series_id}")
    count = _integer(row, "count", context)
    patches = tuple(
        _safe_patch(patch_root, patch)
        for patch in sorted(_safe_series_directory(patch_root, directory).glob("*.patch"))
    )
    if count != len(patches):
        raise ProfileError(issue=f"patch count mismatch for {directory}: metadata={count}, actual={len(patches)}")
    for expected, patch in enumerate(patches, start=1):
        match = PATCH_PATTERN.fullmatch(patch.name)
        if match is None or int(match.group("number")) != expected:
            raise ProfileError(issue=f"patch numbering mismatch for {directory}: {patch.name}")
    head_sha = _sha(_string(row, "head_sha", context), f"{context}.head_sha")
    if patches:
        try:
            first_line = patches[-1].read_text(encoding="utf-8").splitlines()[0]
        except (OSError, UnicodeError, IndexError) as error:
            raise ProfileError(issue=f"cannot read final patch envelope for {directory}") from error
        envelope = PATCH_FROM_PATTERN.match(first_line)
        if envelope is None or envelope.group("sha") != head_sha:
            raise ProfileError(issue=f"head_sha does not match final patch for {directory}")
    return Series(
        series_id=series_id,
        directory=directory,
        target_repo=target_repo,
        source_repo=_string(row, "source_repo", context),
        base_sha=_sha(_string(row, "base_sha", context), f"{context}.base_sha"),
        head_sha=head_sha,
        head_tree_sha=_sha(_string(row, "head_tree_sha", context), f"{context}.head_tree_sha"),
        apply_order=_integer(row, "apply_order", context),
        patches=patches,
    )


def _unique(values: Sequence[str | int], field: str) -> None:
    seen: set[str | int] = set()
    for value in values:
        if value in seen:
            raise ProfileError(issue=f"duplicate {field}: {value}")
        seen.add(value)


def load_profile(patch_root: Path) -> Profile:
    metadata = patch_root / "series.json"
    try:
        raw: JsonValue = json.loads(
            metadata.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object,
        )
    except json.JSONDecodeError as error:
        raise ProfileError(issue=f"invalid metadata JSON: {error.msg}") from error
    except (OSError, UnicodeError) as error:
        raise ProfileError(issue=f"cannot read metadata: {error}") from error
    root = _mapping(raw, "metadata")
    _exact_keys(root, ROOT_KEYS, "metadata")
    if _integer(root, "schema_version", "metadata") != 1:
        raise ProfileError(issue="unsupported metadata schema_version")
    prerequisites = tuple(
        _parse_prerequisite(value, index)
        for index, value in enumerate(_sequence(root["sync_only_prerequisites"], "sync_only_prerequisites"))
    )
    if len(prerequisites) != 6:
        raise ProfileError(issue=f"sync prerequisite count mismatch: expected=6, actual={len(prerequisites)}")
    series = tuple(
        sorted(
            (
                _parse_series(value, index, patch_root)
                for index, value in enumerate(_sequence(root["series"], "series"))
            ),
            key=lambda item: item.apply_order,
        )
    )
    if not series:
        raise ProfileError(issue="profile must contain at least one complete series")
    _unique([item.apply_order for item in series], "apply_order")
    _unique([item.series_id for item in series], "series id")
    _unique([item.directory for item in series], "series directory")
    _unique([item.target_repo.as_posix() for item in series], "target_repo")
    _unique([item.build_path.as_posix() for item in prerequisites], "prerequisite build_path")
    if [item.apply_order for item in series] != list(range(1, len(series) + 1)):
        raise ProfileError(issue="apply_order must be contiguous from 1")
    expected_patches = {patch for item in series for patch in item.patches}
    orphaned = sorted(set(patch_root.rglob("*.patch")) - expected_patches)
    if orphaned:
        relative = orphaned[0].relative_to(patch_root)
        raise ProfileError(issue=f"orphan patch: {relative}")
    return Profile(
        name=_string(root, "profile", "metadata"),
        prerequisites=prerequisites,
        series=series,
    )
