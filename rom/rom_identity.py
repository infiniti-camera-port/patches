"""The build identity: a fold over trees, computed two ways.

The identity is not a representation of the tree, it IS the tree, folded. That
is the whole point: a component that merely describes the tree can drift from
it, and a build ID that describes a tree it no longer matches is exactly the
staleness this exists to prevent.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rom_lanes import Lane, Project

LS_REMOTE_TIMEOUT = 25


class OfflineError(Exception):
    """Raised when the published world cannot be reached to compute expected."""


class IdentityError(Exception):
    pass


@dataclass(frozen=True)
class ProjectState:
    project: Project
    head: str
    effective_tree: str
    dirty: bool


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def resolve(repo: Path, rev: str) -> str:
    """Resolve a rev, refusing the fail-open behaviour of bare rev-parse.

    Bare `git rev-parse <bad>` exits non-zero but still prints its argument to
    stdout, so a caller that ignores the status consumes the argument as if it
    were an OID. `--verify -q` is what makes the failure a failure.
    """
    done = _git(repo, "rev-parse", "--verify", "-q", rev)
    if done.returncode != 0 or not done.stdout.strip():
        raise IdentityError(f"{repo}: cannot resolve {rev}")
    return done.stdout.strip()


def effective_tree(repo: Path) -> tuple[str, bool]:
    """HEAD^{tree} when the worktree is clean, else the worktree written out.

    `git write-tree` serialises the INDEX, not the worktree, so untracked files
    would be invisible to a naive implementation. Staging into a TEMPORARY index
    seeded from HEAD captures modifications and untracked files alike while
    leaving the real index untouched - the tree must never be mutated to be
    measured.

    Because git hashes content, `touch` on an unchanged file cannot move this
    value. That is the property the negative control in the plan checks for.
    """
    status = _git(repo, "status", "--porcelain")
    if status.returncode != 0:
        raise IdentityError(f"{repo}: git status failed: {status.stderr.strip()}")
    if not status.stdout.strip():
        return resolve(repo, "HEAD^{tree}"), False

    with tempfile.TemporaryDirectory() as scratch:
        index = os.path.join(scratch, "index")
        env = dict(os.environ, GIT_INDEX_FILE=index)
        read = subprocess.run(
            ["git", "-C", str(repo), "read-tree", "HEAD"],
            capture_output=True, text=True, env=env,
        )
        if read.returncode != 0:
            raise IdentityError(f"{repo}: read-tree failed: {read.stderr.strip()}")
        add = subprocess.run(
            ["git", "-C", str(repo), "add", "-A", "--", "."],
            capture_output=True, text=True, env=env,
        )
        if add.returncode != 0:
            raise IdentityError(f"{repo}: add -A failed: {add.stderr.strip()}")
        written = subprocess.run(
            ["git", "-C", str(repo), "write-tree"],
            capture_output=True, text=True, env=env,
        )
        if written.returncode != 0:
            raise IdentityError(f"{repo}: write-tree failed: {written.stderr.strip()}")
        return written.stdout.strip(), True


def toolchain_digest(toolchain: dict) -> str:
    """H(base image digest + Containerfile content).

    Deliberately NOT the built image id. Buildah stamps a fresh creation time on
    every `podman build`, so the image id moves when nothing about the toolchain
    did, and it is a local artefact that an amnesiac consumer cannot derive from
    the published world. Both inputs here are version-controlled.
    """
    containerfile = Path(toolchain["containerfile"])
    if not containerfile.is_file():
        raise IdentityError(f"toolchain: {containerfile} is absent")
    text = containerfile.read_text()
    base = ""
    for line in text.splitlines():
        if line.startswith("FROM "):
            base = line[len("FROM "):].strip()
            break
    if not base:
        raise IdentityError(f"toolchain: {containerfile} declares no FROM")
    if "@sha256:" not in base:
        raise IdentityError(
            f"toolchain: base image {base!r} is not digest-pinned; a moving tag "
            "cannot anchor a build identity"
        )
    payload = f"{base}\n{hashlib.sha256(text.encode()).hexdigest()}\n"
    return hashlib.sha256(payload.encode()).hexdigest()


def read_states(lane: Lane, projects: list[Project]) -> list[ProjectState]:
    states = []
    for project in projects:
        repo = lane.tree / project.path
        if not (repo / ".git").exists():
            raise IdentityError(
                f"lane {lane.name}: {project.path} is not present in the tree"
            )
        tree, dirty = effective_tree(repo)
        states.append(
            ProjectState(
                project=project,
                head=resolve(repo, "HEAD"),
                effective_tree=tree,
                dirty=dirty,
            )
        )
    return states


def fold(
    entries: list[tuple[str, str]], base_oid: str, toolchain: str, lunch: str
) -> str:
    body = "".join(f"{path} {tree}\n" for path, tree in sorted(entries))
    payload = f"{body}base-manifest {base_oid}\ntoolchain {toolchain}\nlunch {lunch}\n"
    return hashlib.sha256(payload.encode()).hexdigest()


def published_head(remote_url: str, revision: str) -> str:
    """The published commit for a governed project.

    A 40-character revision is already a pin and needs no lookup. Anything else
    is a branch, and the remote is authoritative for it - never a local
    remote-tracking ref, which a narrow fetch refspec can leave stale in a way
    `fetch --prune` will not correct.
    """
    if len(revision) == 40 and all(c in "0123456789abcdef" for c in revision):
        return revision
    # Manifests carry both bare branch names ("16.0") and fully-qualified refs
    # ("refs/heads/lineage-23.2"). Prefixing unconditionally asks the remote for
    # refs/heads/refs/heads/... which matches nothing - and a query that cannot
    # match is indistinguishable from a branch that does not exist.
    ref = revision if revision.startswith("refs/") else f"refs/heads/{revision}"
    try:
        done = subprocess.run(
            ["git", "ls-remote", remote_url, ref],
            capture_output=True,
            text=True,
            timeout=LS_REMOTE_TIMEOUT,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        )
    except subprocess.TimeoutExpired as exc:
        raise OfflineError(f"{remote_url}: timed out after {LS_REMOTE_TIMEOUT}s") from exc
    if done.returncode != 0:
        raise OfflineError(f"{remote_url}: {done.stderr.strip() or 'ls-remote failed'}")
    if not done.stdout.strip():
        raise IdentityError(f"{remote_url}: no refs/heads/{revision}")
    return done.stdout.split()[0]
