from __future__ import annotations

import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from git_ops import RunnerError, detail, head, head_reference, head_tree, repo_path, require_clean, require_repo, run_git
from profile_model import Profile, Series
from profile_transaction import StagedSeries, promote_stages


def _preflight_prerequisite(profile: Profile, repo_root: Path) -> None:
    for prerequisite in profile.prerequisites:
        path = repo_path(repo_root, prerequisite.build_path, "prerequisite")
        require_repo(path, "prerequisite")
        require_clean(path, "prerequisite")
        actual = head(path)
        if actual != prerequisite.sha:
            raise RunnerError(
                issue=(
                    f"wrong prerequisite HEAD for {prerequisite.build_path}: "
                    f"expected={prerequisite.sha}, actual={actual}"
                )
            )


def _preflight_target(series: Series, repo_root: Path) -> Path:
    path = repo_path(repo_root, series.target_repo, "target")
    require_repo(path, "target")
    require_clean(path, "target")
    actual_head = head(path)
    actual_tree = head_tree(path)
    if actual_head == series.head_sha or actual_tree == series.head_tree_sha:
        raise RunnerError(issue=f"target already promoted; refusing double apply: {series.target_repo}")
    if actual_head != series.base_sha:
        raise RunnerError(
            issue=(
                f"wrong base HEAD for {series.target_repo}: "
                f"expected={series.base_sha}, actual={actual_head}"
            )
        )
    advertised = run_git(path, ["rev-parse", f"{series.head_sha}^{{tree}}"])
    if advertised.returncode == 0 and advertised.stdout != series.head_tree_sha:
        raise RunnerError(issue=f"head tree metadata mismatch for {series.series_id}")
    return path


def preflight(profile: Profile, repo_root: Path) -> Path:
    root = repo_root.resolve()
    if not root.is_dir():
        raise RunnerError(issue=f"repository root does not exist: {repo_root}")
    _preflight_prerequisite(profile, root)
    for series in profile.series:
        _preflight_target(series, root)
    return root


def _am_arguments(series: Series) -> list[str]:
    return [
        "-c",
        "user.name=Infiniti Patch Runner",
        "-c",
        "user.email=patch-runner@example.invalid",
        "am",
        "--keep-cr",
        *[str(patch) for patch in series.patches],
    ]


def _surface_secondary(
    failure: BaseException | None,
    secondary: RunnerError | None,
    label: str,
) -> None:
    if secondary is None:
        return
    if isinstance(failure, RunnerError):
        raise RunnerError(issue=f"{failure}; {label}: {secondary}") from failure
    if failure is not None:
        raise failure from secondary
    raise secondary


def _cleanup_replay(
    target: Path,
    worktree: Path,
    temporary: tempfile.TemporaryDirectory,
    series_id: str,
    registered: bool = True,
) -> RunnerError | None:
    issues: list[str] = []
    if registered:
        try:
            removed = run_git(target, ["worktree", "remove", "--force", str(worktree)])
            if removed.returncode != 0:
                issues.append(f"cannot remove replay worktree for {series_id}: {detail(removed)}")
        except RunnerError as error:
            issues.append(str(error))
    try:
        temporary.cleanup()
    except OSError as error:
        issues.append(f"cannot remove replay directory for {series_id}: {error}")
    if issues:
        return RunnerError(issue="; ".join(issues))
    return None


def _stage_series(series: Series, target: Path) -> StagedSeries:
    temporary = tempfile.TemporaryDirectory(prefix=f"infiniti-{series.series_id}-")
    worktree = Path(temporary.name) / "worktree"
    registered = False
    try:
        added = run_git(target, ["worktree", "add", "--quiet", "--detach", str(worktree), series.base_sha])
        if added.returncode != 0:
            raise RunnerError(issue=f"cannot create replay worktree for {series.series_id}: {detail(added)}")
        registered = True
        applied = run_git(worktree, _am_arguments(series))
        if applied.returncode != 0:
            raise RunnerError(issue=f"git am failed for {series.series_id}: {detail(applied)}")
        actual_tree = head_tree(worktree)
        if actual_tree != series.head_tree_sha:
            raise RunnerError(
                issue=(
                    f"replay tree mismatch for {series.series_id}: "
                    f"expected={series.head_tree_sha}, actual={actual_tree}"
                )
            )
        return StagedSeries(series, target, temporary, worktree, head(worktree))
    finally:
        if sys.exc_info()[1] is not None:
            failure = sys.exc_info()[1]
            cleanup = _cleanup_replay(target, worktree, temporary, series.series_id, registered)
            _surface_secondary(failure, cleanup, "cleanup failed")


def _cleanup_stages(stages: Sequence[StagedSeries]) -> RunnerError | None:
    issues: list[str] = []
    for stage in reversed(stages):
        cleanup = _cleanup_replay(
            stage.target,
            stage.worktree,
            stage.temporary,
            stage.series.series_id,
        )
        if cleanup is not None:
            issues.append(str(cleanup))
    if issues:
        return RunnerError(issue="; ".join(issues))
    return None


def run_profile(profile: Profile, repo_root: Path, apply: bool) -> None:
    root = preflight(profile, repo_root)
    if apply:
        detached = [
            str(series.target_repo)
            for series in profile.series
            if head_reference(repo_path(root, series.target_repo, "target")) == "HEAD"
        ]
        if detached:
            raise RunnerError(
                issue=(
                    "detached target HEADs; create local branches before --apply: "
                    + ", ".join(detached)
                )
            )
    stages: list[StagedSeries] = []
    try:
        for series in profile.series:
            target = repo_path(root, series.target_repo, "target")
            stages.append(_stage_series(series, target))
        if apply:
            preflight(profile, root)
            expected_references = [head_reference(stage.target) for stage in stages]
            detached = [
                str(stage.series.target_repo)
                for stage, reference in zip(stages, expected_references)
                if reference == "HEAD"
            ]
            if detached:
                raise RunnerError(
                    issue=(
                        "detached target HEADs; create local branches before --apply: "
                        + ", ".join(detached)
                    )
                )
            promote_stages(stages, expected_references)
    finally:
        failure = sys.exc_info()[1]
        _surface_secondary(failure, _cleanup_stages(stages), "cleanup failed")
