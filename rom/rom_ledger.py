"""Every attempt leaves a row a fresh agent can reconstruct the tree from.

The preimage stored here is not an independent lock that could disagree with
the id - it is the id's DEFINITION. Recomputing the fold over it reproduces the
id or the record is wrong, and that is checkable without a network or a tree.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import rom_identity as identity

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


COLUMNS = [
    "id", "utc", "lane", "goal", "class", "outcome",
    "first_error", "artifact_sha256", "artifact_dest",
    "run_id", "started_utc", "finished_utc", "seconds", "phase_seconds",
]


@dataclass(frozen=True, slots=True)
class Paths:
    root: Path

    @property
    def attempts(self) -> Path:
        return self.root / "attempts.tsv"

    @property
    def preimages(self) -> Path:
        return self.root / "preimages"

    @property
    def manifests(self) -> Path:
        return self.root / "manifests"


def _clean(value: str) -> str:
    """TSV rows are one line: a stray tab or newline would forge a column."""
    return " ".join(str(value).split()) or "-"


def append_row(paths: Paths, **fields) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    if not paths.attempts.exists():
        paths.attempts.write_text("\t".join(COLUMNS) + "\n")
    else:
        lines = paths.attempts.read_text().splitlines()
        headings = lines[0].split("\t") if lines else []
        if headings != COLUMNS:
            migrated = ["\t".join(COLUMNS)]
            for line in lines[1:]:
                values = dict(zip(headings, line.split("\t")))
                migrated.append("\t".join(_clean(values.get(column, "-")) for column in COLUMNS))
            paths.attempts.write_text("\n".join(migrated) + "\n")
    row = "\t".join(_clean(fields.get(column, "-")) for column in COLUMNS)
    with paths.attempts.open("a") as fh:
        fh.write(row + "\n")


def read_rows(paths: Paths) -> list[dict[str, str]]:
    if not paths.attempts.exists():
        return []
    lines = paths.attempts.read_text().splitlines()
    if not lines:
        return []
    headings = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parsed = dict(zip(headings, line.split("\t")))
        rows.append({column: parsed.get(column, "-") for column in COLUMNS})
    return rows


def _manifest_snapshot(tree: Path) -> tuple[str, bytes] | tuple[None, None]:
    """`repo manifest -r` pins every project, including the ~1170 we do not fold.

    Stored gzipped and addressed by content so ids sharing a manifest share one
    copy, which keeps an append-only record in git from growing by half a
    megabyte per attempt.
    """
    try:
        done = subprocess.run(
            ["repo", "manifest", "-r"], cwd=tree, capture_output=True, text=True
        )
    except (FileNotFoundError, NotADirectoryError):
        # The snapshot is a convenience for reconstructing the ~1170 projects
        # outside the fold. The id's own preimage does not depend on it, so a
        # missing `repo` must not cost us the row - losing the record of an
        # attempt is worse than losing the wider snapshot of it.
        return None, None
    if done.returncode != 0 or not done.stdout.strip():
        return None, None
    raw = done.stdout.encode()
    return hashlib.sha256(raw).hexdigest(), gzip.compress(raw)


def store_preimage(paths: Paths, lane, verdict_id: str, states, base_oid: str,
                   toolchain_digest: str) -> Path:
    """Write the id's inputs, once per distinct id."""
    target = paths.preimages / f"{verdict_id}.json"
    if target.exists():
        return target
    paths.preimages.mkdir(parents=True, exist_ok=True)
    paths.manifests.mkdir(parents=True, exist_ok=True)

    digest, blob = _manifest_snapshot(lane.tree)
    if digest and blob:
        snapshot = paths.manifests / f"{digest}.xml.gz"
        if not snapshot.exists():
            snapshot.write_bytes(blob)

    target.write_text(json.dumps({
        "id": verdict_id,
        "recorded": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lane": lane.name,
        "lunch": lane.lunch,
        "base_manifest_oid": base_oid,
        "toolchain_digest": toolchain_digest,
        "manifest_snapshot_sha256": digest,
        "projects": [
            {"path": s.project.path, "effective_tree": s.effective_tree,
             "head": s.head, "dirty": s.dirty}
            for s in states
        ],
    }, indent=2, sort_keys=True) + "\n")
    return target


def recompute(preimage_path: Path) -> str:
    """Fold the stored preimage back into an id, touching no tree and no network."""
    data = json.loads(preimage_path.read_text())
    return identity.fold(
        [(p["path"], p["effective_tree"]) for p in data["projects"]],
        data["base_manifest_oid"],
        data["toolchain_digest"],
        data["lunch"],
    )


def reconstruct(paths: Paths, wanted: str) -> tuple[bool, str]:
    target = paths.preimages / f"{wanted}.json"
    if not target.exists():
        return False, f"no preimage stored for {wanted}"
    got = recompute(target)
    if got != wanted:
        return False, f"preimage folds to {got}, not {wanted}"
    return True, f"preimage reproduces {wanted} exactly"
