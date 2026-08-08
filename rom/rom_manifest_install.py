"""Crash-safe installation of governed manifest provenance."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

from rom_manifest_provenance import (
    DeclaredManifestIdentity,
    InstalledManifestIdentity,
    ManifestProvenanceError,
    ManifestSpec,
    installed_xml_path,
    load_installed,
    receipt_for,
    receipt_path,
    resolve_declared,
)


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    _fsync_parent(destination)


def _ensure_repo_directory(repo: Path, components: tuple[str, ...]) -> Path:
    if repo.is_symlink() or not repo.is_dir():
        raise ManifestProvenanceError(reason=f"repository metadata root is unsafe: {repo}")
    current = repo
    for component in components:
        current /= component
        if current.is_symlink():
            raise ManifestProvenanceError(reason=f"manifest parent is a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise ManifestProvenanceError(reason=f"manifest parent is not a directory: {current}")
        current.mkdir(exist_ok=True)
        if current.is_symlink():
            raise ManifestProvenanceError(reason=f"manifest parent is a symlink: {current}")
    return current


def install_staged(
    tree: Path, declared: DeclaredManifestIdentity, data: bytes
) -> InstalledManifestIdentity:
    repo = tree / ".repo"
    _ensure_repo_directory(repo, ("local_manifests",))
    _ensure_repo_directory(repo, ("rom", "manifest-provenance"))
    xml = installed_xml_path(tree, declared.install_as)
    receipt = receipt_path(tree, declared.install_as)
    installed = receipt_for(declared, data)
    with tempfile.TemporaryDirectory(
        prefix="manifest-provenance-", dir=tree / ".repo"
    ) as raw_stage:
        stage = Path(raw_stage)
        staged_xml = stage / "manifest.xml"
        staged_receipt = stage / "receipt.json"
        staged_xml.write_bytes(data)
        staged_receipt.write_text(
            json.dumps(asdict(installed), sort_keys=True) + "\n", encoding="utf-8"
        )
        for path in (staged_xml, staged_receipt):
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        if receipt_for(declared, staged_xml.read_bytes()) != installed:
            raise ManifestProvenanceError(reason="staged manifest verification failed")
        _durable_replace(staged_xml, xml)
        _durable_replace(staged_receipt, receipt)
    return load_installed(tree, declared.install_as)


def stage_remote_manifest(
    tree: Path, spec: ManifestSpec
) -> InstalledManifestIdentity:
    declared = resolve_declared(spec)
    with tempfile.TemporaryDirectory(
        prefix="manifest-fetch-", dir=tree / ".repo"
    ) as raw_fetch:
        repository = Path(raw_fetch) / "repository.git"
        subprocess.run(["git", "init", "-q", "--bare", str(repository)], check=True)
        fetched = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "fetch",
                "-q",
                "--depth=1",
                spec.url,
                declared.resolved_oid,
            ],
            capture_output=True,
            check=False,
        )
        if fetched.returncode != 0:
            raise ManifestProvenanceError(
                reason="cannot fetch governed manifest commit",
                declared_oid=declared.resolved_oid,
            )
        shown = subprocess.run(
            ["git", "-C", str(repository), "show", f"FETCH_HEAD:{spec.path}"],
            capture_output=True,
            check=False,
        )
        if shown.returncode != 0:
            raise ManifestProvenanceError(
                reason="cannot read governed manifest commit:path",
                declared_oid=declared.resolved_oid,
            )
        return install_staged(tree, declared, shown.stdout)
