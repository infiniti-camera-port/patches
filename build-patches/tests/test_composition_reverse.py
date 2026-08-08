from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from support import (
    copy_overlay,
    create_repo_root,
    output_of,
    run_overlay,
    set_git_executable,
    write_git_wrapper,
)


def test_apply_and_reverse_share_selection_and_restore_clean_tree(
    tmp_path: Path,
) -> None:
    overlay = copy_overlay(tmp_path / "overlay")
    repo_root = create_repo_root(tmp_path / "repo")
    applied = run_overlay(overlay, repo_root, apply=True)
    assert applied.returncode == 0, output_of(applied)
    reversed_result = subprocess.run(
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
    assert reversed_result.returncode == 0, output_of(reversed_result)
    assert "10 patch(es) / 9 target(s)" in applied.stdout
    assert "10 patch(es) / 9 target(s)" in reversed_result.stdout
    assert "all selected patches reversed transactionally" in reversed_result.stdout
    for path in (
        "build/soong",
        "device/oneplus/infiniti",
        "device/oneplus/macan",
        "device/oneplus/macanc",
        "device/oneplus/fairlady",
        "external/google-highway",
        "external/skia",
        "external/dng_sdk",
        "packages/apps/Settings",
    ):
        status = subprocess.run(
            ["git", "-C", str(repo_root / path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout == ""


@pytest.mark.parametrize(
    ("fault", "marker"), [("apply", "ROLLBACK: PASS"), ("sigint", "INTERRUPTED")]
)
def test_reverse_failure_restores_every_applied_patch(
    tmp_path: Path, fault: str, marker: str
) -> None:
    overlay = copy_overlay(tmp_path / "overlay")
    repo_root = create_repo_root(tmp_path / "repo")
    applied = run_overlay(overlay, repo_root, apply=True)
    assert applied.returncode == 0, output_of(applied)
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = write_git_wrapper(wrapper_dir, repo_root, fault)
    set_git_executable(overlay, wrapper)
    result = subprocess.run(
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
    assert result.returncode != 0
    assert marker in result.stdout + result.stderr
    for path in (
        "build/soong",
        "device/oneplus/infiniti",
        "device/oneplus/macan",
        "device/oneplus/macanc",
        "device/oneplus/fairlady",
        "external/google-highway",
        "external/skia",
        "external/dng_sdk",
        "packages/apps/Settings",
    ):
        status = subprocess.run(
            ["git", "-C", str(repo_root / path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout != ""
