from __future__ import annotations

import os
import shlex
import stat
from pathlib import Path
from typing import Final

FORBIDDEN_MARKERS: Final = (
    b"GIT binary patch",
    b"Binary files ",
    b"old mode ",
    b"new mode ",
    b"new file mode ",
    b"deleted file mode ",
    b"similarity index ",
    b"rename from ",
    b"rename to ",
    b"copy from ",
    b"copy to ",
    b"Subproject commit ",
)


class PatchPayloadError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


def _read_no_follow(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PatchPayloadError(f"patch file does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PatchPayloadError(f"patch file is a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PatchPayloadError(f"cannot open patch file safely: {path}: {exc}") from exc
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise PatchPayloadError(f"patch file is not regular: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_verified_patch(path: Path, expected_target: str) -> bytes:
    payload = _read_no_follow(path)
    if b"\0" in payload or any(marker in payload for marker in FORBIDDEN_MARKERS):
        raise PatchPayloadError(f"patch must contain one text-only file edit: {path.name}")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise PatchPayloadError(f"patch is not valid UTF-8 text: {path.name}") from exc
    diff_targets: list[str] = []
    old_targets: list[str] = []
    new_targets: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            fields = shlex.split(line)
            if len(fields) != 4 or not fields[2].startswith("a/") or not fields[3].startswith("b/"):
                raise PatchPayloadError(f"unsupported diff header in {path.name}")
            if fields[2][2:] != fields[3][2:]:
                raise PatchPayloadError(f"rename/copy diff is forbidden in {path.name}")
            diff_targets.append(fields[2][2:])
        elif line.startswith("--- "):
            old_targets.append(line[4:])
        elif line.startswith("+++ "):
            new_targets.append(line[4:])
    if diff_targets != [expected_target] or old_targets != [f"a/{expected_target}"] or new_targets != [f"b/{expected_target}"]:
        raise PatchPayloadError(
            f"patch must edit exactly target_path {expected_target!r}: diff={diff_targets} old={old_targets} new={new_targets}"
        )
    return payload
