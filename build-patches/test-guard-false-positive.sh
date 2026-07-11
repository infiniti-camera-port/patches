#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/build-patch-guard.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT

repo_root="$scratch/repo"
mkdir -p \
  "$repo_root/build/soong/scripts/check_boot_jars" \
  "$repo_root/build/soong/scripts" \
  "$repo_root/device/oneplus/infiniti" \
  "$repo_root/external/google-highway" \
  "$repo_root/external/skia"

git -C "$repo_root/build/soong" init -q
git -C "$repo_root/device/oneplus/infiniti" init -q
git -C "$repo_root/external/google-highway" init -q
git -C "$repo_root/external/skia" init -q

cat > "$repo_root/build/soong/scripts/check_boot_jars/package_allowed_list.txt" <<'EOF'
com\.oplus\..*
vendor\.oplus\..*
com\.oneplus\..*
vendor\.oneplus\..*

# infiniti camera-port v3: oplus-fwk boot-jar OEM stub namespaces missing from crDroid base.
# MANDATORY: com.oplusx.sysapi.cryptoeng is load-bearing for AIUnit CryptoengNative,
# the cryptoeng HAL client, and FIDO attestation.
com\.oplusx\..*
# CONDITIONAL: activate only after APK-linkage proof shows a live shipped consumer.
# com\.coloros\.deepthinker
# com\.itgsa\.opensdk\.wm

# QC adds
com.qualcomm.qti
EOF

cat > "$repo_root/external/google-highway/Android.bp" <<'EOF'
package {
    default_applicable_licenses: ["external_highway_license"],
}

cc_library {
    name: "libhwy",
    host_supported: true,
    sdk_version: "current",
    stl: "c++_shared",
    export_include_dirs: [
        ".",
    ],
}
EOF

cat > "$repo_root/external/skia/Android.bp" <<'EOF'
rust_bindgen {
    name: "libfontations_ffi_bridge_headers",
}

cc_library_static {
    name: "libskia_skcms",
    host_supported: true,
    sdk_version: "current",
    srcs: [
        "modules/skcms/skcms.cc",
    ],
}
EOF

cat > "$repo_root/build/soong/scripts/gen_build_prop.py" <<'EOF'
TEST_KEY_DIR = "build/make/target/product/security"
def get_build_variant(product_config):
  if product_config["Eng"]:
    return "eng"
  elif product_config["Debuggable"]:
    return "userdebug"
  else:
    return "user"
EOF

cat > "$repo_root/device/oneplus/infiniti/lineage_infiniti.mk" <<'EOF'
$(call inherit-product, device/oneplus/infiniti/device.mk)
# Inherit some common Lineage stuff.
$(call inherit-product, vendor/lineage/config/common_full_phone.mk)

# Keep the userdebug build debuggable so adb root works out of the box.
# crDroid/Lineage set PRODUCT_NOT_DEBUGGABLE_IN_USERDEBUG := true in common.mk,
# forcing ro.debuggable=0 and hiding the rooted-debugging developer toggle.
# add_json_bool treats any non-empty string as true, so ":= false" is still
# truthy. Clear the var after the common inherit so the userdebug build stays
# debuggable.
PRODUCT_NOT_DEBUGGABLE_IN_USERDEBUG :=

PRODUCT_NAME := lineage_infiniti
PRODUCT_DEVICE := infiniti
PRODUCT_MANUFACTURER := OnePlus
EOF

git -C "$repo_root/build/soong" add scripts
git -C "$repo_root/build/soong" -c user.name=guard-test -c user.email=guard-test@example.invalid commit -qm init
git -C "$repo_root/device/oneplus/infiniti" add lineage_infiniti.mk
git -C "$repo_root/device/oneplus/infiniti" -c user.name=guard-test -c user.email=guard-test@example.invalid commit -qm init

report="$scratch/report.txt"
if python3 "$script_dir/apply-build-patches.py" --check-only --repo-root "$repo_root" >"$report" 2>&1; then
  cat "$report"
  echo "expected commented com.coloros.deepthinker to fail the guard" >&2
  exit 1
fi

grep -Eq 'allow-oplus-fwk-boot-jars[[:space:]]+build/soong[[:space:]]+CHECK_FAIL' "$report"
