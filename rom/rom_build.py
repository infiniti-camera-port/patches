"""Executing a build: the container invocation, and the row it leaves."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import rom_gate as gate
import rom_ledger as ledger


def first_error(log: Path) -> str:
    if not log.is_file():
        return "-"
    with log.open(errors="replace") as fh:
        for line in fh:
            if line.startswith(("FAILED:", "ninja: error", "error:")):
                return line.strip()[:200]
    return "-"


def artifact(out_dir: Path, goal: str) -> tuple[str, str]:
    """The thing this goal produced, hashed so a row names a specific artefact."""
    product = out_dir / "target" / "product"
    patterns = ["*.zip"] if goal in ("bacon", "otapackage") else [f"{goal}.img", "*.img"]
    for pattern in patterns:
        found = sorted(product.glob(f"*/{pattern}"), key=lambda p: p.stat().st_mtime)
        if found:
            newest = found[-1]
            digest = hashlib.sha256()
            with newest.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            return str(newest), digest.hexdigest()
    return "-", "-"


def publish(lane, build_id: str, source: str) -> str:
    """Home a successful artefact under its id.

    The id is the whole point of the path: an artefact filed under it can be
    traced back to the exact trees that produced it, which a filename carrying
    only a date cannot do. Two builds a minute apart from different trees would
    otherwise be indistinguishable on disk.
    """
    if source == "-":
        return "-"
    origin = Path(source)
    if not origin.is_file():
        return "-"
    destination = lane.artifacts / build_id / origin.name
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            destination.hardlink_to(origin)
    except OSError as exc:
        # Publication is convenience; the ledger row already names the artefact
        # where the build left it. Failing the build over a destination we may
        # not own would be a worse trade.
        print(f"warning: could not publish under {destination}: {exc}")
        return "-"
    return str(destination)


def mirror_args(toolchain) -> list[str]:
    """`repo init --reference` against a local mirror, when one is mounted."""
    declared = toolchain.get("mirror")
    if not declared:
        return []
    mirror = Path(declared)
    if not mirror.is_dir():
        print(f"warning: no local mirror at {mirror}; syncing from the network")
        return []
    return ["--reference", str(mirror)]


def ledger_paths(toolchain, repo_root) -> ledger.Paths:
    declared = Path(toolchain.get("ledger", "rom/ledger"))
    return ledger.Paths(declared if declared.is_absolute() else repo_root / declared)


def record(paths, lane, verdict, goal: str, outcome: str, cls: str = gate.REFUSED,
            first_error: str = "-", artifact_sha256: str = "-",
            artifact_dest: str = "-") -> None:
    ledger.store_preimage(paths, lane, verdict.actual_id, verdict.states,
                          verdict.base_oid, verdict.toolchain_digest)
    ledger.append_row(
        paths, id=verdict.actual_id, utc=ledger.utc_now(), lane=lane.name, goal=goal,
        **{"class": cls}, outcome=outcome, first_error=first_error,
        artifact_sha256=artifact_sha256, artifact_dest=artifact_dest,
    )


def podman_argv(lane, toolchain, out_dir: Path | None, goal: str) -> list[str]:
    tree = str(lane.tree)
    # out/ stays IN the tree unless asked otherwise. Bind-mounting a separate
    # directory over it hides whatever ninja already built and turns every
    # invocation into a full rebuild, which would make `rom build bootimage`
    # cost hours instead of minutes.
    out_mount = ["-v", f"{out_dir}:{tree}/out:z"] if out_dir else []
    return [
        "podman", "run", "--rm",
        "--userns=keep-id",
        # podman caps PIDs at 2048 by default; AOSP's concurrent metalava JVMs
        # exhaust that and die with pthread_create EAGAIN. A cgroup ceiling, not
        # a capability - CapPrm/CapEff stay empty with it set.
        "--pids-limit=-1",
        "-v", f"{tree}:{tree}:z",
        *out_mount,
        "-v", f"{toolchain['ccache']}:/ccache:z",
        "-w", tree,
        "-e", "HOME=/tmp/buildhome",
        "-e", "USE_CCACHE=1",
        "-e", "CCACHE_DIR=/ccache",
        "-e", "CCACHE_EXEC=/usr/bin/ccache",
        "-e", "CCACHE_MAXSIZE=150G",
        "-e", f"LUNCH={lane.lunch}",
        toolchain["image"],
        "bash", "-c",
        "set -o pipefail; mkdir -p /tmp/buildhome; "
        f"cd {tree} || exit 90; "
        "source build/envsetup.sh || exit 90; "
        'lunch "$LUNCH" || exit 90; '
        "m nothing || exit 91; "
        f"WITH_SU=true mka {goal}",
    ]
