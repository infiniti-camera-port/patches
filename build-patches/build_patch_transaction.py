from __future__ import annotations

import signal
import stat
from pathlib import Path
from types import FrameType
from typing import NamedTuple

from build_patch_manifest import paths_are_current
from build_patch_runtime import GitOperations, PreparedPatch


class TransactionOutcome(NamedTuple):
    ok: bool
    interrupted: bool
    rollback_failed: bool
    message: str


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _matches(path: Path, data: bytes, mode: int) -> bool:
    try:
        return path.read_bytes() == data and _mode(path) == mode
    except OSError:
        return False


def _verify(
    item: PreparedPatch,
    git: GitOperations,
    data: bytes,
    mode: int,
    *,
    applied: bool,
) -> str:
    if not paths_are_current(item.resolved):
        return "repository or target path changed"
    if not _matches(item.resolved.target_file, data, mode):
        return "concurrent target content or mode change"
    try:
        forward = git.apply_check(item.resolved.repo_path, item.resolved.patch_bytes)
        reverse = git.apply_check(item.resolved.repo_path, item.resolved.patch_bytes, reverse=True)
    except OSError as exc:
        return f"{type(exc).__name__} during state verification: {exc}"
    if applied and (not reverse.ok or forward.ok):
        return f"expected applied state, forward_rc={forward.returncode} reverse_rc={reverse.returncode}"
    if not applied and (not forward.ok or reverse.ok):
        return f"expected unapplied state, forward_rc={forward.returncode} reverse_rc={reverse.returncode}"
    return ""


def _rollback(applied_items: list[PreparedPatch], git: GitOperations) -> tuple[bool, str]:
    failures: list[str] = []
    for item in reversed(applied_items):
        name = item.resolved.entry.name
        before_reverse = _verify(item, git, item.after, item.after_mode, applied=True)
        if before_reverse:
            failures.append(f"{name}: rollback CAS refused: {before_reverse}")
            continue
        try:
            result = git.apply(item.resolved.repo_path, item.resolved.patch_bytes, reverse=True)
        except (OSError, KeyboardInterrupt, SystemExit) as exc:
            failures.append(f"{name}: reverse apply raised {type(exc).__name__}: {exc}")
            continue
        if not result.ok:
            failures.append(f"{name}: reverse apply failed: {result.stderr}")
            continue
        after_reverse = _verify(item, git, item.before, item.before_mode, applied=False)
        if after_reverse:
            failures.append(f"{name}: rollback verification failed: {after_reverse}")
    return not failures, "; ".join(failures)


def _recover(
    primary: str,
    prepared_applied: list[PreparedPatch],
    active: PreparedPatch | None,
    git: GitOperations,
    *,
    interrupted: bool,
) -> TransactionOutcome:
    cleanup_failures: list[str] = []
    if active is not None and active not in prepared_applied:
        applied_state = _verify(active, git, active.after, active.after_mode, applied=True)
        if not applied_state:
            prepared_applied.append(active)
        else:
            before_state = _verify(active, git, active.before, active.before_mode, applied=False)
            if before_state:
                cleanup_failures.append(
                    f"{active.resolved.entry.name}: active state is ambiguous: applied={applied_state}; unapplied={before_state}"
                )
    rollback_ok, rollback_detail = _rollback(prepared_applied, git)
    if not rollback_ok:
        cleanup_failures.append(rollback_detail)
    details = "; ".join(cleanup_failures)
    message = primary if not details else f"{primary}; cleanup: {details}"
    return TransactionOutcome(False, interrupted, bool(cleanup_failures), message)


def verify_prepared_snapshot(prepared: list[PreparedPatch], git: GitOperations) -> str:
    for item in prepared:
        initial_state = _verify(
            item,
            git,
            item.before,
            item.before_mode,
            applied=not item.pending,
        )
        if initial_state:
            return f"concurrent prepared-state change for {item.resolved.entry.name}: {initial_state}"
    return ""


def execute_transaction(prepared: list[PreparedPatch], git: GitOperations) -> TransactionOutcome:
    initial_error = verify_prepared_snapshot(prepared, git)
    if initial_error:
        return TransactionOutcome(False, False, False, initial_error)
    applied_items: list[PreparedPatch] = []
    active: PreparedPatch | None = None
    original_handler = signal.getsignal(signal.SIGINT)

    def first_sigint(_signal_number: int, _frame: FrameType | None) -> None:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, first_sigint)
    try:
        for item in prepared:
            if not item.pending:
                continue
            current_state = _verify(item, git, item.before, item.before_mode, applied=False)
            if current_state:
                return _recover(
                    f"concurrent target change before {item.resolved.entry.name}: {current_state}",
                    applied_items,
                    None,
                    git,
                    interrupted=False,
                )
            active = item
            try:
                result = git.apply(item.resolved.repo_path, item.resolved.patch_bytes)
            except (OSError, KeyboardInterrupt, SystemExit) as exc:
                return _recover(
                    f"{type(exc).__name__} during apply of {item.resolved.entry.name}: {exc}",
                    applied_items,
                    active,
                    git,
                    interrupted=isinstance(exc, (KeyboardInterrupt, SystemExit)),
                )
            if not result.ok:
                return _recover(
                    f"apply failed for {item.resolved.entry.name}: {result.stderr}",
                    applied_items,
                    active,
                    git,
                    interrupted=False,
                )
            applied_items.append(item)
            active = None
            post_state = _verify(item, git, item.after, item.after_mode, applied=True)
            if post_state:
                return _recover(
                    f"post-apply verification failed for {item.resolved.entry.name}: {post_state}",
                    applied_items,
                    None,
                    git,
                    interrupted=False,
                )
        for item in prepared:
            final_data = item.after if item.pending else item.before
            final_mode = item.after_mode if item.pending else item.before_mode
            final_state = _verify(item, git, final_data, final_mode, applied=True)
            if final_state:
                return _recover(
                    f"final verification failed for {item.resolved.entry.name}: {final_state}",
                    applied_items,
                    None,
                    git,
                    interrupted=False,
                )
        return TransactionOutcome(True, False, False, "all patches applied and verified")
    except (OSError, KeyboardInterrupt, SystemExit) as exc:
        return _recover(
            f"{type(exc).__name__} during transaction: {exc}",
            applied_items,
            active,
            git,
            interrupted=isinstance(exc, (KeyboardInterrupt, SystemExit)),
        )
    finally:
        signal.signal(signal.SIGINT, original_handler)
