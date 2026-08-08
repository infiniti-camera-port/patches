"""Typed declared and installed manifest provenance boundaries."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NewType

CommitOid = NewType("CommitOid", str)
Sha256 = NewType("Sha256", str)

OID_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
RECEIPT_KEYS: Final = frozenset(
    {
        "url",
        "declared_revision",
        "resolved_oid",
        "path",
        "install_as",
        "blob_oid",
        "sha256",
    }
)


@dataclass(frozen=True, slots=True)
class BaseManifestSpec:
    url: str
    revision: str


@dataclass(frozen=True, slots=True)
class ManifestSpec:
    url: str
    revision: str
    path: str
    install_as: str


@dataclass(frozen=True, slots=True)
class DeclaredManifestIdentity:
    url: str
    declared_revision: str
    resolved_oid: CommitOid
    path: str
    install_as: str


@dataclass(frozen=True, slots=True)
class InstalledManifestIdentity:
    url: str
    declared_revision: str
    resolved_oid: CommitOid
    path: str
    install_as: str
    blob_oid: CommitOid
    sha256: Sha256


class ManifestProvenanceError(Exception):
    __slots__ = ("declared_oid", "installed_oid", "reason")

    def __init__(
        self,
        reason: str,
        declared_oid: str = "<unresolved>",
        installed_oid: str = "<missing>",
    ) -> None:
        super().__init__(reason, declared_oid, installed_oid)
        self.reason = reason
        self.declared_oid = declared_oid
        self.installed_oid = installed_oid

    def __str__(self) -> str:
        return (
            f"manifest provenance {self.reason}; "
            f"installed={self.installed_oid} declared={self.declared_oid}"
        )


def parse_manifest_spec(row: Mapping[str, str]) -> ManifestSpec:
    url = row.get("url")
    revision = row.get("revision")
    path = row.get("path")
    install_as = row.get("install_as")
    if not url:
        raise ManifestProvenanceError(reason="declaration URL is malformed")
    if not revision:
        raise ManifestProvenanceError(reason="declaration revision is malformed")
    if not path:
        raise ManifestProvenanceError(reason="declaration path is malformed")
    if not install_as:
        raise ManifestProvenanceError(reason="declaration is malformed")
    if path.startswith("/") or "\\" in path or any(
        part in {"", ".", ".."} for part in path.split("/")
    ):
        raise ManifestProvenanceError(reason="declaration path is unsafe")
    _safe_install_name(install_as)
    return ManifestSpec(url=url, revision=revision, path=path, install_as=install_as)


def parse_base_manifest_spec(row: Mapping[str, str]) -> BaseManifestSpec:
    url = row.get("url")
    revision = row.get("revision")
    if not url or not revision:
        raise ManifestProvenanceError(reason="base declaration is malformed")
    return BaseManifestSpec(url=url, revision=revision)


def _symbolic_ref(revision: str) -> str:
    return revision if revision.startswith("refs/") else f"refs/heads/{revision}"


def resolve_revision(spec: BaseManifestSpec | ManifestSpec) -> CommitOid:
    if OID_PATTERN.fullmatch(spec.revision):
        return CommitOid(spec.revision)
    ref = _symbolic_ref(spec.revision)
    done = subprocess.run(
        ["git", "ls-remote", "--exit-code", spec.url, ref],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = done.stdout.splitlines()
    if done.returncode != 0 or len(rows) != 1:
        raise ManifestProvenanceError(
            reason=f"cannot resolve {spec.revision!r} from {spec.url}"
        )
    oid = rows[0].split("\t", maxsplit=1)[0]
    if not OID_PATTERN.fullmatch(oid):
        raise ManifestProvenanceError(reason="remote returned a non-OID revision")
    return CommitOid(oid)


def resolve_declared(spec: ManifestSpec) -> DeclaredManifestIdentity:
    resolved = resolve_revision(spec)
    return DeclaredManifestIdentity(
        url=spec.url,
        declared_revision=spec.revision,
        resolved_oid=resolved,
        path=spec.path,
        install_as=spec.install_as,
    )


def receipt_path(tree: Path, install_as: str) -> Path:
    name = _safe_install_name(install_as)
    return tree / ".repo" / "rom" / "manifest-provenance" / f"{name}.json"


def installed_xml_path(tree: Path, install_as: str) -> Path:
    return tree / ".repo" / "local_manifests" / _safe_install_name(install_as)


def _safe_install_name(value: str) -> str:
    if value in {".", ".."} or "/" in value or "\\" in value or "\0" in value:
        raise ManifestProvenanceError(reason="install_as is not a safe filename")
    return value


def _git_blob(data: bytes) -> CommitOid:
    header = f"blob {len(data)}\0".encode()
    return CommitOid(hashlib.sha1(header + data, usedforsecurity=False).hexdigest())


def receipt_for(
    declared: DeclaredManifestIdentity, data: bytes
) -> InstalledManifestIdentity:
    return InstalledManifestIdentity(
        url=declared.url,
        declared_revision=declared.declared_revision,
        resolved_oid=declared.resolved_oid,
        path=declared.path,
        install_as=declared.install_as,
        blob_oid=_git_blob(data),
        sha256=Sha256(hashlib.sha256(data).hexdigest()),
    )


def _parse_receipt(path: Path) -> InstalledManifestIdentity:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestProvenanceError(reason=f"receipt is missing or malformed: {path}") from exc
    if not isinstance(raw, dict) or set(raw) != RECEIPT_KEYS:
        raise ManifestProvenanceError(reason="receipt fields are malformed")
    values = tuple(raw.get(key) for key in RECEIPT_KEYS)
    if not all(isinstance(value, str) and value for value in values):
        raise ManifestProvenanceError(reason="receipt values are malformed")
    resolved_oid = raw["resolved_oid"]
    blob_oid = raw["blob_oid"]
    sha256 = raw["sha256"]
    if not OID_PATTERN.fullmatch(resolved_oid) or not OID_PATTERN.fullmatch(blob_oid):
        raise ManifestProvenanceError(reason="receipt contains a non-OID")
    if not SHA256_PATTERN.fullmatch(sha256):
        raise ManifestProvenanceError(reason="receipt contains a malformed SHA-256")
    return InstalledManifestIdentity(
        url=raw["url"],
        declared_revision=raw["declared_revision"],
        resolved_oid=CommitOid(resolved_oid),
        path=raw["path"],
        install_as=raw["install_as"],
        blob_oid=CommitOid(blob_oid),
        sha256=Sha256(sha256),
    )


def load_installed(tree: Path, install_as: str) -> InstalledManifestIdentity:
    installed = _parse_receipt(receipt_path(tree, install_as))
    xml = installed_xml_path(tree, install_as)
    try:
        data = xml.read_bytes()
    except OSError as exc:
        raise ManifestProvenanceError(
            reason=f"installed XML is unreadable: {xml}",
            installed_oid=installed.resolved_oid,
        ) from exc
    if _git_blob(data) != installed.blob_oid or hashlib.sha256(data).hexdigest() != installed.sha256:
        raise ManifestProvenanceError(
            reason="installed XML does not match its receipt",
            installed_oid=installed.resolved_oid,
        )
    return installed


def validate_governed(
    tree: Path, spec: ManifestSpec
) -> tuple[DeclaredManifestIdentity, InstalledManifestIdentity]:
    declared = resolve_declared(spec)
    try:
        installed = load_installed(tree, spec.install_as)
    except ManifestProvenanceError as exc:
        raise ManifestProvenanceError(
            reason=exc.reason,
            declared_oid=declared.resolved_oid,
            installed_oid=exc.installed_oid,
        ) from exc
    declared_tuple = (
        declared.url,
        declared.declared_revision,
        declared.resolved_oid,
        declared.path,
        declared.install_as,
    )
    installed_tuple = (
        installed.url,
        installed.declared_revision,
        installed.resolved_oid,
        installed.path,
        installed.install_as,
    )
    if declared_tuple != installed_tuple:
        raise ManifestProvenanceError(
            reason="declared and installed identities differ",
            declared_oid=declared.resolved_oid,
            installed_oid=installed.resolved_oid,
        )
    return declared, installed


def validate_base(tree: Path, spec: BaseManifestSpec) -> CommitOid:
    declared = resolve_revision(spec)
    done = subprocess.run(
        ["git", "-C", str(tree / ".repo" / "manifests"), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    head = done.stdout.strip()
    if done.returncode != 0 or head != declared:
        raise ManifestProvenanceError(
            reason="base manifest HEAD differs from its declaration",
            declared_oid=declared,
            installed_oid=head or "<missing>",
        )
    return declared
