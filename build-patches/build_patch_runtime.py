from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Final, Mapping, NamedTuple, Protocol, Sequence

from build_patch_manifest import ResolvedPatch, paths_are_current

ALLOWED_ENVIRONMENT: Final = frozenset({"TMPDIR", "TEMP", "TMP", "LANG"})
GIT_EXECUTABLE: Final = "/usr/bin/git"
EXECUTABLE_CONFIG_PATTERN: Final = re.compile(
    r"^(?:include(?:if)?\.|extensions\.worktreeConfig|core\.(?:hooksPath|fsmonitor|sshCommand)|"
    r"filter\..*\.(?:clean|smudge|process|required)|diff\..*\.command|"
    r"merge\..*\.driver|interactive\.diffFilter|pager\.)",
    re.IGNORECASE,
)


class CommandResult(NamedTuple):
    ok: bool
    stdout: str
    stderr: str
    returncode: int


class PreparedPatch(NamedTuple):
    resolved: ResolvedPatch
    pending: bool
    before: bytes
    before_mode: int
    after: bytes
    after_mode: int


class PatchResult(NamedTuple):
    name: str
    repo: str
    check: str
    applied: str
    sha_prefix: str


class DerivationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class GitConfigError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class GitOperations(Protocol):
    def apply(self, repo: Path, patch_bytes: bytes, *, reverse: bool = False) -> CommandResult:
        ...

    def apply_check(self, repo: Path, patch_bytes: bytes, *, reverse: bool = False) -> CommandResult:
        ...


def safe_git_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source_environment = os.environ if source is None else source
    environment = {
        key: value
        for key, value in source_environment.items()
        if key in ALLOWED_ENVIRONMENT or key.startswith("LC_")
    }
    environment.update(
        {
            "GIT_ASKPASS": "/usr/bin/false",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_EDITOR": "/usr/bin/true",
            "GIT_PAGER": "cat",
            "GIT_SSH_COMMAND": "/usr/bin/false",
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": "/usr/bin:/bin",
        }
    )
    return environment


class GitClient:
    def __init__(self, hooks_directory: Path, environment: Mapping[str, str] | None = None) -> None:
        self._hooks_directory = hooks_directory
        self._environment = safe_git_environment(environment)

    def run(self, arguments: Sequence[str], cwd: Path, stdin: bytes | None = None) -> CommandResult:
        command = [
            GIT_EXECUTABLE,
            "--no-optional-locks",
            "-c",
            f"core.hooksPath={self._hooks_directory}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "core.pager=cat",
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=self._environment,
                input=stdin,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            return CommandResult(False, "", f"{type(exc).__name__}: {exc}", 126)
        return CommandResult(
            result.returncode == 0,
            result.stdout.decode("utf-8", errors="replace").strip(),
            result.stderr.decode("utf-8", errors="replace").strip(),
            result.returncode,
        )

    def apply_check(self, repo: Path, patch_bytes: bytes, *, reverse: bool = False) -> CommandResult:
        arguments = ["apply", "--check"]
        if reverse:
            arguments.append("--reverse")
        arguments.append("-")
        return self.run(arguments, repo, patch_bytes)

    def apply(self, repo: Path, patch_bytes: bytes, *, reverse: bool = False) -> CommandResult:
        arguments = ["apply"]
        if reverse:
            arguments.append("--reverse")
        arguments.append("-")
        return self.run(arguments, repo, patch_bytes)

    def require_safe_repository(self, repo: Path) -> None:
        config = self.run(["config", "--local", "--null", "--list", "--no-includes"], repo)
        if not config.ok:
            raise GitConfigError(f"cannot inspect local Git config for {repo}: {config.stderr or config.stdout}")
        _validate_local_config(config.stdout, repo)
        worktree = self.run(["rev-parse", "--git-path", "config.worktree"], repo)
        if not worktree.ok:
            raise GitConfigError(f"cannot locate worktree Git config for {repo}: {worktree.stderr or worktree.stdout}")
        worktree_path = Path(worktree.stdout)
        if not worktree_path.is_absolute():
            worktree_path = repo / worktree_path
        if worktree_path.exists() or worktree_path.is_symlink():
            raise GitConfigError(f"worktree Git config is disabled for repository {repo}")


def _validate_local_config(raw: str, repo: Path) -> None:
    if not raw:
        return
    records = raw.split("\0")
    if records[-1]:
        raise GitConfigError(f"unsafe executable git config in repository {repo}: unterminated record")
    for record in records[:-1]:
        key, separator, _value = record.partition("\n")
        if not key or not separator or any(ord(character) < 32 or ord(character) == 127 for character in key):
            raise GitConfigError(f"unsafe executable git config in repository {repo}: malformed record")
        if EXECUTABLE_CONFIG_PATTERN.match(key):
            raise GitConfigError(f"unsafe executable git config in repository {repo}: {key}")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _derive_after(resolved: ResolvedPatch, git: GitClient, before: bytes, before_mode: int) -> tuple[bytes, int]:
    with tempfile.TemporaryDirectory(prefix="build-patch-derive-") as raw_directory:
        scratch = Path(raw_directory)
        target = scratch / resolved.entry.target_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(before)
        target.chmod(before_mode)
        initialized = git.run(["init", "-q"], scratch)
        if not initialized.ok:
            raise DerivationError(f"cannot initialize derivation repo: {initialized.stderr}")
        applied = git.apply(scratch, resolved.patch_bytes)
        if not applied.ok:
            raise DerivationError(f"cannot derive expected post-state: {applied.stderr}")
        return target.read_bytes(), _mode(target)


def prepare_patches(resolved_patches: list[ResolvedPatch], git: GitClient) -> tuple[list[PreparedPatch], list[str]]:
    prepared: list[PreparedPatch] = []
    errors: list[str] = []
    for resolved in resolved_patches:
        if not paths_are_current(resolved):
            errors.append(f"{resolved.entry.name}: repository or target path changed after validation")
            continue
        try:
            git.require_safe_repository(resolved.repo_path)
        except GitConfigError as exc:
            errors.append(f"{resolved.entry.name}: {exc}")
            continue
        top_level = git.run(["rev-parse", "--show-toplevel"], resolved.repo_path)
        if not top_level.ok or Path(top_level.stdout) != resolved.repo_path:
            errors.append(
                f"{resolved.entry.name}: Git top-level mismatch expected {resolved.repo_path} got {top_level.stdout or top_level.stderr}"
            )
            continue
        if resolved.entry.expected_head is not None:
            head = git.run(["rev-parse", "HEAD"], resolved.repo_path)
            if not head.ok or head.stdout != resolved.entry.expected_head:
                actual_head = head.stdout or head.stderr
                expected_head = resolved.entry.expected_head
                errors.append(f"{resolved.entry.name}: source HEAD mismatch expected {expected_head} got {actual_head}")
                continue
        forward = git.apply_check(resolved.repo_path, resolved.patch_bytes)
        reverse = git.apply_check(resolved.repo_path, resolved.patch_bytes, reverse=True)
        if forward.ok == reverse.ok:
            errors.append(
                f"{resolved.entry.name}: CHECK_FAIL forward_rc={forward.returncode} reverse_rc={reverse.returncode} "
                f"forward={forward.stderr!r} reverse={reverse.stderr!r}"
            )
            continue
        before = resolved.target_file.read_bytes()
        before_mode = _mode(resolved.target_file)
        expected_sha = (
            resolved.entry.expected_applied_sha256
            if reverse.ok
            else resolved.entry.expected_base_sha256
        )
        actual_sha = hashlib.sha256(before).hexdigest()
        if expected_sha is not None and actual_sha != expected_sha:
            errors.append(
                f"{resolved.entry.name}: source content mismatch expected {expected_sha} got {actual_sha}"
            )
            continue
        if reverse.ok:
            prepared.append(PreparedPatch(resolved, False, before, before_mode, before, before_mode))
            continue
        try:
            after, after_mode = _derive_after(resolved, git, before, before_mode)
        except DerivationError as exc:
            errors.append(f"{resolved.entry.name}: {exc}")
            continue
        prepared.append(PreparedPatch(resolved, True, before, before_mode, after, after_mode))
    return prepared, errors


def report_rows(prepared: list[PreparedPatch], *, applied: bool) -> list[PatchResult]:
    rows: list[PatchResult] = []
    for item in prepared:
        rows.append(
            PatchResult(
                item.resolved.entry.name,
                item.resolved.entry.target_repo,
                "PASS" if item.pending else "ALREADY_APPLIED",
                "YES" if applied else "NOT_APPLIED",
                item.resolved.entry.sha256[:16],
            )
        )
    return rows
