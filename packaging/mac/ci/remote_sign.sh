#!/usr/bin/env bash
# Developer ID signing with the private key OFF the runner (rcodesign remote signing).
#
# The runner holds no secret. `rcodesign sign` opens a session on the relay and prints a session join
# string, encrypted to the public key of packaging/mac/devid-cert.pem: only the holder of the matching
# private key can join it. The owner joins from his own machine (`rcodesign remote-sign`), which
# answers one signature request per Mach-O file; the files never leave the runner and the key never
# leaves his machine. Notarization is submitted from his machine too (the App Store Connect key stays
# there); the runner only waits for Apple's ticket and staples it.
#
#   remote_sign.sh tool            download rcodesign (pinned version + sha256)
#   remote_sign.sh start app|dmg   start signing in the background, write the join string to
#                                  build/mac/remote/sjs-<what>.txt (the workflow uploads it)
#   remote_sign.sh wait app|dmg    wait for that signing to end, print its log, fail if it failed
#   remote_sign.sh staple <path>   wait for the notarization ticket and staple it
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
MAC="$ROOT/packaging/mac"
WORK="$ROOT/build/mac/remote"
RC="$WORK/rcodesign"
CERT="$MAC/devid-cert.pem"
APP="$ROOT/dist/Wavetype.app"
RCODESIGN_VERSION="0.29.0"
RCODESIGN_SHA256="d1a532150adaf90048260d76359261aa716abafc45c53c5dc18845029184334a"
JOIN_WAIT="${REMOTE_JOIN_WAIT:-60}"          # seconds for the relay to hand out the join string
SIGN_WAIT="${REMOTE_SIGN_WAIT:-2400}"        # seconds for the owner to join and sign everything
STAPLE_WAIT="${REMOTE_STAPLE_WAIT:-2700}"    # seconds for Apple's ticket to exist
mkdir -p "$WORK"

target() {
    case "$1" in
        app) echo "$APP" ;;
        dmg) ls "$ROOT"/dist/Wavetype-*-arm64.dmg | head -1 ;;
        *) echo "usage: remote_sign.sh start|wait app|dmg" >&2; exit 2 ;;
    esac
}

case "${1:-}" in
tool)
    NAME="apple-codesign-$RCODESIGN_VERSION-aarch64-apple-darwin"
    curl -fsSL -o "$WORK/$NAME.tar.gz" \
        "https://github.com/indygreg/apple-platform-rs/releases/download/apple-codesign%2F$RCODESIGN_VERSION/$NAME.tar.gz"
    echo "$RCODESIGN_SHA256  $WORK/$NAME.tar.gz" | shasum -a 256 -c -
    tar -xzf "$WORK/$NAME.tar.gz" -C "$WORK"
    cp "$WORK/$NAME/rcodesign" "$RC"
    "$RC" --version
    ;;
start)
    WHAT="$2"; T="$(target "$WHAT")"
    [ -e "$T" ] || { echo "nothing to sign: $T"; exit 1; }
    LOG="$WORK/sign-$WHAT.log"; RCF="$WORK/sign-$WHAT.rc"; SJS="$WORK/sjs-$WHAT.txt"
    rm -f "$LOG" "$RCF" "$SJS"
    ARGS=(sign --remote-public-key-pem-file "$CERT")
    if [ "$WHAT" = app ]; then
        # same choices as build.sh step 6: hardened runtime everywhere, the entitlements on the app
        ARGS+=(--for-notarization --code-signature-flags runtime --entitlements-xml-file "$MAC/entitlements.plist")
    else
        ARGS+=(--binary-identifier com.lorenzomontes.wavetype.dmg)
    fi
    # its own session, so it outlives this step; the exit code lands in a file for `wait`
    export LOG RCF
    nohup bash -c '"$0" "$@" > "$LOG" 2>&1; echo $? > "$RCF"' "$RC" "${ARGS[@]}" "$T" </dev/null >/dev/null 2>&1 &
    for _ in $(seq 1 "$JOIN_WAIT"); do
        sed -nE 's/^ *rcodesign remote-sign ([A-Za-z0-9+\/=_-]+) *$/\1/p' "$LOG" 2>/dev/null | head -1 > "$SJS" || true
        [ -s "$SJS" ] && break
        [ -f "$RCF" ] && break
        sleep 1
    done
    if [ ! -s "$SJS" ]; then echo "no session join string:"; cat "$LOG" 2>/dev/null || true; exit 1; fi
    echo "signing $T: session open, join string in $SJS ($(wc -c < "$SJS" | tr -d ' ') bytes)"
    ;;
wait)
    WHAT="$2"; T="$(target "$WHAT")"
    LOG="$WORK/sign-$WHAT.log"; RCF="$WORK/sign-$WHAT.rc"
    t0=$(date +%s)
    while [ ! -f "$RCF" ]; do
        if [ $(( $(date +%s) - t0 )) -ge "$SIGN_WAIT" ]; then
            echo "signing did not end in $SIGN_WAIT s (did the owner join the session?)"
            pkill -f "$RC sign" || true
            grep -vE '^[A-Za-z0-9+/=_-]{60,}$' "$LOG" | tail -40 || true
            exit 1
        fi
        sleep 5
    done
    # the join string lines are long and say nothing: keep them out of the log
    grep -vE '^[A-Za-z0-9+/=_-]{60,}$|rcodesign remote-sign ' "$LOG" | tail -400 || true
    rc="$(cat "$RCF")"
    echo "rcodesign exit code: $rc after $(( $(date +%s) - t0 )) s of waiting"
    [ "$rc" = 0 ] || exit 1
    if [ "$WHAT" = app ]; then
        # Apple's own judgement on what rcodesign wrote
        codesign --verify --deep --strict --verbose=2 "$T"
    else
        codesign --verify --verbose=2 "$T"
    fi
    codesign -dv --verbose=2 "$T" 2>&1 | grep -E 'Identifier|Format|flags|Authority|TeamIdentifier|Timestamp|Signature' || true
    ;;
staple)
    T="$2"
    t0=$(date +%s)
    until xcrun stapler staple "$T" > "$WORK/staple.log" 2>&1; do
        if [ $(( $(date +%s) - t0 )) -ge "$STAPLE_WAIT" ]; then
            echo "no notarization ticket for $T after $STAPLE_WAIT s"; cat "$WORK/staple.log"; exit 1
        fi
        sleep 20
    done
    cat "$WORK/staple.log"
    xcrun stapler validate "$T"
    echo "stapled after $(( $(date +%s) - t0 )) s"
    ;;
*)
    echo "usage: remote_sign.sh tool | start app|dmg | wait app|dmg | staple <path>"; exit 2 ;;
esac
