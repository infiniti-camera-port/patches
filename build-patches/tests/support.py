from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping

OVERLAY_DIR = Path(__file__).resolve().parents[1]

SOONG_FILES = {
    "scripts/check_boot_jars/package_allowed_list.txt": """vendor\\.oplus\\..*
com\\.oneplus\\..*
vendor\\.oneplus\\..*

# QC adds
com.qualcomm.qti
com.quicinc.tcmiface
""",
    "scripts/gen_build_prop.py": """TEST_KEY_DIR = \"build/make/target/product/security\"

def get_build_variant(product_config):
  if product_config[\"Eng\"]:
    return \"eng\"
  else:
    return \"user\"

""",
}

DEVICE_FILES = {
    "lineage_infiniti.mk": """$(call inherit-product, device/oneplus/infiniti/device.mk)
# Inherit some common Lineage stuff.
$(call inherit-product, vendor/lineage/config/common_full_phone.mk)

PRODUCT_NAME := lineage_infiniti
PRODUCT_DEVICE := infiniti
PRODUCT_MANUFACTURER := OnePlus
"""
}


def run_command(arguments: list[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=None if env is None else dict(env),
        capture_output=True,
        text=True,
        check=False,
    )


def copy_overlay(destination: Path) -> Path:
    return Path(
        shutil.copytree(
            OVERLAY_DIR,
            destination,
            symlinks=True,
            ignore=shutil.ignore_patterns("tests", "__pycache__"),
        )
    )


def initialize_repo(repo: Path, files: Mapping[str, str]) -> None:
    for relative_path, content in files.items():
        target = repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    result = run_command(["git", "init", "-q"], cwd=repo)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


def create_repo_root(root: Path) -> Path:
    soong = root / "build/soong"
    device = root / "device/oneplus/infiniti"
    initialize_repo(soong, SOONG_FILES)
    initialize_repo(device, DEVICE_FILES)
    return root


def run_overlay(
    overlay: Path,
    repo_root: Path,
    *,
    apply: bool = False,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [sys.executable, str(overlay / "apply-build-patches.py"), "--repo-root", str(repo_root)]
    if not apply:
        arguments.append("--check-only")
    process_environment = os.environ.copy()
    if environment is not None:
        process_environment.update(environment)
    return run_command(arguments, cwd=overlay, env=process_environment)


def output_of(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def replace_manifest(overlay: Path, old: str, new: str) -> None:
    manifest = overlay / "manifest.yml"
    content = manifest.read_text(encoding="utf-8")
    if old not in content:
        raise AssertionError(f"manifest fixture does not contain {old!r}")
    manifest.write_text(content.replace(old, new, 1), encoding="utf-8")


def added_payload_lines(patch_file: Path) -> list[str]:
    return [
        line[1:]
        for line in patch_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip() and not line[1:].lstrip().startswith("#")
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
