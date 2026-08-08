from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from build_composition import CompositionError, CompositionReceipt, compose

PATCH_ROOT = Path(__file__).resolve().parents[2] / "build-patches"
INFINITI_PATHS = (
    "build/soong",
    "device/oneplus/infiniti",
    "external/dng_sdk",
    "external/google-highway",
    "external/skia",
    "packages/apps/Settings",
    "prebuilts/build-tools",
)
FULL_PATHS = INFINITI_PATHS + (
    "device/oneplus/fairlady",
    "device/oneplus/macan",
    "device/oneplus/macanc",
)
TARGETS = {
    "build/soong": (
        "scripts/check_boot_jars/package_allowed_list.txt",
        "scripts/gen_build_prop.py",
    ),
    "device/oneplus/infiniti": ("lineage_infiniti.mk",),
    "device/oneplus/fairlady": ("lineage_fairlady.mk",),
    "device/oneplus/macan": ("lineage_macan.mk",),
    "device/oneplus/macanc": ("lineage_macanc.mk",),
    "external/dng_sdk": ("Android.bp",),
    "external/google-highway": ("Android.bp",),
    "external/skia": ("Android.bp",),
    "packages/apps/Settings": (
        "src/com/android/settings/dashboard/CategoryManager.java",
    ),
    "prebuilts/build-tools": (),
}


def _run(root: Path, arguments: list[str]) -> str:
    done = subprocess.run(
        arguments, cwd=root, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _composition(tmp_path: Path, paths: tuple[str, ...]) -> Path:
    root = tmp_path / "tree"
    project_root = root / ".repo" / "projects"
    project_root.mkdir(parents=True)
    revisions: dict[str, str] = {}
    for path in paths:
        checkout = root / path
        checkout.mkdir(parents=True)
        for target in TARGETS.get(path, ("fixture.txt",)):
            file_path = checkout / target
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(f"fixture for {path}/{target}\n", encoding="utf-8")
        if path == "prebuilts/build-tools":
            for platform in ("darwin-x86", "linux-arm64", "linux-x86"):
                for tool in ("date", "tar"):
                    link = checkout / "path" / platform / tool
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(f"../../{platform}/bin/toybox")
        _run(checkout, ["git", "init", "-q"])
        _run(checkout, ["git", "config", "user.name", "Fixture"])
        _run(checkout, ["git", "config", "user.email", "fixture@example.invalid"])
        _run(checkout, ["git", "add", "."])
        _run(checkout, ["git", "commit", "-q", "-m", "fixture"])
        revisions[path] = _run(checkout, ["git", "rev-parse", "HEAD"])
        git_dir = checkout / ".git"
        project_git = project_root / f"{path}.git"
        project_git.parent.mkdir(parents=True, exist_ok=True)
        git_dir.rename(project_git)
        relative = os.path.relpath(project_git, checkout)
        git_dir.write_text(f"gitdir: {relative}\n", encoding="utf-8")
    ordered = sorted(paths)
    (root / ".repo" / "project.list").write_text(
        "\n".join(ordered) + "\n", encoding="utf-8"
    )
    project_xml = "\n".join(
        f'  <project name="{path}" path="{path}" revision="{revisions[path]}" />'
        for path in ordered
    )
    (root / ".repo" / "resolved.xml").write_text(
        f"<manifest>\n{project_xml}\n</manifest>\n", encoding="utf-8"
    )
    binary = tmp_path / "bin"
    binary.mkdir()
    repo = binary / "repo"
    repo.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  'list -p') cat .repo/project.list ;;\n"
        "  'manifest -r') cat .repo/resolved.xml ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    repo.chmod(0o755)
    return root


def _receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paths: tuple[str, ...]
) -> tuple[Path, CompositionReceipt]:
    root = _composition(tmp_path, paths)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    return root, compose(root, PATCH_ROOT, PATCH_ROOT / "manifest.yml")


def test_infiniti_and_full_compositions_select_canonical_subsets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, infiniti = _receipt(tmp_path / "infiniti", monkeypatch, INFINITI_PATHS)
    _, full = _receipt(tmp_path / "full", monkeypatch, FULL_PATHS)
    assert len(infiniti.projects) == 7
    assert len(infiniti.registry) == 11
    assert len(infiniti.selection) == 8
    assert len({item.entry.target_repo for item in infiniti.selection}) == 7
    assert len(full.projects) == 10
    assert len(full.registry) == 11
    assert len(full.selection) == 11
    assert len({item.entry.target_repo for item in full.selection}) == 10


def test_features_v2_fold_changes_only_for_declared_build_tools_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the full features-v2 composition before its declared link deletion.
    root, before = _receipt(tmp_path, monkeypatch, FULL_PATHS)
    operation = next(
        item for item in before.selection if item.entry.name == "remove-broken-build-tools"
    )

    # When exactly that one logical payload is applied.
    subprocess.run(
        ["git", "-C", str(root / operation.entry.target_repo), "apply", "-"],
        input=operation.patch_bytes,
        capture_output=True,
        check=True,
    )
    after = compose(root, PATCH_ROOT, PATCH_ROOT / "manifest.yml")

    # Then only the named target repository contributes a changed effective tree.
    before_trees = {project.path: project.effective_tree for project in before.projects}
    after_trees = {project.path: project.effective_tree for project in after.projects}
    assert {path for path in before_trees if before_trees[path] != after_trees[path]} == {
        "prebuilts/build-tools"
    }
    assert [project.path for project in after.projects if project.dirty] == [
        "prebuilts/build-tools"
    ]


def test_cross_surface_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _composition(tmp_path, INFINITI_PATHS)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    with (root / ".repo" / "project.list").open("a", encoding="utf-8") as stream:
        stream.write("device/oneplus/macan\n")
    with pytest.raises(CompositionError, match="project path sets differ"):
        compose(root, PATCH_ROOT, PATCH_ROOT / "manifest.yml")


@pytest.mark.parametrize("stale_path", ["system/core", "build/soong"])
def test_clean_stale_head_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_path: str
) -> None:
    paths = (
        INFINITI_PATHS
        if stale_path in INFINITI_PATHS
        else INFINITI_PATHS + (stale_path,)
    )
    root = _composition(tmp_path, paths)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    checkout = root / stale_path
    (checkout / "stale").write_text("stale\n", encoding="utf-8")
    _run(checkout, ["git", "add", "."])
    _run(checkout, ["git", "commit", "-q", "-m", "stale"])
    with pytest.raises(CompositionError, match="differs from resolved"):
        compose(root, PATCH_ROOT, PATCH_ROOT / "manifest.yml")


@pytest.mark.parametrize(
    "fault",
    [
        "missing-checkout",
        "missing-project",
        "non-oid",
        "duplicate",
        "missing-surface-path",
        "malformed-manifest",
    ],
)
def test_invalid_composition_surface_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    root = _composition(tmp_path, INFINITI_PATHS)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    if fault == "missing-checkout":
        (root / "build/soong/.git").rename(root / "build/soong/.git-missing")
    elif fault == "missing-project":
        (root / ".repo/projects/build/soong.git").rename(
            root / ".repo/projects/build/soong.git-missing"
        )
    elif fault == "non-oid":
        manifest = root / ".repo/resolved.xml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                _run(root / "build/soong", ["git", "rev-parse", "HEAD"]),
                "refs/heads/main",
            ),
            encoding="utf-8",
        )
    elif fault == "duplicate":
        project_list = root / ".repo/project.list"
        project_list.write_text(
            project_list.read_text(encoding="utf-8") + "build/soong\n", encoding="utf-8"
        )
    elif fault == "missing-surface-path":
        project_list = root / ".repo/project.list"
        project_list.write_text(
            project_list.read_text(encoding="utf-8").replace("build/soong\n", ""),
            encoding="utf-8",
        )
    else:
        (root / ".repo/resolved.xml").write_text("<manifest>", encoding="utf-8")
    with pytest.raises(CompositionError):
        compose(root, PATCH_ROOT, PATCH_ROOT / "manifest.yml")


@pytest.mark.parametrize("fault", ["hash", "order", "orphan", "missing-payload"])
def test_invalid_excluded_registry_row_fails_before_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    root = _composition(tmp_path, INFINITI_PATHS)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    overlay = tmp_path / "overlay"
    shutil.copytree(PATCH_ROOT, overlay)
    manifest = overlay / "manifest.yml"
    if fault == "hash":
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "ec3d03250cbd733c27b4752986dae89188aa8bd09c71ec347db3d7e74d9e8f18",
                "0" * 64,
            ),
            encoding="utf-8",
        )
    elif fault == "order":
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "    apply_order: 10", "    apply_order: 11"
            ),
            encoding="utf-8",
        )
    elif fault == "orphan":
        (overlay / "excluded-orphan.patch").write_text("orphan\n", encoding="utf-8")
    else:
        (overlay / "device-not-debuggable-fairlady.patch").unlink()
    with pytest.raises(CompositionError):
        compose(root, overlay, manifest)
