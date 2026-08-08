from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_composition_overlay import INFINITI_PATHS, PATCH_ROOT, _composition, _run

ROOT = Path(__file__).resolve().parents[1]
OLD_OID = "8346080be9a5b9c3aac83eb76f9cc36186c9f072"
NEW_OID = "07e63b2852bb599574f8331c189996312836c89f"
MANIFEST_URL = "https://github.com/1vivy/1vivy-rom_local_manifest.git"
MANIFEST_PATH = "local_manifests/1vivy-features-v3-display-baseline.xml"
INSTALL_AS = "1vivy-features-v3-display-baseline.xml"


def _write_cli_fixture(
    tmp_path: Path,
    *,
    declared_oid: str = NEW_OID,
    installed_oid: str = OLD_OID,
    mode: str = "published-head",
    dirty_overlay: bool = False,
) -> tuple[Path, Path, Path, Path]:
    tree = _composition(tmp_path, INFINITI_PATHS)
    manifests = tree / ".repo" / "manifests"
    manifests.mkdir()
    _run(manifests, ["git", "init", "-q"])
    _run(manifests, ["git", "config", "user.name", "Fixture"])
    _run(manifests, ["git", "config", "user.email", "fixture@example.invalid"])
    (manifests / "default.xml").write_text("<manifest/>\n", encoding="utf-8")
    _run(manifests, ["git", "add", "."])
    _run(manifests, ["git", "commit", "-q", "-m", "base"])
    base_oid = _run(manifests, ["git", "rev-parse", "HEAD"])

    installed = tree / ".repo" / "local_manifests" / INSTALL_AS
    installed.parent.mkdir()
    installed.write_text("<manifest/>\n", encoding="utf-8")
    receipt_dir = tree / ".repo" / "rom" / "manifest-provenance"
    receipt_dir.mkdir(parents=True)
    receipt = {
        "url": MANIFEST_URL,
        "declared_revision": installed_oid,
        "resolved_oid": installed_oid,
        "path": MANIFEST_PATH,
        "install_as": INSTALL_AS,
        "blob_oid": subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=installed.read_bytes(),
            capture_output=True,
            check=True,
        ).stdout.decode().strip(),
        "sha256": hashlib.sha256(installed.read_bytes()).hexdigest(),
    }
    (receipt_dir / f"{INSTALL_AS}.json").write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )

    tooling = tmp_path / "tooling"
    shutil.copytree(ROOT, tooling / "rom")
    shutil.copytree(PATCH_ROOT, tooling / "build-patches")
    sentinel = tmp_path / "composition-called"
    composition = tooling / "build-patches" / "build_composition.py"
    with composition.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n_original_compose = compose\n"
            "def compose(*args, **kwargs):\n"
            "    from pathlib import Path as _Path\n"
            "    import os as _os\n"
            "    _Path(_os.environ['ROM_COMPOSE_SENTINEL']).write_text('called\\n')\n"
            "    return _original_compose(*args, **kwargs)\n"
        )
    containerfile = tmp_path / "Containerfile"
    containerfile.write_text(
        "FROM ubuntu@sha256:" + "1" * 64 + "\n", encoding="utf-8"
    )
    config = {
        "toolchain": {
            "image": "fixture.invalid/image:latest",
            "containerfile": str(containerfile),
            "ccache": str(tmp_path / "ccache"),
            "overlay_manifest": str(tooling / "build-patches" / "manifest.yml"),
            "ledger": str(tmp_path / "ledger"),
        },
        "lanes": {
            "stale": {
                "tree": str(tree),
                "base_manifest": {"url": "unused", "revision": base_oid},
                "governed_manifest": {
                    "url": MANIFEST_URL,
                    "revision": declared_oid,
                    "path": MANIFEST_PATH,
                    "install_as": INSTALL_AS,
                },
                "governed_org": "orgx",
                "lunch": "fixture-userdebug",
                "status_file": str(tmp_path / "STATUS"),
                "artifacts": str(tmp_path / "artifacts"),
                "reconciliation": {"mode": mode},
            }
        },
    }
    lanes = tmp_path / "lanes.json"
    lanes.write_text(json.dumps(config), encoding="utf-8")
    if dirty_overlay:
        overlay = tree / "external/skia" / "Android.bp"
        overlay.write_text(overlay.read_text(encoding="utf-8") + "overlay\n")
    return tooling / "rom" / "rom", lanes, sentinel, tree


def test_status_refuses_declared_new_installed_old_before_composition(
    tmp_path: Path,
) -> None:
    # Given a coherent OLD checkout whose lane declaration already names NEW.
    cli, lanes, sentinel, _ = _write_cli_fixture(tmp_path)
    env = os.environ | {
        "ROM_LANES": str(lanes),
        "ROM_COMPOSE_SENTINEL": str(sentinel),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
    }

    # When status runs through the actual CLI boundary.
    completed = subprocess.run(
        [sys.executable, str(cli), "stale", "status", "--no-lfs-check"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # Then provenance drift refuses before composition or identity evaluation.
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, (
        f"false green rc=0 output={output!r} composition_called={sentinel.exists()}"
    )
    assert "REFUSED" in output
    assert OLD_OID in output
    assert NEW_OID in output
    assert "PINNED" not in output
    assert "EXPERIMENTAL" not in output
    assert "computed" not in output
    assert "expected" not in output
    assert not sentinel.exists()


@pytest.mark.parametrize("mode", ["published-head", "replayed-profile"])
def test_matching_receipt_preserves_pinned_status(tmp_path: Path, mode: str) -> None:
    # Given matching exact declared and installed identity in either lane mode.
    cli, lanes, sentinel, _ = _write_cli_fixture(
        tmp_path, declared_oid=OLD_OID, mode=mode
    )
    env = os.environ | {
        "ROM_LANES": str(lanes),
        "ROM_COMPOSE_SENTINEL": str(sentinel),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
    }

    # When status runs through the real CLI.
    completed = subprocess.run(
        [sys.executable, str(cli), "stale", "status", "--no-lfs-check"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # Then the existing clean-tree classification remains PINNED.
    assert completed.returncode == 0
    assert "state       PINNED" in completed.stdout
    assert sentinel.is_file()


def test_matching_receipt_preserves_overlay_experimental_status(
    tmp_path: Path,
) -> None:
    # Given matching manifest identity plus one declared overlay-bearing dirty tree.
    cli, lanes, sentinel, _ = _write_cli_fixture(
        tmp_path, declared_oid=OLD_OID, dirty_overlay=True
    )
    env = os.environ | {
        "ROM_LANES": str(lanes),
        "ROM_COMPOSE_SENTINEL": str(sentinel),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
    }

    # When status evaluates the matching tree.
    completed = subprocess.run(
        [sys.executable, str(cli), "stale", "status", "--no-lfs-check"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # Then the pre-existing overlay-only classification remains EXPERIMENTAL.
    assert completed.returncode == 0
    assert "state       EXPERIMENTAL" in completed.stdout
    assert sentinel.is_file()


def test_build_refuses_manifest_mismatch_before_composition(tmp_path: Path) -> None:
    # Given the same declared-NEW and installed-OLD coherent fixture.
    cli, lanes, sentinel, _ = _write_cli_fixture(tmp_path)
    env = os.environ | {
        "ROM_LANES": str(lanes),
        "ROM_COMPOSE_SENTINEL": str(sentinel),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
    }

    # When a build is requested through the actual CLI.
    completed = subprocess.run(
        [sys.executable, str(cli), "stale", "build", "bacon"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # Then build refuses semantically before identity and cannot be overridden.
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "REFUSED" in output
    assert OLD_OID in output
    assert NEW_OID in output
    assert "computed" not in output
    assert "expected" not in output
    assert not sentinel.exists()


def test_scoped_sync_preserves_installed_xml_and_receipt(tmp_path: Path) -> None:
    # Given a mismatched tree and a fixture repo command that accepts one scoped sync.
    cli, lanes, sentinel, tree = _write_cli_fixture(tmp_path)
    repo = tmp_path / "bin" / "repo"
    repo.write_text(
        repo.read_text(encoding="utf-8").replace(
            "  *) exit 64 ;;",
            "  'sync -c --no-clone-bundle -j8 external/skia') exit 0 ;;\n"
            "  *) exit 64 ;;",
        ),
        encoding="utf-8",
    )
    installed = tree / ".repo" / "local_manifests" / INSTALL_AS
    receipt = tree / ".repo" / "rom" / "manifest-provenance" / f"{INSTALL_AS}.json"
    before = (installed.read_bytes(), receipt.read_bytes())
    env = os.environ | {
        "ROM_LANES": str(lanes),
        "ROM_COMPOSE_SENTINEL": str(sentinel),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
    }

    # When scoped sync runs through the actual CLI repair surface.
    completed = subprocess.run(
        [sys.executable, str(cli), "stale", "sync", "external/skia"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # Then it remains callable without rewriting either governed file.
    assert completed.returncode == 0
    assert (installed.read_bytes(), receipt.read_bytes()) == before
    assert not sentinel.exists()
