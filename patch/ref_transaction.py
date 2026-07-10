from __future__ import annotations

import os
from pathlib import Path

from git_ops import RunnerError, git_value


class IndexLease:  # noqa: SLOTS_OK
    def __init__(
        self,
        repo: Path,
        index_path: Path,
        lock_path: Path,
    ) -> None:
        self.repo = repo
        self.index_path = index_path
        self.lock_path = lock_path
        self.committed = False

    @classmethod
    def acquire(cls, repo: Path) -> IndexLease:
        location = git_value(repo, ["rev-parse", "--git-path", "index"], f"cannot locate index for {repo}")
        index_path = Path(location)
        if not index_path.is_absolute():
            index_path = repo / index_path
        if index_path.is_symlink() or not index_path.is_file():
            raise RunnerError(issue=f"unsafe index path for {repo}: {index_path}")
        lock_path = Path(f"{index_path}.lock")
        try:
            descriptor = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                index_path.stat().st_mode & 0o777,
            )
        except OSError as error:
            raise RunnerError(issue=f"cannot lease index for {repo}: {error}") from error
        try:
            with os.fdopen(descriptor, "wb") as destination, index_path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        except OSError as error:
            lock_path.unlink(missing_ok=True)
            raise RunnerError(issue=f"cannot copy leased index for {repo}: {error}") from error
        return cls(repo, index_path, lock_path)

    def commit(self) -> None:
        try:
            os.replace(self.lock_path, self.index_path)
        except OSError as error:
            raise RunnerError(issue=f"cannot commit leased index for {self.repo}: {error}") from error
        self.committed = True

    def release(self) -> None:
        if self.committed:
            return
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError as error:
            raise RunnerError(issue=f"cannot release index lease for {self.repo}: {error}") from error
