from __future__ import annotations

import os
import signal
import stat
from pathlib import Path
from types import FrameType
from typing import NamedTuple

from build_patch_manifest import paths_are_current
from build_patch_runtime import GitOperations, PreparedPatch
from build_patch_transaction import (
    TransactionOutcome,
    verify_patch_state,
    verify_prepared_snapshot,
)


class ReversedPatch(NamedTuple):
    item: PreparedPatch
    data: bytes
    mode: int


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _capture_reversed(
    item: PreparedPatch, git: GitOperations
) -> tuple[ReversedPatch | None, str]:
    if not paths_are_current(item.resolved):
        return None, "repository or target path changed"
    try:
        forward = git.apply_check(item.resolved.repo_path, item.resolved.patch_bytes)
        reverse = git.apply_check(
            item.resolved.repo_path, item.resolved.patch_bytes, reverse=True
        )
        data = b"" if item.members else item.resolved.target_file.read_bytes()
        mode = 0 if item.members else _mode(item.resolved.target_file)
    except (OSError, KeyboardInterrupt, SystemExit) as exc:
        return None, f"{type(exc).__name__} while inspecting reverse state: {exc}"
    if forward.ok and not reverse.ok:
        return ReversedPatch(item, data, mode), ""
    return (
        None,
        f"ambiguous reverse state forward_rc={forward.returncode} reverse_rc={reverse.returncode}",
    )


def _restore(items: list[ReversedPatch], git: GitOperations) -> tuple[bool, str]:
    failures: list[str] = []
    for reversed_item in reversed(items):
        item = reversed_item.item
        name = item.resolved.entry.name
        before_apply = verify_patch_state(
            item, git, reversed_item.data, reversed_item.mode, applied=False
        )
        if before_apply:
            failures.append(f"{name}: rollback CAS refused: {before_apply}")
            continue
        try:
            result = git.apply(item.resolved.repo_path, item.resolved.patch_bytes)
        except (OSError, KeyboardInterrupt, SystemExit) as exc:
            failures.append(
                f"{name}: forward rollback raised {type(exc).__name__}: {exc}"
            )
            continue
        if not result.ok:
            failures.append(f"{name}: forward rollback failed: {result.stderr}")
            continue
        after_apply = verify_patch_state(
            item, git, item.before, item.before_mode, applied=True
        )
        if after_apply:
            failures.append(f"{name}: rollback verification failed: {after_apply}")
    return not failures, "; ".join(failures)


def _remove_active_vector(item: PreparedPatch, git: GitOperations) -> str:
    failures: list[str] = []
    for member in item.members:
        try:
            metadata = member.path.lstat()
        except FileNotFoundError:
            continue
        except (OSError, KeyboardInterrupt, SystemExit) as exc:
            failures.append(f"{member.path}: {type(exc).__name__}: {exc}")
            continue
        if not stat.S_ISLNK(metadata.st_mode):
            failures.append(f"{member.path}: manual recovery required for non-symlink")
            continue
        try:
            target = os.readlink(member.path)
        except (OSError, KeyboardInterrupt, SystemExit) as exc:
            failures.append(f"{member.path}: {type(exc).__name__}: {exc}")
            continue
        if target != member.target or stat.S_IFMT(metadata.st_mode) != member.mode:
            failures.append(f"{member.path}: manual recovery required for changed symlink")
            continue
        try:
            member.path.unlink()
        except (OSError, KeyboardInterrupt, SystemExit) as exc:
            failures.append(f"{member.path}: {type(exc).__name__}: {exc}")
    if failures:
        return "; ".join(failures)
    state = verify_patch_state(item, git, item.after, item.after_mode, applied=True)
    return "" if not state else f"vector recovery verification failed: {state}"


def _recover(
    primary: str,
    reversed_items: list[ReversedPatch],
    active: PreparedPatch | None,
    git: GitOperations,
    *,
    interrupted: bool,
) -> TransactionOutcome:
    cleanup_failures: list[str] = []
    if active is not None:
        still_applied = verify_patch_state(
            active, git, active.before, active.before_mode, applied=True
        )
        if still_applied:
            captured, capture_error = _capture_reversed(active, git)
            if captured is None:
                if active.members:
                    vector_error = _remove_active_vector(active, git)
                    if vector_error:
                        cleanup_failures.append(
                            f"{active.resolved.entry.name}: {vector_error}"
                        )
                else:
                    cleanup_failures.append(
                        f"{active.resolved.entry.name}: active state is ambiguous: "
                        f"applied={still_applied}; reversed={capture_error}"
                    )
            else:
                reversed_items.append(captured)
    rollback_ok, rollback_detail = _restore(reversed_items, git)
    if not rollback_ok:
        cleanup_failures.append(rollback_detail)
    details = "; ".join(cleanup_failures)
    message = primary if not details else f"{primary}; cleanup: {details}"
    return TransactionOutcome(False, interrupted, bool(cleanup_failures), message)


def execute_reverse_transaction(
    prepared: list[PreparedPatch], git: GitOperations
) -> TransactionOutcome:
    if any(item.pending for item in prepared):
        return TransactionOutcome(
            False, False, False, "reverse requires every selected patch applied"
        )
    initial_error = verify_prepared_snapshot(prepared, git)
    if initial_error:
        return TransactionOutcome(False, False, False, initial_error)
    reversed_items: list[ReversedPatch] = []
    active: PreparedPatch | None = None
    original_handler = signal.getsignal(signal.SIGINT)

    def first_sigint(_signal_number: int, _frame: FrameType | None) -> None:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, first_sigint)
    try:
        for item in prepared:
            current_state = verify_patch_state(
                item, git, item.before, item.before_mode, applied=True
            )
            if current_state:
                return _recover(
                    f"concurrent target change before {item.resolved.entry.name}: {current_state}",
                    reversed_items,
                    None,
                    git,
                    interrupted=False,
                )
            active = item
            try:
                result = git.apply(
                    item.resolved.repo_path, item.resolved.patch_bytes, reverse=True
                )
            except (OSError, KeyboardInterrupt, SystemExit) as exc:
                return _recover(
                    f"{type(exc).__name__} during reverse of {item.resolved.entry.name}: {exc}",
                    reversed_items,
                    active,
                    git,
                    interrupted=isinstance(exc, (KeyboardInterrupt, SystemExit)),
                )
            if not result.ok:
                return _recover(
                    f"reverse failed for {item.resolved.entry.name}: {result.stderr}",
                    reversed_items,
                    active,
                    git,
                    interrupted=False,
                )
            captured, capture_error = _capture_reversed(item, git)
            if captured is None:
                return _recover(
                    f"post-reverse verification failed for {item.resolved.entry.name}: {capture_error}",
                    reversed_items,
                    active,
                    git,
                    interrupted=False,
                )
            reversed_items.append(captured)
            active = None
        return TransactionOutcome(
            True, False, False, "all selected patches reversed and verified"
        )
    except (OSError, KeyboardInterrupt, SystemExit) as exc:
        return _recover(
            f"{type(exc).__name__} during reverse transaction: {exc}",
            reversed_items,
            active,
            git,
            interrupted=isinstance(exc, (KeyboardInterrupt, SystemExit)),
        )
    finally:
        signal.signal(signal.SIGINT, original_handler)
