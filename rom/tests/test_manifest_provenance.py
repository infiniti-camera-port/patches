from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest
import rom_manifest_install
from rom_manifest_provenance import (
    BaseManifestSpec,
    CommitOid,
    DeclaredManifestIdentity,
    ManifestProvenanceError,
    ManifestSpec,
    installed_xml_path,
    parse_manifest_spec,
    receipt_for,
    receipt_path,
    resolve_declared,
    validate_base,
    validate_governed,
)

OID_A = "a" * 40
OID_B = "b" * 40
URL = "https://example.invalid/manifest.git"
SOURCE_PATH = "local_manifests/test.xml"
INSTALL_AS = "test.xml"
DATA = b"<manifest/>\n"


def _declared(oid: str = OID_A) -> DeclaredManifestIdentity:
    return DeclaredManifestIdentity(
        url=URL,
        declared_revision=oid,
        resolved_oid=CommitOid(oid),
        path=SOURCE_PATH,
        install_as=INSTALL_AS,
    )


def _tree(tmp_path: Path, oid: str = OID_A) -> Path:
    tree = tmp_path / "tree"
    (tree / ".repo").mkdir(parents=True)
    rom_manifest_install.install_staged(tree, _declared(oid), DATA)
    return tree


def _spec(oid: str = OID_A) -> ManifestSpec:
    return ManifestSpec(
        url=URL, revision=oid, path=SOURCE_PATH, install_as=INSTALL_AS
    )


def _write_receipt(tree: Path, row: dict[str, str]) -> None:
    receipt_path(tree, INSTALL_AS).write_text(
        json.dumps(row, sort_keys=True) + "\n", encoding="utf-8"
    )


def _remote(tmp_path: Path) -> tuple[Path, str]:
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=work, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=work,
        check=True,
    )
    source = work / SOURCE_PATH
    source.parent.mkdir()
    source.write_bytes(DATA)
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "manifest"], cwd=work, check=True)
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "push", "-q", str(remote), "HEAD:main"], cwd=work, check=True)
    return remote, oid


def test_matching_exact_receipt_is_accepted(tmp_path: Path) -> None:
    # Given matching declared and installed exact-OID identities.
    tree = _tree(tmp_path)

    # When the provenance boundary validates them.
    declared, installed = validate_governed(tree, _spec())

    # Then both independently parsed identities retain the same immutable OID.
    assert declared.resolved_oid == OID_A
    assert installed.resolved_oid == OID_A


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "https://other.invalid/manifest.git"),
        ("declared_revision", OID_B),
        ("resolved_oid", OID_B),
        ("path", "local_manifests/other.xml"),
        ("install_as", "other.xml"),
    ],
)
def test_wrong_installed_tuple_is_refused(
    tmp_path: Path, field: str, value: str
) -> None:
    # Given valid XML bytes paired with one false receipt identity field.
    tree = _tree(tmp_path)
    row = asdict(receipt_for(_declared(), DATA))
    row[field] = value
    _write_receipt(tree, row)

    # When the declared identity is checked.
    with pytest.raises(ManifestProvenanceError, match="identities differ"):
        validate_governed(tree, _spec())


@pytest.mark.parametrize("payload", [None, "{", "{}"])
def test_missing_or_malformed_receipt_is_refused(
    tmp_path: Path, payload: str | None
) -> None:
    # Given a missing, syntactically broken, or structurally incomplete receipt.
    tree = _tree(tmp_path)
    receipt = receipt_path(tree, INSTALL_AS)
    receipt.unlink()
    if payload is not None:
        receipt.write_text(payload, encoding="utf-8")

    # When provenance is checked, then the boundary fails closed.
    with pytest.raises(ManifestProvenanceError, match="receipt"):
        validate_governed(tree, _spec())


@pytest.mark.parametrize("field", ["blob_oid", "sha256"])
def test_tampered_receipt_digest_is_refused(tmp_path: Path, field: str) -> None:
    # Given an otherwise valid receipt whose XML digest is forged.
    tree = _tree(tmp_path)
    row = asdict(receipt_for(_declared(), DATA))
    row[field] = ("b" * 40) if field == "blob_oid" else ("b" * 64)
    _write_receipt(tree, row)

    # When provenance is checked, then byte identity cannot certify.
    with pytest.raises(ManifestProvenanceError, match="does not match"):
        validate_governed(tree, _spec())


def test_tampered_xml_is_refused(tmp_path: Path) -> None:
    # Given an installed XML changed after its receipt was made durable.
    tree = _tree(tmp_path)
    installed_xml_path(tree, INSTALL_AS).write_bytes(
        b"<manifest><project/></manifest>\n"
    )

    # When provenance is checked, then both recorded digests cease to certify it.
    with pytest.raises(ManifestProvenanceError, match="does not match"):
        validate_governed(tree, _spec())


def test_symbolic_governed_and_base_revisions_resolve_to_remote_head(
    tmp_path: Path,
) -> None:
    # Given a local remote whose main branch and manifest checkout share one head.
    remote, oid = _remote(tmp_path)
    tree = tmp_path / "tree"
    manifests = tree / ".repo" / "manifests"
    manifests.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(remote), str(manifests)],
        check=True,
    )
    spec = ManifestSpec(
        url=str(remote), revision="main", path=SOURCE_PATH, install_as=INSTALL_AS
    )
    rom_manifest_install.install_staged(tree, resolve_declared(spec), DATA)

    # When symbolic governed and base declarations are validated.
    governed, _ = validate_governed(tree, spec)
    base = validate_base(tree, BaseManifestSpec(url=str(remote), revision="main"))

    # Then remote resolution, not a mutable local name, supplies both OIDs.
    assert governed.resolved_oid == oid
    assert base == oid


def test_literal_base_revision_must_equal_manifest_checkout_head(tmp_path: Path) -> None:
    # Given a base checkout whose HEAD differs from the configured literal OID.
    remote, oid = _remote(tmp_path)
    tree = tmp_path / "tree"
    manifests = tree / ".repo" / "manifests"
    manifests.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(remote), str(manifests)],
        check=True,
    )
    assert oid != OID_A

    # When the literal base declaration is validated, then drift is refused.
    with pytest.raises(ManifestProvenanceError, match="base manifest HEAD differs"):
        validate_base(tree, BaseManifestSpec(url=str(remote), revision=OID_A))


@pytest.mark.parametrize(
    "interruption", [OSError("disk"), KeyboardInterrupt(), SystemExit(9)]
)
def test_interruption_between_xml_and_receipt_is_refused_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    # Given a durable OLD pair and a replacement that interrupts before NEW receipt.
    tree = _tree(tmp_path, OID_A)
    original_replace = rom_manifest_install._durable_replace
    calls = 0

    def interrupt_second(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise interruption
        original_replace(source, destination)

    monkeypatch.setattr(rom_manifest_install, "_durable_replace", interrupt_second)

    # When NEW installation is interrupted in the only two-file crash window.
    with pytest.raises(type(interruption)):
        rom_manifest_install.install_staged(tree, _declared(OID_B), b"<manifest><new/></manifest>\n")

    # Then the mixed pair is refused and private staging is cleaned.
    with pytest.raises(ManifestProvenanceError):
        validate_governed(tree, _spec(OID_B))
    assert not list((tree / ".repo").glob("manifest-provenance-*"))


@pytest.mark.parametrize("revision", ["main", "exact"])
def test_remote_stage_installs_exact_commit_path_and_receipt(
    tmp_path: Path, revision: str
) -> None:
    # Given a remote manifest addressed symbolically or by immutable OID.
    remote, oid = _remote(tmp_path)
    tree = tmp_path / "tree"
    (tree / ".repo").mkdir(parents=True)
    configured = oid if revision == "exact" else revision
    spec = ManifestSpec(
        url=str(remote), revision=configured, path=SOURCE_PATH, install_as=INSTALL_AS
    )

    # When whole-sync staging installs the governed pair.
    installed = rom_manifest_install.stage_remote_manifest(tree, spec)

    # Then exact commit:path bytes and both immutable digests are independently valid.
    assert installed.resolved_oid == oid
    assert installed_xml_path(tree, INSTALL_AS).read_bytes() == DATA
    validate_governed(tree, spec)
    assert not list((tree / ".repo").glob("manifest-fetch-*"))


@pytest.mark.parametrize(
    "install_as",
    [
        "/tmp/escape.xml",
        "../escape.xml",
        "nested/escape.xml",
        "nested\\escape.xml",
        ".",
        "..",
    ],
)
def test_unsafe_install_name_is_rejected_before_path_construction(
    tmp_path: Path, install_as: str
) -> None:
    # Given a lane declaration whose install name can escape or alias a directory.
    row = {
        "url": URL,
        "revision": OID_A,
        "path": SOURCE_PATH,
        "install_as": install_as,
    }

    # When the untrusted declaration crosses the typed parse boundary.
    with pytest.raises(ManifestProvenanceError, match="install"):
        parse_manifest_spec(row)

    # Then no destination can be constructed or created outside the fixture tree.
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("parent", ["local_manifests", "rom"])
def test_symlinked_manifest_parent_is_rejected_before_replace(
    tmp_path: Path, parent: str
) -> None:
    # Given an allowed filename but a local-manifest parent redirected outside .repo.
    tree = tmp_path / "tree"
    outside = tmp_path / "outside"
    (tree / ".repo").mkdir(parents=True)
    outside.mkdir()
    (tree / ".repo" / parent).symlink_to(outside, target_is_directory=True)

    # When installation attempts to prepare its destinations.
    with pytest.raises(ManifestProvenanceError, match="symlink"):
        rom_manifest_install.install_staged(tree, _declared(), DATA)

    # Then neither XML nor receipt escaped the governed roots.
    assert not list(outside.iterdir())
