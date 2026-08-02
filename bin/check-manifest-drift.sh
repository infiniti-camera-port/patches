#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: check-manifest-drift.sh [TREE_ROOT]

Refuse a build when a Repo checkout differs from its synced manifest pins.
TREE_ROOT defaults to $ANDROID_BUILD_ROOT, then /srv/android/cleanroom.
Declared build-patches/ target files are the only allowed worktree changes.
EOF
}

if (($# > 1)); then
  usage >&2
  fail "expected at most one tree root"
fi
if (($# == 1)); then
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      usage >&2
      fail "unknown option: $1"
      ;;
  esac
fi

tree_root="${1:-${ANDROID_BUILD_ROOT:-/srv/android/cleanroom}}"
script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
patches_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
build_patches_dir="$patches_root/build-patches"
overlay_manifest="$build_patches_dir/manifest.yml"

[[ -d "$tree_root" ]] || fail "build tree root does not exist: $tree_root"
[[ -d "$tree_root/.repo" ]] || fail "Repo metadata is absent: $tree_root/.repo"
[[ -f "$overlay_manifest" ]] || fail "build-patches manifest is absent: $overlay_manifest"
command -v git >/dev/null || fail "git is required"
command -v python3 >/dev/null || fail "python3 is required"
command -v repo >/dev/null || fail "repo is required"

scratch="$(mktemp -d "${TMPDIR:-/tmp}/manifest-drift.XXXXXX")"
cleanup() {
  rm -rf -- "$scratch"
}
trap cleanup EXIT

overlay_rows="$scratch/overlay-targets"
if ! PYTHONPATH="$build_patches_dir${PYTHONPATH:+:$PYTHONPATH}" \
  python3 - "$overlay_manifest" >"$overlay_rows" <<'PY'
import sys
from pathlib import Path

from build_patch_manifest import ManifestError, parse_manifest

try:
    entries = parse_manifest(Path(sys.argv[1]))
except (ManifestError, OSError) as error:
    print(f"cannot parse build-patches manifest: {error}", file=sys.stderr)
    raise SystemExit(1) from error

for entry in entries:
    sys.stdout.buffer.write(entry.target_repo.encode("utf-8") + b"\0")
    sys.stdout.buffer.write(entry.target_path.encode("utf-8") + b"\0")
PY
then
  fail "cannot derive declared overlay targets from $overlay_manifest"
fi

declare -A overlay_targets=()
while IFS= read -r -d '' overlay_repo && IFS= read -r -d '' overlay_path; do
  overlay_targets["$overlay_repo/$overlay_path"]=1
done <"$overlay_rows"
((${#overlay_targets[@]} > 0)) || fail "build-patches manifest declared no target paths"

projects="$scratch/projects"
if ! (
  cd -- "$tree_root"
  repo forall -j1 -c \
    'printf "%s\0%s\0%s\0%s\0%s\0" "$REPO_COUNT" "$REPO_PATH" "$REPO_PROJECT" "$REPO_LREV" "$REPO_RREV"'
) >"$projects"; then
  fail "repo forall could not enumerate the synced manifest"
fi
[[ -s "$projects" ]] || fail "the synced manifest contains no projects"

drift_count=0
project_count=0
expected_project_count=""

report_drift() {
  printf '%s\n' "$*" >&2
  ((drift_count += 1))
}

report_commit_log() {
  local project_name="$1"
  local project_path="$2"
  local commit_log="$3"
  local commit

  while IFS= read -r commit; do
    [[ -n "$commit" ]] || continue
    report_drift "[LOCAL COMMIT] project=$project_name path=$project_path commit=$commit"
  done <<<"$commit_log"
}

check_dirty_path() {
  local project_name="$1"
  local project_path="$2"
  local status="$3"
  local dirty_path="$4"
  local key="$project_path/$dirty_path"

  if [[ "$status" == " M" && -n "${overlay_targets[$key]+declared}" ]]; then
    return
  fi
  printf -v status_quoted '%q' "$status"
  printf -v path_quoted '%q' "$dirty_path"
  report_drift \
    "[WORKTREE DRIFT] project=$project_name path=$project_path file=$path_quoted status=$status_quoted"
}

while IFS= read -r -d '' repo_count && \
  IFS= read -r -d '' project_path && \
  IFS= read -r -d '' project_name && \
  IFS= read -r -d '' manifest_ref && \
  IFS= read -r -d '' manifest_revision; do
  ((project_count += 1))
  if [[ -z "$expected_project_count" ]]; then
    expected_project_count="$repo_count"
  elif [[ "$repo_count" != "$expected_project_count" ]]; then
    report_drift \
      "[MANIFEST INVENTORY] project=$project_name path=$project_path inconsistent project count: $repo_count"
  fi

  case "$project_path" in
    "" | /* | .. | ../* | */../* | */..)
      report_drift "[MANIFEST INVENTORY] project=$project_name unsafe path=$project_path"
      continue
      ;;
  esac

  project_dir="$tree_root/$project_path"
  if [[ ! -d "$project_dir" ]]; then
    report_drift "[MISSING PROJECT] project=$project_name path=$project_path"
    continue
  fi
  if [[ "$(git -C "$project_dir" rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ]]; then
    report_drift "[NOT A GIT WORKTREE] project=$project_name path=$project_path"
    continue
  fi
  if [[ -z "$manifest_ref" ]]; then
    report_drift \
      "[MANIFEST PIN] project=$project_name path=$project_path revision=$manifest_revision has no local ref"
    continue
  fi
  if ! manifest_commit="$(git -C "$project_dir" rev-parse --verify -q "${manifest_ref}^{commit}")"; then
    report_drift \
      "[MANIFEST PIN] project=$project_name path=$project_path cannot resolve revision=$manifest_revision local_ref=$manifest_ref"
    continue
  fi
  if ! head_commit="$(git -C "$project_dir" rev-parse --verify -q 'HEAD^{commit}')"; then
    report_drift "[INVALID HEAD] project=$project_name path=$project_path"
    continue
  fi
  if [[ "$head_commit" != "$manifest_commit" ]]; then
    report_drift \
      "[HEAD DRIFT] project=$project_name path=$project_path revision=$manifest_revision expected=$manifest_commit actual=$head_commit"
  fi

  if git -C "$project_dir" rev-parse --verify -q '@{u}^{commit}' >/dev/null; then
    if ! local_commits="$(git -C "$project_dir" log --format='%H %s' '@{u}..HEAD')"; then
      report_drift "[UPSTREAM CHECK] project=$project_name path=$project_path cannot compare HEAD with @{u}"
    elif [[ -n "$local_commits" ]]; then
      report_commit_log "$project_name" "$project_path" "$local_commits"
    fi
  elif [[ "$head_commit" != "$manifest_commit" ]]; then
    if git -C "$project_dir" merge-base --is-ancestor "$manifest_commit" "$head_commit"; then
      if ! local_commits="$(git -C "$project_dir" log --format='%H %s' "$manifest_commit..$head_commit")"; then
        report_drift \
          "[LOCAL COMMIT CHECK] project=$project_name path=$project_path cannot compare HEAD with manifest pin"
      elif [[ -n "$local_commits" ]]; then
        report_commit_log "$project_name" "$project_path" "$local_commits"
      fi
    else
      report_drift \
        "[LOCAL COMMIT CHECK] project=$project_name path=$project_path HEAD is not descended from manifest pin"
    fi
  fi

  status_rows="$scratch/status-$project_count"
  if ! git -C "$project_dir" status --porcelain=v1 -z --untracked-files=all >"$status_rows"; then
    report_drift "[STATUS CHECK] project=$project_name path=$project_path git status failed"
    continue
  fi
  while IFS= read -r -d '' status_row; do
    status="${status_row:0:2}"
    dirty_path="${status_row:3}"
    check_dirty_path "$project_name" "$project_path" "$status" "$dirty_path"
    if [[ "${status:0:1}" == "R" || "${status:0:1}" == "C" || \
      "${status:1:1}" == "R" || "${status:1:1}" == "C" ]]; then
      if IFS= read -r -d '' source_path; then
        check_dirty_path "$project_name" "$project_path" "$status" "$source_path"
      else
        report_drift "[STATUS CHECK] project=$project_name path=$project_path truncated rename record"
      fi
    fi
  done <"$status_rows"
done <"$projects"

if [[ -z "$expected_project_count" || "$expected_project_count" != "$project_count" ]]; then
  report_drift \
    "[MANIFEST INVENTORY] expected=${expected_project_count:-unknown} enumerated=$project_count"
fi

if ((drift_count > 0)); then
  fail "manifest drift check failed with $drift_count finding(s); no changes were made"
fi

printf 'manifest drift check: PASS (%d projects; %d declared overlay targets)\n' \
  "$project_count" "${#overlay_targets[@]}"
