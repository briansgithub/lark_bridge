#!/usr/bin/env bash
# Build the LarkBridge Pixel harness debug APK without Gradle:
#   gen_stimulus.py -> aapt2 link -> javac -> d8 -> zipalign -> apksigner.
# Runs under WSL against the Windows JDK/SDK (the defaults below) or on Linux with native tools.
#
#   ./build.sh            -> build/larkbridge-harness-debug.apk
#   ./build.sh clean
#
# Env overrides: ANDROID_SDK, JDK, BUILD_TOOLS (version), PLATFORM (e.g. android-37.0), PYTHON.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANDROID_SDK="${ANDROID_SDK:-/mnt/c/Users/user1/Android/Sdk}"
JDK="${JDK:-/mnt/c/Users/user1/Tools/jdk-17}"
BUILD_TOOLS="${BUILD_TOOLS:-37.0.0}"
PLATFORM="${PLATFORM:-android-37.0}"
PYTHON="${PYTHON:-python3}"
MIN_SDK=31

OUT="$HERE/build"
KEYSTORE="$HERE/.keystore/debug.keystore"   # gitignored; generated on first build
APK="$OUT/larkbridge-harness-debug.apk"

if [[ "${1:-}" == "clean" ]]; then
    rm -rf -- "$OUT"
    exit 0
fi

BT="$ANDROID_SDK/build-tools/$BUILD_TOOLS"
ANDROID_JAR="$ANDROID_SDK/platforms/$PLATFORM/android.jar"
X=""; [[ -e "$BT/aapt2.exe" ]] && X=".exe"
# Windows tools need Windows paths.
p() { if [[ -n "$X" ]]; then wslpath -w "$1"; else printf '%s' "$1"; fi; }

for f in "$BT/aapt2$X" "$BT/zipalign$X" "$BT/lib/d8.jar" "$BT/lib/apksigner.jar" "$ANDROID_JAR" "$JDK/bin/javac$X"; do
    [[ -e "$f" ]] || { echo "missing: $f" >&2; exit 1; }
done

rm -rf -- "$OUT"
mkdir -p "$OUT/assets/stimulus" "$OUT/classes" "$OUT/dex"

echo "== stimulus"
"$PYTHON" -B "$HERE/tools/gen_stimulus.py" "$OUT/assets/stimulus"

echo "== aapt2 link"
"$BT/aapt2$X" link -o "$(p "$OUT/res.apk")" -I "$(p "$ANDROID_JAR")" \
    --manifest "$(p "$HERE/AndroidManifest.xml")" --min-sdk-version "$MIN_SDK"

echo "== javac"
: > "$OUT/sources.txt"
while IFS= read -r -d '' src; do p "$src" >> "$OUT/sources.txt"; echo >> "$OUT/sources.txt"; done \
    < <(find "$HERE/src" -name '*.java' -print0 | sort -z)
"$JDK/bin/javac$X" -source 8 -target 8 -Xlint:-options -Xlint:deprecation -encoding UTF-8 \
    -bootclasspath "$(p "$ANDROID_JAR")" -d "$(p "$OUT/classes")" "@$(p "$OUT/sources.txt")"

echo "== d8"
: > "$OUT/classes.txt"
while IFS= read -r -d '' c; do p "$c" >> "$OUT/classes.txt"; echo >> "$OUT/classes.txt"; done \
    < <(find "$OUT/classes" -name '*.class' -print0 | sort -z)
"$JDK/bin/java$X" -cp "$(p "$BT/lib/d8.jar")" com.android.tools.r8.D8 --debug --min-api "$MIN_SDK" \
    --lib "$(p "$ANDROID_JAR")" --output "$(p "$OUT/dex")" "@$(p "$OUT/classes.txt")"

echo "== package"
# Assets are added here, not with `aapt2 -A`: Windows aapt2 stores them with backslash paths,
# which AssetManager cannot open. Stored uncompressed, like aapt2 does for audio.
"$PYTHON" -B - "$OUT/res.apk" "$OUT/dex/classes.dex" "$OUT/assets" "$OUT/unaligned.apk" <<'PY_PACKAGE'
import pathlib, shutil, sys, zipfile
res, dex, assets, out = sys.argv[1:]
shutil.copyfile(res, out)
with zipfile.ZipFile(out, "a", zipfile.ZIP_DEFLATED) as z:
    z.write(dex, "classes.dex")
    for f in sorted(pathlib.Path(assets).rglob("*")):
        if f.is_file():
            z.write(f, "assets/" + f.relative_to(assets).as_posix(), compress_type=zipfile.ZIP_STORED)
PY_PACKAGE
"$BT/zipalign$X" -f -p 4 "$(p "$OUT/unaligned.apk")" "$(p "$OUT/aligned.apk")"

if [[ ! -f "$KEYSTORE" ]]; then
    echo "== debug keystore (first build): $KEYSTORE"
    mkdir -p "$(dirname "$KEYSTORE")"
    # Standard Android debug-key convention (password "android"); not a secret, never committed.
    "$JDK/bin/keytool$X" -genkeypair -keystore "$(p "$KEYSTORE")" -storepass android -keypass android \
        -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=LarkBridge Harness Debug,O=LarkBridge,C=US" >/dev/null
fi

echo "== sign"
"$JDK/bin/java$X" -jar "$(p "$BT/lib/apksigner.jar")" sign --ks "$(p "$KEYSTORE")" --ks-pass pass:android \
    --key-pass pass:android --ks-key-alias androiddebugkey --out "$(p "$APK")" "$(p "$OUT/aligned.apk")"
"$JDK/bin/java$X" -jar "$(p "$BT/lib/apksigner.jar")" verify "$(p "$APK")"
rm -f -- "$OUT/res.apk" "$OUT/unaligned.apk" "$OUT/aligned.apk" "$APK.idsig"

echo "== badging"
"$BT/aapt2$X" dump badging "$(p "$APK")" | grep -E "^(package|uses-permission|sdkVersion|targetSdkVersion|application-label):" | tr -d '\r'
echo "built $APK ($(stat -c %s "$APK") bytes)"
