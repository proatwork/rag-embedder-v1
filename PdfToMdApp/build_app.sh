#!/bin/sh
# Builds the Mac app around pdf_to_md.py and wraps it as PdfToMd.app next to this script (double-click to open).
# The app finds the project by walking up from where it sits, so keep it in this folder (or choose the project
# folder in the app once). Tests: swift test (in this folder).
set -e
cd "$(dirname "$0")"

PRODUCT=PdfToMd
APP_DIR="$PRODUCT.app"
DISPLAY_NAME="PDF to Markdown"
BUNDLE_ID=local.pdftomd
MIN_MACOS=14.0

swift build -c release --product "$PRODUCT"
BIN_DIR="$(swift build -c release --show-bin-path)"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
cp "$BIN_DIR/$PRODUCT" "$APP_DIR/Contents/MacOS/$PRODUCT"
cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$PRODUCT</string>
    <key>CFBundleDisplayName</key><string>$DISPLAY_NAME</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key><string>$PRODUCT</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key><string>$MIN_MACOS</string>
    <key>NSPrincipalClass</key><string>NSApplication</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
codesign --force --sign - "$APP_DIR"  # ad-hoc: gives the bundle a valid signature
echo "built $PWD/$APP_DIR"
