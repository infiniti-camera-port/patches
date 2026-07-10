from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


PROFILE: Final = "infiniti-crdroid-16.0"
RUNNER_SOURCE: Final = Path(__file__).parents[1]


@dataclass(frozen=True)  # noqa: SLOTS_OK
class SeriesData:
    series_id: str
    directory: str
    target_repo: str
    base_sha: str
    head_sha: str
    head_tree_sha: str
    count: int = 1
    apply_order: int = 1


@dataclass  # noqa: MUTABLE_OK  # noqa: SLOTS_OK
class ProfileFixture:
    root: Path
    patch_root: Path = field(init=False)
    repo_root: Path = field(init=False)
    series: list[SeriesData] = field(default_factory=list)
    prerequisites: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.patch_root = self.root / "patch-profile"
        self.repo_root = self.root / "android"
        self.patch_root.mkdir()
        self.repo_root.mkdir()
        for source in RUNNER_SOURCE.glob("*.py"):
            shutil.copy2(source, self.patch_root / source.name)
        for index in range(6):
            path = f"sync/prerequisite-{index}"
            sha = self._init_repo(self.repo_root / path, f"prerequisite-{index}")
            self.prerequisites.append((path, sha))

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _init_repo(self, repo: Path, content: str) -> str:
        repo.mkdir(parents=True)
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.name", "Runner Test")
        self._git(repo, "config", "user.email", "runner@example.invalid")
        (repo / "content.txt").write_text(f"{content}\n", encoding="utf-8")
        self._git(repo, "add", "content.txt")
        self._git(repo, "commit", "-qm", "base")
        return self._git(repo, "rev-parse", "HEAD")

    def add_series(self, name: str, order: int) -> SeriesData:
        target = f"platform/{name}"
        repo = self.repo_root / target
        base_sha = self._init_repo(repo, f"base-{name}")
        (repo / "content.txt").write_text(f"promoted-{name}\n", encoding="utf-8")
        self._git(repo, "add", "content.txt")
        self._git(repo, "commit", "-qm", f"promote {name}")
        head_sha = self._git(repo, "rev-parse", "HEAD")
        head_tree_sha = self._git(repo, "rev-parse", "HEAD^{tree}")
        directory = target.replace("/", ",")
        patch_dir = self.patch_root / directory
        patch_dir.mkdir()
        patch = self._git(repo, "format-patch", "-1", "--stdout", "HEAD")
        (patch_dir / f"0001-{name}.patch").write_text(f"{patch}\n", encoding="utf-8")
        self._git(repo, "reset", "--hard", base_sha)
        data = SeriesData(
            series_id=name,
            directory=directory,
            target_repo=target,
            base_sha=base_sha,
            head_sha=head_sha,
            head_tree_sha=head_tree_sha,
            apply_order=order,
        )
        self.series.append(data)
        return data

    def write_metadata(self, profile: str = PROFILE) -> None:
        prerequisite_rows = [
            {
                "repo": f"example/prerequisite-{index}",
                "build_path": path,
                "ref": "16.0",
                "sha": sha,
            }
            for index, (path, sha) in enumerate(self.prerequisites)
        ]
        series_rows = [
            {
                "id": item.series_id,
                "directory": item.directory,
                "target_repo": item.target_repo,
                "source_repo": f"example/{item.series_id}",
                "base_sha": item.base_sha,
                "head_sha": item.head_sha,
                "head_tree_sha": item.head_tree_sha,
                "count": item.count,
                "apply_order": item.apply_order,
            }
            for item in self.series
        ]
        payload = {
            "schema_version": 1,
            "profile": profile,
            "sync_only_prerequisites": prerequisite_rows,
            "series": series_rows,
        }
        (self.patch_root / "series.json").write_text(
            f"{json.dumps(payload, indent=2)}\n",
            encoding="utf-8",
        )

    def run(self, mode: str = "--check-only", profile: str = PROFILE) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                str(self.patch_root / "apply-patches.py"),
                "--profile",
                profile,
                "--repo-root",
                str(self.repo_root),
                mode,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def head(self, series: SeriesData) -> str:
        return self._git(self.repo_root / series.target_repo, "rev-parse", "HEAD")

    def tree(self, series: SeriesData) -> str:
        return self._git(self.repo_root / series.target_repo, "rev-parse", "HEAD^{tree}")


class ApplyPatchesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ProfileFixture(Path(self.temporary.name))
        self.first = self.fixture.add_series("alpha", 1)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_failed_without_moving_first(self, result: subprocess.CompletedProcess[str], text: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(text, result.stderr)
        self.assertEqual(self.fixture.head(self.first), self.first.base_sha)

    def test_good_profile_contract_when_check_only(self) -> None:
        self.fixture.write_metadata()
        before = self.fixture.head(self.first)
        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check-only complete: 1 series, 1 patch", result.stdout)
        self.assertEqual(self.fixture.head(self.first), before)

    def test_bad_base_when_preflight_runs(self) -> None:
        self.fixture.write_metadata()
        repo = self.fixture.repo_root / self.first.target_repo
        (repo / "wrong.txt").write_text("wrong\n", encoding="utf-8")
        self.fixture._git(repo, "add", "wrong.txt")
        self.fixture._git(repo, "commit", "-qm", "wrong base")
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wrong base HEAD", result.stderr)

    def test_dirty_target_when_preflight_runs(self) -> None:
        self.fixture.write_metadata()
        (self.fixture.repo_root / self.first.target_repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        self.assert_failed_without_moving_first(self.fixture.run("--apply"), "dirty target repository")

    def test_duplicate_order_when_metadata_is_parsed(self) -> None:
        second = self.fixture.add_series("beta", 1)
        self.fixture.write_metadata()
        self.assert_failed_without_moving_first(self.fixture.run("--apply"), "duplicate apply_order")
        self.assertEqual(self.fixture.head(second), second.base_sha)

    def test_count_mismatch_when_metadata_is_parsed(self) -> None:
        self.fixture.series[0] = dataclasses.replace(self.first, count=2)
        self.fixture.write_metadata()
        self.assert_failed_without_moving_first(self.fixture.run("--apply"), "patch count mismatch")

    def test_orphan_patch_when_metadata_is_parsed(self) -> None:
        self.fixture.write_metadata()
        orphan = self.fixture.patch_root / "orphan,repo"
        orphan.mkdir()
        (orphan / "0001-orphan.patch").write_text("orphan\n", encoding="utf-8")
        self.assert_failed_without_moving_first(self.fixture.run("--apply"), "orphan patch")

    def test_corrupt_late_patch_when_apply_preflights_every_series(self) -> None:
        second = self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        (self.fixture.patch_root / second.directory / "0001-beta.patch").write_text("corrupt\n", encoding="utf-8")
        self.assert_failed_without_moving_first(self.fixture.run("--apply"), "head_sha does not match final patch")
        self.assertEqual(self.fixture.head(second), second.base_sha)

    def test_missing_prerequisite_when_preflight_runs(self) -> None:
        self.fixture.write_metadata()
        shutil.rmtree(self.fixture.repo_root / self.fixture.prerequisites[0][0])
        self.assert_failed_without_moving_first(self.fixture.run("--apply"), "missing prerequisite repository")

    def test_wrong_prerequisite_when_preflight_runs(self) -> None:
        self.fixture.write_metadata()
        path = self.fixture.repo_root / self.fixture.prerequisites[0][0]
        (path / "wrong.txt").write_text("wrong\n", encoding="utf-8")
        self.fixture._git(path, "add", "wrong.txt")
        self.fixture._git(path, "commit", "-qm", "wrong prerequisite")
        self.assert_failed_without_moving_first(self.fixture.run("--apply"), "wrong prerequisite HEAD")

    def test_missing_target_when_preflight_runs(self) -> None:
        self.fixture.write_metadata()
        shutil.rmtree(self.fixture.repo_root / self.first.target_repo)
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing target repository", result.stderr)

    def test_check_only_when_successful_does_not_mutate_target(self) -> None:
        self.fixture.write_metadata()
        before = self.fixture._git(self.fixture.repo_root / self.first.target_repo, "status", "--porcelain=v1", "--branch")
        worktrees_before = self.fixture._git(self.fixture.repo_root / self.first.target_repo, "worktree", "list", "--porcelain")
        result = self.fixture.run()
        after = self.fixture._git(self.fixture.repo_root / self.first.target_repo, "status", "--porcelain=v1", "--branch")
        worktrees_after = self.fixture._git(self.fixture.repo_root / self.first.target_repo, "worktree", "list", "--porcelain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(after, before)
        self.assertEqual(worktrees_after, worktrees_before)

    def test_double_apply_when_target_is_promoted(self) -> None:
        self.fixture.write_metadata()
        applied = self.fixture.run("--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        result = self.fixture.run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already promoted", result.stderr)

    def test_apply_when_profile_is_valid_matches_promoted_trees(self) -> None:
        second = self.fixture.add_series("beta", 2)
        self.fixture.write_metadata()
        result = self.fixture.run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.tree(self.first), self.first.head_tree_sha)
        self.assertEqual(self.fixture.tree(second), second.head_tree_sha)

    def test_bad_profile_and_malformed_metadata_when_boundary_is_parsed(self) -> None:
        self.fixture.write_metadata()
        bad_profile = self.fixture.run(profile="other")
        self.assertNotEqual(bad_profile.returncode, 0)
        self.assertIn("unknown profile", bad_profile.stderr)
        (self.fixture.patch_root / "series.json").write_text("{not json}\n", encoding="utf-8")
        malformed = self.fixture.run()
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("invalid metadata JSON", malformed.stderr)


if __name__ == "__main__":
    unittest.main()
