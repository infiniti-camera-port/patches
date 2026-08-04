"""LFS pointer stubs: finding them, and filling them in.

A synced tree can be exactly what the published world says and still be
unbuildable, because an LFS pointer and its content are the SAME git blob - the
pointer text is what was committed, the content never was. So `HEAD^{tree}` is
identical either way and the build identity cannot see hydration state at all.
Nothing else checks it, which is how a 134-byte webview.apk reached aapt2.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

POINTER_MAGIC = "^version https://git-lfs"
# An LFS pointer is a few short lines; real content that small is not worth
# hydrating anyway. Sizing the sweep this way is what makes it survivable on a
# 1208-project tree - grepping every file instead takes minutes.
POINTER_MAX_BYTES = 400
SWEEP = (
    r"find . -type f -size -{size}c -not -path '*/.git/*' -not -path './out/*' -print0 "
    r"2>/dev/null | xargs -0 -P 16 -n 300 grep -l '{magic}' 2>/dev/null | sed 's|^\./||'"
)


# Keyed on oid, not path: the oid IS the content, so an allowlisted entry cannot
# silently come to mean a different file. A path could be reused; a hash cannot.
KNOWN_UNHYDRATABLE = {
    "faa09385f32d8b3e4518c14384b52ef94f6430da5a838307a2e0af2940b89608": (
        "android_bp test fixture (external/rust/android-crates-io). AOSP committed "
        "the pointer but ships no .gitattributes for it and serves no LFS backend, "
        "so git-lfs does not even recognise the file as LFS - nothing can hydrate "
        "it in-tree. Test-only: the generated Android.bp declares rust_library and "
        "no rust_test, so a ROM build never reads it. Known upstream import bug, "
        "AOSP issue 476172414. The real 1803816-byte blob is available from the "
        "genuine upstream, tardyp/rs-bp, if the crate's tests are ever needed."
    ),
}


def oid_of(tree: Path, relative: str) -> str | None:
    try:
        for line in (tree / relative).read_text(errors="replace").splitlines():
            if line.startswith("oid sha256:"):
                return line.split("sha256:", 1)[1].strip()
    except OSError:
        return None
    return None


def split_known(tree: Path, paths: list[str]) -> tuple[list[str], list[str]]:
    """Separate stubs worth acting on from ones documented as unhydratable."""
    actionable, known = [], []
    for path in paths:
        (known if oid_of(tree, path) in KNOWN_UNHYDRATABLE else actionable).append(path)
    return actionable, known


@dataclass
class Hydration:
    before: list[str] = field(default_factory=list)
    residual: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    pulled: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def healed(self) -> int:
        return len(self.before) - len(self.residual)


def stubs(tree: Path) -> list[str]:
    """Every LFS pointer stub in the tree, enumerated in one sweep.

    Tree-wide by design. Hydrating repo by repo as build failures surface them
    costs hours per miss, because the next stub is not discovered until the next
    build dies on it.
    """
    done = subprocess.run(
        ["bash", "-c", SWEEP.format(size=POINTER_MAX_BYTES, magic=POINTER_MAGIC)],
        cwd=tree, capture_output=True, text=True,
    )
    return [line for line in done.stdout.splitlines() if line.strip()]


def owning_repo(tree: Path, relative: str) -> str | None:
    """The git repository a stub belongs to, by walking up to the nearest .git."""
    current = (tree / relative).parent
    while current != tree and current != current.parent:
        if (current / ".git").exists():
            return str(current.relative_to(tree))
        current = current.parent
    return "." if (tree / ".git").exists() else None


def hydrate(tree: Path, echo=print) -> Hydration:
    """Enumerate, pull per owning repository, then re-enumerate.

    The re-enumeration is the point: `git lfs pull` exiting 0 does not mean the
    stubs are gone. Residual pointers are reported rather than treated as
    failure, because some cannot be hydrated by anyone - AOSP mirrors committed
    pointers whose objects only ever lived at the upstream project, and
    android.googlesource.com serves no LFS backend to ask.
    """
    result = Hydration(before=stubs(tree))
    if not result.before:
        echo("no LFS pointer stubs")
        return result

    repos = sorted({r for r in (owning_repo(tree, s) for s in result.before) if r})
    result.repos = repos
    echo(f"{len(result.before)} pointer stubs across {len(repos)} repositories")

    for repo in repos:
        done = subprocess.run(
            ["git", "-C", str(tree / repo), "lfs", "pull"],
            capture_output=True, text=True,
        )
        if done.returncode == 0:
            result.pulled += 1
        else:
            result.failed.append(repo)
            echo(f"  lfs pull failed in {repo}: {done.stderr.strip()[:120]}")

    result.residual = stubs(tree)
    echo(f"hydrated {result.healed}, residual {len(result.residual)}")
    return result
