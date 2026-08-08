"""One axis, two divergence classes.

The identity always reflects the tree, so actual == expected means the tree is
what the published world says and it builds. When they differ, WHAT diverged
decides the response, and both classes are visible in the same per-project fold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import rom_identity as identity
from build_composition import CompositionReceipt
from rom_lanes import Lane, base_manifest_oid

PINNED = "PINNED"
EXPERIMENTAL = "EXPERIMENTAL"
REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class Drift:
    path: str
    local: str
    published: str


@dataclass(frozen=True, slots=True)
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
    receipt: CompositionReceipt,
    _repo_root: Path,
    require_expected: bool = True,
) -> Verdict:
    digest = identity.toolchain_digest(toolchain)
    base_oid = base_manifest_oid(lane)

    actual_id = identity.fold(
        [(project.path, project.effective_tree) for project in receipt.projects],
        base_oid,
        digest,
        lane.lunch,
    )
    dirty_paths = [project.path for project in receipt.projects if project.dirty]
    dirty_set = set(dirty_paths)
    overlays = sorted(
        item.entry.name for item in receipt.selection if item.entry.target_repo in dirty_set
    )

    # Expected trees come from the PUBLISHED commit, never from local HEAD.
    # Deriving them locally is cheaper and looks equivalent, but it makes the
    # expected id follow the tree wherever it goes, so it can never disagree -
    # which collapses it onto the actual id and lets an operator satisfy the
    # override by pasting back the value the tool just printed. Observed.
    expected_id = identity.fold(
        [(project.path, project.expected_tree) for project in receipt.projects],
        base_oid,
        digest,
        lane.lunch,
    )
    drift: list[Drift] = []
    if actual_id != expected_id:
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
        states=list(receipt.projects),
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
        lines.append(f"reconcile, do not override: `rom {lane.name} sync`")
    if verdict.dirty_paths:
        lines.append("")
        lines.append("projects carrying uncommitted content:")
        for path in verdict.dirty_paths:
            lines.append(f"  {path}")
        if verdict.overlays:
            lines.append(f"overlays declared for those projects: {', '.join(verdict.overlays)}")
    return "\n".join(lines)
