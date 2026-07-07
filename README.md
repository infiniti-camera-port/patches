# infiniti camera patches

Device-side patches for the OnePlus 15 (infiniti) Oplus camera port — apply the camera stack onto a custom ROM.

## Apply

```
python3 patch/apply-patches.py        # from your Android source root
```
Toggle the sets (space), run (enter). Each subfolder maps to a source path — `device,oneplus,infiniti` → `device/oneplus/infiniti`.

## Base prerequisites (read first)

Your **frameworks/** must carry the camera-side bits the Oplus stack assumes — present in crDroid-class ROMs,
thin or absent in stock LineageOS. We satisfy them by pinning `frameworks/av` to the OEM **A16** fork and
carrying the `native`/`base` deltas; on a different base, port the equivalents first:

- **frameworks/av** — generic vendor-tag handling matched by **camera package name**; OnePlus camera
  extension (zoom) + CameraServiceExt; the `'oplu'` HDR user-data metadata atom in MPEG4Writer (HDR video);
  Y16 buffer-dimension bypass.
- **frameworks/native** — `NUM_BUFFER_SLOTS` bumped (we use 96; the default 64 starves the camera);
  Qualcomm vendor-YUV recognition in `formatIsYuv`; blur-region handling (portrait/bokeh).
- **frameworks/base** — `AHardwareBuffer` fixes for the OEM camera (not a no-op repo).

**vendor/oplus/camera** — don't hand-apply. Just `repo sync` `infiniti-camera-port/vendor_oplus_camera`;
it's a diff on top of **`dodge-camera-port/vendor_oplus_camera`** (dodge tip + a curated camera series), and
its `camera/*` blob tree is extract-generated, not committed.

## build-patches/ — guarded build fixes

`build-patches/` contains manual patches that must be applied after a fresh sync
and before building. Run the guard from the Android source root:

```
python3 patches/build-patches/apply-build-patches.py --check-only
python3 patches/build-patches/apply-build-patches.py
```

The check-only mode is a non-build gate. A build is authorized only by apply mode
with every patch reporting `applied=YES`.

## los-fix-build-patches/ — base build fixes, NOT the camera port

Fixes that only make a LineageOS 23.2 / Android 16 infiniti tree *compile* (legacy OMX AC-4, Pixelworks/iris
display, a CAF HAL1 constant, a `protobuf_vendorcompat` restore). Kept out of `patch/` on purpose — not
camera-port effort. See [`los-fix-build-patches/`](los-fix-build-patches).
