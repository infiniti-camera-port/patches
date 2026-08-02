from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final


GIT_EXECUTABLE: Final = "/usr/bin/git"
SAFE_GIT_CONFIG: Final = (
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "tag.gpgSign=false",
    "-c",
    "core.pager=cat",
)
EXECUTABLE_CONFIG_PATTERN: Final = re.compile(
    r"^(?:include(?:if)?\.|extensions\.worktreeConfig|core\.(?:hooksPath|fsmonitor|sshCommand)|"
    r"filter\..*\.(?:clean|smudge|process|required)|diff\..*\.command|"
    r"merge\..*\.driver|interactive\.diffFilter|pager\.)",
    re.IGNORECASE,
)
CANONICAL_LFS_CONFIG: Final = (
    ("filter.lfs.clean", "git-lfs clean -- %f"),
    ("filter.lfs.smudge", "git-lfs smudge -- %f"),
    ("filter.lfs.process", "git-lfs filter-process"),
    ("filter.lfs.required", "true"),
    # `repo sync` writes these skip variants into every project it manages, so
    # refusing them refused every repo-managed tree with git-lfs installed -
    # which is every tree this profile is meant to be applied to. The exemption
    # is keyed on the WHOLE pair: "git-lfs smudge -- %f" without --skip stays
    # allowed only as the canonical hydrating form above, and any other value
    # under these keys is still rejected as an executable config.
    ("filter.lfs.smudge", "git-lfs smudge --skip -- %f"),
    ("filter.lfs.process", "git-lfs filter-process --skip"),
)


@dataclass(frozen=True)  # noqa: SLOTS_OK
class RunnerError(Exception):
    issue: str

    def __str__(self) -> str:
        return self.issue


@dataclass(frozen=True)  # noqa: SLOTS_OK
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_ASKPASS": "/usr/bin/false",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_EDITOR": "/usr/bin/true",
            "GIT_PAGER": "cat",
            "GIT_SSH_COMMAND": "/usr/bin/false",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }
    )
    return environment


def run_git(repo: Path, arguments: Sequence[str], index_file: Path | None = None) -> GitResult:
    environment = git_environment()
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    try:
        result = subprocess.run(
            [GIT_EXECUTABLE, "-C", str(repo), *SAFE_GIT_CONFIG, *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    except OSError as error:
        raise RunnerError(issue=f"cannot execute git: {error}") from error
    return GitResult(result.returncode, result.stdout.strip(), result.stderr.strip())


def detail(result: GitResult) -> str:
    return result.stderr or result.stdout or "git returned no diagnostic"


def git_value(repo: Path, arguments: Sequence[str], context: str) -> str:
    result = run_git(repo, arguments)
    if result.returncode != 0:
        raise RunnerError(issue=f"{context}: {detail(result)}")
    return result.stdout


def repo_path(repo_root: Path, relative: PurePosixPath, kind: str) -> Path:
    candidate = repo_root.joinpath(*relative.parts)
    if candidate.is_symlink() or candidate.resolve() != candidate:
        raise RunnerError(issue=f"symlinked {kind} repository: {candidate}")
    try:
        candidate.relative_to(repo_root)
    except ValueError as error:
        raise RunnerError(issue=f"{kind} repository escapes repository root: {candidate}") from error
    return candidate


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_local_config(raw: str, kind: str, path: Path) -> None:
    if not raw:
        return
    records = raw.split("\0")
    if records[-1]:
        raise RunnerError(issue=f"unsafe executable git config in {kind} repository {path}: unterminated record")
    seen: set[str] = set()
    for record in records[:-1]:
        if not record:
            raise RunnerError(issue=f"unsafe executable git config in {kind} repository {path}: empty record")
        key, separator, value = record.partition("\n")
        if not key or not separator or _contains_control(key) or _contains_control(value):
            raise RunnerError(issue=f"unsafe executable git config in {kind} repository {path}: malformed record")
        if key in seen:
            raise RunnerError(issue=f"duplicate local Git config key in {kind} repository {path}: {key}")
        seen.add(key)
        # Not restricted by `kind`: repo sync writes these filters into every
        # project it manages, targets as much as prerequisites. What makes the
        # exemption safe is the exact (key, value) match, not which repository
        # it appears in.
        if (key, value) in CANONICAL_LFS_CONFIG:
            continue
        if EXECUTABLE_CONFIG_PATTERN.match(key):
            raise RunnerError(issue=f"unsafe executable git config in {kind} repository {path}: {key}")


def require_repo(path: Path, kind: str) -> None:
    if not path.is_dir():
        raise RunnerError(issue=f"missing {kind} repository: {path}")
    top = run_git(path, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0 or Path(top.stdout).resolve() != path:
        raise RunnerError(issue=f"missing {kind} repository: {path}")
    config = run_git(path, ["config", "--local", "--null", "--list", "--no-includes"])
    if config.returncode != 0:
        raise RunnerError(issue=f"cannot inspect local Git config for {path}: {detail(config)}")
    _validate_local_config(config.stdout, kind, path)
    worktree_config = git_value(
        path,
        ["rev-parse", "--git-path", "config.worktree"],
        f"cannot locate worktree Git config for {path}",
    )
    worktree_config_path = Path(worktree_config)
    if not worktree_config_path.is_absolute():
        worktree_config_path = path / worktree_config_path
    if worktree_config_path.exists() or worktree_config_path.is_symlink():
        raise RunnerError(issue=f"worktree Git config is disabled for {kind} repository {path}")


def require_clean(path: Path, kind: str) -> None:
    status = git_value(
        path,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        f"cannot inspect {kind} repository {path}",
    )
    if status:
        raise RunnerError(issue=f"dirty {kind} repository: {path}")


def head(path: Path) -> str:
    return git_value(path, ["rev-parse", "HEAD"], f"cannot read HEAD for {path}")


def head_tree(path: Path) -> str:
    return git_value(path, ["rev-parse", "HEAD^{tree}"], f"cannot read HEAD tree for {path}")


def head_reference(path: Path) -> str:
    symbolic = run_git(path, ["symbolic-ref", "--quiet", "HEAD"])
    if symbolic.returncode == 0:
        return symbolic.stdout
    if symbolic.returncode == 1:
        return "HEAD"
    raise RunnerError(issue=f"cannot inspect HEAD reference for {path}: {detail(symbolic)}")
