#!/bin/zsh
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$PROJECT_ROOT/build/VibeCue.app"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources" /private/tmp/vibecue-swift-cache
/usr/bin/swiftc -O -module-cache-path /private/tmp/vibecue-swift-cache \
  -framework AppKit -framework Foundation -framework AVFoundation \
  "$PROJECT_ROOT"/Sources/*.swift -o "$APP_DIR/Contents/MacOS/VibeCue"
cp "$PROJECT_ROOT/assets/Info.plist" "$APP_DIR/Contents/Info.plist"
if [[ -f "$PROJECT_ROOT/assets/AppIcon.icns" ]]; then
  cp "$PROJECT_ROOT/assets/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"
fi
if [[ -d "$PROJECT_ROOT/assets/Sounds" ]]; then
  /usr/bin/ditto "$PROJECT_ROOT/assets/Sounds" "$APP_DIR/Contents/Resources/Sounds"
fi
/usr/bin/codesign --force --sign - "$APP_DIR"
printf '%s\n' "$APP_DIR"
