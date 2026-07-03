# infiniti camera patches

Patch exports for the OnePlus 15 (infiniti) Oplus camera port. As of 2026-07-03 the exports are
generated from the **crDroid 16.0** world — every series is `git format-patch <base>..staging/16.0_crdroid`
of its `infiniti-camera-port` org repo. `PATCH-MAP.tsv` maps each patch directory to its build-tree path.

## Layout — three groups

| group | what | apply where |
|---|---|---|
| `camera/` | Reusable OPlus camera-stack series: `hardware/oplus` 24-commit reconcile (oplus-fwk stub closure, vintf, sepolicy), `frameworks/av` HDR `'oplu'` atom + night dead-shutter timestamp bypass, `frameworks/native` buffer-slots/vendor-YUV, `frameworks/base` ImageReader JNI bridge, `vendor/oplus/camera` bring-up series, and the split camera device repos (`device/oneplus/{sm8850-common-camera,infiniti-camera}` content) | any crDroid-class Android 16 tree |
| `crdroid/` | crDroid-16.0-only base adaptation: `vendor/oneplus/sm8850-common` parity update + oplus-interface de-blob (fixes 13 partition-mismatch prebuilt collisions vs crDroid source AIDL/HIDL) + system_ext `libprotobuf-cpp-lite-21.12` shim for `horae`; plus a **reference copy** of the local-manifest overlay (`crdroid/local_manifest/infiniti-camera.xml`, served on the crDroid builder as `.repo/local_manifests/infiniti-camera-crdroid.xml`) | crDroid 16.0 trees only |
| `device/` | General device-tree improvements, candidates for upstreaming to OnePlus-SM8850-Development / LineageOS: `device/oneplus/infiniti` parity + camera-topology wiring, `device/oneplus/sm8850-common` camera-scoped move-out, `device/qcom/sepolicy_vndr/sm8850` opluscamera xdsp/qdsp trio | OnePlus SM8850 device trees |

Camera-topology moves follow the split proven in
`.omo/evidence/crdroid-sm8850-camera-split/task-8-camera-topology.md`
(inheritance: `device/oneplus/infiniti` → `device/oneplus/infiniti-camera` → `device/oneplus/sm8850-common-camera`).

## Apply

Each directory is an ordered `git format-patch` series. From the build-tree path in `PATCH-MAP.tsv`:

```bash
git am /path/to/patches/<group>/<dir>/*.patch     # or: git apply --check first
```

Bases each series applies onto (all = the repo's `staging/16.0_crdroid` history):

| patchdir | base sha | base meaning | tip |
|---|---|---|---|
| `camera/hardware_oplus` | `bd0e44d0` | crdroidandroid `android_hardware_oplus@16.0` | `42f511e4` (24) |
| `camera/frameworks_av` | `2598b568` | crDroid 16.0 av pin | `647d67dd` (5) |
| `camera/frameworks_native` | `da1214c0` | crDroid 16.0 native pin | `caab93b6` (2) |
| `camera/frameworks_base` | `48899feb` | crDroid-OnePlus-SM8850 base fork pin | `de6690c0` (1) |
| `camera/vendor_oplus_camera` | `1217c69` | dodge-camera-port merge-base ("Add blobs for PANO") | `ab98365` (19) |
| `camera/device_oneplus_infiniti-camera` | `59bf24b` | README-only scaffold | `4bef2a25` (1) |
| `camera/device_oneplus_sm8850-common-camera` | `7203d96` | README-only scaffold | `090e6e0b` (1) |
| `crdroid/vendor_oneplus_sm8850-common` | `09fdb814` | crDroid-tree vendor pin | `81f00bb5` (3) |
| `device/device_oneplus_infiniti` | `2f59ab17` | crDroid device pin | `f00a88a4` (16) |
| `device/device_oneplus_sm8850-common` | `b0998084` | staging pin (= 9-commit ledger tree) | `c0d61875` (1) |
| `device/device_qcom_sepolicy_vndr` | `a4a25b69` | lineage-23.2-caf-sm8850 upstream | `637c961c` (3) |

Notes:
- `vendor/oplus/camera` — patches are provided for reference/portability, but the recommended route is
  still to `repo sync` `infiniti-camera-port/vendor_oplus_camera`; its `camera/*` blob payload is
  extract-generated (158 files under `camera/`), not carried in patches.
- `crdroid/vendor_oneplus_sm8850-common/0001` (~21 MB) is the OOS 11.A.47 blob parity update; it is kept
  so 0002 (de-blob) and 0003 (protobuf shim) apply in series from the pin. If your tree already carries
  the OnePlus-SM8850-Development vendor tip, you only need 0002+0003.

## Base prerequisites (read first)

Your **frameworks/** must carry the camera-side bits the Oplus stack assumes — present in crDroid-class
ROMs, thin or absent in stock LineageOS. On crDroid 16.0 the `camera/frameworks_*` series here are the
complete remaining delta; on a different base, port the equivalents first:

- **frameworks/av** — generic vendor-tag handling matched by **camera package name**; OnePlus camera
  extension (zoom) + CameraServiceExt (already native in crDroid 16.0 base); the `'oplu'` HDR user-data
  metadata atom in MPEG4Writer (HDR video); the `ALLOW_NONINCREASING_TIMESTAMPS` night dead-shutter fix.
- **frameworks/native** — `NUM_BUFFER_SLOTS` bumped (we use 96; the default 64 starves the camera);
  Qualcomm vendor-YUV recognition in `formatIsYuv`.
- **frameworks/base** — `nativeGetConsumer` ImageReader JNI bridge for OnePlus APS.

## Regen (PATCH-REGEN)

The org `staging/16.0_crdroid` branches are the source of truth; this repo reflects them. To regenerate
after the staging branches move, from local clones of the org repos:

```bash
# 1. fetch the updated staging branch
git -C <clone> fetch origin staging/16.0_crdroid
# 2. wipe + re-export the group dir (bases from the table above; new base = new crDroid pin after a rebase)
rm patches/<group>/<dir>/*.patch
git -C <clone> format-patch -o patches/<group>/<dir> <base>..origin/staging/16.0_crdroid
# 3. refresh PATCH-MAP.tsv if a directory was added/removed, update the bases table in this README
# 4. QA: on the builder, per PATCH-MAP.tsv row, git worktree at the base pin + `git apply --check` each patch in order
```

Rules: commits must carry NO AI trailers; authorship is preserved by `format-patch` from the
`cherry-pick -x` series on the org branches. Exact per-repo series composition is recorded in
`.omo/evidence/crdroid-sm8850-camera-split/task-{11,12}-*.md` (infiniti-camera-port repo).

## Legacy: `patch/<comma-path>/` (pre-crDroid exports, kept intact)

`patch/` holds the previous-generation exports (LineageOS 23.2 world, comma-encoded path folders,
applied via `python3 patch/apply-patches.py`). Consumer search (Task 14, 2026-07-03) found only
documentation and sanity-check scripts referencing it — no build automation — so it is kept **as-is for
provenance/back-compat with the lineage-23.2 branches** and is NOT regenerated anymore. New work applies
the `camera/` / `crdroid/` / `device/` groups above. Folder↔path mapping for the legacy set lives in the
org `PATCH-REGEN.md`.

## `los-fix-build-patches/` — base build fixes, NOT the camera port

Standalone overlay (2026-06-28 decision — deliberately outside the camera patch groups): fixes that only
make a LineageOS 23.2 / Android 16 infiniti tree *compile* (legacy OMX AC-4, Pixelworks/iris display, a
CAF HAL1 constant, a `protobuf_vendorcompat` restore). See [`los-fix-build-patches/`](los-fix-build-patches).
