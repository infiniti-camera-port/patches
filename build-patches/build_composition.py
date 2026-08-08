from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from build_patch_manifest import (
    BuildPatch,
    ManifestError,
    ResolvedPatch,
    parse_manifest,
    select_registry,
)
from build_patch_registry import validate_registry
from build_patch_runtime import GitClient, GitConfigError, safe_git_environment

OID_PATTERN: Final = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True, slots=True)
class ResolvedProject:
    path: str
    revision: str
    head: str
    expected_tree: str
    effective_tree: str
    dirty: bool


@dataclass(frozen=True, slots=True)
class CompositionReceipt:
    projects: tuple[ResolvedProject, ...]
    registry: tuple[BuildPatch, ...]
    selection: tuple[ResolvedPatch, ...]
    registry_digest: str
    composition_digest: str
    selection_digest: str


class CompositionError(Exception):
    pass


def _git(repo: Path, arguments: list[str], environment: dict[str, str] | None = None) -> str:
    command_environment = safe_git_environment(environment)
    if environment is not None and "GIT_INDEX_FILE" in environment:
        command_environment["GIT_INDEX_FILE"] = environment["GIT_INDEX_FILE"]
    done = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        env=command_environment,
        check=False,
    )
    if done.returncode != 0 or not done.stdout.strip():
        raise CompositionError(f"{repo}: git {' '.join(arguments)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def _git_quiet(repo: Path, arguments: list[str], environment: dict[str, str]) -> None:
    command_environment = safe_git_environment(environment)
    command_environment["GIT_INDEX_FILE"] = environment["GIT_INDEX_FILE"]
    done = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        env=command_environment,
        check=False,
    )
    if done.returncode != 0:
        raise CompositionError(f"{repo}: git {' '.join(arguments)} failed: {done.stderr.strip()}")


def _effective_tree(repo: Path) -> tuple[str, bool]:
    status = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        env=safe_git_environment(),
        check=False,
    )
    if status.returncode != 0:
        raise CompositionError(f"{repo}: git status failed: {status.stderr.strip()}")
    if not status.stdout.strip():
        return _git(repo, ["rev-parse", "--verify", "-q", "HEAD^{tree}"]), False
    with tempfile.TemporaryDirectory(prefix="rom-composition-index-") as scratch:
        environment = dict(os.environ, GIT_INDEX_FILE=str(Path(scratch) / "index"))
        _git_quiet(repo, ["read-tree", "HEAD"], environment)
        added = subprocess.run(
            ["/usr/bin/git", "-C", str(repo), "add", "-A", "--", "."],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        if added.returncode != 0:
            raise CompositionError(f"{repo}: git add failed: {added.stderr.strip()}")
        return _git(repo, ["write-tree"], environment), True


def _command(root: Path, arguments: list[str]) -> str:
    local_repo = root / ".repo" / "repo" / "repo"
    command = [str(local_repo), *arguments[1:]] if local_repo.is_file() else arguments
    done = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise CompositionError(f"{' '.join(arguments)} failed: {done.stderr.strip()}")
    return done.stdout


def _unique_paths(values: list[str], label: str) -> frozenset[str]:
    normalized = [value.strip().removeprefix("./").rstrip("/") for value in values]
    if any(not value for value in normalized):
        raise CompositionError(f"{label} contains an empty project path")
    if len(normalized) != len(set(normalized)):
        raise CompositionError(f"{label} contains duplicate project paths")
    return frozenset(normalized)


def _manifest_revisions(raw_xml: str) -> dict[str, str]:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise CompositionError(f"repo manifest -r is malformed: {exc}") from exc
    revisions: dict[str, str] = {}
    for node in root.findall("project"):
        path = node.get("path") or node.get("name")
        revision = node.get("revision")
        if path is None or revision is None:
            raise CompositionError("repo manifest -r project lacks path/name or revision")
        normalized = path.removeprefix("./").rstrip("/")
        if normalized in revisions:
            raise CompositionError(f"repo manifest -r duplicates {normalized}")
        if OID_PATTERN.fullmatch(revision) is None:
            raise CompositionError(f"repo manifest -r revision for {normalized} is not a 40-hex OID")
        revisions[normalized] = revision
    if not revisions:
        raise CompositionError("repo manifest -r contains no projects")
    return revisions


def _checkout_consistent(root: Path, path: str) -> None:
    checkout_git = root / path / ".git"
    project_git = root / ".repo" / "projects" / f"{path}.git"
    if not checkout_git.exists() or not project_git.is_dir():
        raise CompositionError(f"{path}: checkout or .repo/projects entry is missing")
    if checkout_git.is_dir():
        if not project_git.samefile(checkout_git):
            raise CompositionError(f"{path}: checkout and .repo/projects gitdirs differ")
        return
    if not checkout_git.is_file():
        raise CompositionError(f"{path}: checkout .git is not a gitdir")
    prefix = "gitdir: "
    raw = checkout_git.read_text(encoding="utf-8").strip()
    if not raw.startswith(prefix):
        raise CompositionError(f"{path}: checkout .git has invalid gitdir syntax")
    actual = (checkout_git.parent / raw.removeprefix(prefix)).resolve(strict=True)
    if actual != project_git.resolve(strict=True):
        raise CompositionError(f"{path}: checkout and .repo/projects gitdirs differ")


def _digest(rows: list[str]) -> str:
    return hashlib.sha256("".join(f"{row}\n" for row in rows).encode()).hexdigest()


def compose(repo_root: Path, patches_dir: Path, manifest_path: Path) -> CompositionReceipt:
    try:
        registry = validate_registry(parse_manifest(manifest_path), patches_dir)
    except ManifestError as exc:
        raise CompositionError(str(exc)) from exc
    project_list = _unique_paths(
        (repo_root / ".repo" / "project.list").read_text(encoding="utf-8").splitlines(),
        ".repo/project.list",
    )
    repo_list = _unique_paths(_command(repo_root, ["repo", "list", "-p"]).splitlines(), "repo list -p")
    revisions = _manifest_revisions(_command(repo_root, ["repo", "manifest", "-r"]))
    manifest_paths = frozenset(revisions)
    if project_list != repo_list or repo_list != manifest_paths:
        raise CompositionError("project path sets differ across project.list, repo list -p, and manifest -r")
    try:
        selection = select_registry(registry, repo_root, manifest_paths)
    except ManifestError as exc:
        raise CompositionError(str(exc)) from exc
    projects: list[ResolvedProject] = []
    for path in sorted(manifest_paths):
        _checkout_consistent(repo_root, path)
        checkout = repo_root / path
        try:
            GitClient(Path("/dev/null")).require_safe_repository(checkout)
        except GitConfigError as exc:
            raise CompositionError(str(exc)) from exc
        revision = revisions[path]
        head = _git(checkout, ["rev-parse", "--verify", "-q", "HEAD"])
        if head != revision:
            raise CompositionError(f"{path}: clean/stale HEAD {head} differs from resolved {revision}")
        expected = _git(checkout, ["rev-parse", "--verify", "-q", f"{revision}^{{tree}}"])
        effective, dirty = _effective_tree(checkout)
        projects.append(ResolvedProject(path, revision, head, expected, effective, dirty))
    registry_rows = [json.dumps(item.entry._asdict(), sort_keys=True) for item in registry]
    project_rows = [f"{item.path} {item.revision} {item.expected_tree}" for item in projects]
    selected_rows = [json.dumps(item.entry._asdict(), sort_keys=True) for item in selection]
    return CompositionReceipt(
        tuple(projects),
        tuple(item.entry for item in registry),
        tuple(selection),
        _digest(registry_rows),
        _digest(project_rows),
        _digest(selected_rows),
    )
