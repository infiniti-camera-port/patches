from __future__ import annotations

import signal
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from git_ops import RunnerError, detail, git_value, head, head_reference, run_git
from profile_model import Series
from ref_transaction import IndexLease


@dataclass(frozen=True)  # noqa: SLOTS_OK
class StagedSeries:
    series: Series
    target: Path
    temporary: tempfile.TemporaryDirectory
    worktree: Path
    staged_head: str


@dataclass(frozen=True)  # noqa: SLOTS_OK
class RollbackRequest:
    series: Series
    target: Path
    reference: str
    expected_head: str


def _surface_secondary(failure: BaseException, secondary: RunnerError | None) -> None:
    if secondary is None:
        return
    if isinstance(failure, RunnerError):
        raise RunnerError(issue=f"{failure}; rollback failed: {secondary}") from failure
    raise failure from secondary


def _release_lease(lease: IndexLease | None, failure: BaseException | None) -> None:
    cleanup = None
    if lease is not None:
        try:
            lease.release()
        except RunnerError as error:
            cleanup = error
    if cleanup is None:
        return
    if failure is not None:
        raise failure from cleanup
    raise cleanup


def _restore_worktree(
    request: RollbackRequest,
    old_tree: str,
    new_tree: str,
    operation: str,
    lease: IndexLease,
    expected_reference: str,
) -> None:
    restored = run_git(
        request.target,
        ["read-tree", "-m", "-u", old_tree, new_tree],
        index_file=lease.lock_path,
    )
    if restored.returncode != 0:
        raise RunnerError(issue=f"cannot {operation} worktree for {request.series.series_id}: {detail(restored)}")
    current_reference = head_reference(request.target)
    current_head = head(request.target)
    if current_reference != expected_reference or current_head != new_tree:
        if operation == "promotion" and current_head == old_tree:
            reversed_tree = run_git(
                request.target,
                ["read-tree", "-m", "-u", new_tree, old_tree],
                index_file=lease.lock_path,
            )
            if reversed_tree.returncode != 0:
                raise RunnerError(
                    issue=(
                        f"ref diverged during promotion worktree restore for {request.series.series_id}; "
                        f"cannot reverse staged worktree: {detail(reversed_tree)}"
                    )
                )
        raise RunnerError(issue=f"ref diverged during {operation} worktree restore for {request.series.series_id}")
    status = run_git(
        request.target,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        index_file=lease.lock_path,
    )
    if status.returncode != 0:
        raise RunnerError(issue=f"cannot inspect {operation} repository {request.series.series_id}: {detail(status)}")
    if status.stdout:
        raise RunnerError(issue=f"dirty repository after {operation} for {request.series.series_id}")


def _rollback_one(request: RollbackRequest) -> None:
    lease = IndexLease.acquire(request.target)
    try:
        updated = run_git(
            request.target,
            ["update-ref", "--no-deref", request.reference, request.series.base_sha, request.expected_head],
        )
        if updated.returncode != 0:
            raise RunnerError(
                issue=f"compare-and-swap rejected rollback for {request.series.series_id}: {detail(updated)}"
            )
        actual = git_value(
            request.target,
            ["rev-parse", request.reference],
            f"cannot verify rollback ref for {request.series.series_id}",
        )
        if actual != request.series.base_sha:
            raise RunnerError(issue=f"ref diverged after rollback CAS for {request.series.series_id}")
        current_reference = head_reference(request.target)
        if head(request.target) != request.series.base_sha:
            return
        _restore_worktree(
            request,
            request.expected_head,
            request.series.base_sha,
            "rollback",
            lease,
            current_reference,
        )
        lease.commit()
    finally:
        _release_lease(lease, sys.exc_info()[1])


def _rollback(requests: Sequence[RollbackRequest]) -> RunnerError | None:
    failures: list[str] = []
    for request in reversed(requests):
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            _rollback_one(request)
        except RunnerError as error:
            failures.append(str(error))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    if failures:
        return RunnerError(issue="; ".join(failures))
    return None


def _promote_stage(
    stage: StagedSeries,
    expected_reference: str,
    completed: list[RollbackRequest],
) -> None:
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    lease: IndexLease | None = None
    try:
        lease = IndexLease.acquire(stage.target)
        actual_reference = head_reference(stage.target)
        if actual_reference == "HEAD" or actual_reference != expected_reference or head(stage.target) != stage.series.base_sha:
            raise RunnerError(issue=f"HEAD changed before promotion for {stage.series.series_id}")
        request = RollbackRequest(stage.series, stage.target, actual_reference, stage.staged_head)
        updated = run_git(
            stage.target,
            ["update-ref", "--no-deref", actual_reference, stage.staged_head, stage.series.base_sha],
        )
        if updated.returncode != 0:
            raise RunnerError(
                issue=f"compare-and-swap rejected promotion for {stage.series.series_id}: {detail(updated)}"
            )
        completed.append(request)
        _restore_worktree(
            request,
            stage.series.base_sha,
            stage.staged_head,
            "promotion",
            lease,
            request.reference,
        )
        lease.commit()
    finally:
        failure = sys.exc_info()[1]
        try:
            _release_lease(lease, failure)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def promote_stages(stages: Sequence[StagedSeries], expected_references: Sequence[str]) -> None:
    completed: list[RollbackRequest] = []
    try:
        for stage, expected_reference in zip(stages, expected_references):
            _promote_stage(stage, expected_reference, completed)
    finally:
        failure = sys.exc_info()[1]
        if failure is not None:
            _surface_secondary(failure, _rollback(completed))
