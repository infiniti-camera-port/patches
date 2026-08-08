from __future__ import annotations

import os
from pathlib import Path

import pytest
from build_patch_reverse import execute_reverse_transaction
from build_patch_transaction import execute_transaction
from support import copy_overlay, create_repo_root
from test_symlink_delete_set import PartialVectorGit, _prepared_vector


@pytest.mark.parametrize(
    "check_fault",
    [OSError("secondary"), KeyboardInterrupt(), SystemExit(29)],
    ids=["oserror", "sigint", "system-exit"],
)
def test_partial_apply_recovery_probe_failure_still_restores_complete_vector(
    tmp_path: Path, check_fault: BaseException
) -> None:
    # Given three members are deleted before apply and every recovery probe fails.
    overlay = copy_overlay(tmp_path / "overlay")
    repo_root = create_repo_root(tmp_path / "repo")
    prepared, git = _prepared_vector(overlay, repo_root, tmp_path / "hooks")
    item = next(row for row in prepared if row.resolved.entry.name == "remove-broken-build-tools")
    faulty = PartialVectorGit(
        git,
        item,
        OSError("primary"),
        reverse_operation=False,
        check_fault=check_fault,
    )

    # When recovery encounters OSError, SIGINT, or SystemExit during state probing.
    outcome = execute_transaction([item], faulty)

    # Then the exception is contained and all six exact preimage links are restored.
    assert not outcome.ok
    assert outcome.rollback_failed
    assert tuple(os.readlink(member.path) for member in item.members) == tuple(
        member.target for member in item.members
    )


@pytest.mark.parametrize(
    "check_fault",
    [OSError("secondary"), KeyboardInterrupt(), SystemExit(29)],
    ids=["oserror", "sigint", "system-exit"],
)
def test_partial_reverse_recovery_probe_failure_still_restores_applied_vector(
    tmp_path: Path, check_fault: BaseException
) -> None:
    # Given three members are restored before reverse and every recovery probe fails.
    overlay = copy_overlay(tmp_path / "overlay")
    repo_root = create_repo_root(tmp_path / "repo")
    prepared, git = _prepared_vector(overlay, repo_root, tmp_path / "hooks")
    item = next(row for row in prepared if row.resolved.entry.name == "remove-broken-build-tools")
    assert execute_transaction([item], git).ok
    applied, git = _prepared_vector(overlay, repo_root, tmp_path / "hooks-2")
    applied_item = next(row for row in applied if row.resolved.entry.name == "remove-broken-build-tools")
    faulty = PartialVectorGit(
        git,
        applied_item,
        OSError("primary"),
        reverse_operation=True,
        check_fault=check_fault,
    )

    # When recovery encounters OSError, SIGINT, or SystemExit during state probing.
    outcome = execute_reverse_transaction([applied_item], faulty)

    # Then the exception is contained and all six invocation-owned links stay absent.
    assert not outcome.ok
    assert outcome.rollback_failed
    assert all(
        not member.path.exists() and not member.path.is_symlink()
        for member in applied_item.members
    )
