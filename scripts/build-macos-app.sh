#!/usr/bin/env bash
#
# Build and sign Voicemode.app.
#
#   scripts/build-macos-app.sh [signing-identity]
#
# With no argument the first "Apple Development" identity in the keychain is used. Pass an
# identity name to override, or NONE to skip signing.
#
# Signing is not cosmetic here. macOS ties the Microphone and Accessibility grants to the code
# signature, so an unsigned or ad-hoc-signed bundle asks for both permissions again on every
# single rebuild. Signing with a stable identity and bundle id makes the grants stick.
#
# This produces a bundle for use on your own machines. Distributing it to other people needs a
# Developer ID Application certificate and notarization, which is a different certificate type
# from either Apple Development or Apple Distribution.

set -euo pipefail

cd "$(dirname "$0")/.."

ROOT="$PWD"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
APP="$ROOT/dist/Voicemode.app"
ENTITLEMENTS="$ROOT/resources/Voicemode.entitlements"
# py2app refuses to run when the distribution carries install_requires, and setuptools fills
# that in from pyproject.toml's [project] dependencies. Staging the few files the bundle needs
# in a directory without a pyproject.toml is the way past that without contorting the package
# metadata or editing files in place mid-build.
STAGING="$ROOT/build/staging"

if [[ ! -x "$PYTHON" ]]; then
    echo "error: $PYTHON not found. Create the venv first:" >&2
    echo "    python3.11 -m venv .venv && .venv/bin/python -m pip install -e '.[app]'" >&2
    exit 1
fi

IDENTITY="${1:-}"
if [[ -z "$IDENTITY" ]]; then
    IDENTITY=$(security find-identity -v -p codesigning \
        | grep "Apple Development" \
        | head -1 \
        | sed -E 's/.*"(.*)"/\1/') || true
    if [[ -z "$IDENTITY" ]]; then
        echo "warning: no Apple Development identity found; building unsigned." >&2
        echo "         Permissions will have to be granted again after every rebuild." >&2
        IDENTITY="NONE"
    fi
fi

echo "==> Regenerating icon"
"$PYTHON" "$ROOT/scripts/make-icon.py" "$ROOT/resources/Voicemode.icns"

echo "==> Cleaning previous build"
rm -rf "$ROOT/build" "$APP"

echo "==> Staging sources"
mkdir -p "$STAGING/resources"
cp "$ROOT/voice.py" "$ROOT/voiceapp.py" "$ROOT/setup_app.py" "$STAGING/"
cp "$ROOT/resources/Voicemode.icns" "$STAGING/resources/"

echo "==> Running py2app"
( cd "$STAGING" && "$PYTHON" setup_app.py py2app --dist-dir "$ROOT/dist" )

if [[ "$IDENTITY" == "NONE" ]]; then
    echo "==> Skipping signing"
else
    echo "==> Signing with: $IDENTITY"
    # Inside-out: every nested binary has to be signed before the bundle that contains it.
    # py2app ships a lot of unsigned .so files, and one unsigned nested binary invalidates
    # the whole signature.
    find "$APP/Contents" \( -name "*.so" -o -name "*.dylib" \) -print0 \
        | xargs -0 -I{} codesign --force --timestamp=none --sign "$IDENTITY" {} 2>/dev/null || true

    codesign --force --deep --timestamp=none \
        --entitlements "$ENTITLEMENTS" \
        --sign "$IDENTITY" \
        "$APP"

    echo "==> Verifying signature"
    codesign --verify --verbose=2 "$APP"
fi

SIZE=$(du -sh "$APP" | cut -f1)
echo
echo "Built $APP ($SIZE)"
echo
echo "Install it by dragging it to /Applications, then launch it once."
echo "macOS will ask for Microphone access on the first dictation, and for Accessibility"
echo "the first time it tries to insert text."
