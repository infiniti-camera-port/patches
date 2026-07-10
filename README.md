# infiniti crDroid 16.0 patch packaging

This repository has three deliberately separate lanes:

- `patch/`: guarded downstream portability profile, eight series and 40 source
  patches for `infiniti` only.
- `build-patches/`: three guarded crDroid build-only overlays.
- `los-fix-build-patches/`: four historical LineageOS-only compile fixes.

The complete promoted build graph is published separately in
[`infiniti-camera-port/local_manifest`](https://github.com/infiniti-camera-port/local_manifest).
Manifest consumers already have the eight source series and must not apply
`patch/` on top. Doing so would double-apply the same commits.

Only `lineage_infiniti-bp4a-userdebug` has been built and runtime-validated.
macan, macanc, and fairlady are included in the split repository graph, but
their builds are deferred, not failed blockers. The archived monoliths
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
| `vendor/oneplus/camera-sm8850-common` | `vendor_oneplus_camera-sm8850-common` | `16.0-infiniti` | `ee141662ce8baccf31e69a4f9e5436cb16ded3e2` |
| `vendor/oneplus/camera-infiniti` | `vendor_oneplus_camera-infiniti` | `16.0` | `51cb97c2f11c4fd718bcaa5c6c6992cc3625cab4` |
| `proprietary/vendor/oneplus/camera-sm8850-common` | `proprietary_vendor_oneplus_camera-sm8850-common` | `16.0-infiniti` | `7b6375ecb47d70657b8acfccc9dd7d3390ec80f5` |
| `proprietary/vendor/oneplus/camera-infiniti` | `proprietary_vendor_oneplus_camera-infiniti` | `16.0` | `484b54c3113de412b4ba385734401217fc0714af` |
| `vendor/oneplus/sm8850-common` | `proprietary_vendor_oneplus_sm8850-common` | `16.0-infiniti` | `9a87ebee9c2eda1379f75f4710bf87ed078d4fbf` |
| `vendor/oneplus/infiniti` | `proprietary_vendor_oneplus_infiniti` | `16.0` | `f3b56122d8b362c8a5fcce2b2ecb2a517c6c11b6` |

The following is a copy-ready local-manifest core. If the target manifest
already owns one of these paths, remove its existing project by its exact
project name before adding the replacement; do not use broad force-sync as a
collision workaround.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="github-infiniti-camera-port" fetch="https://github.com/" />
  <project path="vendor/oneplus/camera-sm8850-common" name="infiniti-camera-port/vendor_oneplus_camera-sm8850-common" remote="github-infiniti-camera-port" revision="16.0-infiniti" />
  <project path="vendor/oneplus/camera-infiniti" name="infiniti-camera-port/vendor_oneplus_camera-infiniti" remote="github-infiniti-camera-port" revision="16.0" />
  <project path="proprietary/vendor/oneplus/camera-sm8850-common" name="infiniti-camera-port/proprietary_vendor_oneplus_camera-sm8850-common" remote="github-infiniti-camera-port" revision="16.0-infiniti" />
  <project path="proprietary/vendor/oneplus/camera-infiniti" name="infiniti-camera-port/proprietary_vendor_oneplus_camera-infiniti" remote="github-infiniti-camera-port" revision="16.0" />
  <project path="vendor/oneplus/sm8850-common" name="infiniti-camera-port/proprietary_vendor_oneplus_sm8850-common" remote="github-infiniti-camera-port" revision="16.0-infiniti" />
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

For the retired Lineage lane, see
[`los-fix-build-patches/README.md`](los-fix-build-patches/README.md). Do not mix
those four files into the crDroid build overlay or portable profile.
