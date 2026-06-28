# los-fix-build-patches — base / LineageOS build fixes (NOT the camera port)

These patches make a **LineageOS 23.2 (Android 16) infiniti** tree *compile* given our branch
composition — `frameworks/av` pinned to the OEM **A16** fork while its sibling repos are LineageOS
base, plus a couple of base/sync artifacts. They are **explicitly NOT part of the Oplus camera port.**
The camera-port patch set is in [`../patch/`](../patch) and is independent of everything here.

They live here only so the build is reproducible and the attribution is unmistakable: nothing in this
folder is camera work — it's base/OEM-inherited gaps and a sync artifact.

## Why a standalone patchset (and NOT committed into the org repos)

These are deliberately kept as an overlay and **not** committed into the `infiniti-camera-port` source
repos — to keep them out of camera-patch regeneration. The camera patches in `../patch/` are produced
with `git format-patch <camera-base>..HEAD` per repo; that range captures *every* commit on the branch.
If e.g. 0003 were committed onto `device/oneplus/sm8850-common`, the next regen would sweep that
Pixelworks/iris fix into `patch/device,oneplus,sm8850-common/` as a bogus "camera" patch. Keeping the
build fixes here means the org camera repos stay pristine and camera-patch regen yields *only* camera
commits. (0004 would also be clobbered by the next extract anyway — the real fix is 0003 in
`extract-files.py`.) Apply this overlay separately, after the camera stack.

## Patches

| # | apply from (repo) | what it does | why it is NOT camera-port |
|---|---|---|---|
| 0001 | `system/core` | declare the CAF HAL1 constant `CAMERA_FRAME_DATA_FD` (used by `frameworks/av@A16` `ICameraClient.cpp`, CodeAurora `969d880e03`) | OEM/CAF base constant for the legacy HAL1 path (dead on HAL3); not one of our camera commits |
| 0002 | `frameworks/av` | drop the incomplete **OMX AC4-table** block (`eb913ff3a3` "Add AC4Tbl params… [1/2]", the `[2/2]` OMX-header half is absent) | legacy OMX/ACodec Dolby **AC-4 / codec2-era** scaffolding — superseded by Codec2; a community/OEM commit, nothing to do with the port |
| 0003 | `device/oneplus/sm8850-common` | trim 6 vendor-only **Pixelworks** libs from `extract-files.py` `lib_fixup_vendor_suffix` | Pixelworks = the display/**iris** hardware, unrelated to camera; inherited from the upstream sm8550/sm8650 device-tree conversion |
| 0004 | `vendor/oneplus/sm8850-common` | rewrite the dangling `<lib>_vendor` Pixelworks deps to bare names in the generated `Android.bp` | output side of 0003 — same display/iris, same non-camera attribution |

## Not a patch — a restore (sync artifact)

`prebuilts/misc/protobuf_vendorcompat/`: `repo sync --force-sync` can drop its 11 tracked files
(LICENSE + the `arm/`,`arm64/` `libprotobuf-cpp-*-vendorcompat.so`). It's not a diff — just restore them:
```
git -C prebuilts/misc checkout -- protobuf_vendorcompat/
```

## Apply (overlay, after a clean repo sync)

```
git -C system/core                  apply los-fix-build-patches/0001-*.patch
git -C frameworks/av                apply los-fix-build-patches/0002-*.patch
git -C device/oneplus/sm8850-common apply los-fix-build-patches/0003-*.patch
git -C vendor/oneplus/sm8850-common apply los-fix-build-patches/0004-*.patch
```
0003 edits `extract-files.py`; re-run that repo's extract to regenerate `Android.bp` consistently
(0004 is that regenerated output, so applying both also works). These are needed only if you reproduce
our exact branch composition (A16 `frameworks/av` + LineageOS base). Full triage + rationale: the
`infiniti-camera-port` build notes (`BUILD-ISSUES-v3.0.md`).
