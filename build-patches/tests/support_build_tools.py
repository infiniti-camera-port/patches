from __future__ import annotations

from pathlib import Path


def populate_build_tools(checkout: Path) -> None:
    for platform in ("darwin-x86", "linux-arm64", "linux-x86"):
        for tool in ("date", "tar"):
            link = checkout / "path" / platform / tool
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(f"../../{platform}/bin/toybox")
