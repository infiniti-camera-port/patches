# Historical LineageOS 23.2 build fixes

> [!WARNING]
> This is a retired reproduction lane. These four patches are not the current
> crDroid source profile and not the guarded three-patch crDroid build overlay in
> [`../build-patches/`](../build-patches). Do not apply them to the promoted
> manifest unless you are intentionally reconstructing the old LineageOS
> 23.2/OEM-A16-frameworks composition.

These patches address base, codec, display, and generated-output issues from the
historical composition. They are kept separate so non-camera fixes never enter
the portable camera ranges.

| patch | target repo | historical purpose | SHA-256 |
| --- | --- | --- | --- |
| `0001-system_core-camera.h-add-CAMERA_FRAME_DATA_FD.patch` | `system/core` | add the CAF HAL1 constant expected by the old OEM `frameworks/av` | `90648387c66fb692ead56178882dc0d793431135fdb4e26bb72a7fff906e61c5` |
| `0002-frameworks_av-ACodec-drop-incomplete-AC4Tbl.patch` | `frameworks/av` | remove an incomplete legacy OMX AC-4 table block | `5ec20793966d60020e3ce1b2c8986bf3af33972c38db81883635dbc7257fce1d` |
| `0003-device_sm8850-common-extract-files-trim-pixelworks-vendor-suffix.patch` | `device/oneplus/sm8850-common` | stop generating dangling Pixelworks `_vendor` dependencies | `f07f3206c01283bc0524bea1ac58c56f75262ae3c943218f64682aa416b4977f` |
| `0004-vendor_sm8850-common-Android.bp-bare-pixelworks-deps.patch` | `vendor/oneplus/sm8850-common` | repair the corresponding historical generated `Android.bp` output | `26f1a5d1d3eaba2848f0fd94d45953541ea6dc65f9346109544d1fd9c6864b1a` |

## Reproduction procedure

Start from a clean historical Lineage checkout. Set `PATCHES_ROOT` to this
repository's absolute path so `git -C` does not reinterpret a relative patch
path inside the target repo:

```sh
set -eu
PATCHES_ROOT="$(cd /path/to/patches && pwd)"
repo start lineage-legacy-build-fixes system/core frameworks/av device/oneplus/sm8850-common vendor/oneplus/sm8850-common

git -C system/core apply --check \
  "$PATCHES_ROOT/los-fix-build-patches/0001-system_core-camera.h-add-CAMERA_FRAME_DATA_FD.patch"
git -C frameworks/av apply --check \
  "$PATCHES_ROOT/los-fix-build-patches/0002-frameworks_av-ACodec-drop-incomplete-AC4Tbl.patch"
git -C device/oneplus/sm8850-common apply --check \
  "$PATCHES_ROOT/los-fix-build-patches/0003-device_sm8850-common-extract-files-trim-pixelworks-vendor-suffix.patch"
git -C vendor/oneplus/sm8850-common apply --check \
  "$PATCHES_ROOT/los-fix-build-patches/0004-vendor_sm8850-common-Android.bp-bare-pixelworks-deps.patch"

git -C system/core apply \
  "$PATCHES_ROOT/los-fix-build-patches/0001-system_core-camera.h-add-CAMERA_FRAME_DATA_FD.patch"
git -C frameworks/av apply \
  "$PATCHES_ROOT/los-fix-build-patches/0002-frameworks_av-ACodec-drop-incomplete-AC4Tbl.patch"
git -C device/oneplus/sm8850-common apply \
  "$PATCHES_ROOT/los-fix-build-patches/0003-device_sm8850-common-extract-files-trim-pixelworks-vendor-suffix.patch"
git -C vendor/oneplus/sm8850-common apply \
  "$PATCHES_ROOT/los-fix-build-patches/0004-vendor_sm8850-common-Android.bp-bare-pixelworks-deps.patch"
```

The four checks are a fail-fast gate, but this retired manual lane is not a
cross-repository transaction. Use only disposable topic branches. If an apply
fails unexpectedly, stop and inspect or recreate those branches before trying
again; do not continue with a partially applied set.

Patch 0003 changes `extract-files.py`; regenerate the proprietary output instead
of treating 0004 as long-term source truth. Any old
`prebuilts/misc/protobuf_vendorcompat` restore was a sync repair, not a fifth
patch, and is outside this repository.
