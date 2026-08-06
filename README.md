# infiniti crDroid 16.0 patch packaging

This repository has three deliberately separate lanes:

- `patch/`: guarded downstream portability profile, eight series and 40 source
  patches for `infiniti` only.
- `build-patches/`: ten guarded crDroid build-only overlays.
- `los-fix-build-patches/`: four historical LineageOS-only compile fixes.

The complete promoted build graph is published separately in
[`infiniti-camera-port/local_manifest`](https://github.com/infiniti-camera-port/local_manifest).
Manifest consumers already have the eight source series and must not apply
`patch/` on top. Doing so would double-apply the same commits.

Only `lineage_infiniti-bp4a-userdebug` has been built and runtime-validated.
That validation is a flashed device test of the promoted graph: build r4
(`crDroidAndroid-16.0-20260731-infiniti-v12.11-r4.zip`, sha256
`08eaf7a602deb548efe81b1057e194dbf3c42d6da4394731d1da329493b71fd6`), flashed
with no overlay present, camera mode switching and stills/video exercised.
macan, macanc, and fairlady are included in the split repository graph, but
are not yet built/runtime-validated; they are queued in the approved all-device
build/static-QA pass. No result is inferred yet. The archived monoliths
`vendor_oplus_camera`, `device_oneplus_infiniti-camera`, and
`device_oneplus_sm8850-common-camera` are not active dependencies.

## Downstream porter quick start

The profile requires the exact base commits and prerequisite SHAs in
[`patch/series.json`](patch/series.json). From your Android root, first add and
sync the six split prerequisites below. Then run the detached-compatible
preflight before creating branches:

```sh
PATCHES_ROOT="$(cd /path/to/patches && pwd)"
python3 "$PATCHES_ROOT/patch/apply-patches.py" \
  --profile infiniti-crdroid-16.0 --repo-root "$PWD" --check-only

repo start infiniti-crdroid-16.0 hardware/oplus frameworks/base frameworks/native frameworks/av device/qcom/sepolicy_vndr/sm8850 packages/apps/Sandbox device/oneplus/sm8850-common device/oneplus/infiniti

python3 "$PATCHES_ROOT/patch/apply-patches.py" \
  --profile infiniti-crdroid-16.0 --repo-root "$PWD" --apply
```

`--check-only` replays every complete series in disposable worktrees, supports
detached Repo checkouts, compares final trees with the promoted heads, and does
not move target refs. `--apply` requires clean branch-backed targets and stages
the complete profile before target mutation. On failure it verifies rollback
of invocation-owned changes; an unverifiable rollback is a fatal error that
requires manual recovery. Both modes reject wrong bases, dirty or
already-promoted targets, malformed or incomplete patch inventories, and
mismatched prerequisites. The runner performs no network access.

## Six sync-only prerequisites

These repositories contain split camera source or generated LFS-backed
proprietary output. They are direct sync inputs, never portable patch series.

| build path | repository | ref | required SHA |
| --- | --- | --- | --- |
| `vendor/oneplus/camera-sm8850-common` | `vendor_oneplus_camera-sm8850-common` | `16.0` | `41fd10618f0437e2e57c985b73e42daf3b2b080d` |
| `vendor/oneplus/camera-infiniti` | `vendor_oneplus_camera-infiniti` | `16.0` | `e57bdeb1ae75c7aa1c2a0577f4a1d14d152a3fa4` |
| `proprietary/vendor/oneplus/camera-sm8850-common` | `proprietary_vendor_oneplus_camera-sm8850-common` | `16.0` | `c9b0760407740e8853bbf660bddc3d407efe09d3` |
| `proprietary/vendor/oneplus/camera-infiniti` | `proprietary_vendor_oneplus_camera-infiniti` | `16.0` | `31aa2b78ec6b2cee85d446db176e39441c45c7ba` |
| `vendor/oneplus/sm8850-common` | `proprietary_vendor_oneplus_sm8850-common` | `16.0` | `d072e8b9ee1524332e4da747f9bb47301497682b` |
| `vendor/oneplus/infiniti` | `proprietary_vendor_oneplus_infiniti` | `16.0` | `5a9ed0409d390e3b00d08b6c8596220681038084` |

The following is a copy-ready local-manifest core. If the target manifest
already owns one of these paths, remove its existing project by its exact
project name before adding the replacement; do not use broad force-sync as a
collision workaround.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="github-infiniti-camera-port" fetch="https://github.com/" />
  <project path="vendor/oneplus/camera-sm8850-common" name="infiniti-camera-port/vendor_oneplus_camera-sm8850-common" remote="github-infiniti-camera-port" revision="16.0" />
  <project path="vendor/oneplus/camera-infiniti" name="infiniti-camera-port/vendor_oneplus_camera-infiniti" remote="github-infiniti-camera-port" revision="16.0" />
  <project path="proprietary/vendor/oneplus/camera-sm8850-common" name="infiniti-camera-port/proprietary_vendor_oneplus_camera-sm8850-common" remote="github-infiniti-camera-port" revision="16.0" />
  <project path="proprietary/vendor/oneplus/camera-infiniti" name="infiniti-camera-port/proprietary_vendor_oneplus_camera-infiniti" remote="github-infiniti-camera-port" revision="16.0" />
  <project path="vendor/oneplus/sm8850-common" name="infiniti-camera-port/proprietary_vendor_oneplus_sm8850-common" remote="github-infiniti-camera-port" revision="16.0" />
  <project path="vendor/oneplus/infiniti" name="infiniti-camera-port/proprietary_vendor_oneplus_infiniti" remote="github-infiniti-camera-port" revision="16.0" />
</manifest>
```

After sync, hydrate and verify the four proprietary paths:

```sh
for path in \
  proprietary/vendor/oneplus/camera-infiniti \
  proprietary/vendor/oneplus/camera-sm8850-common \
  vendor/oneplus/infiniti \
  vendor/oneplus/sm8850-common
do
  git -C "$path" lfs pull
  git -C "$path" lfs fsck
done
```

The LFS service may request credentials for `lfs.p0g.ca`. Use a Git credential
helper or protected netrc; never put credentials in a manifest, committed
configuration, command line, or shell history.

## Guarded crDroid build overlay

Run this only for the promoted crDroid composition, after source sync and before
building:

```sh
python3 "$PATCHES_ROOT/build-patches/apply-build-patches.py" \
  --repo-root "$PWD" --check-only
python3 "$PATCHES_ROOT/build-patches/apply-build-patches.py" --repo-root "$PWD"
```

The first command validates state and reports whether each patch is forward- or
reverse-applicable. It is not a compile or build gate. The second command stages
all pending overlays before mutation and verifies rollback of invocation-owned
changes on failure. A reported rollback failure requires manual recovery.

The libjxl graph overlays are deliberately limited to `libhwy` in
`external/google-highway` and `libskia_skcms` in `external/skia`. They add only
`vendor_available: true` so vendor `libjxl` can resolve its two static
dependencies; `external/libjxl` itself is not patched.

The DNG graph overlay removes only `vendor_available: true` from `libdng_sdk`.
Android 16 DNG 1.7.1 links the core-only XMP toolkit, and the Infiniti graph has
no vendor DNG consumer. The overlay does not expose `xmp_toolkit_sdk` or
`zuid_md5` to vendor.

For the retired Lineage lane, see
[`los-fix-build-patches/README.md`](los-fix-build-patches/README.md). Do not mix
those four files into the crDroid build overlay or portable profile.

## 16.0.9 promotion status

The `staging/16.0.9` topic has been promoted to the contract branches, and this
profile was regenerated from the promoted graph afterwards. `main` and
`staging/16.0.9` carry the same head here; the staging branch is retained per
the topic-hygiene rule and is not a separate consumer release.

- `patch/` describes the PROMOTED contract graph. Its eight `series` head SHAs
  and its six `sync_only_prerequisites` are the promoted contract heads, and
  every recorded `head_tree_sha` equals the corresponding promoted tree. The
  table above and `patch/series.json` agree; `series.json` is authoritative if
  they ever diverge.
- The pre-promotion profile carried a series that re-extracted stock `libui` and
  `libutils` under private `-stock` names. That relink was the cause of a camera
  mode-switch crash and has been removed, not merely superseded. Do not resurrect
  a profile snapshot older than this one.
- `build-patches/` is graph-independent: each overlay is guarded by the target
  file's own `sha256`, and the `expected_head` pins name external projects that
  the 16.0.9 bump does not touch. It remains the lane the build applies.
- Runtime validation extends to `infiniti` only, via the r4 artifact named
  above. No result should be inferred for macan, macanc, or fairlady.
