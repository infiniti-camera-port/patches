# infiniti camera patches

Device-side patches for the OnePlus 15 (infiniti) Oplus camera port — a source for
applying the camera stack onto a Custom ROM.

## Apply

From your Android source root:

```
python3 patch/apply-patches.py
```

Toggle the patch sets to apply (space), then run (enter). Each subfolder maps to a
source path — e.g. `device,oneplus,infiniti` → `device/oneplus/infiniti`.

## Note

Assumes an appropriate base. Some camera framework prerequisites are not adopted by
every ROM; ensure your base provides them before applying.

## `los-fix-build-patches/` — base build fixes, NOT the camera port

[`los-fix-build-patches/`](los-fix-build-patches) holds fixes that only make a LineageOS 23.2 / Android 16
infiniti tree **compile** under our branch composition — the legacy OMX AC-4 (codec2-era) block, the
Pixelworks/iris display libs, a CAF HAL1 constant, and a `protobuf_vendorcompat` prebuilt restore.

They are deliberately kept **out of `patch/`**: none of them are camera-port effort — they are
base/OEM-inherited gaps and a sync artifact. See that folder's README for the per-patch rationale.
