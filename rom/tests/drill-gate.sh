#!/usr/bin/env bash
# Drill the identity gate in an isolated scratch tree.
#
# A gate that has only ever been observed to PASS is not evidence of anything -
# a check that cannot fail passes for free. So every row here must be observed
# FAILING (or moving the id) before the gate's agreement is trusted anywhere.
#
# The fixture uses local bare repos as remotes, so `git ls-remote` resolves
# without a network and the whole matrix is deterministic and offline.
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROM="$HERE/../rom"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

pass=0
fail=0

ok()   { printf '  PASS  %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

check() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then ok "$label"; else
        bad "$label (expected $expected, got $actual)"
    fi
}

git_q() { git -C "$1" -c user.email=d@d -c user.name=d -c commit.gpgsign=false "${@:2}"; }

make_repo() {
    local path="$1" name="$2" content="$3"
    mkdir -p "$SCRATCH/remotes/$(dirname "$name")"
    git init -q --bare "$SCRATCH/remotes/$name.git"
    local work="$SCRATCH/build/$path"
    mkdir -p "$work"
    git init -q -b main "$work"
    printf '%s\n' "$content" > "$work/file.txt"
    git_q "$work" add -A
    git_q "$work" commit -qm init
    git_q "$work" remote add origin "$SCRATCH/remotes/$name.git"
    git_q "$work" push -q origin main
}

# --- fixture -----------------------------------------------------------------
mkdir -p "$SCRATCH/build/.repo/local_manifests"
git init -q -b main "$SCRATCH/build/.repo/manifests"
echo base > "$SCRATCH/build/.repo/manifests/default.xml"
git_q "$SCRATCH/build/.repo/manifests" add -A
git_q "$SCRATCH/build/.repo/manifests" commit -qm base

make_repo governed_a orgx/governed_a "alpha"
make_repo governed_b orgx/governed_b "beta"
make_repo overlay_target third/overlay_target "gamma"

cat > "$SCRATCH/build/.repo/local_manifests/test.xml" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="local" fetch="$SCRATCH/remotes/" />
  <project path="governed_a" name="orgx/governed_a" revision="main" remote="local" />
  <project path="governed_b" name="orgx/governed_b" revision="refs/heads/main" remote="local" />
</manifest>
XML

cat > "$SCRATCH/overlays.yml" <<'YML'
patches:
  - name: test-overlay
    target_repo: overlay_target
    target_path: file.txt
    apply_order: 1
YML

cat > "$SCRATCH/Containerfile" <<'DOCKER'
FROM docker.io/library/ubuntu@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90
RUN true
DOCKER

write_lanes() {
    cat > "$SCRATCH/lanes.json" <<JSON
{
  "toolchain": {
    "image": "localhost/none:none",
    "containerfile": "$SCRATCH/Containerfile",
    "ccache": "$SCRATCH/ccache",
    "overlay_manifest": "$SCRATCH/overlays.yml",
    "ledger": "$SCRATCH/ledger"
  },
  "lanes": {
    "drill": {
      "tree": "$SCRATCH/build",
      "base_manifest": {"url": "unused", "revision": "main"},
      "governed_manifest": {"url": "unused", "revision": "main",
                            "path": "unused", "install_as": "test.xml"},
      "governed_org": "orgx",
      "lunch": "${1:-drill_target-userdebug}",
      "status_file": "$SCRATCH/STATUS",
      "artifacts": "$SCRATCH/artifacts"
    }
  }
}
JSON
}
write_lanes

id_of() { ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill status 2>/dev/null | awk '/^computed/{print $2}'; }
state_of() { ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill status 2>/dev/null | awk '/^state/{print $2}'; }

BASE_ID="$(id_of)"
echo "baseline id: ${BASE_ID:-<none>}  state: $(state_of)"
[ -n "$BASE_ID" ] || { echo "FATAL: fixture produced no id"; exit 1; }
check "baseline is PINNED" "PINNED" "$(state_of)"

# --- negative control: this one MUST NOT move --------------------------------
# Runs first. If touch moved the id, every later row would "pass" for the wrong
# reason - any perturbation would look significant.
touch "$SCRATCH/build/governed_a/file.txt"
check "touch without content change leaves the id UNCHANGED" "$BASE_ID" "$(id_of)"

# --- perturbation matrix: every row MUST move the id -------------------------
moved() {
    local label="$1" now
    now="$(id_of)"
    if [ "$now" != "$BASE_ID" ]; then ok "$label moves the id"; else
        bad "$label did NOT move the id"
    fi
}

printf 'edited\n' > "$SCRATCH/build/governed_a/file.txt"
moved "a tracked file edited in a non-overlay project"
git_q "$SCRATCH/build/governed_a" checkout -q -- file.txt
check "  ...and reverting restores the baseline id" "$BASE_ID" "$(id_of)"

printf 'stray\n' > "$SCRATCH/build/governed_a/untracked.txt"
moved "an untracked file added"
rm -f "$SCRATCH/build/governed_a/untracked.txt"

printf 'overlay applied\n' > "$SCRATCH/build/overlay_target/file.txt"
moved "an overlay applied in the tree"
check "  ...and overlay-only drift builds as EXPERIMENTAL" "EXPERIMENTAL" "$(state_of)"
git_q "$SCRATCH/build/overlay_target" checkout -q -- file.txt

printf 'FROM docker.io/library/ubuntu@sha256:%064d\nRUN true\n' 1 > "$SCRATCH/Containerfile"
moved "the toolchain digest changed"
cat > "$SCRATCH/Containerfile" <<'DOCKER'
FROM docker.io/library/ubuntu@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90
RUN true
DOCKER

write_lanes "other_target-userdebug"
moved "the lunch target changed"
write_lanes

printf 'second commit\n' > "$SCRATCH/build/governed_b/file.txt"
git_q "$SCRATCH/build/governed_b" add -A
git_q "$SCRATCH/build/governed_b" commit -qm second
moved "a governed project moved one commit"

# --- the refusal drill -------------------------------------------------------
# governed_b's HEAD is now ahead of what its remote publishes, which is exactly
# the staleness the gate exists to catch.
check "a governed project off its published revision is REFUSED" "REFUSED" "$(state_of)"

out="$(ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill build bacon 2>&1)"
rc=$?
check '  ...and rom build exits non-zero' "2" "$rc"
case "$out" in
    *governed_b*) ok "  ...and names the offending project" ;;
    *)            bad "  ...but did NOT name the offending project" ;;
esac
local_sha="$(git -C "$SCRATCH/build/governed_b" rev-parse HEAD)"
pub_sha="$(git -C "$SCRATCH/remotes/orgx/governed_b.git" rev-parse main)"
case "$out" in
    *"$local_sha"*) ok "  ...and reports the local SHA" ;;
    *)              bad "  ...but omitted the local SHA" ;;
esac
case "$out" in
    *"$pub_sha"*) ok "  ...and reports the published SHA" ;;
    *)            bad "  ...but omitted the published SHA" ;;
esac
case "$out" in
    *"state=REFUSED"*|*REFUSED*) ok "  ...and says REFUSED" ;;
    *)                           bad "  ...but did not say REFUSED" ;;
esac
grep -q 'state=REFUSED' "$SCRATCH/STATUS" \
    && ok "  ...and writes state=REFUSED to the status file" \
    || bad "  ...but the status file does not say REFUSED"

# The override must demand the ACTUAL id. The expected id is the value the tool
# just printed, so naming it would prove nothing; naming the actual forces the
# operator to look at what they have.
expected_id="$(ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill status 2>/dev/null | awk '/^expected/{print $2}')"
ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill build bacon --override "$expected_id" >/dev/null 2>&1
check "override with the EXPECTED id is rejected" "2" "$?"

# The other half of the same rule: naming the ACTUAL id must let the operator
# through. Rejecting everything would satisfy the test above while making the
# escape hatch a lie.
actual_id="$(id_of)"
accept_out="$(ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill build bacon --override "$actual_id" 2>&1)"
case "$accept_out" in
    *"override accepted"*) ok "override with the ACTUAL id is accepted" ;;
    *)                     bad "override with the ACTUAL id was NOT accepted" ;;
esac
case "$accept_out" in
    *"$actual_id"*) ok "  ...and the accepted id is echoed back" ;;
    *)              bad "  ...but the accepted id was not echoed" ;;
esac

# --- offline: say so plainly, do not hang, do not guess ----------------------
git_q "$SCRATCH/build/governed_b" reset -q --hard HEAD~1
sed -i "s|fetch=\"$SCRATCH/remotes/\"|fetch=\"$SCRATCH/nowhere/\"|" \
    "$SCRATCH/build/.repo/local_manifests/test.xml"

offline_out="$(ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill status 2>&1)"
check "status still exits 0 when the published world is unreachable" "0" "$?"
case "$offline_out" in
    *"<not computed>"*) ok "  ...and says the expected id was not computed" ;;
    *)                  bad "  ...but did not say the expected id was missing" ;;
esac
case "$offline_out" in
    *"cannot reach the published world"*) ok "  ...and says why, plainly" ;;
    *)                                    bad "  ...but gave no plain reason" ;;
esac
case "$offline_out" in
    *"computed    "*) ok "  ...and still reports the computed id" ;;
    *)                bad "  ...but dropped the computed id" ;;
esac

ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill build bacon >/dev/null 2>&1
check "build REFUSES rather than guessing when expected is unavailable" "3" "$?"

# --- the ledger: rows must be reconstructable, not merely written -------------
sed -i "s|fetch=\"$SCRATCH/nowhere/\"|fetch=\"$SCRATCH/remotes/\"|" \
    "$SCRATCH/build/.repo/local_manifests/test.xml"

ledger_rows() { grep -c . "$SCRATCH/ledger/attempts.tsv" 2>/dev/null || echo 0; }

ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill build bacon      >/dev/null 2>&1
ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill build bootimage  >/dev/null 2>&1
printf 'overlay\n' > "$SCRATCH/build/overlay_target/file.txt"
ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill build bacon      >/dev/null 2>&1

rows="$(ledger_rows)"
[ "$rows" -ge 4 ] && ok "every attempt left a row (header + $((rows - 1)))" \
                  || bad "expected a row per attempt, found $rows lines"

# Reconstruction is the acceptance: fold each stored preimage back and it must
# reproduce the id it is filed under. This needs no tree and no network, which
# is the point - an amnesiac consumer can check it.
bad_recon=0
seen_recon=0
for pre in "$SCRATCH"/ledger/preimages/*.json; do
    [ -e "$pre" ] || continue
    seen_recon=$((seen_recon + 1))
    want="$(basename "$pre" .json)"
    ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill reconstruct "$want" >/dev/null 2>&1 \
        || bad_recon=$((bad_recon + 1))
done
# seen_recon guards against the vacuous pass: with no preimages on disk the loop
# body never runs and a bare "no failures" check reports success having tested
# nothing. This exact check did pass that way before the runtime bug was fixed.
if [ "$seen_recon" -eq 0 ]; then
    bad "no preimages were stored, so reconstruction was never exercised"
elif [ "$bad_recon" -eq 0 ]; then
    ok "every stored preimage folds back to its own id ($seen_recon checked)"
else
    bad "$bad_recon of $seen_recon preimage(s) did not reproduce their id"
fi

ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill reconstruct \
    0000000000000000000000000000000000000000000000000000000000000000 >/dev/null 2>&1
check "reconstructing an unknown id fails rather than inventing one" "1" "$?"

# A tampered preimage must be caught, or the record proves nothing.
victim="$(ls "$SCRATCH"/ledger/preimages/*.json 2>/dev/null | head -1)"
if [ -z "$victim" ]; then
    bad "no preimage to tamper with, so the tamper check was never exercised"
else
python3 - "$victim" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["projects"][0]["effective_tree"] = "0" * 40
json.dump(d, open(p, "w"))
PY
ROM_LANES="$SCRATCH/lanes.json" "$ROM" drill reconstruct "$(basename "$victim" .json)" >/dev/null 2>&1
check "a tampered preimage no longer reproduces its id" "1" "$?"
fi

goals="$(cut -f4 "$SCRATCH/ledger/attempts.tsv" | tail -n +2 | sort -u | tr '\n' ',')"
case "$goals" in
    *bacon*bootimage*|*bootimage*bacon*) ok "a partial bootimage run is distinguishable from a full bacon run" ;;
    *) bad "rows do not distinguish goals (saw: $goals)" ;;
esac
classes="$(cut -f5 "$SCRATCH/ledger/attempts.tsv" | tail -n +2 | sort -u | tr '\n' ',')"
case "$classes" in
    *EXPERIMENTAL*) ok "an EXPERIMENTAL run is distinguishable by its class column" ;;
    *) bad "no EXPERIMENTAL class recorded (saw: $classes)" ;;
esac

# --- mirror and artifact homing degrade, never fail --------------------------
python3 - "$SCRATCH" <<'PY'
import json, sys
s = sys.argv[1]
c = json.load(open(f"{s}/lanes.json"))
c["toolchain"]["mirror"] = f"{s}/no-such-mirror"
json.dump(c, open(f"{s}/lanes.json", "w"), indent=2)
PY
# mirror_args is an init-time concern - `repo init --reference` - so a sync on
# an already-initialised tree never reaches it. Exercising it through `rom sync`
# therefore tested nothing and passed for it; call it directly instead.
mkdir -p "$SCRATCH/present-mirror"
mirror_probe() {
    ROM_LANES="$SCRATCH/lanes.json" python3 - "$HERE/.." "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import rom_lanes, rom_build
_, tool = rom_lanes.load_lane("drill")
tool["mirror"] = sys.argv[2]
print("ARGS:" + " ".join(rom_build.mirror_args(tool)))
PY
}

out="$(mirror_probe "$SCRATCH/no-such-mirror" 2>&1)"
case "$out" in
    *"no local mirror"*) ok "an absent mirror warns rather than failing" ;;
    *)                   bad "no warning emitted for the absent mirror" ;;
esac
case "$out" in
    *"ARGS:"*--reference*) bad "passed --reference for a mirror that is absent" ;;
    *"ARGS:"*)             ok "  ...and passes no --reference" ;;
esac

out="$(mirror_probe "$SCRATCH/present-mirror" 2>&1)"
case "$out" in
    *"no local mirror"*) bad "warned about a mirror that is present" ;;
    *)                   ok "a present mirror produces no warning" ;;
esac
case "$out" in
    *--reference*present-mirror*) ok "  ...and is passed to repo init as --reference" ;;
    *)                            bad "  ...but was not passed as --reference (got: $out)" ;;
esac

# Artifact homing: publish() must file under the id, and must not fail the
# build when the destination is unwritable.
mkdir -p "$SCRATCH/fakeout/target/product/dev"
: > "$SCRATCH/fakeout/target/product/dev/boot.img"
homed="$(ROM_LANES="$SCRATCH/lanes.json" python3 - "$SCRATCH" "$HERE/.." <<'PY'
import sys
sys.path.insert(0, sys.argv[2])
import rom_lanes, rom_build
lane, _ = rom_lanes.load_lane("drill")
print(rom_build.publish(lane, "deadbeef" * 8,
                        f"{sys.argv[1]}/fakeout/target/product/dev/boot.img"))
PY
)"
case "$homed" in
    *deadbeefdeadbeef*/boot.img) ok "a successful artefact is homed under its id" ;;
    *)                           bad "artefact not homed under its id (got: $homed)" ;;
esac

# --- LFS pointer stubs are visible even though the identity cannot see them ---
lfs_probe() {
    ROM_LANES="$SCRATCH/lanes.json" python3 - "$HERE/.." "$SCRATCH/build" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import rom_lfs
tree = Path(sys.argv[2])
found = rom_lfs.stubs(tree)
print("STUBS:%d" % len(found))
for f in found:
    print("OWNER:%s:%s" % (f, rom_lfs.owning_repo(tree, f)))
PY
}

check "a clean tree reports no stubs" "STUBS:0" "$(lfs_probe | head -1)"

printf 'version https://git-lfs.github.com/spec/v1\noid sha256:%064d\nsize 1234\n' 0 \
    > "$SCRATCH/build/governed_a/blob.bin"
out="$(lfs_probe)"
check "a pointer stub is detected" "STUBS:1" "$(echo "$out" | head -1)"
case "$out" in
    *"OWNER:governed_a/blob.bin:governed_a"*) ok "  ...and is attributed to its owning repository" ;;
    *) bad "  ...but was not attributed correctly ($out)" ;;
esac

rm -f "$SCRATCH/build/governed_a/blob.bin"
check "removing the stub clears the count" "STUBS:0" "$(lfs_probe | head -1)"

# A file that is small but NOT a pointer must not be counted - the sweep is
# size-bounded for speed, so size alone must never be the test.
printf 'tiny but not a pointer\n' > "$SCRATCH/build/governed_a/small.txt"
check "a small non-pointer file is not counted as a stub" "STUBS:0" "$(lfs_probe | head -1)"
rm -f "$SCRATCH/build/governed_a/small.txt"

# A documented-unhydratable stub must be separated out, not counted as work.
printf 'version https://git-lfs.github.com/spec/v1\noid sha256:faa09385f32d8b3e4518c14384b52ef94f6430da5a838307a2e0af2940b89608\nsize 1803816\n' \
    > "$SCRATCH/build/governed_a/known.bin"
split="$(ROM_LANES="$SCRATCH/lanes.json" python3 - "$HERE/.." "$SCRATCH/build" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import rom_lfs
tree = Path(sys.argv[2])
a, k = rom_lfs.split_known(tree, rom_lfs.stubs(tree))
print("ACTIONABLE:%d KNOWN:%d" % (len(a), len(k)))
PYEOF
)"
check "a known-unhydratable oid is not counted as actionable" "ACTIONABLE:0 KNOWN:1" "$split"
printf 'version https://git-lfs.github.com/spec/v1\noid sha256:%064d\nsize 5\n' 7 \
    > "$SCRATCH/build/governed_a/other.bin"
split="$(ROM_LANES="$SCRATCH/lanes.json" python3 - "$HERE/.." "$SCRATCH/build" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import rom_lfs
tree = Path(sys.argv[2])
a, k = rom_lfs.split_known(tree, rom_lfs.stubs(tree))
print("ACTIONABLE:%d KNOWN:%d" % (len(a), len(k)))
PYEOF
)"
check "  ...while an unknown oid still is" "ACTIONABLE:1 KNOWN:1" "$split"
rm -f "$SCRATCH/build/governed_a/known.bin" "$SCRATCH/build/governed_a/other.bin"

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
