from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Final, Mapping, NamedTuple, Protocol, Sequence

from build_patch_manifest import ResolvedPatch, paths_are_current

ALLOWED_ENVIRONMENT: Final = frozenset({"PATH", "PATHEXT", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG"})


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
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


class GitClient:
    def __init__(self, hooks_directory: Path, environment: Mapping[str, str] | None = None) -> None:
        self._hooks_directory = hooks_directory
        self._environment = safe_git_environment(environment)

    def run(self, arguments: Sequence[str], cwd: Path, stdin: bytes | None = None) -> CommandResult:
        command = [
            "git",
            "--no-optional-locks",
            "-c",
            f"core.hooksPath={self._hooks_directory}",
            "-c",
            "core.fsmonitor=false",
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
        top_level = git.run(["rev-parse", "--show-toplevel"], resolved.repo_path)
        if not top_level.ok or Path(top_level.stdout) != resolved.repo_path:
            errors.append(
                f"{resolved.entry.name}: Git top-level mismatch expected {resolved.repo_path} got {top_level.stdout or top_level.stderr}"
            )
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
