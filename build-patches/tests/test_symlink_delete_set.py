from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from build_patch_manifest import parse_manifest, select_registry
from build_patch_registry import validate_registry
from build_patch_reverse import execute_reverse_transaction
from build_patch_runtime import CommandResult, GitClient, PreparedPatch, prepare_patches
from build_patch_transaction import execute_transaction
from support import copy_overlay, create_repo_root, output_of, run_overlay

MEMBERS = tuple(
    (f"path/{platform}/{tool}", f"../../{platform}/bin/toybox")
    for platform in ("darwin-x86", "linux-arm64", "linux-x86")
    for tool in ("date", "tar")
)


class PartialVectorGit:
    def __init__(
        self,
        delegate: GitClient,
        item: PreparedPatch,
        fault: BaseException,
        *,
        reverse_operation: bool,
        corrupt_member: bool = False,
    ) -> None:
        self.delegate = delegate
        self.item = item
        self.fault = fault
        self.reverse_operation = reverse_operation
        self.corrupt_member = corrupt_member

    def apply(self, repo: Path, patch_bytes: bytes, *, reverse: bool = False) -> CommandResult:
        if reverse == self.reverse_operation and patch_bytes == self.item.resolved.patch_bytes:
            for member in self.item.members[:3]:
                if reverse:
                    member.path.parent.mkdir(parents=True, exist_ok=True)
                    member.path.symlink_to(member.target)
                else:
                    member.path.unlink()
            if self.corrupt_member:
                member = self.item.members[3]
                member.path.unlink(missing_ok=True)
                member.path.symlink_to("../../unexpected/bin/tool")
            raise self.fault
        return self.delegate.apply(repo, patch_bytes, reverse=reverse)

    def apply_check(self, repo: Path, patch_bytes: bytes, *, reverse: bool = False) -> CommandResult:
        return self.delegate.apply_check(repo, patch_bytes, reverse=reverse)


def _prepared_vector(
    overlay: Path, repo_root: Path, hooks: Path
) -> tuple[list[PreparedPatch], GitClient]:
    entries = validate_registry(parse_manifest(overlay / "manifest.yml"), overlay)
    resolved = select_registry(
        entries,
        repo_root,
        frozenset(entry.entry.target_repo for entry in entries),
    )
    hooks.mkdir(exist_ok=True)
    git = GitClient(hooks)
    prepared, errors = prepare_patches(resolved, git)
    assert not errors
    return prepared, git


def _reverse(overlay: Path, repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(overlay / "apply-build-patches.py"),
            "--reverse",
            "--repo-root",
            str(repo_root),
        ],
        cwd=overlay,
        capture_output=True,
        text=True,
        check=False,
    )


def test_registry_declares_one_six_member_symlink_delete_vector(tmp_path: Path) -> None:
    # Given the canonical registry.
    overlay = copy_overlay(tmp_path / "overlay")
    sys.path.insert(0, str(overlay))
    try:
        from build_patch_manifest import parse_manifest
    finally:
        sys.path.pop(0)

    # When its typed operations are parsed.
    entries = parse_manifest(overlay / "manifest.yml")
    operation = next(entry for entry in entries if entry.name == "remove-broken-build-tools")

    # Then it is one order-8 vector over the exact six symlinks.
    assert len(entries) == 11
    assert operation.target_repo == "prebuilts/build-tools"
    assert operation.target_path == "path"
    assert operation.apply_order == 8
    assert operation.operation_kind == "symlink-delete-set-v1"
    assert tuple((member.path, member.target) for member in operation.members) == MEMBERS
    assert {member.mode for member in operation.members} == {0o120000}


def test_symlink_delete_vector_applies_and_reverses_exact_targets_and_modes(
    tmp_path: Path,
) -> None:
    # Given six tracked mode-120000 links in the exact base state.
    overlay = copy_overlay(tmp_path / "overlay")
    repo_root = create_repo_root(tmp_path / "repo")
    build_tools = repo_root / "prebuilts/build-tools"

    # When the logical overlay is applied and then reversed.
    applied = run_overlay(overlay, repo_root, apply=True)
    assert applied.returncode == 0, output_of(applied)
    assert all(not (build_tools / path).exists() and not (build_tools / path).is_symlink() for path, _ in MEMBERS)
    reversed_result = _reverse(overlay, repo_root)

    # Then every exact symlink target and Git mode is restored together.
    assert reversed_result.returncode == 0, output_of(reversed_result)
    assert tuple(os.readlink(build_tools / path) for path, _ in MEMBERS) == tuple(target for _, target in MEMBERS)
    modes = subprocess.run(
        ["git", "-C", str(build_tools), "ls-files", "-s", "--", *[path for path, _ in MEMBERS]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert len(modes) == 6
    assert all(line.startswith("120000 ") for line in modes)


def test_predeleted_vector_is_idempotent_for_envsetup_removal(tmp_path: Path) -> None:
    # Given the declared overlay already removed every link.
    overlay = copy_overlay(tmp_path / "overlay")
    repo_root = create_repo_root(tmp_path / "repo")
    build_tools = repo_root / "prebuilts/build-tools"
    first = run_overlay(overlay, repo_root, apply=True)
    assert first.returncode == 0, output_of(first)

    # When the envsetup-equivalent removal runs again and the registry is checked.
    for path, _target in MEMBERS:
        (build_tools / path).unlink(missing_ok=True)
    checked = run_overlay(overlay, repo_root)

    # Then it is an already-applied logical operation with no further mutation.
    assert checked.returncode == 0, output_of(checked)
    assert "remove-broken-build-tools" in checked.stdout
    assert "ALREADY_APPLIED" in checked.stdout


@pytest.mark.parametrize(
    "fault",
    [OSError("injected"), KeyboardInterrupt(), SystemExit(23)],
    ids=["oserror", "sigint", "system-exit"],
)
def test_partial_vector_apply_restores_every_invocation_owned_member(
    tmp_path: Path, fault: BaseException
) -> None:
    # Given a prepared vector whose git operation stops after deleting three links.
    overlay = copy_overlay(tmp_path / "overlay")
    repo_root = create_repo_root(tmp_path / "repo")
    prepared, git = _prepared_vector(overlay, repo_root, tmp_path / "hooks")
    item = next(row for row in prepared if row.resolved.entry.name == "remove-broken-build-tools")

    # When transaction execution observes OSError, SIGINT, or SystemExit.
    outcome = execute_transaction(
        [item], PartialVectorGit(git, item, fault, reverse_operation=False)
    )

    # Then recovery restores the entire exact preimage and reports no rollback failure.
    assert not outcome.ok
    assert not outcome.rollback_failed
    assert tuple(os.readlink(member.path) for member in item.members) == tuple(
        member.target for member in item.members
    )


@pytest.mark.parametrize(
    "fault",
    [OSError("injected"), KeyboardInterrupt(), SystemExit(23)],
    ids=["oserror", "sigint", "system-exit"],
)
def test_partial_vector_reverse_restores_fully_applied_state(
    tmp_path: Path, fault: BaseException
) -> None:
    # Given a fully applied vector whose reverse stops after restoring three links.
    overlay = copy_overlay(tmp_path / "overlay")
    repo_root = create_repo_root(tmp_path / "repo")
    prepared, git = _prepared_vector(overlay, repo_root, tmp_path / "hooks")
    item = next(row for row in prepared if row.resolved.entry.name == "remove-broken-build-tools")
    assert execute_transaction([item], git).ok
    applied, git = _prepared_vector(overlay, repo_root, tmp_path / "hooks-2")
    applied_item = next(row for row in applied if row.resolved.entry.name == "remove-broken-build-tools")

    # When reverse transaction execution observes OSError, SIGINT, or SystemExit.
    outcome = execute_reverse_transaction(
        [applied_item],
        PartialVectorGit(git, applied_item, fault, reverse_operation=True),
    )

    # Then recovery removes every invocation-owned restoration and keeps all six absent.
    assert not outcome.ok
    assert not outcome.rollback_failed
    assert all(not member.path.exists() and not member.path.is_symlink() for member in applied_item.members)


def test_partial_vector_precondition_and_symlinked_parent_fail_before_mutation(
    tmp_path: Path,
) -> None:
    # Given one missing member and, separately, a member parent redirected by symlink.
    overlay = copy_overlay(tmp_path / "overlay")
    partial_root = create_repo_root(tmp_path / "partial")
    partial_member = partial_root / "prebuilts/build-tools" / MEMBERS[0][0]
    partial_member.unlink()

    # When each unsafe composition is checked.
    partial = run_overlay(overlay, partial_root)
    symlink_root = create_repo_root(tmp_path / "symlink")
    parent = symlink_root / "prebuilts/build-tools/path/darwin-x86"
    for child in parent.iterdir():
        child.unlink()
    parent.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    parent.symlink_to(outside, target_is_directory=True)
    redirected = run_overlay(overlay, symlink_root)

    # Then both fail closed before any of the other five links changes.
    assert partial.returncode != 0
    assert "partial symlink-delete-set state" in output_of(partial)
    assert redirected.returncode != 0
    assert "symlink component" in output_of(redirected)
    assert sum(path.is_symlink() for path in (partial_root / "prebuilts/build-tools/path").glob("*/*")) == 5


def test_partial_vector_with_concurrent_wrong_target_fails_closed_with_recovery_path(
    tmp_path: Path,
) -> None:
    # Given a failed apply that also exposes a changed symlink not owned by the invocation.
    overlay = copy_overlay(tmp_path / "overlay")
    repo_root = create_repo_root(tmp_path / "repo")
    prepared, git = _prepared_vector(overlay, repo_root, tmp_path / "hooks")
    item = next(row for row in prepared if row.resolved.entry.name == "remove-broken-build-tools")

    # When recovery probes the entire vector.
    outcome = execute_transaction(
        [item],
        PartialVectorGit(
            git,
            item,
            OSError("injected"),
            reverse_operation=False,
            corrupt_member=True,
        ),
    )

    # Then it refuses to overwrite the changed member and names exact manual recovery.
    assert not outcome.ok
    assert outcome.rollback_failed
    assert "manual recovery required for changed symlink" in outcome.message
    assert str(item.members[3].path) in outcome.message
