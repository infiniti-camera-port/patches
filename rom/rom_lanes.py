"""Lane declarations and the project set an identity is folded over."""

from __future__ import annotations

import json
import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_FETCH_BASE = "https://github.com/"


class LaneError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Project:
    path: str
    name: str
    revision: str
    remote_url: str
    governed: bool


@dataclass(frozen=True, slots=True)
class Lane:
    name: str
    tree: Path
    base_manifest: dict
    governed_manifest: dict
    lunch: str
    status_file: Path
    artifacts: Path
    governed_org: str
    reconciliation: dict


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_config() -> dict:  # noqa: DICT_OK - legacy JSON schema shared by lane helpers
    # ROM_LANES exists so the gate can be drilled against a scratch fixture.
    # A gate whose failure path has never been observed is not a gate.
    override = os.environ.get("ROM_LANES")
    path = Path(override) if override else Path(__file__).resolve().parent / "lanes.json"
    with path.open() as fh:
        return json.load(fh)


def load_lane(name: str) -> tuple[Lane, dict]:
    config = load_config()
    declared = config["lanes"]
    if name not in declared:
        raise LaneError(
            f"unknown lane {name!r}; declared lanes are {', '.join(sorted(declared))}"
        )
    row = declared[name]
    lane = Lane(
        name=name,
        tree=Path(row["tree"]),
        base_manifest=row["base_manifest"],
        governed_manifest=row["governed_manifest"],
        lunch=row["lunch"],
        status_file=Path(row["status_file"]),
        artifacts=Path(row["artifacts"]),
        governed_org=row["governed_org"],
        reconciliation=row.get("reconciliation", {"mode": "published-head"}),
    )
    toolchain = dict(config["toolchain"])
    toolchain["monitoring"] = config.get("monitoring", {})
    return lane, toolchain


def overlay_targets(toolchain: dict) -> list[str]:
    """Build-patch target repos, which are build-tree paths.

    Five of these are outside the governed set entirely - build/soong,
    external/skia, external/dng_sdk, external/google-highway and
    packages/apps/Settings. They still belong in the fold: an overlay changes
    that project's tree, and the plan requires overlay-induced drift to be
    visible rather than invisible.
    """
    declared = Path(toolchain["overlay_manifest"])
    path = declared if declared.is_absolute() else _repo_root() / declared
    with path.open() as fh:
        manifest = yaml.safe_load(fh)
    return sorted({entry["target_repo"] for entry in manifest["patches"]})


def _fetch_bases(root: ET.Element) -> dict[str, str]:
    bases = {}
    for remote in root.findall("remote"):
        name = remote.get("name")
        fetch = remote.get("fetch") or DEFAULT_FETCH_BASE
        if name:
            bases[name] = fetch
    return bases


def manifest_projects(manifest_xml: str, governed_org: str) -> list[Project]:
    """Every project the lane's local manifest declares.

    All of them are folded, because their content is in the build either way.
    Only those under governed_org are GOVERNED: those are the repositories this
    programme publishes, so they are the ones whose HEAD is checked against the
    remote and the ones whose drift can refuse a build. The rest - kernel
    prebuilts, LineageOS and OnePlus-SM8850-Development trees - are third party.
    Their remotes are declared by the base manifest rather than here, so a URL
    guessed for them would be wrong as often as right, and refusing a build on a
    third-party branch move is not this gate's business.

    `remove-project` elements are ignored: they retire an upstream project so a
    governed one can take its path, and carry no revision of ours.
    """
    root = ET.fromstring(manifest_xml)
    bases = _fetch_bases(root)
    prefix = governed_org if governed_org.endswith("/") else f"{governed_org}/"
    projects = []
    for element in root.findall("project"):
        path = element.get("path")
        name = element.get("name")
        revision = element.get("revision")
        if not (path and name and revision):
            continue
        base = bases.get(element.get("remote", ""), DEFAULT_FETCH_BASE)
        if not base.endswith("/"):
            base += "/"
        projects.append(
            Project(
                path=path,
                name=name,
                revision=revision,
                remote_url=f"{base}{name}.git",
                governed=name.startswith(prefix),
            )
        )
    return projects


def read_installed_manifest(lane: Lane) -> str:
    """The governed manifest as the TREE actually has it, not as published.

    Read from the tree so that a manifest swapped underneath us is visible as
    drift instead of being masked by re-reading the published copy.
    """
    installed = (
        lane.tree
        / ".repo"
        / "local_manifests"
        / lane.governed_manifest["install_as"]
    )
    if not installed.is_file():
        raise LaneError(
            f"lane {lane.name}: no local manifest at {installed}; the tree has "
            "not been synced through `rom sync`"
        )
    return installed.read_text()


def fold_set(lane: Lane, toolchain: dict) -> list[Project]:
    """Governed projects plus overlay targets, deduplicated by path."""
    declared = manifest_projects(read_installed_manifest(lane), lane.governed_org)
    projects = {p.path: p for p in declared}
    for path in overlay_targets(toolchain):
        if path not in projects:
            projects[path] = Project(
                path=path, name=path, revision="", remote_url="", governed=False
            )
    return [projects[path] for path in sorted(projects)]


def base_manifest_oid(lane: Lane) -> str:
    """The crDroid manifest commit the tree is actually standing on."""
    manifests = lane.tree / ".repo" / "manifests"
    if not manifests.is_dir():
        raise LaneError(f"lane {lane.name}: {manifests} is absent; tree not initialised")
    done = subprocess.run(
        ["git", "-C", str(manifests), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise LaneError(f"lane {lane.name}: cannot resolve base manifest HEAD")
    return done.stdout.strip()
