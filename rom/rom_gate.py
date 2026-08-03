"""One axis, two divergence classes.

The identity always reflects the tree, so actual == expected means the tree is
what the published world says and it builds. When they differ, WHAT diverged
decides the response, and both classes are visible in the same per-project fold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import json

import yaml

import rom_identity as identity
from rom_lanes import Lane, Project, base_manifest_oid

PINNED = "PINNED"
EXPERIMENTAL = "EXPERIMENTAL"
REFUSED = "REFUSED"


@dataclass
class Drift:
    path: str
    local: str
    published: str


@dataclass
class Verdict:
    state: str
    actual_id: str
    expected_id: str | None
    drift: list[Drift] = field(default_factory=list)
    dirty_paths: list[str] = field(default_factory=list)
    overlays: list[str] = field(default_factory=list)
    offline_reason: str | None = None
    states: list = field(default_factory=list)
    base_oid: str = ""
    toolchain_digest: str = ""

    @property
    def builds(self) -> bool:
        return self.state in (PINNED, EXPERIMENTAL)


def _overlays_for(repo_root: Path, overlay_manifest: str, paths: set[str]) -> list[str]:
    """Overlay names whose target project currently carries uncommitted content.

    Derived from which target projects are dirty, not from verifying each patch
    applied cleanly - that is `apply-build-patches.py`'s job and it owns its own
    preconditions. Named accordingly so the ledger row is not read as a stronger
    claim than it is.
    """
    declared = Path(overlay_manifest)
    resolved = declared if declared.is_absolute() else repo_root / declared
    with resolved.open() as fh:
        manifest = yaml.safe_load(fh)
    return sorted(
        entry["name"]
        for entry in manifest["patches"]
        if entry["target_repo"] in paths
    )


def replayed_trees(lane: Lane) -> dict[str, str]:
    """Promoted TREE per project for a lane that replays a patch profile.

    `apply-patches.py` re-authors every commit it replays, so a correct replay
    produces HEADs that can never equal the promoted commits - comparing them
    reports a healthy tree as broken, which is why series.json records
    head_tree_sha alongside head_sha. Empty for a lane that syncs directly.
    """
    mode = lane.reconciliation.get("mode", "published-head")
    if mode != "replayed-profile":
        return {}
    series_path = Path(lane.reconciliation["series"])
    with series_path.open() as fh:
        series = json.load(fh)
    return {row["target_repo"]: row["head_tree_sha"] for row in series["series"]}


def evaluate(
    lane: Lane,
    toolchain: dict,
    projects: list[Project],
    repo_root: Path,
    require_expected: bool = True,
) -> Verdict:
    states = identity.read_states(lane, projects)
    digest = identity.toolchain_digest(toolchain)
    base_oid = base_manifest_oid(lane)
    replayed = replayed_trees(lane)

    actual_id = identity.fold(
        [(s.project.path, s.effective_tree) for s in states],
        base_oid,
        digest,
        lane.lunch,
    )
    dirty_paths = [s.project.path for s in states if s.dirty]
    overlays = _overlays_for(repo_root, toolchain["overlay_manifest"], set(dirty_paths))

    # Expected trees come from the PUBLISHED commit, never from local HEAD.
    # Deriving them locally is cheaper and looks equivalent, but it makes the
    # expected id follow the tree wherever it goes, so it can never disagree -
    # which collapses it onto the actual id and lets an operator satisfy the
    # override by pasting back the value the tool just printed. Observed.
    expected_entries = []
    drift: list[Drift] = []
    unresolved = False
    try:
        for state in states:
            project = state.project
            repo = lane.tree / project.path
            if not project.governed:
                expected_entries.append((project.path, identity.resolve(repo, "HEAD^{tree}")))
                continue
            if project.path in replayed:
                want = replayed[project.path]
                if state.effective_tree != want and not state.dirty:
                    drift.append(Drift(project.path, state.effective_tree, want))
                expected_entries.append((project.path, want))
                continue
            published = identity.published_head(project.remote_url, project.revision)
            if state.head != published:
                drift.append(Drift(project.path, state.head, published))
            try:
                expected_entries.append(
                    (project.path, identity.resolve(repo, f"{published}^{{tree}}"))
                )
            except identity.IdentityError:
                # The published commit is not in this clone yet, so its tree
                # cannot be named without fetching. The drift row already
                # carries both SHAs, which is what reconciliation needs.
                unresolved = True
    except identity.OfflineError as exc:
        if require_expected:
            raise
        return Verdict(
            state="UNKNOWN",
            actual_id=actual_id,
            expected_id=None,
            dirty_paths=dirty_paths,
            overlays=overlays,
            offline_reason=str(exc),
            states=states,
            base_oid=base_oid,
            toolchain_digest=digest,
        )

    expected_id = (
        None if unresolved else identity.fold(expected_entries, base_oid, digest, lane.lunch)
    )

    if drift:
        state = REFUSED
    elif actual_id != expected_id:
        state = EXPERIMENTAL
    else:
        state = PINNED

    return Verdict(
        state=state,
        actual_id=actual_id,
        expected_id=expected_id,
        drift=drift,
        dirty_paths=dirty_paths,
        overlays=overlays,
        states=states,
        base_oid=base_oid,
        toolchain_digest=digest,
    )


def render(verdict: Verdict, lane: Lane) -> str:
    lines = [
        f"lane        {lane.name}",
        f"tree        {lane.tree}",
        f"lunch       {lane.lunch}",
        f"state       {verdict.state}",
        f"computed    {verdict.actual_id}",
    ]
    if verdict.expected_id is None:
        lines.append("expected    <not computed>")
        lines.append(
            f"            cannot reach the published world: {verdict.offline_reason}"
        )
        lines.append(
            "            the computed id above is still correct; only the "
            "comparison is unavailable."
        )
    else:
        lines.append(f"expected    {verdict.expected_id}")
    if verdict.drift:
        lines.append("")
        lines.append("governed projects standing off their published revision:")
        for row in verdict.drift:
            lines.append(f"  {row.path}")
            lines.append(f"      local     {row.local}")
            lines.append(f"      published {row.published}")
        lines.append("")
        lines.append("reconcile, do not override: `rom %s sync`" % lane.name)
    if verdict.dirty_paths:
        lines.append("")
        lines.append("projects carrying uncommitted content:")
        for path in verdict.dirty_paths:
            lines.append(f"  {path}")
        if verdict.overlays:
            lines.append(f"overlays declared for those projects: {', '.join(verdict.overlays)}")
    return "\n".join(lines)
