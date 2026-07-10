#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from profile_model import ProfileError, load_profile
from profile_runner import RunnerError, run_profile


PATCH_ROOT: Final = Path(__file__).resolve().parent


class ParsedArguments(argparse.Namespace):
    profile: str = ""
    repo_root: Path = Path()
    check_only: bool = False
    apply: bool = False


def parse_arguments(arguments: Sequence[str] | None = None) -> ParsedArguments:
    parser = argparse.ArgumentParser(
        description="Validate or apply a complete guarded Infiniti crDroid patch profile.",
    )
    parser.add_argument("--profile", required=True, help="Profile name recorded in patch/series.json.")
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        help="Android source root containing every target and sync-only prerequisite.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-only",
        action="store_true",
        help="Replay every series in disposable detached worktrees without changing targets.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Preflight and replay every series before applying the complete profile.",
    )
    namespace = ParsedArguments()
    parser.parse_args(arguments, namespace=namespace)
    return namespace


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_arguments(arguments)
    try:
        profile = load_profile(PATCH_ROOT)
        if args.profile != profile.name:
            raise ProfileError(issue=f"unknown profile: {args.profile}")
        run_profile(profile, args.repo_root.resolve(), apply=args.apply)
    except (ProfileError, RunnerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    count = profile.patch_count
    noun = "patch" if count == 1 else "patches"
    action = "apply" if args.apply else "check-only"
    print(f"{action} complete: {len(profile.series)} series, {count} {noun}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
