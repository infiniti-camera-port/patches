#!/usr/bin/env bash
# The promoted manifest already contains the published patch/ profile. Applying
# or checking it here would double-apply that profile against the promoted tree.
set -euo pipefail

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: build-infiniti.sh [TREE_ROOT]

Build Infiniti from an already-synced promoted manifest checkout.
TREE_ROOT defaults to $ANDROID_BUILD_ROOT, then /srv/android/cleanroom.
BUILD_LOG_DIR may override the default TREE_ROOT/out/build-logs directory.
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
script_dir="$(unset CDPATH; cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
patches_root="$(unset CDPATH; cd -- "$script_dir/.." && pwd)"
drift_check="$script_dir/check-manifest-drift.sh"
overlay_applier="$patches_root/build-patches/apply-build-patches.py"
envsetup="$tree_root/build/envsetup.sh"

[[ -d "$tree_root" ]] || fail "build tree root does not exist: $tree_root"
[[ -f "$envsetup" ]] || fail "Android envsetup is absent: $envsetup"
[[ -x "$drift_check" ]] || fail "manifest drift check is absent or not executable: $drift_check"
[[ -f "$overlay_applier" ]] || fail "build-patches applier is absent: $overlay_applier"
command -v python3 >/dev/null || fail "python3 is required"
command -v sha256sum >/dev/null || fail "sha256sum is required"
command -v tee >/dev/null || fail "tee is required"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_dir="${BUILD_LOG_DIR:-$tree_root/out/build-logs}"
mkdir -p -- "$log_dir"
log_file="$log_dir/build-infiniti-$timestamp.log"
exec > >(tee "$log_file") 2>&1

printf 'build tree: %s\n' "$tree_root"
printf 'build log: %s\n' "$log_file"

"$drift_check" "$tree_root"
python3 "$overlay_applier" --repo-root "$tree_root"

cd -- "$tree_root"
# shellcheck source=/dev/null
source build/envsetup.sh
lunch lineage_infiniti-bp4a-userdebug
m nothing
WITH_SU=true mka bacon

[[ -n "${OUT:-}" ]] || fail "lunch did not set OUT"
case "$OUT" in
  /*) out_dir="$OUT" ;;
  *) out_dir="$tree_root/$OUT" ;;
esac
[[ -d "$out_dir" ]] || fail "product output directory does not exist: $out_dir"

shopt -s nullglob
artifacts=("$out_dir"/crDroidAndroid-*.zip)
shopt -u nullglob
((${#artifacts[@]} > 0)) || fail "no crDroidAndroid ZIP artifact found in $out_dir"

artifact="${artifacts[0]}"
for candidate in "${artifacts[@]:1}"; do
  if [[ "$candidate" -nt "$artifact" ]]; then
    artifact="$candidate"
  fi
done
artifact_sha256="$(sha256sum "$artifact")"
artifact_sha256="${artifact_sha256%% *}"

printf 'artifact: %s\n' "$artifact"
printf 'sha256: %s\n' "$artifact_sha256"
printf 'log: %s\n' "$log_file"
