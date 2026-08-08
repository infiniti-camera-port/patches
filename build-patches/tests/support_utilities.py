from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path


def output_of(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def replace_manifest(overlay: Path, old: str, new: str) -> None:
    manifest = overlay / "manifest.yml"
    content = manifest.read_text(encoding="utf-8")
    if old not in content:
        raise AssertionError(f"manifest fixture does not contain {old!r}")
    manifest.write_text(content.replace(old, new, 1), encoding="utf-8")


def set_git_executable(overlay: Path, executable: Path) -> None:
    runtime = overlay / "build_patch_runtime.py"
    content = runtime.read_text(encoding="utf-8")
    declaration = 'GIT_EXECUTABLE: Final = "/usr/bin/git"'
    if declaration not in content:
        raise AssertionError("overlay fixture does not contain the trusted Git declaration")
    runtime.write_text(
        content.replace(declaration, f'GIT_EXECUTABLE: Final = "{executable}"', 1),
        encoding="utf-8",
    )


def added_payload_lines(patch_file: Path) -> list[str]:
    return [
        line[1:]
        for line in patch_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("+")
        and not line.startswith("+++")
        and line[1:].strip()
        and not line[1:].lstrip().startswith("#")
    ]


def write_git_wrapper(directory: Path, target_root: Path, fault: str) -> Path:
    wrapper = directory / "git"
    real_git = shutil.which("git")
    if real_git is None:
        raise FileNotFoundError("git is not available")
    fail_at = 2 if fault in {"apply", "rollback", "sigint"} else 0
    fail_rollback = 1 if fault == "rollback" else 0
    send_sigint = 1 if fault == "sigint" else 0
    marker = directory / "unsafe-environment"
    count_file = directory / "count"
    resolved_target_root = target_root.resolve()
    wrapper.write_text(
        f"""#!/usr/bin/env bash
set -eu
if test "${{GIT_CONFIG_GLOBAL:-}}" != /dev/null || test -n "${{XDG_CONFIG_HOME:-}}"; then
  touch {shlex.quote(str(marker))}
fi
is_apply=0
is_check=0
is_reverse=0
for argument in "$@"; do
  test "$argument" = "apply" && is_apply=1
  test "$argument" = "--check" && is_check=1
  test "$argument" = "--reverse" && is_reverse=1
done
case "$PWD/" in
  {shlex.quote(str(resolved_target_root))}/*) in_target=1 ;;
  *) in_target=0 ;;
esac
if test "$is_apply" = 1 && test "$is_check" = 0 && test "$in_target" = 1; then
  count=0
  test ! -f {shlex.quote(str(count_file))} || count="$(<{shlex.quote(str(count_file))})"
  count=$((count + 1))
  printf '%s' "$count" >{shlex.quote(str(count_file))}
  if test "$is_reverse" = 1 && test {fail_rollback} = 1; then
    exit 87
  fi
  if test "$count" = {fail_at}; then
    if test {send_sigint} = 1; then
      kill -INT "$PPID"
      exit 130
    fi
    exit 86
  fi
fi
exec {shlex.quote(real_git)} "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper
