#!/usr/bin/env bash
# Build Wavetype for macOS: venv -> PyInstaller .app -> secret check -> sign -> DMG
# (-> notarize + staple when the Apple credentials are in the environment).
#
#   bash packaging/mac/build.sh          # everything (local Mac, Codemagic)
#   bash packaging/mac/build.sh app      # venv + PyInstaller + secret check: needs no credentials
#   bash packaging/mac/build.sh sign     # sign + DMG (+ notarize): runs no pip, no PyInstaller
#
# CI runs the two halves as separate steps so the signing secrets never share an environment with
# pip installs and PyInstaller imports of third-party packages.
#
# Needs: macOS 14+ on Apple silicon, Python 3.12 from python.org (actions/setup-python installs
# exactly that; its Tk 8.6 is the one the app uses). Set PYTHON to pick another interpreter.
# Outputs: dist/Wavetype.app and dist/Wavetype-<version>-arm64.dmg (dist/ and build/ are gitignored).
#
# Signing, all optional (nothing set -> ad-hoc signature, good for CI and local tests):
#   DEVID_IDENTITY       "Developer ID Application: Name (TEAMID)", or "auto" to take the first one
#                        in the keychain. Without it: ad-hoc (`codesign -s -`).
#   DEVID_P12_BASE64     optional: a base64 .p12 to import into a throwaway keychain first
#   DEVID_P12_PASSWORD   (GitHub Actions secrets; on Codemagic the keychain is set up by its CLI)
#   APP_STORE_CONNECT_PRIVATE_KEY / APP_STORE_CONNECT_KEY_IDENTIFIER / APP_STORE_CONNECT_ISSUER_ID
#                        App Store Connect Team API key: with a Developer ID identity, the .app and
#                        the .dmg are notarized (notarytool --wait) and stapled.
#   WAVETYPE_HARDENED    1 (default): even the ad-hoc build runs with the hardened runtime and the
#                        entitlements, so CI exercises the same runtime rules as the notarized app.
#   WAVETYPE_WITH_LOCAL  1 (default): bundle the offline engine (see wavetype-mac.spec).
#   WAVETYPE_REQUIRE_NOTARIZATION=1  fail instead of shipping an un-notarized build (release CI).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
MAC="$ROOT/packaging/mac"
WORK="$ROOT/build/mac"
VENV="$WORK/venv"
APP="$ROOT/dist/Wavetype.app"
ENT="$MAC/entitlements.plist"
PYTHON="${PYTHON:-python3}"
PHASE="${1:-all}"
case "$PHASE" in all|app|sign) ;; *) echo "usage: build.sh [all|app|sign]"; exit 2;; esac
HARDENED="${WAVETYPE_HARDENED:-1}"
export WAVETYPE_WITH_LOCAL="${WAVETYPE_WITH_LOCAL:-1}"
mkdir -p "$WORK" "$ROOT/dist"

step() { echo; echo "== $*"; }

TMPS=()
cleanup() {
    # credentials written for notarytool / the throwaway keychain never outlive the script
    for f in "${TMPS[@]:-}"; do [ -n "$f" ] && rm -rf "$f"; done
    if [ -n "${KEYCHAIN:-}" ]; then security delete-keychain "$KEYCHAIN" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

# 0. version: same place as the Windows build (ProductVersion in packaging/version_info.txt)
VERSION="$(sed -nE "s/.*StringStruct\('ProductVersion', *'([0-9.]+)'\).*/\1/p" packaging/version_info.txt | head -1)"
[ -n "$VERSION" ] || { echo "ProductVersion not found in packaging/version_info.txt"; exit 1; }
DMG="$ROOT/dist/Wavetype-$VERSION-arm64.dmg"
echo "Wavetype $VERSION | macOS $(sw_vers -productVersion) $(uname -m) | python: $PYTHON"

if [ "$PHASE" != sign ]; then
# 1. build environment
step "venv ($WORK/venv)"
"$PYTHON" - <<'EOF'
import sys, platform, sysconfig
print("python", sys.version.split()[0], platform.machine(), "| framework:", bool(sysconfig.get_config_var("PYTHONFRAMEWORK")))
if sys.version_info[:2] != (3, 12):
    print("WARNING: built and tested with Python 3.12 (python.org)")
EOF
if [ ! -x "$VENV/bin/python" ]; then
    "$PYTHON" -m venv "$VENV"
fi
PY="$VENV/bin/python"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt "pyinstaller>=6.10,<7"
"$PY" -c "import tkinter; print('tk', tkinter.TkVersion)"
"$PY" -m pip freeze > "$WORK/pip-freeze.txt"

# 2. icon: docs/img/logo-square.png -> Wavetype.icns (sips + iconutil, same source as the .ico)
step "icon"
SET="$WORK/Wavetype.iconset"
rm -rf "$SET"; mkdir -p "$SET"
SRC="$ROOT/docs/img/logo-square.png"
for s in 16 32 128 256 512; do
    sips -z "$s" "$s" "$SRC" -s format png --out "$SET/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2))
    sips -z "$d" "$d" "$SRC" -s format png --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$SET" -o "$WORK/Wavetype.icns"
export WAVETYPE_ICNS="$WORK/Wavetype.icns"
unset WAVETYPE_CODESIGN_IDENTITY || true

# 3. PyInstaller
step "PyInstaller"
rm -rf "$APP" "$ROOT/dist/Wavetype"
"$PY" -m PyInstaller --noconfirm --clean --distpath "$ROOT/dist" --workpath "$WORK/pyi" "$MAC/wavetype-mac.spec"
[ -x "$APP/Contents/MacOS/Wavetype" ] || { echo "no $APP/Contents/MacOS/Wavetype"; exit 1; }

# 4. secret check: no user files, no Groq key anywhere in the build (same list as build.ps1)
step "secret check"
BANNED='^(groq_key\.txt|vocab\.txt|card_style\.txt|skin\.txt|wavetype\.log|wavetype_history\.txt|last_rec\.wav|groq_usage\.json)$'
bad="$(find "$APP" -type f | awk -F/ '{print $NF}' | grep -E "$BANNED" || true)"
bad+="$(find "$APP" -type d \( -name recordings -o -name models \) || true)"
if [ -n "$bad" ]; then echo "user files in the build:"; echo "$bad"; exit 1; fi
if grep -rlaE 'gsk_[A-Za-z0-9]{20,}' "$APP" >/dev/null 2>&1; then
    grep -rlaE 'gsk_[A-Za-z0-9]{20,}' "$APP" | sed 's/^/  key pattern in: /'
    exit 1
fi
echo "   ok: no user files, no gsk_ key pattern. Wavetype.app = $(du -sh "$APP" | cut -f1)"
fi
[ "$PHASE" = app ] && { step "done (app)"; echo "app: $APP"; exit 0; }
[ -x "$APP/Contents/MacOS/Wavetype" ] || { echo "no $APP: run build.sh app first"; exit 1; }

# 5. signing identity (optional), after PyInstaller, which always signs ad-hoc: step 6 re-signs
# every binary anyway, and the credentials stay out of the build half.
IDENTITY=""
if [ -n "${DEVID_P12_BASE64:-}" ]; then
    step "importing the Developer ID certificate into a throwaway keychain"
    KEYCHAIN="$WORK/signing.keychain-db"
    KCPASS="$(uuidgen)"
    P12="$(mktemp -t devid)"; TMPS+=("$P12")          # created 0600 by mktemp
    printf '%s' "$DEVID_P12_BASE64" | base64 --decode > "$P12"
    security create-keychain -p "$KCPASS" "$KEYCHAIN"
    security set-keychain-settings -lut 21600 "$KEYCHAIN"
    security unlock-keychain -p "$KCPASS" "$KEYCHAIN"
    security import "$P12" -P "${DEVID_P12_PASSWORD:-}" -A -t cert -f pkcs12 -k "$KEYCHAIN"
    security set-key-partition-list -S apple-tool:,apple: -k "$KCPASS" "$KEYCHAIN" >/dev/null
    security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')
fi
if [ "${DEVID_IDENTITY:-}" = "auto" ]; then
    IDENTITY="$(security find-identity -v -p codesigning | sed -nE 's/.*"(Developer ID Application: [^"]+)".*/\1/p' | head -1)"
    [ -n "$IDENTITY" ] || { echo "DEVID_IDENTITY=auto but no Developer ID Application in the keychain"; exit 1; }
elif [ -n "${DEVID_IDENTITY:-}" ]; then
    IDENTITY="$DEVID_IDENTITY"
fi
if [ -n "$IDENTITY" ]; then
    echo "signing identity: $IDENTITY"
    HARDENED=1
else
    echo "signing identity: ad-hoc (hardened runtime: $HARDENED)"
fi

# 6. sign, inside out: every Mach-O file, then the main executable and the bundle with the
# entitlements. PyInstaller already signed the binaries; this pass makes the flags explicit
# (timestamp, runtime) and is the one `codesign --verify` and notarization judge.
step "codesign"
SIGN=(codesign --force --sign "${IDENTITY:--}")
if [ -n "$IDENTITY" ]; then SIGN+=(--timestamp); fi
if [ "$HARDENED" = "1" ]; then SIGN+=(--options runtime); fi
n=0
while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q 'Mach-O'; then
        [ "$f" = "$APP/Contents/MacOS/Wavetype" ] && continue
        "${SIGN[@]}" "$f" >/dev/null 2>&1 || { echo "codesign failed: $f"; "${SIGN[@]}" "$f"; }
        n=$((n + 1))
    fi
done < <(find "$APP/Contents" -type f -print0)
echo "   $n nested Mach-O files signed"
"${SIGN[@]}" --entitlements "$ENT" "$APP/Contents/MacOS/Wavetype"
"${SIGN[@]}" --entitlements "$ENT" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -dv --verbose=2 "$APP" 2>&1 | grep -E 'Identifier|Format|flags|Authority|TeamIdentifier|Signature' || true

notarize() {   # $1 = file to submit (.zip or .dmg)
    local out
    out="$(xcrun notarytool submit "$1" --key "$P8" --key-id "$APP_STORE_CONNECT_KEY_IDENTIFIER" \
           --issuer "$APP_STORE_CONNECT_ISSUER_ID" --wait --timeout 30m --output-format json)" || true
    echo "$out"
    local id status
    id="$(printf '%s' "$out" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)"
    status="$(printf '%s' "$out" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)"
    if [ "$status" != "Accepted" ]; then
        [ -n "$id" ] && xcrun notarytool log "$id" --key "$P8" --key-id "$APP_STORE_CONNECT_KEY_IDENTIFIER" \
                            --issuer "$APP_STORE_CONNECT_ISSUER_ID" || true
        echo "notarization: $status"; return 1
    fi
}

NOTARIZE=0
if [ -n "$IDENTITY" ] && [ -n "${APP_STORE_CONNECT_PRIVATE_KEY:-}" ] \
   && [ -n "${APP_STORE_CONNECT_KEY_IDENTIFIER:-}" ] && [ -n "${APP_STORE_CONNECT_ISSUER_ID:-}" ]; then
    NOTARIZE=1
    P8BASE="$(mktemp -t asc)"; P8="$P8BASE.p8"; TMPS+=("$P8BASE" "$P8")
    ( umask 077; printf '%s\n' "$APP_STORE_CONNECT_PRIVATE_KEY" > "$P8" )
    step "notarizing Wavetype.app"
    ZIP="$WORK/Wavetype-notarize.zip"; TMPS+=("$ZIP")
    ditto -c -k --keepParent "$APP" "$ZIP"
    notarize "$ZIP"
    xcrun stapler staple "$APP"
    xcrun stapler validate "$APP"
elif [ -n "$IDENTITY" ]; then
    echo "   Developer ID signature without App Store Connect key: NOT notarized"
fi
if [ "${WAVETYPE_REQUIRE_NOTARIZATION:-0}" = "1" ] && [ "$NOTARIZE" != "1" ]; then
    echo "WAVETYPE_REQUIRE_NOTARIZATION=1 but identity or App Store Connect key missing"
    exit 1
fi

# 7. DMG: the app + a link to /Applications (drag to install)
step "DMG"
STAGE="$WORK/dmg"
rm -rf "$STAGE" "$DMG"; mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Wavetype.app"
ln -s /Applications "$STAGE/Applications"
ok=0
for i in 1 2 3; do           # hdiutil on hosted runners fails now and then with "Resource busy"
    if hdiutil create -volname "Wavetype" -srcfolder "$STAGE" -ov -fs HFS+ -format UDZO "$DMG"; then ok=1; break; fi
    echo "   hdiutil failed (try $i/3), retrying in 5 s"; sleep 5
done
[ "$ok" = 1 ] || { echo "hdiutil create failed 3 times"; exit 1; }
if [ -n "$IDENTITY" ]; then
    codesign --force --sign "$IDENTITY" --timestamp -i com.lorenzomontes.wavetype.dmg "$DMG"
fi
hdiutil verify "$DMG" >/dev/null
echo "   hdiutil verify ok"
if [ "$NOTARIZE" = 1 ]; then
    step "notarizing the DMG"
    notarize "$DMG"
    xcrun stapler staple "$DMG"
    xcrun stapler validate "$DMG"
fi

step "done"
echo "app: $APP ($(du -sh "$APP" | cut -f1))"
echo "dmg: $DMG ($(du -h "$DMG" | cut -f1))"
echo "signature: ${IDENTITY:-ad-hoc} | hardened runtime: $HARDENED | notarized: $NOTARIZE"
