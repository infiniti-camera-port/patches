# infiniti camera patches

Device-side patches for the OnePlus 15 (infiniti) Oplus camera port — apply the camera stack onto a custom ROM.

## Apply

```
python3 patch/apply-patches.py        # from your Android source root
```
Toggle the sets (space), run (enter). Each subfolder maps to a source path — `device,oneplus,infiniti` → `device/oneplus/infiniti`.

## Base prerequisites (read first)

- **frameworks/av** must provide generic camera **vendor-tag handling matched by camera package name** —
  present in crDroid-class ROMs, **absent in stock LineageOS**. We pin `frameworks/av` to the OEM **A16**
  fork (already has it); on a LineageOS-av base, port that in before expecting the Oplus camera to open.
- **vendor/oplus/camera** — don't hand-apply. Just `repo sync` `infiniti-camera-port/vendor_oplus_camera`;
  it's effectively a diff on top of **`dodge-camera-port/vendor_oplus_camera`** (force-set to the dodge tip
  + a curated camera series). Its `camera/*` blob tree is extract-generated, not committed.

## los-fix-build-patches/ — base build fixes, NOT the camera port

Fixes that only make a LineageOS 23.2 / Android 16 infiniti tree *compile* (legacy OMX AC-4, Pixelworks/iris
display, a CAF HAL1 constant, a `protobuf_vendorcompat` restore). Kept out of `patch/` on purpose — not
camera-port effort. See [`los-fix-build-patches/`](los-fix-build-patches).
