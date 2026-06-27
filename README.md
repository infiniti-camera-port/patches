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
